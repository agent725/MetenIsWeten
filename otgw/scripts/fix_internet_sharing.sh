#!/bin/bash
# fix_internet_sharing.sh — Fix macOS Internet Sharing routing bug (Darwin 25.x)
#
# SYMPTOM: ssh/ping to 192.168.2.100 fails with "No route to host" even though
# bridge100 is up, en7 is a member, and the Pi's ARP entry is in the cache.
#
# ROOT CAUSE: macOS Internet Sharing registers NECP (Network Extension Control
# Plane) policies in the kernel when it starts. These policies sit BETWEEN the
# BSD socket layer and the routing table — NECP can return EHOSTUNREACH even
# when the routing table shows a valid route. After a reboot (Darwin 25.x
# regression), NECP policies become stale and block all traffic to the
# 192.168.2.0/24 subnet.
#
# NOTE: The '!' in 'netstat -rn' for the 192.168.2 network route is NORMAL —
# it means "no expiry" for an interface/network route, not an error.
#
# FIX: Use 'launchctl bootout' to fully unregister Internet Sharing (this
# resets NECP policies) then 'launchctl bootstrap' to re-register it cleanly.
# This is equivalent to toggling Internet Sharing in System Settings.
# 'launchctl stop/start' is NOT sufficient — it sends signals but does not
# reset NECP kernel state.
#
# Usage:
#   sudo ./scripts/fix_internet_sharing.sh        # detect and fix
#   ./scripts/fix_internet_sharing.sh --check     # check only (no sudo needed)
#
# Exit codes: 0 = OK (or fixed), 1 = broken and not fixed, 2 = needs sudo

set -euo pipefail

BRIDGE="bridge100"
SUBNET="192.168.2"
PI_IP="${SUBNET}.100"
IS_LABEL="com.apple.InternetSharing"
IS_PLIST="/System/Library/LaunchDaemons/com.apple.InternetSharing.plist"

check_only=false
[[ "${1:-}" == "--check" ]] && check_only=true

# Detect broken routing: "No route to host" means NECP is blocking.
# "Request timeout" (or success) means routing is fine — Pi may just be off.
route_broken() {
    ping -c1 -W 500 "$PI_IP" 2>&1 | grep -q "No route to host"
}

# ── Diagnostics ─────────────────────────────────────────────────────────────

print_state() {
    echo ""
    echo "  bridge100 state:"
    if ifconfig "$BRIDGE" &>/dev/null; then
        local members
        members=$(ifconfig "$BRIDGE" | grep "member:" | awk '{print $2}' | tr '\n' ' ')
        echo "    members : ${members:-none}"
        echo "    status  : $(ifconfig "$BRIDGE" | grep status: | awk '{print $2}')"
    else
        echo "    (interface does not exist)"
    fi
    echo "  routes to ${SUBNET}.x:"
    netstat -rn 2>/dev/null | awk -v s="$SUBNET" '$1 ~ "^"s {printf "    %-20s %s\n", $1, $0}'
    echo ""
}

# ── Check ───────────────────────────────────────────────────────────────────

if ! route_broken; then
    echo "Routing OK: ${PI_IP} is reachable (or Pi is off but routing works)"
    exit 0
fi

echo "Routing BROKEN: ${PI_IP} — No route to host"
echo "  bridge100 is up, ARP works, but NECP policies are stale (Darwin 25.x)"
print_state

if $check_only; then
    echo "  Fix: sudo $(cd "$(dirname "$0")" && pwd)/fix_internet_sharing.sh"
    exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
    echo "  Needs root. Run: sudo $0"
    exit 2
fi

# ── Fix ─────────────────────────────────────────────────────────────────────

echo "Step 1: bootout Internet Sharing (fully unregisters NECP policies)..."
launchctl bootout "system/${IS_LABEL}" 2>/dev/null || true

# Wait for bridge100 to disappear (Internet Sharing owns it)
for i in $(seq 1 10); do
    ifconfig "$BRIDGE" &>/dev/null || break
    sleep 1
done

echo "Step 2: bootstrap Internet Sharing (re-registers with fresh NECP state)..."
launchctl bootstrap system "$IS_PLIST" 2>/dev/null || true

# Wait for bridge100 to come back
echo "  Waiting for ${BRIDGE} to come up..."
for i in $(seq 1 20); do
    ifconfig "$BRIDGE" &>/dev/null && break
    sleep 1
done
sleep 5  # extra settle time for DHCP + NECP

if ! route_broken; then
    echo "Fixed: routing restored to ${PI_IP}"
    print_state
    exit 0
fi

echo ""
echo "WARNING: still broken after bootout/bootstrap."
print_state
echo "Manual fix: toggle Internet Sharing in System Settings:"
echo "  System Settings → General → Sharing → Internet Sharing"
echo "  Turn OFF, wait 10 s, turn ON, wait 15 s, then retry."
exit 1
