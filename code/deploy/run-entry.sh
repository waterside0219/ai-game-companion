#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
umask 077
cd "$ROOT"
mkdir -p "$ROOT/logs" "$ROOT/.private"
exec /usr/bin/flock -n "$ROOT/.private/entry.lock" /usr/bin/python3 -B -S "$ROOT/entry/desk_entry.py" >> "$ROOT/logs/entry.log" 2>&1
