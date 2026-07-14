"""Run every pulled game through Stockfish and record the player's mistakes.

For each position we ask the engine once for its evaluation (White's point of
view) and its best move. Walking the game, the centipawn a *player* threw away
on a given move is:

    cp_loss = eval_before(player POV) - eval_after(player POV)

which we classify into inaccuracy / mistake / blunder. We only score the moves
of the *player we're coaching* (identified by matching the PGN's White/Black tag
to their username), because those are the only moves they can learn from.

Output: one JSON per game in data/analysis/, safe to re-run (already-analyzed
games are skipped).

Usage:
    python -m coach.analyze [--username NAME] [--depth 15] [--movetime 0.0]
    STOCKFISH_PATH=/path/to/stockfish python -m coach.analyze
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys

import chess
import chess.engine
import chess.pgn

from .paths import ANALYSIS_DIR, GAMES_DIR

# We score moves by how much *win probability* a move throws away, not raw
# centipawns. This bounds forced mates, and correctly ignores giving back eval
# while still totally winning (or "blundering" in an already-lost position).
# Thresholds are in win-percentage points (0–100).
INACCURACY = 5.0
MISTAKE = 10.0
BLUNDER = 20.0
MATE_SCORE = 100_000  # cp value assigned to forced mate (saturates win% to 0/100)


def win_pct(cp: int) -> float:
    """Convert a centipawn eval (from the mover's POV) to win probability 0–100.
    Lichess's logistic model; naturally saturates near ±1000cp and for mates."""
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def move_accuracy(win_loss: float) -> float:
    """Lichess per-move accuracy% from win% dropped on the move."""
    return max(0.0, min(100.0, 103.1668 * math.exp(-0.04354 * win_loss) - 3.1668))

PIECE_VALUES = {
    chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
    chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0,
}


def find_stockfish() -> str:
    from .paths import ROOT
    candidates = [os.environ.get("STOCKFISH_PATH"), shutil.which("stockfish")]
    for name in ("stockfish", "stockfish.exe"):      # a binary dropped in the repo root
        p = ROOT / name
        if p.exists():
            candidates.append(str(p))
    path = next((c for c in candidates if c), None)
    if not path:
        raise SystemExit(
            "Stockfish not found. Run ./setup.sh (setup.ps1 on Windows), install it "
            "(`sudo apt-get install stockfish` / `brew install stockfish`), put the binary "
            "on your PATH, or set STOCKFISH_PATH."
        )
    return path


def game_id(headers: chess.pgn.Headers) -> str:
    """Stable id from the game's URL/date/players so we don't re-analyze."""
    key = "|".join(headers.get(k, "") for k in
                   ("Link", "Site", "Date", "White", "Black", "UTCTime"))
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def severity(win_loss: float) -> str | None:
    if win_loss >= BLUNDER:
        return "blunder"
    if win_loss >= MISTAKE:
        return "mistake"
    if win_loss >= INACCURACY:
        return "inaccuracy"
    return None


def phase_of(board: chess.Board) -> str:
    """Rough game phase from remaining non-pawn material and move number."""
    non_pawn = sum(
        1 for sq in chess.SQUARES
        if (p := board.piece_at(sq)) and p.piece_type not in (chess.PAWN, chess.KING)
    )
    if non_pawn <= 6:
        return "endgame"
    if board.fullmove_number <= 12:
        return "opening"
    return "middlegame"


def hung_piece_value(board_before: chess.Board, played: chess.Move,
                     best_reply: chess.Move | None) -> int:
    """If the opponent's best reply to the played move just grabs an undefended
    piece, return its value (a cheap 'you hung something' heuristic — the coach
    refines the rest). `best_reply` must be the engine's best move in the position
    AFTER `played` (i.e. the opponent's reply), not the player's own alternative."""
    if best_reply is None or board_before.is_capture(played):
        return 0                                   # only score quiet moves that drop material
    player = board_before.turn                     # side that just played `played`
    board_after = board_before.copy()
    board_after.push(played)                       # opponent to move in board_after
    if not board_after.is_capture(best_reply):
        return 0
    victim = board_after.piece_at(best_reply.to_square)
    # the reply must capture the player's piece, and the player must not be able to
    # recapture on that square (undefended = truly hung, not an even trade).
    if (victim and victim.color == player
            and not board_after.is_attacked_by(player, best_reply.to_square)):
        return PIECE_VALUES.get(victim.piece_type, 0)
    return 0


def cp_white(info) -> int:
    return info["score"].white().score(mate_score=MATE_SCORE)


def analyze_game(game: chess.pgn.Game, engine: chess.engine.SimpleEngine,
                 username: str, limit: chess.engine.Limit) -> dict | None:
    headers = game.headers
    white = headers.get("White", "")
    black = headers.get("Black", "")
    uname = username.lower()
    if uname == white.lower():
        player_color = chess.WHITE
    elif uname == black.lower():
        player_color = chess.BLACK
    else:
        return None  # not the coached player's game

    board = game.board()
    moves = list(game.mainline_moves())
    if not moves:
        return None

    # A per-game token so python-chess sends `ucinewgame` when we move to a new game:
    # this clears the transposition table BETWEEN games (positions WITHIN a game still
    # share it), making each game's analysis independent of what was analyzed before it.
    # Without it, results depend on game order / how the corpus is sharded across workers.
    token = game_id(headers)

    # Evaluate the starting position, then after each move. Store (cp_white, best_move).
    evals: list[tuple[int, chess.Move | None]] = []
    info = engine.analyse(board, limit, game=token)
    evals.append((cp_white(info), info.get("pv", [None])[0]))
    for mv in moves:
        board.push(mv)
        info = engine.analyse(board, limit, game=token)
        evals.append((cp_white(info), info.get("pv", [None])[0]))

    # Walk again, scoring only the player's moves.
    board = game.board()
    mistakes = []
    accuracies = []
    cp_losses = []
    for i, mv in enumerate(moves):
        is_player = (board.turn == player_color)
        cp_before_w, best_before = evals[i]
        cp_after_w, best_reply = evals[i + 1]   # engine's best move for the OPPONENT now
        san = board.san(mv)

        if is_player:
            # everything from the player's point of view
            cp_before = cp_before_w if player_color == chess.WHITE else -cp_before_w
            cp_after = cp_after_w if player_color == chess.WHITE else -cp_after_w
            wb, wa = win_pct(cp_before), win_pct(cp_after)

            # If you played the engine's own top move, you threw away nothing —
            # any eval wobble is search-horizon noise, not a mistake.
            win_loss = 0.0 if mv == best_before else max(0.0, wb - wa)

            accuracies.append(move_accuracy(win_loss))
            cp_losses.append(min(1000, max(0, cp_before - cp_after)))  # clamped, for ACPL
            sev = severity(win_loss)
            if sev:
                best_san = board.san(best_before) if best_before else None
                mistakes.append({
                    "ply": i + 1,
                    "move_number": board.fullmove_number,
                    "color": "white" if player_color == chess.WHITE else "black",
                    "played": san,
                    "best": best_san,
                    "win_loss": round(win_loss, 1),      # win% dropped (primary)
                    "cp_loss": min(2000, max(0, cp_before - cp_after)),
                    "severity": sev,
                    "phase": phase_of(board),
                    "win_before": round(wb, 1),
                    "win_after": round(wa, 1),
                    "fen": board.fen(),
                    "hung_value": hung_piece_value(board, mv, best_reply),
                })
        board.push(mv)

    acpl = round(sum(cp_losses) / len(cp_losses)) if cp_losses else 0
    accuracy = round(sum(accuracies) / len(accuracies), 1) if accuracies else 0.0
    result = headers.get("Result", "")
    if player_color == chess.WHITE:
        outcome = {"1-0": "win", "0-1": "loss"}.get(result, "draw")
    else:
        outcome = {"0-1": "win", "1-0": "loss"}.get(result, "draw")

    return {
        "id": game_id(headers),
        "url": headers.get("Link", ""),
        "date": headers.get("Date", headers.get("UTCDate", "")),
        "color": "white" if player_color == chess.WHITE else "black",
        "opponent": black if player_color == chess.WHITE else white,
        "player_elo": headers.get("WhiteElo" if player_color == chess.WHITE else "BlackElo", ""),
        "time_control": headers.get("TimeControl", ""),
        "eco": headers.get("ECO", ""),
        "opening": headers.get("ECOUrl", "").rsplit("/", 1)[-1].replace("-", " "),
        "result": result,
        "outcome": outcome,
        "accuracy": accuracy,
        "acpl": acpl,
        "n_moves": len(cp_losses),
        "mistakes": mistakes,
    }


def iter_games():
    for pgn_path in sorted(GAMES_DIR.glob("*.pgn")):
        with pgn_path.open(encoding="utf-8") as fh:
            while (game := chess.pgn.read_game(fh)) is not None:
                yield game


def guess_username() -> str | None:
    """Infer the coached player from the most common name across pulled files."""
    from collections import Counter
    names = Counter()
    for g in iter_games():
        names[g.headers.get("White", "").lower()] += 1
        names[g.headers.get("Black", "").lower()] += 1
    names.pop("", None)
    return names.most_common(1)[0][0] if names else None


def _skip_decision(out, reanalyze: bool, deepen: bool, depth_run: bool,
                   target_depth: int) -> str:
    """Whether to 'analyze', 'deepen', or 'skip' a game whose output file is `out`."""
    if not out.exists() or reanalyze:
        return "analyze"
    prev_depth = None
    try:
        prev_depth = json.loads(out.read_text()).get("analysis_depth")
    except Exception:  # noqa: BLE001
        pass
    already_deep = isinstance(prev_depth, int) and prev_depth >= target_depth
    if deepen and depth_run and not already_deep:
        return "deepen"      # recorded below target depth (or legacy) — re-run deeper
    return "skip"


def _in_shard(gid: str, shard: tuple[int, int] | None) -> bool:
    """With --shard I/N, a process handles only games whose id lands in its bucket, so N
    independent processes partition the corpus with no overlap and no coordination."""
    if shard is None:
        return True
    i, n = shard
    return int(gid, 16) % n == i


def _parallel(args, username: str) -> None:
    """--workers N: launch N sharded copies of ourselves. Each child is the ordinary
    single-engine sequential analyzer (proven, no shared engine/asyncio to deadlock);
    they just partition the games by --shard. This is the real full-history speedup."""
    base = [sys.executable, "-m", "coach.analyze", "--username", username,
            "--depth", str(args.depth), "--movetime", str(args.movetime)]
    if args.reanalyze:
        base.append("--reanalyze")
    if args.deepen:
        base.append("--deepen")
    print(f"Launching {args.workers} sharded workers…")
    procs = [subprocess.Popen(base + ["--shard", f"{i}/{args.workers}"])
             for i in range(args.workers)]
    rc = [p.wait() for p in procs]
    failed = [i for i, c in enumerate(rc) if c != 0]
    print(f"\nAll {args.workers} workers done."
          + (f" (workers {failed} exited non-zero)" if failed else ""))
    if failed:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Analyze pulled games with Stockfish.")
    p.add_argument("--username", help="player to coach (default: inferred from PGNs)")
    p.add_argument("--depth", type=int, default=15, help="engine search depth (default 15)")
    p.add_argument("--movetime", type=float, default=0.0,
                   help="seconds per position instead of fixed depth (0 = use depth)")
    p.add_argument("--reanalyze", action="store_true", help="ignore cached results")
    p.add_argument("--deepen", action="store_true",
                   help="re-analyze games recorded below --depth (never downgrades a "
                        "game already analyzed at >= --depth)")
    p.add_argument("--workers", type=int, default=1,
                   help="analyze this many games in parallel by launching N independent "
                        "sharded processes (default 1). The real full-history speedup: "
                        "games are independent, so N processes ≈ N× throughput. "
                        "(Stockfish THREADS don't help a fixed-depth search — verified.)")
    p.add_argument("--shard", default=None, metavar="I/N",
                   help="internal: only handle games whose id falls in bucket I of N "
                        "(set automatically by --workers on each child process)")
    args = p.parse_args(argv)

    shard = None
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        shard = (i, n)

    username = args.username or guess_username()
    if not username:
        raise SystemExit("No games found. Run coach.fetch_games first.")
    print(f"Coaching player: {username}")

    limit = (chess.engine.Limit(time=args.movetime) if args.movetime > 0
             else chess.engine.Limit(depth=args.depth))

    depth_run = args.movetime <= 0                 # depth mode vs fixed movetime
    analyzed = skipped = deepened = 0

    def line(res):                                 # res = (date, opp, outcome, acpl, m, redo)
        date, opp, outcome, acpl, m, redo = res
        print(f"  {date} vs {opp[:18]:18} {outcome:5} ACPL {acpl:4}  {m} flagged"
              f"{'  (deepened)' if redo else ''}")

    # Games are independent, so a full re-run parallelises cleanly by launching N
    # sharded subprocesses. (Stockfish THREADS don't speed up a fixed-depth search
    # — verified — so we scale by games, not by threads-per-search. We shell out to
    # separate processes rather than a worker pool because python-chess's asyncio
    # engine deadlocks inside multiprocessing workers.)
    if args.workers > 1 and shard is None:
        return _parallel(args, username)

    engine = chess.engine.SimpleEngine.popen_uci(find_stockfish())
    try:
        for game in iter_games():
            gid = game_id(game.headers)
            if not _in_shard(gid, shard):
                continue                           # another worker owns this game
            out = ANALYSIS_DIR / f"{gid}.json"
            act = _skip_decision(out, args.reanalyze, args.deepen, depth_run, args.depth)
            if act == "skip":
                skipped += 1
                continue
            redo = act == "deepen"
            data = analyze_game(game, engine, username, limit)
            if data is None:
                continue
            data["analysis_depth"] = args.depth if depth_run else None
            data["analysis_movetime"] = args.movetime if not depth_run else None
            out.write_text(json.dumps(data, indent=2))
            analyzed += 1
            deepened += 1 if redo else 0
            line((data["date"], data["opponent"], data["outcome"], data["acpl"],
                  len(data["mistakes"]), redo))
    finally:
        engine.quit()

    tag = f"Analyzed {analyzed} game(s)"
    if args.deepen:
        tag += f" ({deepened} deepened to depth {args.depth})"
    print(f"\n{tag}, skipped {skipped} cached.")
    if analyzed:
        print("Next: python -m coach.report")


if __name__ == "__main__":
    main()
