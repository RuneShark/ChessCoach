"""FastAPI backend for the ChessCoach UI.

Serves the single-page board + chat app and the JSON/SSE APIs behind it: game list and
review, drills, Stockfish eval, play-vs-engine, coach chat, sync, and settings.

Stockfish runs as one process behind a lock; blocking calls go to a threadpool so they
don't stall the event loop. The coach chat streams from whichever backend the user
picked in Settings (Claude Code CLI, Anthropic API, or a local Ollama model); see
`_backend()` and `config.json`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import statistics
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import chess
import chess.engine
import chess.pgn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..analyze import INACCURACY, find_stockfish, game_id, guess_username, win_pct
from ..paths import ANALYSIS_DIR, DATA, GAMES_DIR, JOURNAL, ROOT

STATIC = Path(__file__).parent / "static"
MATE = 100_000
DRILL_LOG = DATA / "drill_log.jsonl"  # append-only record of every drill attempt
ENDGAME_DRILLS = DATA / "endgame_drills.json"  # winning-endgame conversion drill pool


def _load_dotenv() -> None:
    """Minimal .env loader so users can drop ANTHROPIC_API_KEY in a file."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
app = FastAPI(title="ChessCoach")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# --------------------------------------------------------------------------- config
# Runtime settings the user manages from the Settings panel: their Chess.com username
# and which coach backend to use. Stored in config.json (gitignored) and read live, so
# changes apply without a restart. Environment variables act as fallbacks.
CONFIG_FILE = ROOT / "config.json"
LOCAL_BACKENDS = {"local", "qwen", "ollama", "gemma"}
_cfg_cache = {"mtime": -1.0, "data": {}}


def _config() -> dict:
    if CONFIG_FILE.exists():
        m = CONFIG_FILE.stat().st_mtime
        if m != _cfg_cache["mtime"]:
            try:
                _cfg_cache["data"] = json.loads(CONFIG_FILE.read_text())
            except json.JSONDecodeError:
                _cfg_cache["data"] = {}
            _cfg_cache["mtime"] = m
    else:
        _cfg_cache["data"] = {}
    d = _cfg_cache["data"]
    env = os.environ.get
    think = d.get("think")
    return {
        "username": d.get("username") or env("COACH_USER"),
        "backend": (d.get("backend") or env("COACH_BACKEND") or "").lower(),
        "model": d.get("model") or env("COACH_MODEL") or "",
        "anthropic_api_key": d.get("anthropic_api_key") or env("ANTHROPIC_API_KEY"),
        "local_url": d.get("local_url") or env("COACH_LOCAL_URL") or "http://localhost:11434",
        "think": think if think is not None else env("COACH_THINK", "0").lower() in ("1", "true", "yes", "on"),
        "max_tokens": int(d.get("max_tokens") or env("COACH_MAX_TOKENS") or 700),
    }


def _backend() -> str:
    """The active coach backend: 'local', 'api', 'subscription', or 'none'."""
    cfg = _config()
    b = cfg["backend"]
    if b == "local" or b in LOCAL_BACKENDS:
        return "local"
    if b == "api":
        return "api" if cfg["anthropic_api_key"] else "none"
    if b == "subscription":
        return "subscription" if shutil.which("claude") else "none"
    # No explicit choice: prefer the Claude CLI, then an API key.
    if shutil.which("claude"):
        return "subscription"
    if cfg["anthropic_api_key"]:
        return "api"
    return "none"


def _save_config(patch: dict) -> None:
    data = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            data = {}
    for k in ("username", "backend", "model", "local_url"):
        if k in patch:
            data[k] = (patch[k] or "").strip() or None
    if "think" in patch:
        data["think"] = bool(patch["think"])
    if patch.get("max_tokens"):
        data["max_tokens"] = int(patch["max_tokens"])
    if patch.get("anthropic_api_key"):            # only overwrite when a new key is sent
        data["anthropic_api_key"] = patch["anthropic_api_key"].strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=2))
    _cfg_cache["mtime"] = -1

# --------------------------------------------------------------------------- engine
_engine = None
_elock = threading.Lock()


def _engine_get():
    global _engine
    if _engine is None:
        _engine = chess.engine.SimpleEngine.popen_uci(find_stockfish())
        # Interactive evals/grades are serialised by _elock, so let the one engine
        # use a few threads. Tunable via COACH_ENGINE_THREADS / COACH_ENGINE_HASH.
        try:
            _engine.configure({
                "Threads": max(1, int(os.environ.get("COACH_ENGINE_THREADS", "2"))),
                "Hash": max(16, int(os.environ.get("COACH_ENGINE_HASH", "128"))),
            })
        except Exception:  # noqa: BLE001 — some builds reject options
            pass
    return _engine


def _eval_fen(fen: str, movetime: float = 0.2, depth: int | None = None) -> dict:
    with _elock:
        board = chess.Board(fen)
        if board.is_game_over():
            return {"cp": None, "mate": None, "best": None, "best_uci": None,
                    "turn": "white" if board.turn else "black",
                    "over": board.result()}
        limit = chess.engine.Limit(depth=depth) if depth else chess.engine.Limit(time=movetime)
        info = _engine_get().analyse(board, limit)
        score = info["score"].white()
        pv = info.get("pv", [])
        best = pv[0] if pv else None
        return {
            "cp": score.score(mate_score=MATE),
            "mate": score.mate(),
            "best": board.san(best) if best else None,
            "best_uci": best.uci() if best else None,
            "turn": "white" if board.turn else "black",
        }


def _top_lines(fen: str, n: int = 3, depth: int = 14) -> dict:
    """The engine's top-N candidate moves for a position, each with its SAN, UCI and
    eval (cp/mate from the side-to-move's POV). Powers the side-branch explorer."""
    with _elock:
        board = chess.Board(fen)
        if board.is_game_over():
            return {"lines": [], "over": board.result()}
        infos = _engine_get().analyse(board, chess.engine.Limit(depth=depth),
                                      multipv=max(1, min(5, n)))
        lines = []
        for info in infos:
            pv = info.get("pv", [])
            if not pv:
                continue
            mv = pv[0]
            score = info["score"].white()                 # White POV (matches the eval bar)
            lines.append({
                "san": board.san(mv), "uci": mv.uci(),
                "cp": score.score(mate_score=MATE), "mate": score.mate(),
            })
        return {"lines": lines, "turn": "white" if board.turn else "black"}


