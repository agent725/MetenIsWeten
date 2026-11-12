#!/bin/bash
#
# test_image_ap.sh — Image acceptance test via Pi config AP
#
# Runs from the Mac mini. Connects WiFi to the Pi's config AP, tests the image,
# optionally provisions WiFi and verifies normal operation + reboot recovery.
#
# Stage 1 (always):  Connect to Pi AP → check_pi.sh --fresh (fresh invariants)
# Stage 2 (--full):  POST home WiFi creds via portal → check on home network
# Stage 3 (--full):  Reboot Pi → verify all services recover
#
# Prerequisites:
#   - Mac with WiFi + ethernet (ethernet keeps internet uninterrupted)
#   - sshpass: brew install hudochenkov/sshpass/sshpass
#   - Pi flashed with a fresh image, powered on (allow ≥ 90s before running)
#
# Usage:
#   ./scripts/test_image_ap.sh                                 # Stage 1 only
#   ./scripts/test_image_ap.sh --full --home-ssid S --home-pass P
#   ./scripts/test_image_ap.sh --ssid otgw-a1b2c3             # skip AP scan
#   ./scripts/test_image_ap.sh --timeout 240                  # longer AP scan
#   ./scripts/test_image_ap.sh --wifi-if en1                  # override interface
#
# On a fresh image the AP auto-opens 60s after boot, is open (no password),
# has SSH enabled, and stays up indefinitely while wifi.conf is absent.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Defaults ──────────────────────────────────────────────────────────────
PI_AP_IP="10.42.0.1"
PI_USER="otgw"
PI_PASS="otgw"
SCAN_TIMEOUT=180
TARGET_SSID=""
WIFI_IF=""
FULL=0
HOME_SSID=""
HOME_PASS=""

# Tracks whether Mac WiFi has been reconnected to home (Stage 2+).
# Cleanup skips reconnection if already done.
_ON_HOME=0
_CLEANUP_DONE=0

# ── Airport scan helper ────────────────────────────────────────────────────
# macOS 15+ removed the airport binary; fall back to CoreWLAN via Python.
_AIRPORT="/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"

_scan_otgw_ssid() {
    if [[ -x "$_AIRPORT" ]]; then
        local s
        s=$("$_AIRPORT" -s 2>/dev/null | grep -o 'otgw-[A-Za-z0-9]*' | head -1)
        [[ -n "$s" ]] && { echo "$s"; return 0; }
    fi
    python3 - <<'PY' 2>/dev/null
import sys
try:
    import CoreWLAN
    client = CoreWLAN.CWWiFiClient.sharedWiFiClient()
    iface  = client.interface()
    nets, _err = iface.scanForNetworksWithName_error_(None, None)
    if nets:
        for n in nets:
            ssid = n.ssid()
            if ssid and ssid.startswith('otgw-'):
                print(ssid)
                sys.exit(0)
except Exception:
    pass
PY
}

# ── Colors + helpers ───────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; NC='\033[0m'

_ok()    { echo -e " ${GREEN}OK${NC}"; }
_fail()  { echo -e " ${RED}FAILED${NC}"; }
_stage() { echo -e "\n${BOLD}── $1 ──────────────────────────────────────────${NC}\n"; }

# ── Argument parsing ───────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --full)       FULL=1; shift ;;
        --home-ssid)  HOME_SSID="$2"; shift 2 ;;
        --home-pass)  HOME_PASS="$2"; shift 2 ;;
        --ssid)       TARGET_SSID="$2"; shift 2 ;;
        --timeout)    SCAN_TIMEOUT="$2"; shift 2 ;;
        --wifi-if)    WIFI_IF="$2"; shift 2 ;;
        *)
            echo "Usage: $0 [--full --home-ssid SSID --home-pass PASS]"
            echo "          [--ssid otgw-XXXXXX] [--timeout SECONDS] [--wifi-if IFACE]"
            exit 1 ;;
    esac
done

# ── Preflight checks ───────────────────────────────────────────────────────
if ! command -v sshpass >/dev/null 2>&1; then
    echo -e "${RED}sshpass not found${NC}"
    echo "  Install: brew install hudochenkov/sshpass/sshpass"
    exit 1
fi

