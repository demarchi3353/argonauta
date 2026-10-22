#!/usr/bin/env bash
# Create .venv with Python 3.12 and install Argonauta in editable mode with all extras.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3.12}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "✗ $PY not found. Install Python 3.12 or set PYTHON=/path/to/python3.12" >&2
  exit 1
fi
"$PY" -c 'import sys; assert sys.version_info[:2] >= (3, 12), "Python 3.12+ required"'

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e ".[anthropic,dev]"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "· Created .env from .env.example (edit it to add an API key; optional)."
fi
echo "✓ Installed. Try: scripts/dev.sh --help"
