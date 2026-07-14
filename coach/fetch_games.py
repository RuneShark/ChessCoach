"""Pull a player's games from the Chess.com public API.

Chess.com exposes finished games per month at:
    https://api.chess.com/pub/player/{user}/games/archives   -> list of month URLs
    <archive-url>                                             -> {"games": [...]}

No authentication is needed for public games. The API does require a
User-Agent header, so we set one.

We write two things:
  * data/games/<user>_<yyyy>_<mm>.pgn   — PGNs for the engine to analyze
  * data/games/index.jsonl              — one metadata row per game (timestamp,
    ratings, result, outcome, time_class). This powers tilt/session analysis
    WITHOUT needing the engine, so it's available immediately.

Usage:
    python -m coach.fetch_games <username>                 # ALL history, all speeds
    python -m coach.fetch_games <username> --months 3      # last 3 months
    python -m coach.fetch_games <username> --time-class rapid
"""
from __future__ import annotations

import argparse
import json
import sys

import requests

from .paths import DATA, GAMES_DIR

API = "https://api.chess.com/pub"
HEADERS = {"User-Agent": "ChessCoach/1.0 (personal improvement tool)"}
INDEX = DATA / "games" / "index.jsonl"

# non-win Chess.com result codes that are actually draws
DRAW_RESULTS = {"agreed", "repetition", "stalemate", "insufficient",
                "50move", "timevsinsufficient"}


def _get(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code == 404:
        raise SystemExit(f"Not found: {url}\nIs the username spelled correctly?")
    resp.raise_for_status()
    return resp.json()


def list_archives(username: str) -> list[str]:
    data = _get(f"{API}/player/{username.lower()}/games/archives")
    return data.get("archives", [])


def _meta(g: dict, uname: str) -> dict:
    """Extract one metadata row from a game, from the coached player's POV."""
    white, black = g["white"], g["black"]
    if white["username"].lower() == uname:
        me, opp, color = white, black, "white"
    else:
        me, opp, color = black, white, "black"
    res = me.get("result", "")
    outcome = "win" if res == "win" else "draw" if res in DRAW_RESULTS else "loss"
    return {
        "url": g.get("url", ""),
        "end_time": g.get("end_time", 0),          # unix seconds (game end)
        "time_class": g.get("time_class", ""),
        "time_control": g.get("time_control", ""),
        "rated": g.get("rated", False),
        "color": color,
        "my_rating": me.get("rating"),
        "opp_rating": opp.get("rating"),
        "opponent": opp.get("username", ""),
        "result_raw": res,
        "outcome": outcome,
        "eco": g.get("eco", "").rsplit("/", 1)[-1],
    }


def fetch(username: str, months: int | None, time_classes: set[str]) -> int:
    archives = list_archives(username)
    if not archives:
        raise SystemExit(f"No public games found for '{username}'.")
    selected = archives if months is None else archives[-months:]

    uname = username.lower()
    index_rows: dict[str, dict] = {}
    # preserve any existing index rows (keyed by url) so re-runs merge, not clobber
    if INDEX.exists():
        for line in INDEX.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue                 # skip a corrupt row rather than abort the merge
            if r.get("url"):
                index_rows[r["url"]] = r

    total_written = 0
    for arch_url in selected:
        year, month = arch_url.rstrip("/").split("/")[-2:]
        games = _get(arch_url).get("games", [])
        kept = [g for g in games
                if g.get("time_class") in time_classes and g.get("pgn")]
        if not kept:
            print(f"  {year}-{month}: 0 of {len(games)} games matched")
            continue

        out_path = GAMES_DIR / f"{username.lower()}_{year}_{month}.pgn"
        with out_path.open("w", encoding="utf-8") as fh:
            for g in kept:
                fh.write(g["pgn"].strip() + "\n\n")
                index_rows[g["url"]] = _meta(g, uname)
        total_written += len(kept)
        print(f"  {year}-{month}: wrote {len(kept)} games -> {out_path.name}")

    # persist the merged, time-sorted metadata index
    with INDEX.open("w", encoding="utf-8") as fh:
        for r in sorted(index_rows.values(), key=lambda x: x.get("end_time", 0)):
            fh.write(json.dumps(r) + "\n")

    return total_written


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Fetch Chess.com games as PGN + metadata.")
    p.add_argument("username", help="your Chess.com username")
    p.add_argument("--months", type=int, default=None,
                   help="how many recent months to pull (default: ALL history)")
    p.add_argument("--time-class", default="rapid,blitz,bullet,daily",
                   help="comma-separated speeds to include "
                        "(default: rapid,blitz,bullet,daily)")
    args = p.parse_args(argv)

    time_classes = {t.strip() for t in args.time_class.split(",") if t.strip()}
    scope = "ALL history" if args.months is None else f"{args.months} month(s)"
    print(f"Fetching {scope} for '{args.username}' "
          f"[{', '.join(sorted(time_classes))}]...")
    n = fetch(args.username, args.months, time_classes)
    print(f"\nDone. {n} games saved to {GAMES_DIR}")
    print(f"Metadata index -> {INDEX}")
    if n == 0:
        print("Tip: widen --time-class or --months.", file=sys.stderr)


if __name__ == "__main__":
    main()
