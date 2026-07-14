#!/usr/bin/env bash
# Remove everything ChessCoach created in this folder — the virtualenv, the downloaded
# Stockfish engine, and your games / config / generated reports — leaving just the source.
# Delete the folder afterwards to remove ChessCoach completely. Your profile.md is kept.
set -uo pipefail
cd "$(dirname "$0")"

read -r -p "Remove ChessCoach's data, config, virtualenv and downloaded engine from this folder? [y/N] " ans
case "${ans:-}" in [yY]*) ;; *) echo "Cancelled."; exit 0 ;; esac

[ -x ./serve ] && ./serve stop >/dev/null 2>&1     # stop the server if it's running

rm -rf .venv stockfish
rm -f  config.json .env .web.log .web.pid
rm -f  data/games/*.pgn data/games/*.jsonl \
       data/analysis/*.json data/*.jsonl data/*.json
for f in report tilt accuracy endgame_conversion progress drill_progress weaknesses plan; do
  rm -f "journal/$f.md"                             # generated reports (keeps profile.md)
done
find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null

echo
echo "Done — ChessCoach's generated files are gone; only the source remains."
echo "To remove it completely, delete this folder:"
echo "    $(pwd)"
