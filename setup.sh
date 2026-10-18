#!/usr/bin/env bash
# One-command setup and start for the Copilot Gateway.
# Works on macOS, Linux, WSL and Git Bash on Windows. Windows PowerShell users
# run setup.ps1 instead.
#
#   bash setup.sh                 set up, then start the gateway in the foreground
#   bash setup.sh --no-start      set up only
#   bash setup.sh --no-browser    skip installing Playwright Chromium
#
# Re-running is safe: a working .venv is reused and an existing .env is left alone.
# Uses only bash features that bash 3.2 (the macOS default) supports.
set -eu

cd "$(dirname "$0")"

START=1
BROWSER=1
for arg in "$@"; do
  case "$arg" in
    --no-start) START=0 ;;
    --no-browser) BROWSER=0 ;;
    -h|--help)
      sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- Platform (used only for messages) ----------------------------------------
case "$(uname -s)" in
  Darwin) PLATFORM="macOS" ;;
  Linux)
    if grep -qi microsoft /proc/version 2>/dev/null; then PLATFORM="WSL"; else PLATFORM="Linux"; fi
    ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM="Git Bash" ;;
  *) PLATFORM="unknown" ;;
esac
say "Platform: $PLATFORM"

# --- Python 3.11+ -------------------------------------------------------------
# PY holds the command that works, for example (python3) or (py -3).
PY=()
try_python() {
  if command -v "$1" >/dev/null 2>&1 && \
     "$@" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PY=("$@")
    return 0
  fi
  return 1
}

if [ -n "${PYTHON:-}" ]; then
  # PYTHON is an explicit choice, so there is no fallback. It may hold a command plus arguments.
  # shellcheck disable=SC2086
  try_python $PYTHON || die "PYTHON=$PYTHON is not Python 3.11 or newer."
else
  try_python python3 || try_python python || try_python py -3 || {
    case "$PLATFORM" in
      macOS) hint="brew install python@3.12" ;;
      WSL|Linux) hint="sudo apt install python3 python3-venv   (or your distro's equivalent)" ;;
      "Git Bash") hint="winget install Python.Python.3.12, then open a new Git Bash window" ;;
      *) hint="install Python 3.11 or newer from https://www.python.org/downloads/" ;;
    esac
    die "Python 3.11 or newer was not found. Install it with: $hint"
  }
fi
say "Using Python: $("${PY[@]}" -c 'import sys; print(sys.version.split()[0])')"

# --- Virtualenv ---------------------------------------------------------------
# macOS, Linux and WSL put the interpreter in bin/. Windows (Git Bash) uses Scripts/.
VENV_PY=""
find_venv_python() {
  for candidate in .venv/bin/python .venv/Scripts/python.exe; do
    if [ -x "$candidate" ] && "$candidate" -c 'import sys' >/dev/null 2>&1; then
      VENV_PY="$candidate"
      return 0
    fi
  done
  return 1
}

if find_venv_python; then
  say "Using existing .venv"
else
  if [ -e .venv ]; then
    say "Existing .venv does not work on this system (created on another OS?). Recreating it."
    rm -rf .venv
  fi
  say "Creating virtualenv in .venv"
  "${PY[@]}" -m venv .venv \
    || die "Could not create .venv. On Debian or Ubuntu, install it with: sudo apt install python3-venv"
  find_venv_python || die "The new .venv has no working Python. Delete .venv and run setup again."
fi

# --- Dependencies -------------------------------------------------------------
say "Installing the gateway and its dependencies"
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -e ".[dev]"

# --- Playwright Chromium ------------------------------------------------------
# Microsoft sign-in opens a real browser window, so Chromium is needed.
if [ "$BROWSER" = 1 ]; then
  say "Installing Playwright Chromium"
  if ! "$VENV_PY" -m playwright install chromium; then
    warn "Playwright could not install Chromium, and sign-in needs it."
    warn "Retry: $VENV_PY -m playwright install chromium"
    warn "On Linux you may also need: $VENV_PY -m playwright install-deps chromium (needs sudo)"
    warn "Or set CHROMIUM_PATH in .env to an existing Chrome or Chromium."
  fi
else
  say "Skipping Playwright Chromium (--no-browser)"
fi

# --- Local settings -----------------------------------------------------------
if [ -f .env ]; then
  say ".env already exists, leaving it alone"
else
  [ -f .env.example ] || die ".env.example is missing from this checkout"
  cp .env.example .env
  say "Created .env from .env.example"
fi

# --- Start --------------------------------------------------------------------
if [ "$START" = 1 ]; then
  say "Starting the gateway (Ctrl+C to stop)"
  exec "$VENV_PY" -m copilot_gateway
fi
say "Setup complete. Start the gateway with: $VENV_PY -m copilot_gateway"
