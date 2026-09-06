#!/bin/sh
# FUTURE APPROVED WINDOW ONLY; no old production sender is restarted.
set -eu
test "${1:-}" = --approved-window
test -n "${2:-}"
test "$(id -u)" = 0
test "$2" = "$(cat /etc/xianyu-egress/window-id)"
exec /usr/bin/python3 -I /etc/xianyu-egress/rollback-window.py \
    --manifest /etc/xianyu-egress/rollback-manifest.json
