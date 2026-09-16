#!/usr/bin/env sh
set -eu

# Usage: ./scripts/run.sh                         # run all scenarios
# Usage: ./scripts/run.sh --scenario "scenario"  # run one scenario
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
exec python "$SCRIPT_DIR/run_e2e.py" "$@"
