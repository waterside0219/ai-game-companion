#!/usr/bin/env bash
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
umask 077
cd "$ROOT"
# /dev/shm is emptied at reboot; recreate before starting the unchanged judge.
mkdir -p "$ROOT/logs" "$ROOT/.private" /dev/shm/desk
if [[ -f "$ROOT/.private/providers.env" ]]; then
    set -a
    source "$ROOT/.private/providers.env"
    set +a
fi
export DESK_PROVIDERS="${DESK_PROVIDERS:-mock}"
exec /usr/bin/flock -n "$ROOT/.private/desk.lock" /usr/bin/python3 -B -S -m judge.judge --config "$ROOT/.private/config.toml" >> "$ROOT/logs/desk.log" 2>&1
