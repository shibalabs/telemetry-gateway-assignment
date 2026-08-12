#!/usr/bin/env sh
set -eu
python -m compileall -q telemetry_gateway simulator.py tests
python -m pytest

# The dashboard tests run on Node's built-in test runner, with no dependencies
# to install. They are skipped when Node is unavailable so the Python checks
# remain the baseline.
if command -v node >/dev/null 2>&1; then
  node --test tests/dashboard.test.mjs
else
  echo "Skipping dashboard tests: node was not found on PATH."
fi