def _engine_move(fen: str, skill: int, movetime: float) -> dict:
    with _elock:
        eng = _engine_get()
        try:
            eng.configure({"Skill Level": max(0, min(20, skill))})
        except Exception:
            pass
        board = chess.Board(fen)
        res = eng.play(board, chess.engine.Limit(time=movetime))
        san = board.san(res.move)
        board.push(res.move)
        return {"uci": res.move.uci(), "san": san, "fen": board.fen(),
                "over": board.result() if board.is_game_over() else None}


async def _in_thread(fn, *a):
    return await asyncio.get_event_loop().run_in_executor(None, fn, *a)

# ----------------------------------------------------------------------------- data
_games_cache = {"count": -1, "data": []}
_drill_cache = {"count": -1, "pool": []}
_pgn_moves: dict[str, list[str]] | None = None


def _summary(g: dict) -> dict:
    ms = g.get("mistakes", [])
    return {
        "id": g.get("id"), "date": g.get("date"), "opponent": g.get("opponent"),
        "color": g.get("color"), "outcome": g.get("outcome"), "result": g.get("result"),
        "accuracy": g.get("accuracy"), "opening": g.get("opening"),
        "analysis_depth": g.get("analysis_depth"),
        "n_blunders": sum(1 for m in ms if m["severity"] == "blunder"),
        "n_mistakes": sum(1 for m in ms if m["severity"] == "mistake"),
    }


def _games_list() -> list[dict]:
    files = list(ANALYSIS_DIR.glob("*.json"))
    if len(files) != _games_cache["count"]:
        out = []
        for f in files:
            try:
                out.append(_summary(json.loads(f.read_text())))
            except json.JSONDecodeError:
                continue
        out.sort(key=lambda x: x.get("date") or "", reverse=True)
        _games_cache.update(count=len(files), data=out)
    return _games_cache["data"]


def _depth_stats() -> dict:
    """Analysis-depth coverage across analyzed games: the min depth present (so the UI
    can offer to deepen), plus a per-depth breakdown. Legacy games with no recorded
    depth are counted as 'unknown'."""
    depths = {}
    unknown = 0
    for g in _games_list():
        d = g.get("analysis_depth")
        if isinstance(d, int):
            depths[d] = depths.get(d, 0) + 1
        else:
            unknown += 1
    known_min = min(depths) if depths else None
    return {
        "min": known_min, "max": (max(depths) if depths else None),
        "unknown": unknown, "by_depth": depths,
        "analyzed": sum(depths.values()) + unknown,
    }


def _drill_pool() -> list[dict]:
    files = list(ANALYSIS_DIR.glob("*.json"))
    if len(files) != _drill_cache["count"]:
        pool = []
        for f in files:
            try:
                g = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            for m in g.get("mistakes", []):
                if m["severity"] == "blunder" and m.get("best") and m.get("fen"):
                    wb, wa = m.get("win_before"), m.get("win_after")
                    # "deciding" = threw a position you weren't losing (>=50 -> <50);
                    # these are the instructive ones our training targets.
                    deciding = (wb is not None and wa is not None
                                and wb >= 50 and wa < 50)
                    pool.append({"id": hashlib.sha1(m["fen"].encode()).hexdigest()[:12],
                                 "fen": m["fen"], "best": m["best"],
                                 "played": m["played"], "win_before": wb,
                                 "win_after": wa, "phase": m.get("phase"),
                                 "deciding": deciding,
                                 "color": g.get("color"), "url": g.get("url")})
        _drill_cache.update(count=len(files), pool=pool)
    return _drill_cache["pool"]


_eg_cache = {"mtime": -1.0, "pool": []}


def _endgame_pool() -> list[dict]:
    """The winning-endgame conversion drill pool, built by `coach.endgame_conversion`.

    Kept as its own pool (not merged into `_drill_pool`) because these are the endgame
    positions the generic drill deliberately excludes — safe to grade here only because
    every one is still decisively winning. Regenerate with `python -m coach.endgame_conversion`."""
    if not ENDGAME_DRILLS.exists():
        return []
    mtime = ENDGAME_DRILLS.stat().st_mtime
    if mtime != _eg_cache["mtime"]:
        try:
            _eg_cache["pool"] = json.loads(ENDGAME_DRILLS.read_text())
        except json.JSONDecodeError:
            _eg_cache["pool"] = []
        _eg_cache["mtime"] = mtime
    return _eg_cache["pool"]


# --------------------------------------------------------------------------- drill log
_drill_log_lock = threading.Lock()


def _log_attempt(rec: dict) -> None:
    rec = {"ts": time.time(), "date": datetime.now().strftime("%Y-%m-%d"), **rec}
    with _drill_log_lock:
        with DRILL_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")


def _drill_stats() -> dict:
    """Aggregate the attempt log for the live panel. outcome in solved/missed/revealed."""
    if not DRILL_LOG.exists():
        return {"total": 0, "today": 0, "today_solved": 0, "solved": 0,
                "solve_rate": 0, "streak": 0, "distinct": 0}
    rows = []
    for line in DRILL_LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    today = datetime.now().strftime("%Y-%m-%d")
    solved = sum(r.get("outcome") == "solved" for r in rows)
    trows = [r for r in rows if r.get("date") == today]
    # current streak of consecutive "solved" from the most recent attempt backwards
    streak = 0
    for r in reversed(rows):
        if r.get("outcome") == "solved":
            streak += 1
        else:
            break
    return {
        "total": len(rows),
        "today": len(trows),
        "today_solved": sum(r.get("outcome") == "solved" for r in trows),
        "solved": solved,
        "solve_rate": round(100 * solved / len(rows)) if rows else 0,
        "streak": streak,
        "distinct": len({r.get("id") for r in rows if r.get("id")}),
    }


