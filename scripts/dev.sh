#!/usr/bin/env bash
# Run the CLI from source (editable install) with any arguments.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "✗ Run scripts/install.sh first." >&2; exit 1; }
exec .venv/bin/python -m argonauta "$@"
