#!/usr/bin/env bash
# Double-click to start GhostVoice from source (macOS; on Linux run ./GhostVoice.command).
# The first run sets up a private Python environment in .venv (needs internet once).
cd "$(dirname "$0")" || exit 1

fail() {
  echo
  echo "$1"
  if [ -t 0 ]; then read -r -p "Press Enter to close." _; fi
  exit 1
}

PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || fail "Python 3.9+ is needed: https://www.python.org/downloads/"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || fail "Python 3.9 or newer is needed: https://www.python.org/downloads/"
"$PY" -c 'import tkinter' 2>/dev/null || fail "Your Python has no Tk. macOS: use the python.org installer. Linux: sudo apt install python3-tk"

if [ ! -f .venv/.installed ]; then
  echo "First run: setting up GhostVoice (one time, needs internet)..."
  rm -rf .venv
  "$PY" -m venv .venv || fail "Could not create a Python environment. Linux: sudo apt install python3-venv"
  .venv/bin/python -m pip install --quiet --upgrade pip || fail "Setup failed (pip)."
  .venv/bin/python -m pip install --quiet -e . || fail "Setup failed: see the messages above."
  touch .venv/.installed
fi

if [ "$1" = "--selftest" ]; then
  exec .venv/bin/python -m ghostvoice gui --selftest
fi
exec .venv/bin/python -m ghostvoice gui