def _moves_for(gid: str) -> list[str]:
    global _pgn_moves
    if _pgn_moves is None:
        _pgn_moves = {}
        for p in GAMES_DIR.glob("*.pgn"):
            with p.open(encoding="utf-8") as fh:
                while (gm := chess.pgn.read_game(fh)) is not None:
                    b = gm.board()
                    sans = []
                    for mv in gm.mainline_moves():
                        sans.append(b.san(mv))
                        b.push(mv)
                    _pgn_moves[game_id(gm.headers)] = sans
    return _pgn_moves.get(gid, [])

# ------------------------------------------------------------------------------ chat
COACH_PERSONA = (
    "You are a world-class chess coach working with ONE specific student, whose full "
    "profile and game analysis are below. Be direct, warm, and concrete. Explain the "
    "*why* and give an actionable drill rather than dumping long engine lines. You are "
    "NOT a chess engine: when a position needs exact calculation, say so and rely on "
    "the eval the app shows next to the board. Keep answers tight unless asked to go deep."
    "\n\n"
    "BOARD CONTROL — you can drive the board the student is looking at. Emit a directive "
    "on its OWN line and the app executes it (the directive text is hidden from the "
    "student, so also say in words what you did):\n"
    "  [[fen: <FEN>]]         set up any position (arbitrary piece placement)\n"
    "  [[moves: e4 e5 Nf3]]   play these SAN moves. From the starting position if the "
    "line begins at move 1 (it resets first), otherwise from the CURRENT position.\n"
    "  [[reset]]              go to the starting position\n"
    "  [[orient: black]]      view the board from a side (white|black)\n"
    "  [[flip]]               flip the board to the other side\n"
    "  [[analyze]]            open the current position in Analyze mode\n"
    "  [[play: black]]        play the current position vs the engine, student takes this "
    "side (white|black); the engine takes the OTHER side and moves first if it's its turn. "
    "Omit the colour ([[play]]) to give the student whichever side is to move.\n"
    "  [[level: 12]]          set engine strength, 0 (weakest) to 20 (strongest).\n"
    "  [[arrow: Bc4/0.9/green]]  draw an arrow for a move — name it in SAN (Bc4, Nf3, exd5) or "
    "give from+to squares (f1c4). The /WEIGHT (0-1) sets its OPACITY — make your MAIN point bold "
    "(~0.9) and lesser ideas faint (~0.3) so the student sees your ranking at a glance. /colour "
    "is optional (green red blue yellow orange violet teal; default green). Several at once: "
    "[[arrows: Bc4/0.9 Nc3/0.6 d4/0.3]].\n"
    "  [[mark: d5/0.8/red]]   highlight a square (weight = opacity); several: "
    "[[marks: d5/0.8 e5/0.5]].\n"
    "  [[clearann]]           erase the arrows/highlights you drew.\n"
    "USE ANNOTATIONS FREELY: they only draw ON TOP of the current position — they never move a "
    "piece — so when you explain a position, POINT with weighted arrows instead of only naming "
    "squares in prose (e.g. show two candidate moves, the stronger one bolder). This works even "
    "while you're just discussing the board (you don't need to set up or change anything first). "
    "When you DO set up a new position, draw the arrows AFTER the [[fen]]/[[moves]] line (a new "
    "position clears old annotations).\n"
    "Directives COMPOSE and run in order, so you can set up a position and start a game in "
    "one reply. Example — to play the Italian with the student as Black against a moderate "
    "engine, emit these three lines:\n"
    "  [[moves: e4 e5 Nf3 Nc6 Bc4]]\n"
    "  [[level: 10]]\n"
    "  [[play: black]]\n"
    "ENDGAMES & any position that is NOT reached by a natural opening sequence — use "
    "[[fen: <FEN>]], never [[moves]]. You CANNOT reach an endgame with [[moves]] from the "
    "start: those moves just build an early middlegame with every piece still on the board. "
    "For a king-and-pawn, rook, or queen ending you must place only the pieces that belong "
    "there via a FEN. Give a COMPLETE, valid FEN (a legal position — kings present, pawns "
    "off the 1st/8th ranks). The placement MUST have 8 ranks separated by '/'. Prefer to "
    "COPY one of these ready-to-use FENs EXACTLY, character for character:\n"
    "  Opposition (K+P vs K):  8/8/4k3/8/4P3/4K3/8/8 w - - 0 1\n"
    "  Lucena (R+P vs R, win): 1K1k4/1P6/8/8/8/8/r7/2R5 w - - 0 1\n"
    "  Philidor (R vs R+P):    8/8/8/8/4pk2/8/4RK2/6r1 w - - 0 1\n"
    "  K+Q vs K mate:          8/8/8/4k3/8/8/8/3QK3 w - - 0 1\n"
    "  K+R vs K mate:          8/8/8/4k3/8/8/8/R3K3 w - - 0 1\n"
    "Then add [[play: white]] (or black) so the student can convert it against the engine. "
    "When you use [[fen]], NEVER also emit a [[moves]] line in the same reply — the FEN alone "
    "sets the whole position; a stray moves line is contradictory and is ignored.\n"
    "Each turn you are given the CURRENT BOARD: the position's FEN, the SAN move list that "
    "led to it (with where the student's cursor is), and — if it's a game they loaded from "
    "Chess.com — which game (colour, opponent, date, result, accuracy, opening). Read that "
    "before you answer: reference the actual moves played and, for a loaded game, coach from "
    "what really happened. Build any [[moves]] from the current FEN. Use board "
    "control to SHOW rather than tell: set up a pattern the student struggles with, walk a "
    "line, spar a position, or reconstruct one they describe. Only move pieces when it "
    "genuinely helps, but don't hesitate to set up and start a game when asked to play."
)


