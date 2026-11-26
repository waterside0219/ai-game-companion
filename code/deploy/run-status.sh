#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
umask 077
cd "$ROOT"
mkdir -p "$ROOT/logs" "$ROOT/.private"
exec /usr/bin/flock -n "$ROOT/.private/status.lock" /usr/bin/python3 -B -S "$ROOT/entry/status_reporter.py" >> "$ROOT/logs/status.log" 2>&1
