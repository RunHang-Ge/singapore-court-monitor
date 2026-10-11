#!/usr/bin/env bash
set -euo pipefail
pip install -r requirements.txt
# Browser binaries are included in the built application, not the persistent disk.
export PLAYWRIGHT_BROWSERS_PATH="${PWD}/.playwright-browsers"
python -m playwright install chromium
# Fail early with a readable error if the native host lacks browser system libraries.
python browser_diagnostic.py --check