def _format_moves(moves) -> str:
    """SAN move list -> numbered string, e.g. ['e4','e5','Nf3'] -> '1.e4 e5 2.Nf3'."""
    out = []
    for i in range(0, len(moves), 2):
        num = i // 2 + 1
        pair = f"{num}.{moves[i]}"
        if i + 1 < len(moves):
            pair += f" {moves[i + 1]}"
        out.append(pair)
    return " ".join(out)


def _board_context(fen=None, moves=None, ply=None, game=None) -> str:
    """A human-readable snapshot of what's on the board: the loaded game (if any), the
    moves that led here, where the cursor is, and the current FEN. Empty if nothing given."""
    lines = []
    if game:
        bits = []
        if game.get("color"):    bits.append(f"student had {game['color']}")
        if game.get("opponent"): bits.append(f"opponent {game['opponent']}")
        if game.get("date"):     bits.append(str(game["date"]))
        if game.get("outcome"):  bits.append(f"result {game['outcome']}")
        if game.get("accuracy") is not None: bits.append(f"{game['accuracy']}% accuracy")
        if game.get("opening"):  bits.append(str(game["opening"]))
        lines.append("This board is a LOADED Chess.com game of the student"
                     + (" — " + ", ".join(bits) if bits else "") + ".")
    if moves:
        lines.append(f"Moves played so far (SAN): {_format_moves(moves)}")
        if isinstance(ply, int) and 0 <= ply < len(moves):
            seen = moves[ply - 1] if ply > 0 else "the start"
            lines.append(f"The student is currently viewing the position after "
                         f"{ply} half-move(s) (i.e. just after {seen}), NOT the final move.")
    if fen:
        lines.append(f"Current position on the board (FEN): {fen}")
    return "\n".join(lines)


def coach_system(fen: str | None, moves=None, ply=None, game=None) -> str:
    parts = [COACH_PERSONA]
    for name, cap in (("profile.md", 2000), ("weaknesses.md", 3500),
                      ("plan.md", 3000), ("tilt.md", 2500), ("report.md", 2500)):
        p = JOURNAL / name
        if p.exists():
            parts.append(f"\n\n===== {name} =====\n{p.read_text()[:cap]}")
    hist = _history_digest()
    if hist:
        parts.append("\n\n===== GAME HISTORY (the student's Chess.com record) =====\n"
                     + hist + "\nUse these real games, openings, opponents and results "
                     "when the student asks about their history — cite specifics.")
    ctx = _board_context(fen, moves, ply, game)
    if ctx:
        parts.append("\n\n===== CURRENT BOARD =====\n" + ctx +
                     "\nWhen relevant, talk about THIS position and these moves.")
    return "".join(parts)


async def _stream_via_api(system: str, messages: list[dict]):
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=_config()["anthropic_api_key"])
    model = _config()["model"] or "claude-sonnet-5"
    async with client.messages.stream(
        model=model, max_tokens=1200, system=system, messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text


async def _stream_via_ollama(system: str, messages: list[dict]):
    """Stream from an Ollama endpoint via its native /api/chat (the OpenAI-compat /v1
    ignores `think`, so a thinking model would waste minutes on hidden tokens)."""
    import httpx
    cfg = _config()
    base = cfg["local_url"].rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    model = cfg["model"] or "llama3.1"
    # num_ctx must be generous or Ollama's 4096 default truncates the ~5k-token persona.
    body = {"model": model, "stream": True, "think": cfg["think"], "keep_alive": "30m",
            "messages": [{"role": "system", "content": system}, *messages],
            "options": {"num_ctx": 16384, "num_predict": cfg["max_tokens"], "temperature": 0.3}}
    # Fail fast: a wedged Ollama runner accepts the connection but streams nothing, so a
    # read timeout surfaces a clear error instead of hanging for minutes.
    timeout = float(os.environ.get("COACH_TIMEOUT", "60"))
    hint = (f"Local coach '{model}' at {base} did not respond. Is Ollama running and the "
            f"model pulled? You can switch backends in Settings.")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0)) as client:
            async with client.stream("POST", base + "/api/chat", json=body) as resp:
                resp.raise_for_status()
                got = False
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk = (ev.get("message") or {}).get("content")
                    if chunk:
                        got = True
                        yield chunk
                    if ev.get("done"):
                        if not got:
                            raise RuntimeError(hint)   # completed but produced no text
                        return
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"{hint} (HTTP {e.response.status_code})") from e
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise RuntimeError(f"{hint} ({type(e).__name__})") from e


# One warm `claude` process for the whole server run: the persona + journal live in its
# system prompt and the conversation in its session, so only the new turn is sent each time
# and only the first message pays the cold start. A lock serialises turns.
_coach_proc = None
_coach_lock = asyncio.Lock()


async def _coach_get():
    """The running warm coach process, (re)started as needed."""
    global _coach_proc
    if _coach_proc is not None and _coach_proc.returncode is None:
        return _coach_proc
    cli = shutil.which("claude")
    model = _config()["model"] or "sonnet"
    _coach_proc = await asyncio.create_subprocess_exec(
        cli, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
        "--include-partial-messages", "--verbose",
        "--system-prompt", coach_system(None),      # journal persona; fen rides per-turn
        "--allowedTools", "none", "--strict-mcp-config",  # no tools, no MCP servers
        "--model", model,                           # (the coach is pure text — don't
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,  # spin up Playwright)
        stderr=asyncio.subprocess.DEVNULL, cwd=str(ROOT))
    return _coach_proc


