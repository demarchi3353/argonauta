#!/usr/bin/env bash
# Production-style local run: install the built wheel (not the source tree)
# into a separate .venv-prod and run the CLI from there.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3.12}"
WHEEL="$(ls -1t dist/argonauta-*.whl 2>/dev/null | head -n1 || true)"
if [ -z "$WHEEL" ]; then
  echo "· No wheel in dist/, building one first." >&2
  scripts/build.sh >&2
  WHEEL="$(ls -1t dist/argonauta-*.whl | head -n1)"
fi
if [ ! -d .venv-prod ]; then
  "$PY" -m venv .venv-prod
fi
.venv-prod/bin/pip install --quiet --upgrade pip
.venv-prod/bin/pip install --quiet --force-reinstall --no-deps "$WHEEL"
.venv-prod/bin/pip install --quiet "argonauta[anthropic]@file://$PWD/$WHEEL"
exec .venv-prod/bin/argonauta "$@"
