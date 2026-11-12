#!/bin/bash
# setup_bench_net.sh — Persistent pfctl NAT for bench Pi access
#
# Replaces macOS Internet Sharing with a simple, reliable setup:
#   - en7 (USB ethernet) gets a static IP via networksetup -setmanual
#   - IP forwarding enabled in kernel and /etc/sysctl.conf
#   - pf NAT rule masquerades 192.168.2.0/24 out through en0 (wired uplink)
#
# Uses networksetup (not ifconfig) so macOS NECP registers the interface
# and traffic is routable. The self-router trick (router = own IP) puts
# en7 in the NWI without causing ICMP redirects. No en7 LaunchDaemon needed
# — networksetup config persists in SCNetwork across reboots.
#
# Safe to run multiple times (idempotent).
#
# Usage:
#   sudo ./scripts/setup_bench_net.sh           # install
#   sudo ./scripts/setup_bench_net.sh --remove  # uninstall (leaves IS disabled)
#   sudo ./scripts/setup_bench_net.sh --status  # show current state
#
# Prerequisites:
#   - Internet Sharing must be DISABLED in System Settings before running.
#   - USB ethernet dongle (en7, MAC 00:e0:4c:06:00:dd) must be plugged in.
#   - Uplink interface must be en0 (check: route get default | grep interface).

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────
EN7_IP="192.168.2.1"
EN7_MASK="255.255.255.0"
UPLINK="en0"
BENCH_SUBNET="192.168.2.0/24"

# USB ethernet dongle — identified by MAC for robustness in case name changes
EN7_MAC="00:e0:4c:06:00:dd"
EN7_HINT="en7"   # expected name; used as fallback if MAC lookup fails

ANCHOR_FILE="/etc/pf.anchors/bench-nat"
PF_DAEMON="/Library/LaunchDaemons/local.bench-net-pf.plist"
SYSCTL_CONF="/etc/sysctl.conf"

# ── Helpers ───────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $*"; }
err()  { echo -e "  ${RED}✗${NC} $*"; }