async def _stream_via_session(text: str):
    """Send one user turn to the warm session and stream its text back."""
    global _coach_proc
    async with _coach_lock:
        proc = await _coach_get()
        payload = json.dumps({"type": "user",
                              "message": {"role": "user", "content": text}}) + "\n"
        try:
            proc.stdin.write(payload.encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            _coach_proc = None                       # died — restart once
            proc = await _coach_get()
            proc.stdin.write(payload.encode())
            await proc.stdin.drain()
        emitted = False
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=120)
            except asyncio.TimeoutError:
                _coach_proc = None
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                raise RuntimeError("coach timed out")
            if not raw:                              # process exited unexpectedly
                _coach_proc = None
                raise RuntimeError("coach session ended")
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "stream_event":
                e = ev.get("event", {})
                if e.get("type") == "content_block_delta":
                    d = e.get("delta", {})
                    if d.get("type") == "text_delta" and d.get("text"):
                        emitted = True
                        yield d["text"]
            elif ev.get("type") == "result":
                if not emitted and isinstance(ev.get("result"), str) and ev["result"]:
                    yield ev["result"]
                return                               # turn done; process stays warm


def _coach_sse(*, fen=None, moves=None, ply=None, game=None, prompt=None, messages=None):
    """SSE generator streaming a coach reply from whichever backend is configured."""
    backend = _backend()
    text = prompt if prompt is not None else ((messages or [{}])[-1].get("content", ""))
    ctx = _board_context(fen, moves, ply, game)
    if ctx:                                          # the warm session carries context inline
        text = f"[BOARD CONTEXT]\n{ctx}\n\n{text}"

    async def gen():
        if backend == "none":
            yield f"data: {json.dumps({'error': 'No coach backend configured — pick one in Settings.'})}\n\n"
            yield "data: [DONE]\n\n"
            return
        try:
            msgs = messages if messages is not None else [
                {"role": "user", "content": prompt or ""}]
            if backend == "local":
                src = _stream_via_ollama(coach_system(fen, moves, ply, game), msgs)
            elif backend == "subscription":
                src = _stream_via_session(text)
            else:                                    # api
                src = _stream_via_api(coach_system(fen, moves, ply, game), msgs)
            async for t in src:
                yield f"data: {json.dumps({'t': t})}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return gen


@app.post("/api/chat")
async def api_chat(req: Request):
    body = await req.json()
    gen = _coach_sse(fen=body.get("fen"), moves=body.get("moves"),
                     ply=body.get("ply"), game=body.get("game"),
                     messages=body.get("messages", []))
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/coach/board")
async def api_coach_board(req: Request):
    """Silent board sync: tell the warm coach session about the current position without
    producing a visible reply, so it's already grounded when the student asks next. Only
    meaningful for the warm subscription session (its system prompt is frozen at startup);
    the API/local backends get fresh board context on every /api/chat call anyway, so this
    is a no-op for them."""
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False})
    ctx = _board_context(body.get("fen"), body.get("moves"), body.get("ply"), body.get("game"))
    if not ctx or _backend() != "subscription":   # only the warm session needs a silent sync
        return JSONResponse({"ok": True, "synced": False})
    text = ("[SILENT BOARD SYNC] The student changed the board. Remember this position for "
            "when they ask next; do NOT analyze it now. Reply with only: ok\n" + ctx)
    try:
        async for _ in _stream_via_session(text):   # drain + discard the terse ack
            pass
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": True, "synced": False})
    return JSONResponse({"ok": True, "synced": True})


@app.post("/api/coach/review")
async def api_coach_review(req: Request):
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        body = {}
    gen = _coach_sse(prompt=_progress_prompt(body.get("new_analyzed")))
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/coach/reset")
async def api_coach_reset():
    """Kill the warm coach session so the next message starts a fresh one. Done
    without the lock so it works even if a turn is stuck (kill → in-flight read
    gets EOF and unwinds)."""
    global _coach_proc
    p, _coach_proc = _coach_proc, None
    if p is not None and p.returncode is None:
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True}

# -------------------------------------------------------------------------- dashboard
INDEX_FILE = GAMES_DIR / "index.jsonl"
DRILL_TARGET = 5  # drills/day that mark a weekday "trained"
TRAINING_BULLETS = [
    "Rule #0 — after ANY loss, 10-min break; after 3 losses in a day, stop.",
    f"{DRILL_TARGET} blunder-check drills a day (that marks the day done).",
    "Before every move: what is my opponent's most forcing reply?",
    "Play slow (15|10+), then review one game here afterwards.",
]

_index_cache = {"mtime": -1.0, "rows": []}
_ana_cache = {"count": -1, "by_url": {}}


def _index_rows() -> list[dict]:
    """All metadata rows, sorted oldest→newest (cached by file mtime)."""
    if not INDEX_FILE.exists():
        return []
    m = INDEX_FILE.stat().st_mtime
    if m != _index_cache["mtime"]:
        rows = []
        for line in INDEX_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        rows.sort(key=lambda r: r.get("end_time", 0))
        _index_cache.update(mtime=m, rows=rows)
    return _index_cache["rows"]


def _analysis_by_url() -> dict:
    """url -> {id, accuracy, outcome, n_blunders, deciding, opponent, date} (cached)."""
    files = list(ANALYSIS_DIR.glob("*.json"))
    if len(files) != _ana_cache["count"]:
        by_url = {}
        for f in files:
            try:
                d = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            ms = d.get("mistakes") or []
            n_bl = sum(1 for m in ms if m.get("severity") == "blunder")
            deciding = any(m.get("severity") == "blunder"
                           and m.get("win_before", 0) >= 50 and m.get("win_after", 100) < 50
                           for m in ms)
            by_url[d.get("url")] = {"id": d.get("id"), "accuracy": d.get("accuracy"),
                                    "outcome": d.get("outcome"), "n_blunders": n_bl,
                                    "deciding": deciding, "opponent": d.get("opponent"),
                                    "date": d.get("date")}
        _ana_cache.update(count=len(files), by_url=by_url)
    return _ana_cache["by_url"]


def _drill_day_counts() -> dict:
    counts: dict = {}
    if DRILL_LOG.exists():
        for line in DRILL_LOG.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                counts[d.get("date")] = counts.get(d.get("date"), 0) + 1
    return counts


def _date_of(row: dict) -> str:
    et = row.get("end_time")
    return datetime.fromtimestamp(et).strftime("%Y-%m-%d") if et else ""


_hist_cache = {"key": None, "text": ""}


