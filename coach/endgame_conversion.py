"""Isolate and drill the endgame-conversion blunder — throwing a winning endgame.

An endgame-conversion blunder = a blunder in the endgame phase from a winning position
(win_before >= WINNING). Each is classified by ending type (pawn / rook / queen / minor
/ rook+minor / opposite-bishops / heavy) so recurring motifs surface.

Writes journal/endgame_conversion.md (report) and data/endgame_drills.json (the web
app's endgame drill pool). This gets its own pool because the generic drill skips
endgames — near-drawn endings make single-move grading noisy — so we keep only
decisively-winning positions here, where the win% signal is sharp.

Usage:
    python -m coach.endgame_conversion
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime

import chess

from .paths import ANALYSIS_DIR, DATA, JOURNAL

# A blunder counts as a *conversion* blunder only from a clearly-winning endgame.
WINNING = 70.0        # win_before threshold to call the position "winning"
DRILL_WINNING = 75.0  # stricter bar for the auto-graded drill pool (sharp signal)

DRILLS_OUT = DATA / "endgame_drills.json"
REPORT_OUT = JOURNAL / "endgame_conversion.md"


# --------------------------------------------------------------------------- classify
def endgame_type(fen: str) -> str:
    """Bucket an endgame by the non-king, non-pawn material on the board (both sides).

    Buckets chosen to match how the motifs are actually studied/drilled, so the
    report groups by 'rook ending', 'pawn ending', etc. rather than raw piece lists."""
    try:
        board = chess.Board(fen)
    except ValueError:
        return "unknown"

    pieces = [p for sq in chess.SQUARES if (p := board.piece_at(sq))
              and p.piece_type not in (chess.PAWN, chess.KING)]
    types = Counter(p.piece_type for p in pieces)

    n_q = types.get(chess.QUEEN, 0)
    n_r = types.get(chess.ROOK, 0)
    n_b = types.get(chess.BISHOP, 0)
    n_n = types.get(chess.KNIGHT, 0)
    minors = n_b + n_n

    if not pieces:
        return "pawn"
    if n_q:
        return "queen"
    if n_r and minors:
        return "rook+minor"
    if n_r:
        return "rook"
    # only minors from here
    if n_b == 2 and n_n == 0:
        # opposite-coloured bishops (one each side, different square colour) are the
        # notorious "can't convert an extra pawn" ending — call it out specifically.
        bishops = [sq for sq in chess.SQUARES if (p := board.piece_at(sq))
                   and p.piece_type == chess.BISHOP]
        by_color = {board.piece_at(sq).color for sq in bishops}
        if len(by_color) == 2 and (chess.square_rank(bishops[0]) + chess.square_file(bishops[0])) % 2 \
                != (chess.square_rank(bishops[1]) + chess.square_file(bishops[1])) % 2:
            return "opposite-bishops"
    if minors:
        return "minor"
    return "other"


# --------------------------------------------------------------------------- load
def load_games() -> list[dict]:
    games = []
    for f in ANALYSIS_DIR.glob("*.json"):
        try:
            games.append(json.loads(f.read_text()))
        except json.JSONDecodeError:
            continue
    return games


def conversion_blunders(games: list[dict]) -> list[dict]:
    """Flat list of endgame-conversion blunders across all games (winning-endgame throws)."""
    out = []
    for g in games:
        for m in g.get("mistakes", []):
            if (m.get("severity") == "blunder"
                    and m.get("phase") == "endgame"
                    and m.get("fen") and m.get("best")
                    and (m.get("win_before") or 0) >= WINNING):
                out.append({
                    "fen": m["fen"], "best": m["best"], "played": m["played"],
                    "win_before": m.get("win_before"), "win_after": m.get("win_after"),
                    "win_loss": m.get("win_loss"), "cp_loss": m.get("cp_loss"),
                    "move_number": m.get("move_number"), "color": m.get("color"),
                    "eg_type": endgame_type(m["fen"]),
                    "outcome": g.get("outcome"), "opponent": g.get("opponent"),
                    "date": g.get("date"), "url": g.get("url"), "game_id": g.get("id"),
                })
    return out


def game_conversion_stats(games: list[dict]) -> dict:
    """Game-level: of games that reached a winning endgame, how many were NOT won."""
    reached = thrown = 0
    for g in games:
        winning_eg = any(m.get("phase") == "endgame" and (m.get("win_before") or 0) >= 85
                         for m in g.get("mistakes", []))
        if winning_eg:
            reached += 1
            if g.get("outcome") != "win":
                thrown += 1
    return {"reached": reached, "thrown": thrown}


# --------------------------------------------------------------------------- report
def bar(n: int, total: int, width: int = 22) -> str:
    if not total:
        return ""
    return "█" * round(width * n / total) + "·" * (width - round(width * n / total))


def throw_move_type(fen: str, san: str) -> str:
    """Classify the move that threw the win — to see if it's a tactic or quiet drift."""
    try:
        b = chess.Board(fen)
        m = b.parse_san(san)
        pc = b.piece_at(m.from_square)
    except Exception:
        return "?"
    kind = {chess.KING: "king", chess.PAWN: "pawn", chess.QUEEN: "queen",
            chess.ROOK: "rook"}.get(pc.piece_type if pc else None, "minor")
    tags = [f"{kind} move"]
    if b.is_capture(m):
        tags.append("capture")
    if san.endswith(("+", "#")):
        tags.append("check")
    return " + ".join(tags)


