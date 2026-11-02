#!/bin/zsh
set -e
COURSEMATCH_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$COURSEMATCH_ROOT"
exec "$COURSEMATCH_ROOT/.venv/bin/python" -m scripts.equivalency serve --port 8799