def _opening_name(eco: str) -> str:
    """Chess.com's eco field is 'Opening-Name-Variation...4.f3-e6' — keep the named part
    (before the move continuation) as the repertoire grouping key."""
    name = re.split(r"\.\.\.|(?<=[a-z])-\d", eco)[0]      # drop the move list
    name = re.sub(r"-\d+$", "", name).replace("-", " ").strip()
    return name[:48] or "?"


def _history_digest(recent_n: int = 14) -> str:
    """A compact, always-current summary of the student's whole Chess.com history for the
    coach: record, ratings, opening repertoire (per colour, with win%), record vs
    stronger/weaker opponents, and the most recent games. Cached by the index+analysis
    signature so it only rebuilds when the data changes."""
    rows = _index_rows()
    if not rows:
        return ""
    ana = _analysis_by_url()
    key = (len(rows), rows[-1].get("end_time"), len(ana))
    if _hist_cache["key"] == key:
        return _hist_cache["text"]

    from collections import Counter
    L: list[str] = []
    rec = Counter(r.get("outcome") for r in rows)
    L.append(f"{len(rows)} games {_date_of(rows[0])}→{_date_of(rows[-1])}: "
             f"{rec.get('win',0)}W / {rec.get('loss',0)}L / {rec.get('draw',0)}D.")
    tc = Counter(r.get("time_class") for r in rows)
    L.append("By speed: " + ", ".join(f"{k} {v}" for k, v in tc.most_common() if k))
    for pool in ("rapid", "blitz", "bullet"):
        pr = [r for r in rows if r.get("time_class") == pool and r.get("my_rating")]
        if pr:
            L.append(f"{pool.title()}: current {pr[-1]['my_rating']}, "
                     f"peak {max(x['my_rating'] for x in pr)} ({len(pr)} games)")

    def repertoire(color: str) -> str:
        gs = [r for r in rows if r.get("color") == color and r.get("eco")]
        by = Counter(_opening_name(r["eco"]) for r in gs)
        out = []
        for name, n in by.most_common(6):
            sub = [r for r in gs if _opening_name(r["eco"]) == name]
            w = sum(1 for r in sub if r.get("outcome") == "win")
            out.append(f"{name} ({n}, {round(100*w/n)}% win)")
        return "; ".join(out) or "n/a"

    # "as White/Black" = the games where the student had that colour; the opening name is
    # the whole opening as classified (so it can be the OPPONENT's defence, not the
    # student's own choice) — say "played/faced", not "your repertoire".
    L.append("Openings most played as White (whole opening incl. opponent's defence, win%): "
             + repertoire("white"))
    L.append("Openings most played as Black (win%): " + repertoire("black"))
    # NB: no "record vs stronger/weaker" line — chess.com reports POST-game ratings, so
    # comparing my_rating to opp_rating is circular (a win already moved both), not a
    # pre-game skill signal. Opening win% (opening choice ≠ caused by result) is fine.

    L.append(f"Most recent {recent_n} games (newest first):")
    for r in reversed(rows[-recent_n:]):
        a = ana.get(r.get("url")) or {}
        acc = f", {a['accuracy']}% acc" if a.get("accuracy") is not None else ""
        bl = f", {a['n_blunders']}bl" if a.get("n_blunders") is not None else ""
        L.append(f"  {_date_of(r)} {r.get('time_class','')} as {r.get('color','')} vs "
                 f"{r.get('opponent','?')} ({r.get('opp_rating','?')}): "
                 f"{r.get('outcome','?')} — {_opening_name(r.get('eco','') or '')}{acc}{bl}")

    _hist_cache.update(key=key, text="\n".join(L))
    return _hist_cache["text"]


def _training_week(counts: dict):
    """Mon–Fri of the current week + a completed-weekday streak."""
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    week = []
    for i in range(5):
        d = monday + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        n = counts.get(ds, 0)
        week.append({"date": ds, "dow": dows[i], "drills": n,
                     "completed": n >= DRILL_TARGET,
                     "is_today": d == today, "is_future": d > today})

    def done(dd):
        return counts.get(dd.strftime("%Y-%m-%d"), 0) >= DRILL_TARGET
    d = today
    # an in-progress (incomplete) today shouldn't zero a prior streak
    if d.weekday() < 5 and not done(d):
        d -= timedelta(days=1)
    streak, guard = 0, 0
    while guard < 90:
        guard += 1
        if d.weekday() < 5:           # weekends neither add nor break
            if done(d):
                streak += 1
            else:
                break
        d -= timedelta(days=1)
    return week, streak


@app.get("/api/dashboard")
def api_dashboard():
    rows = _index_rows()
    ana = _analysis_by_url()
    rap = [r for r in rows if r.get("time_class") == "rapid" and r.get("my_rating")]
    current = rap[-1]["my_rating"] if rap else None
    peak = max((r["my_rating"] for r in rap), default=None)
    history = [[r["end_time"], r["my_rating"]] for r in rap]  # chronological rapid rating

    last10 = []
    for r in reversed(rows[-10:]):        # newest first
        a = ana.get(r.get("url")) or {}
        last10.append({"id": a.get("id"), "date": _date_of(r), "opponent": r.get("opponent"),
                       "color": r.get("color"), "outcome": r.get("outcome"),
                       "time_class": r.get("time_class"), "my_rating": r.get("my_rating"),
                       "accuracy": a.get("accuracy"), "n_blunders": a.get("n_blunders")})

    counts = _drill_day_counts()
    week, streak = _training_week(counts)
    today = datetime.now().strftime("%Y-%m-%d")

    review = None
    for r in reversed(rows):              # most recent loss with a deciding blunder
        a = ana.get(r.get("url"))
        if a and a.get("outcome") == "loss" and a.get("deciding") and a.get("id"):
            review = {"id": a["id"], "opponent": a.get("opponent") or r.get("opponent"),
                      "date": a.get("date") or _date_of(r)}
            break

    return {
        "rating": {"current": current, "peak": peak, "floor": 1000, "target": 2000,
                   "pool": "rapid"},
        "rating_history": history,
        "totals": {"games": len(rows), "analyzed": len(ana)},
        "depth": _depth_stats(),
        "last10": last10,
        "week": week, "streak": streak,
        "recommended": {"drills_target": DRILL_TARGET,
                        "drills_today": counts.get(today, 0), "review": review},
        "training": TRAINING_BULLETS,
    }


