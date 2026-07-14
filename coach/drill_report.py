"""Roll the drill attempt log into a readable, committed progress record.

The web app appends one JSON row per drill attempt to `data/drill_log.jsonl`
(that raw log is git-ignored, like the rest of `data/`). This summarises it into
`journal/drill_progress.md` — the tracked coaching record: are you solving your own
blunder positions, is the solve-rate trending up, and are repeat positions sticking?

An attempt's `outcome` is one of:
  solved   — you found the engine's best move without revealing
  missed   — you played a move, it wasn't best
  revealed — you gave up and revealed before trying

Usage:
    python -m coach.drill_report
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime

from .paths import DATA, JOURNAL

DRILL_LOG = DATA / "drill_log.jsonl"


def load() -> list[dict]:
    if not DRILL_LOG.exists():
        return []
    rows = []
    for line in DRILL_LOG.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda r: r.get("ts", 0))
    return rows


def rate(rows) -> float:
    n = len(rows)
    return 100 * sum(r.get("outcome") == "solved" for r in rows) / n if n else 0.0


def build(rows) -> str:
    L, w = [], None
    out = []
    w = out.append
    n = len(rows)
    w(f"# Drill Progress — {datetime.now():%Y-%m-%d}")
    w("")
    if not n:
        w("No drill attempts logged yet. Open the web app (`python -m coach.web`), go to "
          "**Drill**, and solve a few — every attempt is recorded here.")
        return "\n".join(out) + "\n"

    solved = sum(r.get("outcome") == "solved" for r in rows)
    missed = sum(r.get("outcome") == "missed" for r in rows)
    revealed = sum(r.get("outcome") == "revealed" for r in rows)
    span = (f"{datetime.fromtimestamp(rows[0]['ts']):%Y-%m-%d} → "
            f"{datetime.fromtimestamp(rows[-1]['ts']):%Y-%m-%d}")
    w(f"**{n} attempts** ({span}) — **{solved} solved ({rate(rows):.0f}%)**, "
      f"{missed} missed, {revealed} revealed (gave up). "
      f"**{len({r.get('id') for r in rows if r.get('id')})}** distinct positions.")
    w("")
    w("*A drill = one of your own game-losing blunder positions. \"Solved\" = you found "
      "the engine's move without revealing. Target the leak: before moving, ask "
      "\"what is my opponent's most forcing reply?\"*")
    w("")

    # ---- by day ----
    byday = defaultdict(list)
    for r in rows:
        byday[r.get("date", "?")].append(r)
    w("## By day")
    w("| Date | attempts | solved | rate |")
    w("|---|---|---|---|")
    for d in sorted(byday):
        gs = byday[d]
        w(f"| {d} | {len(gs)} | {sum(x.get('outcome')=='solved' for x in gs)} | {rate(gs):.0f}% |")
    w("")

    # ---- solve rate on deciding vs the rest ----
    dec = [r for r in rows if r.get("deciding")]
    nondec = [r for r in rows if not r.get("deciding")]
    w("## Where you stand")
    if dec:
        w(f"- **Game-deciding positions (threw a won/equal game): {rate(dec):.0f}% solved** "
          f"over {len(dec)} attempts — these are the ones that actually cost rating.")
    if nondec:
        w(f"- Other blunder positions: {rate(nondec):.0f}% over {len(nondec)} attempts.")

    # ---- by phase ----
    byphase = defaultdict(list)
    for r in rows:
        byphase[r.get("phase") or "unknown"].append(r)
    if len(byphase) > 1:
        w("")
        w("**By phase:** " + " · ".join(
            f"{ph} {rate(gs):.0f}% ({len(gs)})"
            for ph, gs in sorted(byphase.items(), key=lambda kv: -len(kv[1]))))

    # ---- repeat positions: are they sticking? ----
    byid = defaultdict(list)
    for r in rows:
        if r.get("id"):
            byid[r["id"]].append(r)
    repeats = {k: v for k, v in byid.items() if len(v) >= 2}
    if repeats:
        improved = same = worse = 0
        for v in repeats.values():
            first = v[0].get("outcome") == "solved"
            last = v[-1].get("outcome") == "solved"
            if last and not first:
                improved += 1
            elif first and not last:
                worse += 1
            else:
                same += 1
        w("")
        w("## Are repeats sticking?")
        w(f"{len(repeats)} positions seen more than once. First → latest attempt: "
          f"**{improved} now solved that weren't**, {same} unchanged, {worse} regressed. "
          f"(Positions you keep missing are your live study list.)")

    w("")
    w("---")
    w("*Generated from `data/drill_log.jsonl` (raw log, local). Re-run "
      "`python -m coach.drill_report` after a session to refresh.*")
    return "\n".join(out) + "\n"


def main() -> None:
    rows = load()
    out = JOURNAL / "drill_progress.md"
    out.write_text(build(rows))
    print(f"Wrote {out} ({len(rows)} attempts).")


if __name__ == "__main__":
    main()