def build_report(games: list[dict], blunders: list[dict], gstats: dict) -> str:
    by_type = Counter(b["eg_type"] for b in blunders)
    lost_after = sum(1 for b in blunders if b["outcome"] == "loss")
    total = len(blunders)
    n_games = len(games)

    L: list[str] = []
    w = L.append
    w(f"# The Endgame-Conversion Blunder — {datetime.now():%Y-%m-%d}")
    w("")
    w("*Isolated leak: throwing a **winning endgame**. Auto-generated by "
      "`python -m coach.endgame_conversion`.*")
    w("")
    w("## Why this is its own thing")
    w("")
    w(f"- **{total}** endgame blunders came from a *winning* position "
      f"(win_before ≥ {WINNING:.0f}%) across {n_games} analyzed games.")
    if gstats["reached"]:
        pct = 100 * gstats["thrown"] / gstats["reached"]
        w(f"- Of **{gstats['reached']}** games where you reached a clearly winning endgame "
          f"(≥85%), you **failed to win {gstats['thrown']} ({pct:.0f}%)**.")
    w(f"- Of those {total} winning-endgame blunders, **{lost_after} "
      f"({100*lost_after/max(1,total):.0f}%)** were in games you went on to lose.")
    w("- The generic drill skips endgames (near-drawn = noisy grading); this pool keeps "
      f"only *decisive* ones (win_before ≥ {DRILL_WINNING:.0f}%) so the win% signal is sharp.")
    w("")
    w("## Which endgames you throw")
    w("*(winning-endgame blunders grouped by material — where to put the study hours)*")
    w("")
    label = {"pawn": "King & pawn", "rook": "Rook", "queen": "Queen",
             "rook+minor": "Rook + minor", "minor": "Minor piece",
             "opposite-bishops": "Opposite-colour bishops", "other": "Other/heavy",
             "unknown": "unknown"}
    for t, c in by_type.most_common():
        w(f"- `{label.get(t, t):24}` {c:4}  {bar(c, total)}")
    w("")
    w("## How you throw them (recurring pattern)")
    w("")
    # draw vs loss: are you drawing won endings (technique) or losing them (counterplay)?
    to_draw = sum(1 for b in blunders if (b.get("win_after") or 0) >= 40)
    w(f"- **{to_draw} of {total} ({100*to_draw/max(1,total):.0f}%) resolve to a DRAW**, "
      f"not a loss — you bleed *half-points* to missed technique far more than you get "
      f"mated. (The rest, {total-to_draw}, flip to a loss.)")
    # by type: which endings you draw (pure technique) vs actually lose (counterplay)
    w("- Draw-vs-loss by ending — where it's technique vs where counterplay bites:")
    for t in ("rook", "minor", "opposite-bishops", "rook+minor", "pawn", "queen"):
        sub = [b for b in blunders if b["eg_type"] == t]
        if not sub:
            continue
        d = sum(1 for b in sub if (b.get("win_after") or 0) >= 40)
        w(f"    - `{label.get(t, t):24}` n={len(sub):4}  →  "
          f"{100*d/len(sub):3.0f}% drawn / {100*(len(sub)-d)/len(sub):3.0f}% lost")
    # move that throws it: tactic or quiet drift?
    mt = Counter(throw_move_type(b["fen"], b["played"]) for b in blunders)
    w("- The move that throws it (mostly *quiet* moves — technique, not walking into tactics):")
    for k, c in mt.most_common(6):
        w(f"    - `{k:22}` {c:4}  {bar(c, total)}")
    # clock zone
    band = Counter()
    for b in blunders:
        mn = b.get("move_number") or 0
        band["≤20" if mn <= 20 else ("21–30" if mn <= 30 else ("31–40" if mn <= 40 else "41+"))] += 1
    late = band["31–40"] + band["41+"]
    w(f"- **{100*late/max(1,total):.0f}% happen after move 30** "
      f"(31–40: {band['31–40']}, 41+: {band['41+']}) — deep in the game, the rapid clock zone.")
    w("")
    w("## Worst winning endgames thrown")
    w("*The coach should open these and turn each into a lesson / drill.*")
    w("")
    for b in sorted(blunders, key=lambda x: x.get("win_loss") or 0, reverse=True)[:12]:
        swing = f"{b.get('win_before','?')}%→{b.get('win_after','?')}%"
        w(f"- move {b['move_number']} ({b['color']}, {label.get(b['eg_type'], b['eg_type'])}): "
          f"played **{b['played']}**, best {b['best']} ({swing}, {b['outcome']})")
        w(f"  - `{b['fen']}`")
    w("")
    w("---")
    w(f"*{total} conversion blunders · drill pool → `data/endgame_drills.json` "
      f"(served at `/api/drill?mode=endgame`).*")
    return "\n".join(L) + "\n"