def _progress_prompt(new_analyzed=None) -> str:
    """Compose a compact progression summary for the coach to react to."""
    rows = _index_rows()
    ana = _analysis_by_url()
    rap = [r for r in rows if r.get("time_class") == "rapid" and r.get("my_rating")]
    current = rap[-1]["my_rating"] if rap else "?"
    peak = max((r["my_rating"] for r in rap), default="?")
    recent = rap[-30:]
    delta = (recent[-1]["my_rating"] - recent[0]["my_rating"]) if len(recent) >= 2 else 0
    accs, bls = [], []
    for r in rows[-40:]:
        a = ana.get(r.get("url"))
        if a and a.get("accuracy") is not None:
            accs.append(a["accuracy"])
            bls.append(a.get("n_blunders", 0))
    racc = round(statistics.mean(accs), 1) if accs else "?"
    rbl = round(statistics.mean(bls), 2) if bls else "?"
    all_acc = [a["accuracy"] for a in ana.values() if a.get("accuracy") is not None]
    oacc = round(statistics.mean(all_acc), 1) if all_acc else "?"
    _, streak = _training_week(_drill_day_counts())
    lines = [
        f"Current rapid rating {current} (peak {peak}). Last ~30 rapid games: {delta:+d}.",
        f"Recent (~40 games) accuracy {racc}% vs overall {oacc}%; recent blunders/game {rbl}.",
        f"Drill streak: {streak} weekday(s). Total games analyzed: {len(rows)}.",
    ]
    if new_analyzed:
        lines.append(f"Just synced {new_analyzed} new game(s).")
    return ("Here is my recent progression and stats:\n" + "\n".join(lines) +
            "\n\nBased on my plan and tracked weaknesses, tell me briefly: what's improving, "
            "what's going wrong lately, and the single most important thing to focus on next. "
            "Be concrete and reference my actual numbers.")


# ----------------------------------------------------------------------------- sync
_sync_running = False


