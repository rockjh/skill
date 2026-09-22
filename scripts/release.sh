#!/usr/bin/env sh
set -eu
exec "${PYTHON:-python3}" scripts/release.py "$@"