def build_drill_pool(blunders: list[dict]) -> list[dict]:
    """Decisive winning-endgame positions with a clear best — sharp enough to auto-grade."""
    import hashlib
    pool = []
    seen = set()
    for b in blunders:
        if (b.get("win_before") or 0) < DRILL_WINNING:
            continue
        fen = b["fen"]
        if fen in seen:
            continue
        seen.add(fen)
        pool.append({
            "id": hashlib.sha1(fen.encode()).hexdigest()[:12],
            "fen": fen, "best": b["best"], "played": b["played"],
            "win_before": b["win_before"], "win_after": b["win_after"],
            "phase": "endgame", "eg_type": b["eg_type"], "deciding": True,
            "color": b["color"], "url": b["url"],
        })
    return pool


def main() -> None:
    games = load_games()
    blunders = conversion_blunders(games)
    gstats = game_conversion_stats(games)

    REPORT_OUT.write_text(build_report(games, blunders, gstats))
    pool = build_drill_pool(blunders)
    DRILLS_OUT.write_text(json.dumps(pool, indent=1))

    print(f"Analyzed {len(games)} games.")
    print(f"Winning-endgame (conversion) blunders: {len(blunders)}")
    print(f"Drill pool (win_before>={DRILL_WINNING:.0f}%): {len(pool)} positions -> {DRILLS_OUT}")
    print(f"Report -> {REPORT_OUT}")


if __name__ == "__main__":
    main()
