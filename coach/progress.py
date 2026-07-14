"""Before/after your training started: is the work actually moving the numbers?

Splits the whole game history at a date and reports hard numbers on each side: volume,
win%, rating, accuracy, blunders, and game-deciding blunders/game. Then breaks the
*after* period into weekly buckets so the trend becomes visible as it accumulates.

The split date is --since, or the "since" date saved when you set up the app, or (if
neither) the midpoint of your history. It's honest about sample size — a small "after"
window also prints a 95% confidence interval on the win rate.

Outputs journal/progress.md. The web "Sync & analyze" re-runs it automatically.

Usage:
    python -m coach.progress [--since YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timedelta

from .paths import DATA, JOURNAL, ROOT

INDEX = DATA / "games" / "index.jsonl"
ANALYSIS = DATA / "analysis"
POOL = "rapid"                    # the main rated pool we track


def _resolve_since(rows: list[dict], arg: str | None) -> str:
    if arg:
        return arg
    cfg = ROOT / "config.json"
    if cfg.exists():
        try:
            s = json.loads(cfg.read_text()).get("since")
            if s:
                return s
        except (json.JSONDecodeError, OSError):
            pass
    mid = sorted(r["end_time"] for r in rows)[len(rows) // 2]      # midpoint of history
    return datetime.fromtimestamp(mid).strftime("%Y-%m-%d")


def _load_index() -> list[dict]:
    rows = []
    if not INDEX.exists():
        return rows
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("end_time"):
            rows.append(r)
    return rows


def _load_analysis() -> dict:
    """url -> per-game {accuracy, acpl, n_bl, deciding, outcome, won_eg}."""
    by_url = {}
    for f in glob.glob(str(ANALYSIS / "*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        if not d.get("url"):
            continue
        ms = d.get("mistakes") or []
        n_bl = sum(1 for m in ms if m.get("severity") == "blunder")
        # deciding = threw a game you weren't losing (>=50% win% -> <50%)
        deciding = sum(1 for m in ms if m.get("severity") == "blunder"
                       and (m.get("win_before") or 0) >= 50 and (m.get("win_after") or 100) < 50)
        # reached a clearly-winning endgame (>=85%) at some point
        won_eg = any(m.get("phase") == "endgame" and (m.get("win_before") or 0) >= 85 for m in ms)
        by_url[d["url"]] = {"accuracy": d.get("accuracy"), "acpl": d.get("acpl"),
                            "n_bl": n_bl, "deciding": deciding,
                            "outcome": d.get("outcome"), "won_eg": won_eg}
    return by_url


def _stats(rows: list[dict], ana: dict) -> dict | None:
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r["end_time"])
    n = len(rows)
    rec = Counter(r.get("outcome") for r in rows)
    days = len({datetime.fromtimestamp(r["end_time"]).date() for r in rows})
    rated = [r for r in rows if r.get("my_rating")]
    accs, acpls, bls, decs = [], [], [], []
    eg_reached = eg_won = 0
    for r in rows:
        a = ana.get(r.get("url"))
        if a and a.get("accuracy") is not None:
            accs.append(a["accuracy"]); bls.append(a["n_bl"]); decs.append(a["deciding"])
            if a.get("acpl") is not None:
                acpls.append(a["acpl"])
            if a["won_eg"]:
                eg_reached += 1
                eg_won += (a["outcome"] == "win")
    wins = rec.get("win", 0)
    winp = 100 * wins / n
    ci = 100 * 1.96 * math.sqrt((winp / 100) * (1 - winp / 100) / n)   # 95% on win rate
    return {
        "n": n, "days": days, "gpd": n / days if days else 0,
        "W": wins, "L": rec.get("loss", 0), "D": rec.get("draw", 0),
        "win": winp, "win_ci": ci,
        "r_first": rated[0]["my_rating"] if rated else None,
        "r_last": rated[-1]["my_rating"] if rated else None,
        "r_peak": max((x["my_rating"] for x in rated), default=None),
        "acc": statistics.mean(accs) if accs else None,
        "acpl": statistics.mean(acpls) if acpls else None,
        "bl": statistics.mean(bls) if bls else None,
        "dec": statistics.mean(decs) if decs else None,
        "eg_reached": eg_reached,
        "eg_notwon": 100 * (eg_reached - eg_won) / eg_reached if eg_reached else None,
        "span": (datetime.fromtimestamp(rows[0]["end_time"]).strftime("%Y-%m-%d"),
                 datetime.fromtimestamp(rows[-1]["end_time"]).strftime("%Y-%m-%d")),
    }


def _cell(s: dict, key: str) -> str:
    v = s.get(key)
    if v is None:
        return "—"
    if key == "gpd":
        return f"{v:.1f}/day"
    if key == "win":
        return f"{v:.1f}%"
    if key in ("acc", "eg_notwon"):
        return f"{v:.1f}%" if key == "acc" else f"{v:.0f}%"
    if key in ("bl", "dec"):
        return f"{v:.2f}"
    if key == "acpl":
        return f"{v:.0f}"
    return str(v)


def build(rows: list[dict], ana: dict, since: str) -> str:
    cut = datetime.strptime(since, "%Y-%m-%d").timestamp()
    rap = [r for r in rows if r.get("time_class") == POOL]
    before = _stats([r for r in rap if r["end_time"] < cut], ana)
    after = _stats([r for r in rap if r["end_time"] >= cut], ana)
    all_before = _stats([r for r in rows if r["end_time"] < cut], ana)
    all_after = _stats([r for r in rows if r["end_time"] >= cut], ana)

    L: list[str] = []
    w = L.append
    w(f"# Before / After — {datetime.now():%Y-%m-%d}")
    w("")
    w(f"*Split at **{since}** (your training start). "
      f"Auto-generated by `python -m coach.progress`; rebuilt on every sync.*")
    w("")
    if not after or not before:
        w("Not enough games on one side of the split yet.")
        return "\n".join(L) + "\n"

    w(f"## Rapid — the tracked pool")
    w("")
    w("| Metric | Before | After |")
    w("|---|---|---|")
    rowspec = [
        ("Span", lambda s: f"{s['span'][0]} → {s['span'][1]}"),
        ("Games", lambda s: f"{s['n']} over {s['days']}d"),
        ("Volume", lambda s: _cell(s, "gpd")),
        ("Record", lambda s: f"{s['W']}-{s['L']}-{s['D']}"),
        ("Win %", lambda s: f"{s['win']:.1f}% (±{s['win_ci']:.0f})"),
        ("Rating", lambda s: (f"{s['r_first']}→{s['r_last']} ({s['r_last']-s['r_first']:+d}), "
                              f"peak {s['r_peak']}") if s['r_first'] else "—"),
        ("Accuracy", lambda s: _cell(s, "acc")),
        ("ACPL", lambda s: _cell(s, "acpl")),
        ("Blunders/game", lambda s: _cell(s, "bl")),
        ("**Deciding blunders/game**", lambda s: _cell(s, "dec")),
        ("Winning endgames not won", lambda s: (f"{s['eg_notwon']:.0f}% (of {s['eg_reached']})"
                                                if s['eg_notwon'] is not None else "—")),
    ]
    for label, fn in rowspec:
        w(f"| {label} | {fn(before)} | {fn(after)} |")
    w("")

    # ---- honest read ----
    dv = before["gpd"] - after["gpd"]
    dwin = after["win"] - before["win"]
    ddec = after["dec"] - before["dec"] if (after["dec"] is not None and before["dec"] is not None) else None
    w("### Read (be honest about the sample)")
    w(f"- **Volume:** {before['gpd']:.1f} → {after['gpd']:.1f} games/day "
      f"({'down' if dv > 0 else 'up'} {abs(dv):.1f}) — the one thing the plan directly changes.")
    w(f"- **Win%:** {dwin:+.1f} pts. The 95% CI on the after rate is "
      f"±{after['win_ci']:.0f} pts ({after['win']-after['win_ci']:.0f}–{after['win']+after['win_ci']:.0f}%), "
      f"so with {after['n']} games this is {'suggestive, not conclusive' if after['win_ci'] > 8 else 'fairly solid'}.")
    if ddec is not None:
        w(f"- **Deciding blunders/game:** {before['dec']:.2f} → {after['dec']:.2f} "
          f"({ddec:+.2f}) — the tracked leak; the metric that most directly reflects the goal.")
    w(f"- Accuracy {before['acc']:.1f}% → {after['acc']:.1f}% (mostly flat, as expected — "
      f"the leak was never average move quality).")
    if after["n"] < 150:
        w(f"- ⚠ **{after['n']} rapid games is a small sample** — give it ~4 weeks / 150+ games "
          f"before trusting the win%/rating deltas. Watch the weekly table below.")
    w("")

    # ---- weekly buckets of the after period (watch the trend build) ----
    w("## After, week by week")
    w("*7-day buckets from the split date, rapid. This is the trend to watch as it grows.*")
    w("")
    w("| Week | Games | Win % | Acc | Bl/g | Deciding/g | Rating end |")
    w("|---|---|---|---|---|---|---|")
    after_rows = sorted([r for r in rap if r["end_time"] >= cut], key=lambda r: r["end_time"])
    start = datetime.strptime(since, "%Y-%m-%d")
    wk = 0
    while True:
        lo = (start + timedelta(days=7 * wk)).timestamp()
        hi = (start + timedelta(days=7 * (wk + 1))).timestamp()
        bucket = [r for r in after_rows if lo <= r["end_time"] < hi]
        if not bucket and lo > after_rows[-1]["end_time"]:
            break
        if bucket:
            s = _stats(bucket, ana)
            lbl = f"{(start + timedelta(days=7*wk)):%m-%d}"
            rend = s["r_last"] if s["r_last"] else "—"
            w(f"| W{wk+1} ({lbl}) | {s['n']} | {s['win']:.0f}% | "
              f"{_cell(s,'acc')} | {_cell(s,'bl')} | {_cell(s,'dec')} | {rend} |")
        wk += 1
        if wk > 520:      # safety
            break
    w("")
    w("---")
    w(f"*Overall (all pools): before {all_before['n']} games {all_before['win']:.0f}% win, "
      f"after {all_after['n']} games {all_after['win']:.0f}% win. "
      f"Regenerate with `python -m coach.progress`.*")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Before/after progress report.")
    p.add_argument("--since", default=None,
                   help="split date YYYY-MM-DD (default: config's 'since', else history midpoint)")
    args = p.parse_args(argv)

    rows = _load_index()
    if not rows:
        raise SystemExit("No metadata index. Run coach.fetch_games first.")
    ana = _load_analysis()
    since = _resolve_since(rows, args.since)
    out = JOURNAL / "progress.md"
    out.write_text(build(rows, ana, since))
    print(f"Wrote {out} ({len(rows)} games, split at {since}).")


if __name__ == "__main__":
    main()