if [[ "$FULL" -eq 1 ]]; then
    if ! command -v curl >/dev/null 2>&1; then
        echo -e "${RED}curl not found (required for --full)${NC}"; exit 1
    fi
    if [[ -z "$HOME_SSID" || -z "$HOME_PASS" ]]; then
        echo -e "${RED}--full requires --home-ssid and --home-pass${NC}"
        exit 1
    fi
fi

if [[ -z "$WIFI_IF" ]]; then
    WIFI_IF=$(networksetup -listallhardwareports \
        | awk '/Wi-Fi/{getline; print $2}' | head -1)
fi
if [[ -z "$WIFI_IF" ]]; then
    echo -e "${RED}No WiFi interface found${NC}"; exit 1
fi

PREV_SSID=$(networksetup -getairportnetwork "$WIFI_IF" 2>/dev/null \
    | grep 'Current Wi-Fi Network:' | sed 's/Current Wi-Fi Network: //' || true)

# ── Header ────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}OTGW Image Acceptance Test${NC}"
echo "WiFi interface : $WIFI_IF"
echo "Previous SSID  : ${PREV_SSID:-<none>}"
echo "Pi AP IP       : $PI_AP_IP"
if [[ "$FULL" -eq 1 ]]; then
    echo "Mode           : full (Stage 1 + 2 + 3)"
    echo "Home SSID      : $HOME_SSID"
else
    echo "Mode           : Stage 1 only (--full for WiFi provisioning + reboot test)"
fi

# ── Cleanup: reconnect Mac WiFi on any exit ───────────────────────────────
cleanup() {
    [[ "$_CLEANUP_DONE" -eq 1 ]] && return
    _CLEANUP_DONE=1
    [[ "$_ON_HOME" -eq 1 ]] && return   # Already reconnected in Stage 2
    echo ""
    if [[ -n "$PREV_SSID" ]]; then
        printf 'Reconnecting WiFi to "%s"...' "$PREV_SSID"
        networksetup -setairportnetwork "$WIFI_IF" "$PREV_SSID" >/dev/null 2>&1 \
            && _ok || echo -e " ${YELLOW}check manually${NC}"
    else
        [[ -x "$_AIRPORT" ]] && "$_AIRPORT" -z >/dev/null 2>&1 || true
        echo "WiFi: disconnected (was not connected before)"
    fi
}
trap cleanup EXIT INT TERM

# ── Shared: wait for SSH port to open ─────────────────────────────────────
_wait_ssh() {
    local host="$1" label="$2" attempts="${3:-15}" interval="${4:-2}"
    printf '%s' "$label"
    for _ in $(seq 1 "$attempts"); do
        if nc -z -G 2 "$host" 22 >/dev/null 2>&1; then
            _ok; return 0
        fi
        printf '.'; sleep "$interval"
    done
    _fail; return 1
}

# ── Shared: find Pi on home network ───────────────────────────────────────
_find_pi_home() {
    local timeout="${1:-90}"
    local elapsed=0
    printf 'Waiting for Pi on home network'
    while [[ $elapsed -lt $timeout ]]; do
        if nc -z -G 2 otgw-logger.local 22 >/dev/null 2>&1; then
            echo ""; echo "otgw-logger.local"; return 0
        fi
        for ip in 192.168.9.5{0..9}; do
            if nc -z -G 1 "$ip" 22 >/dev/null 2>&1; then
                if sshpass -p "$PI_PASS" ssh \
                        -o ConnectTimeout=3 -o StrictHostKeyChecking=no \
                        -o UserKnownHostsFile=/dev/null \
                        "${PI_USER}@${ip}" "test -f /etc/otgw/pi-id" 2>/dev/null; then
                    echo ""; echo "$ip"; return 0
                fi
            fi
        done
        printf '.'; sleep 5; elapsed=$((elapsed + 5))
    done
    echo ""; return 1
}

# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — Fresh image check via Pi config AP
# ─────────────────────────────────────────────────────────────────────────────
_stage "Stage 1 — Fresh image check via Pi AP"

