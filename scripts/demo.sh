#!/usr/bin/env bash
# Run the whole core loop on a throwaway copy of examples/sample_project using
# the TEST-ONLY scripted provider. No network access, nothing touches this repo.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -x "$ROOT/.venv/bin/python" ] || { echo "✗ Run scripts/install.sh first." >&2; exit 1; }

DEMO="$(mktemp -d "${TMPDIR:-/tmp}/argonauta-demo.XXXXXX")"
cp -R "$ROOT/examples/sample_project/." "$DEMO/"
cd "$DEMO"
git init -q
git -c user.name=demo -c user.email=demo@example.invalid add -A
git -c user.name=demo -c user.email=demo@example.invalid commit -q -m "sample project"

export ARGONAUTA_PROVIDER=scripted
export ARGONAUTA_SCRIPTED_DIFF="$ROOT/examples/sample_reply.md"
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN
A="$ROOT/.venv/bin/python -m argonauta"

echo "── demo repo: $DEMO"
$A init --test-command "$ROOT/.venv/bin/python -m pytest -q"
$A index
$A status
$A propose "Make power() handle negative exponents" --dry-run
$A propose "Make power() handle negative exponents"
$A apply 1 --yes
$A history --events
$A export "$DEMO/argonauta-export.json"
echo "── done. Inspect: cd $DEMO && git log --oneline"