@app.post("/api/sync")
async def api_sync(req: Request):
    global _sync_running
    if _sync_running:
        return JSONResponse({"error": "busy"}, status_code=409)
    user = _config()["username"] or guess_username()
    if not user:
        return JSONResponse({"error": "no_username"}, status_code=400)
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        body = {}
    depth = max(6, min(30, int(body.get("depth") or 12)))    # clamp to a sane range
    deepen = bool(body.get("deepen"))

    async def gen():
        global _sync_running, _pgn_moves
        _sync_running = True
        new_analyzed = 0
        try:
            analyze_cmd = [sys.executable, "-m", "coach.analyze",
                           "--username", user, "--depth", str(depth)]
            if deepen:
                # Deepen existing games only — no fetch. Re-analyzes anything below `depth`.
                # A deepen re-runs the whole corpus, so parallelise it across processes
                # (the single-threaded multi-day pass was the pain point). Tunable via
                # COACH_ANALYZE_WORKERS; default = half the cores, leaving UI headroom.
                workers = max(1, int(os.environ.get(
                    "COACH_ANALYZE_WORKERS", (os.cpu_count() or 2) // 2)))
                phases = (
                    (f"Re-analyzing games below depth {depth} ({workers} workers)…",
                     analyze_cmd + ["--deepen", "--workers", str(workers)]),
                )
            else:
                phases = (
                    ("Fetching recent games…",
                     [sys.executable, "-m", "coach.fetch_games", user, "--months", "2"]),
                    (f"Analyzing new games (depth {depth})…", analyze_cmd),
                )
            # Always refresh the before/after progress report after (re)analysis.
            phases = (*phases, ("Updating progress report…",
                                [sys.executable, "-m", "coach.progress"]))
            for label, cmd in phases:
                yield f"data: {json.dumps({'t': label})}\n\n"
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=str(ROOT), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", "replace").rstrip()
                    if not line:
                        continue
                    m = re.search(r"Analyzed (\d+) game", line)
                    if m:
                        # Sum across shard workers (each prints its own count); a plain
                        # sequential run prints exactly one such line, so this still works.
                        new_analyzed += int(m.group(1))
                    yield f"data: {json.dumps({'t': line})}\n\n"
                await proc.wait()
            rows = _index_rows()
            rap = [r for r in rows if r.get("time_class") == "rapid" and r.get("my_rating")]
            cur = rap[-1]["my_rating"] if rap else None
            yield ("data: " + json.dumps({"done": {
                "new_analyzed": new_analyzed, "games_total": len(rows),
                "current_rating": cur}}) + "\n\n")
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            _sync_running = False
            # A deepen run rewrites files without changing their count, so the
            # count-keyed caches would go stale — force a rebuild next read.
            _games_cache["count"] = _drill_cache["count"] = _ana_cache["count"] = -1
            # Newly-fetched PGNs mean new games to review; drop the once-built move
            # cache so /api/game/<id> serves their moves instead of an empty list.
            _pgn_moves = None
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ------------------------------------------------------------------------------ apis
@app.get("/api/games")
def api_games():
    return _games_list()


@app.get("/api/game/{gid}")
def api_game(gid: str):
    f = ANALYSIS_DIR / f"{gid}.json"
    if not f.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    data = json.loads(f.read_text())
    data["moves"] = _moves_for(gid)
    return data


@app.get("/api/drill")
def api_drill(mode: str = "standard"):
    if mode == "endgame":
        eg = _endgame_pool()
        if not eg:
            return JSONResponse(
                {"error": "no endgame drills yet — run `python -m coach.endgame_conversion`"},
                status_code=404)
        return random.choice(eg)
    pool = _drill_pool()
    if not pool:
        return JSONResponse({"error": "no blunders analyzed yet"}, status_code=404)
    # Prefer clean, gradeable positions: a "deciding" blunder (threw a non-lost game),
    # in the opening/middlegame (endgames are near-drawn and win% is too noisy to
    # auto-grade), with a clear advantage to defend. Fall back progressively.
    deciding = [p for p in pool if p.get("deciding")]
    clean = [p for p in deciding
             if p.get("phase") != "endgame" and (p.get("win_before") or 0) >= 58]
    if clean and random.random() < 0.85:
        src = clean
    elif deciding:
        src = deciding
    else:
        src = pool
    return random.choice(src)


@app.post("/api/drill/attempt")
async def api_drill_attempt(req: Request):
    body = await req.json()
    outcome = body.get("outcome")
    if outcome not in ("solved", "missed", "revealed"):
        return JSONResponse({"error": "bad outcome"}, status_code=400)
    _log_attempt({
        "id": body.get("id"), "outcome": outcome,
        "your_move": body.get("your_move"), "best": body.get("best"),
        "played": body.get("played"), "phase": body.get("phase"),
        "deciding": body.get("deciding"),
        "win_before": body.get("win_before"), "win_after": body.get("win_after"),
        "url": body.get("url"),
    })
    return _drill_stats()


@app.get("/api/drill/stats")
def api_drill_stats():
    return _drill_stats()


@app.post("/api/drill/grade")
async def api_drill_grade(req: Request):
    """Grade a drill move by fresh engine eval: how much win% it drops vs the best.

    grade = best (== engine's move) / good (< INACCURACY drop) / retry (>=). This is
    robust where a hard-coded single "best SAN" was not — several moves can pass.
    """
    body = await req.json()
    fen, uci = body.get("fen", ""), body.get("uci", "")
    try:
        board = chess.Board(fen)
        move = chess.Move.from_uci(uci)
    except ValueError:
        return JSONResponse({"error": "bad fen/uci"}, status_code=400)
    if move not in board.legal_moves:
        return JSONResponse({"error": "illegal move"}, status_code=400)

    mover_white = board.turn                       # side to move in the drill position
    before = await _in_thread(_eval_fen, fen, 0.2, 14)   # fixed depth = stable grading
    best_uci, best_san, cp0 = before.get("best_uci"), before.get("best"), before.get("cp")

    def mover_win(cp):
        return None if cp is None else win_pct(cp if mover_white else -cp)

    ref = mover_win(cp0)
    played_san = board.san(move)
    board.push(move)
    if board.is_checkmate():
        uwin = 100.0                               # mover just delivered mate
    elif board.is_game_over():
        uwin = 50.0                                # stalemate / draw
    else:
        after = await _in_thread(_eval_fen, board.fen(), 0.2, 14)
        uwin = mover_win(after.get("cp"))

    win_loss = max(0.0, ref - uwin) if (ref is not None and uwin is not None) else 0.0
    if best_uci and uci == best_uci:
        grade = "best"
    elif win_loss < INACCURACY:
        grade = "good"
    else:
        grade = "retry"
    return {"grade": grade, "win_loss": round(win_loss, 1),
            "best_san": best_san, "best_uci": best_uci, "played_san": played_san}


@app.get("/api/status")
def api_status():
    backend = _backend()
    analyzed = len(list(ANALYSIS_DIR.glob("*.json")))
    return {"analyzed": analyzed, "coach": backend != "none", "backend": backend,
            "username": _config()["username"],
            "configured": bool(_config()["username"]) or analyzed > 0}


@app.get("/api/config")
def api_get_config():
    cfg = _config()
    return {"username": cfg["username"] or "", "backend": cfg["backend"] or "",
            "model": cfg["model"], "local_url": cfg["local_url"], "think": cfg["think"],
            "has_key": bool(cfg["anthropic_api_key"]),
            "claude_cli": bool(shutil.which("claude"))}


@app.post("/api/config")
async def api_set_config(req: Request):
    global _coach_proc
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    _save_config(body)
    p, _coach_proc = _coach_proc, None             # reset the warm session so the new
    if p is not None and p.returncode is None:     # backend/model applies immediately
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "backend": _backend()}


@app.post("/api/eval")
async def api_eval(req: Request):
    body = await req.json()
    fen = body.get("fen", "")
    try:
        chess.Board(fen)
    except ValueError:
        return JSONResponse({"error": "bad fen"}, status_code=400)
    depth = body.get("depth")
    return await _in_thread(_eval_fen, fen, float(body.get("movetime", 0.2)),
                            int(depth) if depth else None)


@app.post("/api/lines")
async def api_lines(req: Request):
    """Top-N candidate moves + evals (side-branch explorer)."""
    body = await req.json()
    fen = body.get("fen", "")
    try:
        chess.Board(fen)
    except ValueError:
        return JSONResponse({"error": "bad fen"}, status_code=400)
    return await _in_thread(_top_lines, fen, int(body.get("n", 3)),
                            int(body.get("depth", 14)))


@app.post("/api/move")
async def api_move(req: Request):
    body = await req.json()
    fen = body.get("fen", "")
    try:
        chess.Board(fen)
    except ValueError:
        return JSONResponse({"error": "bad fen"}, status_code=400)
    return await _in_thread(_engine_move, fen, int(body.get("skill", 8)),
                            float(body.get("movetime", 0.3)))


@app.get("/")
def index():
    # No-cache so a freshly-deployed app.js never pairs with a browser-cached index.html
    # (that mismatch throws in wire() when new markup is missing).
    return FileResponse(str(STATIC / "index.html"),
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/favicon.ico")
def favicon():
    from fastapi import Response
    return Response(status_code=204)


@app.on_event("shutdown")
def _shutdown():
    global _engine
    if _engine is not None:
        try:
            _engine.quit()
        except Exception:
            pass
    if _coach_proc is not None and _coach_proc.returncode is None:
        try:
            _coach_proc.kill()
        except Exception:
            pass
