#!/usr/bin/env bash
set -euo pipefail
export PLAYWRIGHT_BROWSERS_PATH="${PWD}/.playwright-browsers"
exec python -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1