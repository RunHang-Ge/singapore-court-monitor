#!/usr/bin/env bash
set -euo pipefail
pip install -r requirements.txt
# Remote mode runs Chrome outside Render; no local binary or launch is needed.
if [[ "${BROWSER_PROVIDER:-auto}" == "browserless" ]] || { [[ "${BROWSER_PROVIDER:-auto}" == "auto" ]] && [[ -n "${BROWSERLESS_TOKEN:-}" ]]; }; then
  echo "Remote browser mode: skipping local Chromium installation and startup."
  exit 0
fi
# Browser binaries are included in the built application, not the persistent disk.
export PLAYWRIGHT_BROWSERS_PATH="${PWD}/.playwright-browsers"
python -m playwright install chromium
# Fail early with a readable error if the native host lacks browser system libraries.
python browser_diagnostic.py --check