find_en7() {
    # Find interface by MAC address (robust against rename)
    local iface
    iface=$(ifconfig | awk -F: -v mac="$EN7_MAC" '
        /^[a-z]/ { cur = $1 }
        /ether / { gsub(/\t| /, "", $0); if (index($0, mac)) print cur }
    ' | head -1)
    echo "${iface:-$EN7_HINT}"
}

find_service() {
    # Find SCNetwork service name for the USB ethernet dongle by MAC
    networksetup -listallhardwareports | awk -v mac="$EN7_MAC" '
        /^Hardware Port:/ { name = substr($0, 17) }
        /Ethernet Address:/ {
            if (index(tolower($0), tolower(mac))) print name
        }
    '
}

# ── Root check ────────────────────────────────────────────────────────────
if [[ "$(id -u)" -ne 0 ]]; then
    echo "Needs root. Run: sudo $0 $*"
    exit 1
fi

# ── --status ─────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--status" ]]; then
    EN7_IF=$(find_en7)
    echo ""
    echo "Bench network status"
    echo "────────────────────"
    # en7 IP (via networksetup — source of truth)
    EN7_SVC=$(find_service)
    if [[ -n "$EN7_SVC" ]]; then
        ns_ip=$(networksetup -getinfo "$EN7_SVC" \
            | awk '/^IP address:/{print $NF}')
        ns_router=$(networksetup -getinfo "$EN7_SVC" \
            | awk '/^Router:/{print $NF}')
        if [[ "$ns_ip" == "$EN7_IP" && "$ns_router" == "$EN7_IP" ]]; then
            ok "$EN7_SVC: $ns_ip (manual, self-router)"
        else
            warn "$EN7_SVC: ip=${ns_ip:-none} router=${ns_router:-none}" \
                 "(expected $EN7_IP/self)"
        fi
    else
        warn "USB ethernet service not found for MAC $EN7_MAC"
    fi
    current_ip=$(ifconfig "$EN7_IF" 2>/dev/null | awk '/inet /{print $2}')
    if [[ "$current_ip" == "$EN7_IP" ]]; then
        ok "$EN7_IF: live IP $current_ip"
    else
        warn "$EN7_IF: live IP ${current_ip:-not assigned}"
    fi
    # IP forwarding
    fwd=$(sysctl -n net.inet.ip.forwarding 2>/dev/null)
    [[ "$fwd" == "1" ]] && ok "IP forwarding: enabled" || warn "IP forwarding: disabled"
    # pf NAT
    if pfctl -s nat 2>/dev/null | grep -q "192.168.2.0"; then
        ok "pf NAT: active ($(pfctl -s nat 2>/dev/null | grep 192.168.2.0 | head -1 | xargs))"
    else
        warn "pf NAT: not active"
    fi
    # LaunchDaemon (only pf — en7 IP is now managed by SCNetwork)
    [[ -f "$PF_DAEMON" ]] && ok "pf LaunchDaemon: installed" \
                          || warn "pf LaunchDaemon: not installed"
    # Connectivity
    echo ""
    if ping -c1 -W500 192.168.2.100 >/dev/null 2>&1; then
        ok "Pi reachable (192.168.2.100)"
    else
        warn "Pi not reachable — is it powered on?"
    fi
    echo ""
    exit 0
fi

# ── --remove ─────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--remove" ]]; then
    echo "Removing bench network config..."
    EN7_SVC=$(find_service)
    if [[ -n "$EN7_SVC" ]]; then
        networksetup -setdhcp "$EN7_SVC"
        ok "Reverted $EN7_SVC to DHCP"
    fi
    launchctl bootout system/local.bench-net-pf 2>/dev/null || true
    rm -f "$PF_DAEMON" "$ANCHOR_FILE"
    sed -i '' '/net.inet.ip.forwarding/d' "$SYSCTL_CONF" 2>/dev/null || true
    ok "pf LaunchDaemon removed"
    ok "Anchor file removed"
    warn "pf NAT rule still active until next reboot"
    exit 0
fi

# ── Install ───────────────────────────────────────────────────────────────
echo ""
echo "Setting up bench network (pfctl NAT)"
echo "──────────────────────────────────────"

# Sanity: check Internet Sharing is not running
if ifconfig bridge100 &>/dev/null; then
    err "bridge100 exists — Internet Sharing appears to be running."
    echo "    Disable it in System Settings → General → Sharing, then re-run."
    exit 1
fi

EN7_IF=$(find_en7)
if ! ifconfig "$EN7_IF" &>/dev/null; then
    err "Interface $EN7_IF not found. Is the USB ethernet dongle plugged in?"
    echo "    Available interfaces: $(ifconfig -l)"
    exit 1
fi
echo "  USB ethernet: $EN7_IF (MAC $EN7_MAC)"

# Sanity: check uplink
if ! ifconfig "$UPLINK" &>/dev/null; then
    err "Uplink interface $UPLINK not found. Edit UPLINK= in this script."
    exit 1
fi
echo "  Uplink      : $UPLINK ($(ifconfig "$UPLINK" | awk '/inet /{print $2}'))"

echo ""

# 1. IP forwarding
echo "Step 1: IP forwarding"
sysctl -w net.inet.ip.forwarding=1 >/dev/null
ok "Enabled now (sysctl)"
if ! grep -q "net.inet.ip.forwarding" "$SYSCTL_CONF" 2>/dev/null; then
    echo "net.inet.ip.forwarding=1" >> "$SYSCTL_CONF"
    ok "Written to $SYSCTL_CONF (persists across reboots)"
else
    ok "Already in $SYSCTL_CONF"
fi

# 2. en7 static IP via networksetup
# Using networksetup (not ifconfig) so macOS SCNetwork/NECP registers the
# interface as viable — required for socket routing to work. The "router"
# is set to the interface's own IP (self-router trick): satisfies configd
# without adding a real default route or causing ICMP redirects from the Pi.
echo ""
echo "Step 2: $EN7_IF static IP (via networksetup)"
EN7_SVC=$(find_service)
if [[ -z "$EN7_SVC" ]]; then
    err "Could not find SCNetwork service for MAC $EN7_MAC"
    exit 1
fi
networksetup -setmanual "$EN7_SVC" "$EN7_IP" "$EN7_MASK" "$EN7_IP"
ok "Configured '$EN7_SVC': $EN7_IP/$EN7_MASK, router=$EN7_IP (self)"
ok "Persists in SCNetwork — no LaunchDaemon needed for en7"

# 3. pf NAT
echo ""
echo "Step 3: pf NAT"
mkdir -p /etc/pf.anchors
echo "nat on $UPLINK from $BENCH_SUBNET to any -> ($UPLINK)" > "$ANCHOR_FILE"
pfctl -e 2>/dev/null || true   # enable pf (may already be enabled)
pfctl -N -f "$ANCHOR_FILE"
ok "NAT rule active: $BENCH_SUBNET -> $UPLINK"

cat > "$PF_DAEMON" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.bench-net-pf</string>
  <key>RunAtLoad</key><true/>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>/sbin/pfctl -e 2>/dev/null; /sbin/pfctl -N -f ${ANCHOR_FILE}</string>
  </array>
</dict></plist>
EOF
launchctl bootout system/local.bench-net-pf 2>/dev/null || true
launchctl bootstrap system "$PF_DAEMON"
ok "LaunchDaemon installed: $PF_DAEMON"

# ── Summary ───────────────────────────────────────────────────────────────
echo ""
echo "──────────────────────────────────────"
echo -e "${GREEN}Done.${NC} Bench network active and persistent."
echo ""
echo "  en7  : $EN7_IP/$EN7_MASK (static, persists via SCNetwork)"
echo "  NAT  : $BENCH_SUBNET → $UPLINK (persists via LaunchDaemon)"
echo "  Fwd  : net.inet.ip.forwarding=1 (persists via $SYSCTL_CONF)"
echo ""
echo "Verify:"
echo "  ping 192.168.2.100"
echo "  ssh pi@192.168.2.100 'ping -c1 8.8.8.8'"
echo "  ./scripts/check_pi.sh"
echo ""
echo "On next Mac reboot, everything comes back automatically."
echo "Internet Sharing must remain DISABLED (System Settings → General → Sharing)."
echo ""