# Find AP ---
if [[ -z "$TARGET_SSID" ]]; then
    echo -e "Scanning for ${BOLD}otgw-*${NC} AP (up to ${SCAN_TIMEOUT}s)..."
    echo "  Tip: Pi must be powered on ≥ 60 s for the AP to auto-open."
    ELAPSED=0
    while [[ $ELAPSED -lt $SCAN_TIMEOUT ]]; do
        TARGET_SSID=$(_scan_otgw_ssid)
        [[ -n "$TARGET_SSID" ]] && break
        printf '  .'; sleep 5; ELAPSED=$((ELAPSED + 5))
    done
    echo ""
    if [[ -z "$TARGET_SSID" ]]; then
        echo -e "${RED}No otgw-* AP found after ${SCAN_TIMEOUT}s${NC}"
        echo "  Possible causes:"
        echo "    • Pi not yet booted — wait 90 s from power-on and retry"
        echo "    • wifi.conf already present — AP only auto-opens when absent"
        echo "    • SD card not correctly flashed"
        echo "    • WiFi antenna not connected"
        echo "  If you know the SSID: $0 --ssid otgw-XXXXXX"
        exit 1
    fi
fi
echo -e "Found AP: ${GREEN}${BOLD}${TARGET_SSID}${NC}"

# Connect Mac WiFi to Pi AP ---
printf 'Connecting to AP...'
networksetup -setairportnetwork "$WIFI_IF" "$TARGET_SSID" >/dev/null 2>&1 || true
CONNECTED=0
for _ in $(seq 1 20); do
    sleep 2
    MY_IP=$(ipconfig getifaddr "$WIFI_IF" 2>/dev/null || true)
    if [[ "$MY_IP" == 10.42.* ]]; then
        CONNECTED=1; echo -e " ${GREEN}OK${NC} (my IP: $MY_IP)"; break
    fi
    printf '.'
done
if [[ "$CONNECTED" -eq 0 ]]; then
    _fail
    echo "  Could not get a 10.42.x.x address — check: networksetup -getairportnetwork $WIFI_IF"
    exit 1
fi

# Wait for SSH ---
_wait_ssh "$PI_AP_IP" "Waiting for SSH on $PI_AP_IP" 15 2 || {
    echo "  The AP firewall opens SSH automatically when the portal starts."
    echo "  If the Pi is still booting, wait 30 s and retry."
    exit 1
}

# Run fresh check ---
export SSHPASS="$PI_PASS"
"$SCRIPT_DIR/check_pi.sh" --fresh "${PI_USER}@${PI_AP_IP}"
STAGE1_EXIT=$?

if [[ "$STAGE1_EXIT" -ne 0 ]]; then
    echo -e "\n${RED}Stage 1 FAILED — image has problems; aborting.${NC}"
    exit $STAGE1_EXIT
fi

[[ "$FULL" -eq 0 ]] && exit 0

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — WiFi provisioning + home-network check
# ─────────────────────────────────────────────────────────────────────────────
_stage "Stage 2 — WiFi provisioning"

# POST WiFi credentials to the portal ---
printf 'Posting WiFi credentials to portal...'
# curl may lose connection mid-flight as the AP closes; treat any response or
# connection reset as success — the POST was received before the Pi disconnected.
curl -s -X POST "http://${PI_AP_IP}/wifi" \
    --data-urlencode "ssid=${HOME_SSID}" \
    --data-urlencode "password=${HOME_PASS}" \
    -o /dev/null -m 10 2>/dev/null \
    && _ok \
    || echo -e " ${YELLOW}connection dropped (Pi is connecting — this is normal)${NC}"

# Wait for Pi AP to close (3 consecutive failed pings = AP gone) ---
# In dual-mode (Pi 4+) the AP may stay up alongside home WiFi — time out after
# 60 s and move on regardless.
printf 'Waiting for AP to close'
AP_CLOSED=0
CONSEC=0
for _ in $(seq 1 30); do
    sleep 2
    if ! ping -c 1 -t 1 "$PI_AP_IP" >/dev/null 2>&1; then
        CONSEC=$((CONSEC + 1)); printf '-'
        if [[ $CONSEC -ge 3 ]]; then AP_CLOSED=1; break; fi
    else
        CONSEC=0; printf '.'
    fi
done
echo ""
if [[ "$AP_CLOSED" -eq 1 ]]; then
    echo "  AP closed — Pi is connecting to home WiFi"
else
    echo "  AP still up after 60 s — Pi may be in dual-mode (AP + home WiFi)"
fi

