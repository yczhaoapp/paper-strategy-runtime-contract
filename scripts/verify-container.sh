#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" == "0" ]]; then
  echo "Strict verification refuses root execution." >&2
  exit 1
fi
# Executes the interpreter installed at build time. No uv run, pip, sync or build here.
# scripts/verify.py performs ruff check, mypy src tests, schema export, package export,
# generated content comparison (portable equivalent of diff -ru), pytest, demo all,
# demo failures, demo adapters, demo compatibility, paper suite and psrc verify.
exec /app/.venv/bin/python /app/scripts/verify.py --offline --require-strict --output "${1:-/psrc/reports}"
