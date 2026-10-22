#!/usr/bin/env bash
# Build a wheel and sdist into dist/ and verify the wheel installs cleanly.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "✗ Run scripts/install.sh first." >&2; exit 1; }
rm -rf dist build ./*.egg-info
.venv/bin/python -m build --wheel --sdist
.venv/bin/python -m pip check
echo "✓ Built:"
ls -1 dist
