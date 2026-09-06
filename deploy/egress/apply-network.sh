#!/bin/sh
# FUTURE APPROVED WINDOW ONLY; run a reviewed root-owned copy, not this Git tree.
set -eu
test "${1:-}" = --approved-window
test -n "${2:-}"
test "$(id -u)" = 0
BUNDLE=/etc/xianyu-egress
test "$(readlink -f "$0")" = "$BUNDLE/apply-network.sh"
for file in "$BUNDLE" "$BUNDLE/apply-network.sh" "$BUNDLE/guard.nft"; do
    test "$(stat -c %u "$file")" = 0
    test -z "$(find "$file" -maxdepth 0 -perm /022 -print)"
done
# Never enable host forwarding or change Exit Node implicitly.
test "$(cat /proc/sys/net/ipv4/ip_forward)" = 1
# The approved wrapper MUST load scope protection before enabling forwarding.
nft list table ip xianyu_forward_scope >/dev/null
ip link show tailscale0 >/dev/null
test ! -e /run/netns/xianyu-business
if ip link show xmg-host >/dev/null 2>&1; then exit 1; fi
nft list tables >/dev/null
if nft list table inet xianyu_guard >/dev/null 2>&1; then exit 1; fi
if nft list table ip xianyu_nat >/dev/null 2>&1; then exit 1; fi
if ip -4 rule show | grep -q '^1000:'; then exit 1; fi
# Operator must ALSO check broader prefixes in every policy-routing table.
test -z "$(ip -4 route show table all | grep '10.203.0.' || true)"
nft --check --file "$BUNDLE/guard.nft"
# Install empty lease BEFORE attaching the private interface. Errors stay closed.
nft --file "$BUNDLE/guard.nft"
ip netns add xianyu-business
ip link add xmg-host type veth peer name xmg-net
ip link set xmg-net netns xianyu-business
ip addr add 10.203.0.1/30 dev xmg-host
# Explicit return path: Tailscale's table-52 default must not capture veth replies.
ip -4 rule add priority 1000 to 10.203.0.0/30 lookup main
ip link set xmg-host up
ip -n xianyu-business addr add 10.203.0.2/30 dev xmg-net
ip -n xianyu-business link set lo up
ip -n xianyu-business link set xmg-net up
ip -n xianyu-business route add default via 10.203.0.1 dev xmg-net
ip netns exec xianyu-business sysctl -q -w net.ipv6.conf.all.disable_ipv6=1
install -d -o root -g root -m 755 /run/xianyu-egress /var/lib/xianyu-egress
printf '%s\n' 'Installed EMPTY lease. No business authorized.'
