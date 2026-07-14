#!/usr/bin/env bash
# One-shot install for ChessCoach: a Python virtualenv with the dependencies, plus the
# Stockfish engine. Run once:
#
#     ./setup.sh
#
# Then start the app with `./serve start`. Safe to re-run.
set -uo pipefail
cd "$(dirname "$0")"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

# --------------------------------------------------------------- 1. Python + deps
PY=python3
command -v "$PY" >/dev/null || { echo "Python 3.10+ is required but was not found."; exit 1; }
bold "Setting up a virtual environment (.venv) and installing dependencies…"
"$PY" -m venv .venv || { echo "Could not create the virtualenv."; exit 1; }
# shellcheck disable=SC1091
. .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt || { echo "pip install failed."; exit 1; }
ok "Python dependencies installed."

# --------------------------------------------------------------- 2. Stockfish
have_stockfish() { command -v stockfish >/dev/null 2>&1 || [ -x ./stockfish ]; }

runs() {  # does this binary answer UCI?
  printf 'uci\nquit\n' | "$1" 2>/dev/null | grep -q uciok
}

download_stockfish() {   # no sudo needed: grab the official prebuilt binary
  local os arch asset url
  os=$(uname -s); arch=$(uname -m)
  case "$os-$arch" in
    Linux-x86_64)  asset="stockfish-ubuntu-x86-64-sse41-popcnt.tar" ;;   # broad-compat build
    Darwin-arm64)  asset="stockfish-macos-m1-apple-silicon.tar" ;;
    Darwin-x86_64) asset="stockfish-macos-x86-64-sse41-popcnt.tar" ;;
    *) return 1 ;;   # unusual platform — fall back to a package manager
  esac
  command -v curl >/dev/null || return 1
  # The /releases/latest/download/<asset> redirect always points at the newest release
  # and needs no GitHub API (which rate-limits unauthenticated requests).
  url="https://github.com/official-stockfish/Stockfish/releases/latest/download/$asset"
  echo "  downloading Stockfish ($asset, ~110 MB)…"
  curl -sfL "$url" -o /tmp/cc_sf.tar || return 1
  rm -rf /tmp/cc_sf && mkdir -p /tmp/cc_sf
  tar -xf /tmp/cc_sf.tar -C /tmp/cc_sf || return 1
  local bin; bin=$(find /tmp/cc_sf -type f -name 'stockfish-*' | head -1)
  [ -n "$bin" ] || return 1
  cp "$bin" ./stockfish && chmod +x ./stockfish
  rm -rf /tmp/cc_sf.tar /tmp/cc_sf
  runs ./stockfish || { rm -f ./stockfish; return 1; }
}

pkg_stockfish() {        # system package manager (may prompt for sudo)
  if   command -v brew    >/dev/null; then brew install stockfish
  elif command -v apt-get >/dev/null; then sudo apt-get update -qq && sudo apt-get install -y stockfish
  elif command -v dnf     >/dev/null; then sudo dnf install -y stockfish
  elif command -v pacman  >/dev/null; then sudo pacman -S --noconfirm stockfish
  else return 1; fi
}

bold "Checking for the Stockfish engine…"
if have_stockfish; then
  ok "Stockfish already available."
elif download_stockfish; then
  ok "Stockfish downloaded to ./stockfish"
elif pkg_stockfish && have_stockfish; then
  ok "Stockfish installed via your package manager."
else
  warn "Couldn't get Stockfish automatically."
  warn "Install it from https://stockfishchess.org/download/ and put the binary on your"
  warn "PATH, drop it here as ./stockfish, or set STOCKFISH_PATH. Then re-run ./setup.sh."
fi

bold "All set. Start ChessCoach:"
echo "    ./serve start          # then open http://127.0.0.1:6464"
echo
echo "  On first run, enter your Chess.com username, then pick your AI coach in ⚙ Settings."