# Reconnect Mac WiFi to home network ---
printf 'Reconnecting Mac WiFi to "%s"...' "$HOME_SSID"
networksetup -setairportnetwork "$WIFI_IF" "$HOME_SSID" "$HOME_PASS" >/dev/null 2>&1 || true
HOME_IP=""
for _ in $(seq 1 15); do
    sleep 2
    HOME_IP=$(ipconfig getifaddr "$WIFI_IF" 2>/dev/null || true)
    if [[ -n "$HOME_IP" && "$HOME_IP" != 10.42.* ]]; then
        echo -e " ${GREEN}OK${NC} (my IP: $HOME_IP)"; break
    fi
    printf '.'
done
if [[ -z "$HOME_IP" || "$HOME_IP" == 10.42.* ]]; then
    echo -e " ${RED}FAILED${NC} — could not get home network IP"
    echo "  Check home WiFi credentials and retry."
    exit 1
fi
_ON_HOME=1

# Discover Pi on home network ---
PI_HOME=$(_find_pi_home 90) || {
    echo -e "${RED}Pi not found on home network after 90 s${NC}"
    echo "  Possible causes:"
    echo "    • Wrong WiFi password (Pi failed to connect)"
    echo "    • Pi not in 192.168.9.50–59 range and mDNS not resolving"
    echo "  Try: ssh otgw@otgw-logger.local"
    exit 1
}
echo -e "Pi found at: ${GREEN}${BOLD}${PI_HOME}${NC}"
sleep 5   # Give services a moment to settle after joining home WiFi

# Run normal check ---
"$SCRIPT_DIR/check_pi.sh" "${PI_USER}@${PI_HOME}"
STAGE2_EXIT=$?

if [[ "$STAGE2_EXIT" -ne 0 ]]; then
    echo -e "\n${RED}Stage 2 FAILED — Pi is on home network but something is wrong.${NC}"
    exit $STAGE2_EXIT
fi

# ─────────────────────────────────────────────────────────────────────────────
# Stage 3 — Reboot + recovery check
# ─────────────────────────────────────────────────────────────────────────────
_stage "Stage 3 — Reboot and recovery"

# Send reboot command ---
printf 'Rebooting Pi...'
sshpass -p "$PI_PASS" ssh \
    -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "${PI_USER}@${PI_HOME}" "sudo systemctl reboot" 2>/dev/null || true
echo -e " ${GREEN}command sent${NC}"

# Wait for SSH to go down (confirms reboot started) ---
printf 'Waiting for Pi to go offline'
for _ in $(seq 1 30); do
    sleep 2
    nc -z -G 1 "$PI_HOME" 22 >/dev/null 2>&1 || { echo -e " ${GREEN}offline${NC}"; break; }
    printf '.'
done

# Wait for SSH to come back (Pi 3: ~60 s, Pi 4/5: ~30 s; allow 180 s) ---
_wait_ssh "$PI_HOME" "Waiting for Pi to come back online" 36 5 || {
    echo -e "${RED}Pi did not come back within 180 s${NC}"
    echo "  Check: ping otgw-logger.local  |  ssh otgw@otgw-logger.local"
    exit 1
}

sleep 10   # Give systemd time to start all services

# Run normal check after reboot ---
"$SCRIPT_DIR/check_pi.sh" "${PI_USER}@${PI_HOME}"
STAGE3_EXIT=$?

# ─────────────────────────────────────────────────────────────────────────────
# Full-test summary
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}  Full Test Results${NC}"
echo -e "  Stage 1 (fresh AP):     $([ "$STAGE1_EXIT" -eq 0 ] && echo -e "${GREEN}PASS${NC}" || echo -e "${RED}FAIL${NC}")"
echo -e "  Stage 2 (home WiFi):    $([ "$STAGE2_EXIT" -eq 0 ] && echo -e "${GREEN}PASS${NC}" || echo -e "${RED}FAIL${NC}")"
echo -e "  Stage 3 (post-reboot):  $([ "$STAGE3_EXIT" -eq 0 ] && echo -e "${GREEN}PASS${NC}" || echo -e "${RED}FAIL${NC}")"
if [[ "$STAGE3_EXIT" -eq 0 ]]; then
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "  ${GREEN}${BOLD}PASS${NC} — image accepted, Pi is ready for deployment"
else
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "  ${RED}${BOLD}FAIL${NC}"
fi
echo ""

exit $STAGE3_EXIT
