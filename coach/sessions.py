"""Tilt / session analysis from game metadata (no engine needed).

A "session" = games played back-to-back with < SESSION_GAP between them. Tilt
shows up as: results getting worse deeper into a session, worse right after a
loss, worse late at night, and rating drawdowns concentrated in long sessions.

All timestamps use the machine's local timezone (Chess.com stores UTC end_time).
If that's wrong for you, pass --tz-offset HOURS.

Usage:
    python -m coach.sessions [--gap-min 45] [--tz-offset 0]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from .paths import DATA, JOURNAL

INDEX = DATA / "games" / "index.jsonl"


def load_rows(tz_offset: float | None = None):
    if not INDEX.exists():
        raise SystemExit("No metadata. Run coach.fetch_games first.")
    # tz_offset=None → the machine's local timezone (fromtimestamp with tz=None);
    # a number pins a fixed UTC offset instead.
    tz = None if tz_offset is None else timezone(timedelta(hours=tz_offset))
    rows = []
    for line in INDEX.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("end_time"):
            continue
        r["dt"] = datetime.fromtimestamp(r["end_time"], tz)
        rows.append(r)
    rows.sort(key=lambda r: r["end_time"])
    return rows


def sessionize(rows, gap_seconds: int):
    sessions, cur = [], []
    for r in rows:
        if cur and r["end_time"] - cur[-1]["end_time"] > gap_seconds:
            sessions.append(cur)
            cur = []
        cur.append(r)
    if cur:
        sessions.append(cur)
    return sessions


def longest_loss_streak(games) -> int:
    best = cur = 0
    for g in games:
        cur = cur + 1 if g["outcome"] == "loss" else 0
        best = max(best, cur)
    return best


def winrate(games) -> float:
    n = len(games)
    return 100 * sum(g["outcome"] == "win" for g in games) / n if n else 0.0


def drawdowns(rows, time_class: str):
    """Largest peak-to-trough rating drops within one time-class pool."""
    series = [(r["dt"], r["my_rating"]) for r in rows
              if r["time_class"] == time_class and r["my_rating"]]
    if not series:
        return None, []
    peak_val, peak_dt = series[0][1], series[0][0]
    dds = []
    trough_val, trough_dt = peak_val, peak_dt
    for dt, v in series:
        if v > peak_val:
            if peak_val - trough_val > 0:
                dds.append((peak_val, trough_val, peak_dt, trough_dt))
            peak_val, peak_dt = v, dt
            trough_val, trough_dt = v, dt
        elif v < trough_val:
            trough_val, trough_dt = v, dt
    if peak_val - trough_val > 0:
        dds.append((peak_val, trough_val, peak_dt, trough_dt))
    peak_all = max(v for _, v in series)
    dds.sort(key=lambda d: d[0] - d[1], reverse=True)
    return peak_all, dds[:4]


def build(rows, sessions) -> str:
    L = []
    w = L.append
    n = len(rows)
    w(f"# Tilt & Session Report — {datetime.now():%Y-%m-%d}")
    w("")
    w(f"**{n} games**, {rows[0]['dt']:%Y-%m-%d} → {rows[-1]['dt']:%Y-%m-%d}, "
      f"grouped into **{len(sessions)} sessions** "
      f"(a session = games <45 min apart).")
    w("")

    # ---- rating drawdowns per pool ----
    w("## The drops from ~1800")
    for tc in ("rapid", "blitz"):
        peak, dds = drawdowns(rows, tc)
        if not peak:
            continue
        w(f"\n**{tc.title()}** — peak {peak}. Biggest drawdowns:")
        for hi, lo, hidt, lodt in dds:
            span = lodt - hidt
            days = span.days + span.seconds / 86400
            w(f"- **{hi} → {lo}  (−{hi-lo})** between {hidt:%b %d} and "
              f"{lodt:%b %d} ({days:.1f} days)")
    w("")

    # ---- volume / discipline: the single biggest lever ----
    from collections import defaultdict as _dd
    byday = _dd(list)
    for r in rows:
        byday[(r["dt"].date(), r["time_class"])].append(r)
    light = [g for (d, tc), gs in byday.items() if len(gs) <= 6 for g in gs]
    marath = [g for (d, tc), gs in byday.items() if len(gs) >= 15 for g in gs]
    # counterfactual: stop after 3 losses in a day (per pool)
    saved = 0
    for (d, tc), gs in byday.items():
        gs = sorted(gs, key=lambda r: r["end_time"])
        losses = 0
        cut = None
        for i, g in enumerate(gs):
            if g["outcome"] == "loss":
                losses += 1
                if losses == 3:
                    cut = i
                    break
        if cut is not None and cut < len(gs) - 1 and gs[-1]["my_rating"] and gs[cut]["my_rating"]:
            after = gs[-1]["my_rating"] - gs[cut]["my_rating"]
            if after < 0:
                saved += -after
    w("## The biggest lever: volume, not skill")
    if light and marath:
        w(f"- **Light days (≤6 games): {winrate(light):.0f}% win** ({len(light)} games)")
        w(f"- **Marathon days (≥15 games): {winrate(marath):.0f}% win** ({len(marath)} games)")
        w(f"- When you're fresh you play well above your rating; the grind is what "
          f"drags it down.")
    w(f"- **Counterfactual:** stopping after **3 losses in a day** would have averted "
      f"roughly **{saved} points** of rating bleed (illustrative — deltas approximated).")
    w("")

    # ---- the tilt signature: results by depth into a session ----
    w("## Tilt signature: do you get worse the longer you sit?")
    w("*Win% by the game's position within its session.*")
    w("")
    by_pos = defaultdict(list)
    for s in sessions:
        for i, g in enumerate(s, 1):
            by_pos[min(i, 7)].append(g)
    w("| Game # in session | games | win% |")
    w("|---|---|---|")
    for pos in sorted(by_pos):
        gs = by_pos[pos]
        label = f"{pos}" if pos < 7 else "7+"
        w(f"| {label} | {len(gs)} | {winrate(gs):.0f}% |")
    early = [g for s in sessions for g in s[:3]]
    late = [g for s in sessions for g in s[3:]]
    if late:
        w("")
        w(f"**First 3 games of a session: {winrate(early):.0f}% win "
          f"({len(early)} games) → 4th game onward: {winrate(late):.0f}% win "
          f"({len(late)} games).**")
    w("")

    # ---- revenge effect: result after a loss ----
    after_loss, after_win = [], []
    for s in sessions:
        for prev, nxt in zip(s, s[1:]):
            (after_loss if prev["outcome"] == "loss" else after_win).append(nxt)
    w("## The revenge game (classic tilt)")
    w(f"- Right **after a loss** (same session): **{winrate(after_loss):.0f}% win** "
      f"over {len(after_loss)} games")
    w(f"- After a win: {winrate(after_win):.0f}% win over {len(after_win)} games")
    w("")

    # ---- time of day ----
    w("## Time of day")
    w("*Win% by local hour the game ended.*")
    w("")
    buckets = {"06–12 morning": [], "12–18 afternoon": [],
               "18–22 evening": [], "22–02 late night": [], "02–06 small hours": []}
    for r in rows:
        h = r["dt"].hour
        key = ("06–12 morning" if 6 <= h < 12 else "12–18 afternoon" if 12 <= h < 18
               else "18–22 evening" if 18 <= h < 22
               else "22–02 late night" if (h >= 22 or h < 2) else "02–06 small hours")
        buckets[key].append(r)
    w("| Time | games | win% |")
    w("|---|---|---|")
    for k, gs in buckets.items():
        if gs:
            w(f"| {k} | {len(gs)} | {winrate(gs):.0f}% |")
    w("")

    # ---- worst sessions (the ragequeue marathons) ----
    w("## Worst marathons (long sessions that bled rating)")
    scored = []
    for s in sessions:
        if len(s) < 4:
            continue
        # rating delta within session (same-pool games only, use first/last present)
        rated = [g for g in s if g["my_rating"]]
        delta = (rated[-1]["my_rating"] - rated[0]["my_rating"]) if len(rated) >= 2 else 0
        scored.append((delta, s))
    scored.sort(key=lambda t: t[0])
    for delta, s in scored[:6]:
        streak = longest_loss_streak(s)
        rec = Counter(g["outcome"] for g in s)
        w(f"- **{s[0]['dt']:%Y-%m-%d %H:%M}** — {len(s)} games in a row "
          f"({rec.get('win',0)}W/{rec.get('loss',0)}L/{rec.get('draw',0)}D), "
          f"rating {delta:+d}, longest losing streak {streak}")
    w("")
    w("---")
    w("*Metadata-only (no engine). Once the full Stockfish pass finishes we can also "
      "show whether your **accuracy** drops in these marathons — the smoking gun.*")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Tilt/session analysis from metadata.")
    p.add_argument("--gap-min", type=int, default=45,
                   help="minutes between games that starts a new session (default 45)")
    p.add_argument("--tz-offset", type=float, default=None,
                   help="hours offset from UTC (default: the machine's local timezone)")
    args = p.parse_args(argv)

    rows = load_rows(args.tz_offset)
    sessions = sessionize(rows, args.gap_min * 60)
    out = JOURNAL / "tilt.md"
    out.write_text(build(rows, sessions))
    print(f"Wrote {out} ({len(rows)} games, {len(sessions)} sessions).")


if __name__ == "__main__":
    main()
