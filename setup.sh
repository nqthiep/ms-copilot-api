#!/usr/bin/env bash
# Full setup + start for the Copilot Gateway (macOS / Linux).
#
# What it does, in order:
#   1. Creates a virtualenv in .venv (skipped if it already exists)
#   2. Installs the gateway and its dependencies into the venv
#   3. Installs the Playwright Chromium browser (the one-time Microsoft
#      sign-in opens a real browser window, so this is required)
#   4. Creates .env from .env.example (skipped if .env already exists)
#   5. Starts the gateway in the foreground
#
# Re-running is safe: existing .venv and .env are left untouched.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

# --- Python 3.11+ required ---------------------------------------------------
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  echo "ERROR: Python 3.11 or newer is required (found: $("$PYTHON" --version 2>&1))." >&2
  exit 1
fi

# --- Virtualenv ---------------------------------------------------------------
if [ ! -d ".venv" ]; then
  echo "==> Creating virtualenv in .venv ..."
  "$PYTHON" -m venv .venv
else
  echo "==> Using existing .venv"
fi

VENV_PY=".venv/bin/python"
VENV_PW=".venv/bin/playwright"

# --- Dependencies -------------------------------------------------------------
echo "==> Installing the gateway and dependencies ..."
"$VENV_PY" -m pip install --upgrade pip -q
"$VENV_PY" -m pip install -e ".[dev]"

# --- Playwright browser -------------------------------------------------------
echo "==> Installing Playwright Chromium ..."
"$VENV_PW" install chromium

# --- Local settings -----------------------------------------------------------
if [ ! -f ".env" ]; then
  echo "==> Creating .env from .env.example ..."
  cp .env.example .env
  echo "    Edit .env if you need to change host/port."
else
  echo "==> .env already exists, leaving it alone"
fi

# --- Start --------------------------------------------------------------------
echo "==> Starting the gateway ..."
exec "$VENV_PY" -m copilot_gateway
