#!/bin/sh
# FUTURE APPROVED WINDOW ONLY; no old production sender is restarted.
set -eu
test "${1:-}" = --approved-window
test -n "${2:-}"
test "$(id -u)" = 0
systemctl stop xianyu-egress.service xianyu-isolated-prepare.service xianyu-api.socket xianyu-api.service
test -z "$(ip netns pids xianyu-business)"
nft flush set inet xianyu_guard lease
# Detach first. Delete ONLY this deployment's own objects.
ip link del xmg-host
ip -4 rule del priority 1000 to 10.203.0.0/30 lookup main
ip netns del xianyu-business
nft delete table inet xianyu_guard
nft delete table ip xianyu_nat
printf '%s\n' 'Dedicated network removed. Login/business remain disabled.'
