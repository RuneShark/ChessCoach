"""Shared filesystem locations for the pipeline."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# CHESSCOACH_DATA overrides the data root (test isolation / alternate corpora); it must
# be an env var, not a runtime patch, so `spawn`ed analysis workers re-read it on import.
DATA = (Path(os.environ["CHESSCOACH_DATA"]).resolve()
        if os.environ.get("CHESSCOACH_DATA") else ROOT / "data")
GAMES_DIR = DATA / "games"
ANALYSIS_DIR = DATA / "analysis"
JOURNAL = ROOT / "journal"

for _d in (GAMES_DIR, ANALYSIS_DIR, JOURNAL):
    _d.mkdir(parents=True, exist_ok=True)
