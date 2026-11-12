#!/bin/bash
#
# check_pi.sh — OTGW Logger installation sign-off
#
# Runs on the Mac. SSHes to the Pi, checks all services and config,
# prints a pass/fail report, and saves a timestamped log.
#
# Usage:
#   ./scripts/check_pi.sh                         # otgw@otgw-logger.local (default)
#   ./scripts/check_pi.sh otgw-logger.local       # explicit mDNS
#   ./scripts/check_pi.sh 192.168.9.50            # explicit IP
#   ./scripts/check_pi.sh --fresh otgw@10.42.0.1  # fresh-image acceptance test via AP
#
# --fresh mode: inverts WiFi/internet/NTP/sync/mode checks.
#   On a freshly flashed image these must be absent — their presence is a FAIL.
#   Intended for use by test_image_ap.sh (connects via the Pi's config AP).
#
# Password auth: set SSHPASS=<password> in the environment and sshpass is used
#   automatically. test_image_ap.sh sets this for the default 'otgw' password.

set -uo pipefail

FRESH=0
while [[ $# -gt 0 && "$1" == --* ]]; do
    case "$1" in
        --fresh) FRESH=1; shift ;;
        *) echo "Unknown flag: $1"; exit 1 ;;
    esac
done

PI_ARG="${1:-otgw-logger.local}"
[[ "$PI_ARG" == *@* ]] && PI_HOST="$PI_ARG" || PI_HOST="otgw@$PI_ARG"

# Use sshpass when SSHPASS env var is set (password auth without a prompt).
if [[ -n "${SSHPASS:-}" ]] && command -v sshpass >/dev/null 2>&1; then
    _SSH="sshpass -e ssh"
else
    _SSH="ssh"
fi

REPORT_DIR="${HOME}/.otgw/checks"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; NC='\033[0m'

PASSES=0; FAILS=0; WARNS=0
REPORT=""

_pass() { echo -e "  ${GREEN}✓${NC} $1"; REPORT+="  PASS  $1"$'\n'; PASSES=$((PASSES + 1)); }
_fail() { echo -e "  ${RED}✗${NC} $1"; REPORT+="  FAIL  $1"$'\n'; FAILS=$((FAILS + 1)); }
_warn() { echo -e "  ${YELLOW}⚠${NC} $1"; REPORT+="  WARN  $1"$'\n'; WARNS=$((WARNS + 1)); }
_head() { echo -e "\n${BOLD}$1${NC}"; REPORT+=$'\n'"$1"$'\n'; }

# ── Connect and gather ────────────────────────────────────────────────────

if [[ "$FRESH" -eq 1 ]]; then
    _TITLE="OTGW Logger — Fresh Image Acceptance Test"
else
    _TITLE="OTGW Logger — Installation Sign-Off"
fi

echo ""
echo -e "${BOLD}${_TITLE}${NC}"
echo "Host: $PI_HOST  |  $(date)"
REPORT="${_TITLE}"$'\n'
REPORT+="Host: $PI_HOST  |  $(date)"$'\n'

# ── Preflight: macOS Internet Sharing routing check ───────────────────────
# Only relevant when target is in the 192.168.2.x range (local Pi via bridge100).
if [[ "$PI_HOST" =~ 192\.168\.2\. ]]; then
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    FIX_SCRIPT="$SCRIPT_DIR/fix_internet_sharing.sh"
    if [[ -x "$FIX_SCRIPT" ]] && ! "$FIX_SCRIPT" --check >/dev/null 2>&1; then
        echo -e "${RED}Preflight FAIL: bridge100 routing is broken${NC}"
        echo "  Run: sudo $FIX_SCRIPT"
        echo "  Then retry: $0 $*"
        exit 1
    fi
fi

printf 'Connecting...'

RAW=$($_SSH -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "$PI_HOST" bash <<'REMOTE' 2>/dev/null

# ── Identity ──────────────────────────────────────────────────────────────
image_id=$(cat /etc/otgw/pi-id 2>/dev/null || echo "")
echo "image_id=$image_id"
echo "hostname=$(hostname)"
echo "kernel=$(uname -r)"
echo "uptime=$(uptime -p 2>/dev/null || uptime)"

# ── Partitions ────────────────────────────────────────────────────────────
root_total_gb=$(df / | awk 'NR==2{printf "%.0f", $2/1048576}')
root_pct=$(df / | awk 'NR==2{gsub(/%/,"",$5); print $5}')
root_fstype=$(stat -f -c %T / 2>/dev/null || echo unknown)
boot_fs=$(lsblk -n -o FSTYPE /dev/mmcblk0p1 2>/dev/null | head -1 || echo unknown)
boot_size_mb=$(lsblk -b -n -o SIZE /dev/mmcblk0p1 2>/dev/null \
    | head -1 | awk '{printf "%.0f", $1/1048576}' || echo 0)
echo "root_total_gb=$root_total_gb"
echo "root_pct=$root_pct"
echo "root_fstype=$root_fstype"
echo "boot_fs=$boot_fs"
echo "boot_size_mb=$boot_size_mb"

# ── Services ──────────────────────────────────────────────────────────────
failed=$(systemctl --failed --no-legend 2>/dev/null | wc -l | tr -d ' ')
echo "failed=$failed"
for svc in mosquitto otgw-listener ds18b20-listener mqtt-logger heartbeat.timer log-sync.timer; do
    key="svc_$(echo "$svc" | tr '.-' '__')"
    echo "$key=$(systemctl is-active "$svc" 2>/dev/null || echo unknown)"
done

# ── Log file activity ─────────────────────────────────────────────────────
# Log files live in /var/log/otgw/<image_id>/ (FAT32, flushed every 5 min).
# mqtt-logger writes in real-time to /run/otgw/<image_id>/ (RAM); log-flush.timer
# rsyncs to FAT32 every 5 min. Age checks prefer the live RAM dir.
log_dir="/var/log/otgw${image_id:+/$image_id}"
live_log_dir="/run/otgw${image_id:+/$image_id}"
echo "log_dir_exists=$([[ -d $log_dir ]] && echo yes || echo no)"
_log_age_h() {
    local f
    # Check live RAM dir first (updated every ~60s), then persistent FAT32 dir
    for dir in "$live_log_dir" "$log_dir"; do
        f=$(find "$dir" -maxdepth 1 -name "$1" -type f 2>/dev/null \
            | xargs ls -t 2>/dev/null | head -1)
        [[ -n "$f" ]] && break
    done
    [[ -z "$f" ]] && { echo -1; return; }
    echo $(( ($(date +%s) - $(stat -c %Y "$f")) / 3600 ))
}
echo "otgw_log_h=$(_log_age_h 'otgw_*.log')"
echo "temp_log_h=$(_log_age_h 'temperature_*.log')"
echo "gas_log_h=$(_log_age_h 'gas_*.log')"
# Heartbeat: check heartbeat.json directly (written by heartbeat.py to the
# per-image RAM-disk subdir). The MQTT-logged heartbeat file has a different
# naming pattern (home_{id}_system_heartbeat_{date}.log) and lives in the
# parent /run/otgw/ directory — heartbeat_*.log glob never matches it.
# heartbeat.json is always in $live_log_dir and reflects the last run.
# See FSD §G.13 and NFR-4.18.
_hb_json="${live_log_dir}/heartbeat.json"
if [[ -f "$_hb_json" ]]; then
    echo "hb_log_h=$(( ($(date +%s) - $(stat -c %Y "$_hb_json")) / 3600 ))"
else
    echo "hb_log_h=-1"
fi

# ── System health ─────────────────────────────────────────────────────────
ntp_sync=$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)
mem_pct=$(free | awk '/^Mem:/{printf "%.0f", $3/$2*100}')
# Count upgradable packages without running apt-get update (reads cached state)
upgradable=$(apt list --upgradable 2>/dev/null | grep '\[upgradable' | wc -l | tr -d ' ')
# Verify journal uses volatile storage only (Storage=volatile in journald.conf).
# Check /var/log/journal/ directly — journalctl --disk-usage includes the RAM
# journal (/run/log/journal/) and is not a reliable indicator of disk writes.
journal_disk_kb=$(du -sk /var/log/journal/ 2>/dev/null | awk '{print $1}' || echo 0)
mode=$(cat /etc/otgw/mode 2>/dev/null || echo not-configured)
code_version=$(cat /etc/otgw/version 2>/dev/null || echo unknown)
ds18b20_count=$(ls -d /sys/bus/w1/devices/28-* 2>/dev/null | wc -l | tr -d ' ')
ds18b20_ids=$(ls -d /sys/bus/w1/devices/28-* 2>/dev/null \
    | xargs -I{} basename {} | tr '\n' ',' | sed 's/,$//')
# Read temperature from each DS18B20 sensor (format: "id:temp_c,id:temp_c,...")
ds18b20_temps=""
for sensor_path in /sys/bus/w1/devices/28-*/w1_slave; do
    sensor_id=$(basename "$(dirname "$sensor_path")")
    raw=$(cat "$sensor_path" 2>/dev/null)
    if echo "$raw" | grep -q "YES"; then
        temp_raw=$(echo "$raw" | grep -o "t=[0-9-]*" | cut -d= -f2)
        temp_c=$(awk "BEGIN{printf \"%.1f\", $temp_raw/1000}")
        ds18b20_temps="${ds18b20_temps}${sensor_id}:${temp_c},"
    else
        ds18b20_temps="${ds18b20_temps}${sensor_id}:err,"
    fi
done
ds18b20_temps="${ds18b20_temps%,}"
echo "ntp_sync=$ntp_sync"
echo "mem_pct=$mem_pct"
echo "upgradable=$upgradable"
echo "journal_disk_kb=$journal_disk_kb"
echo "mode=$mode"
echo "code_version=$code_version"
echo "ds18b20_count=$ds18b20_count"
echo "ds18b20_ids=$ds18b20_ids"
echo "ds18b20_temps=$ds18b20_temps"

# ── Network ───────────────────────────────────────────────────────────────
internet=$(ping -c1 -W3 8.8.8.8 >/dev/null 2>&1 && echo ok || echo fail)
wifi_ssid=$(iwgetid wlan0 -r 2>/dev/null || echo "")
wifi_ip=$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{split($2,a,"/"); print a[1]}')
sync_count=$(ls /etc/otgw/sync.d/*.conf 2>/dev/null | wc -l | tr -d ' ')
echo "internet=$internet"
echo "wifi_ssid=$wifi_ssid"
echo "wifi_ip=$wifi_ip"
echo "sync_count=$sync_count"

# ── OTGW hardware check ────────────────────────────────────────────────────
# Check journal to distinguish "active with device" from "active but retrying"
if journalctl -u otgw-listener -n 50 --no-pager 2>/dev/null \
        | grep -q "No serial device found"; then
    echo "otgw_hw=retrying"
elif systemctl is-active otgw-listener >/dev/null 2>&1; then
    echo "otgw_hw=connected"
else
    echo "otgw_hw=inactive"
fi

# ── P1 MQTT data check ────────────────────────────────────────────────────
# Listen briefly for a P1/gas message on MQTT (timeout 3s)
image_id_val=$(cat /etc/otgw/pi-id 2>/dev/null || echo "")
p1_topic="home/${image_id_val}/energy/gas"
if timeout 3 mosquitto_sub -t "$p1_topic" -C 1 2>/dev/null | grep -q .; then
    echo "p1_mqtt=active"
else
    echo "p1_mqtt=silent"
fi

REMOTE
) || {
    echo -e " ${RED}FAILED${NC}\n"
    echo -e "${RED}Cannot reach $PI_HOST${NC}"
    echo "  • Check ethernet cable is connected"
    echo "  • Wait ~30 s after power-on and retry"
    echo "  • Try: ssh $PI_HOST"
    exit 1
}

echo -e " ${GREEN}OK${NC}"

get() { echo "$RAW" | grep "^$1=" | cut -d= -f2-; }

IMAGE_ID=$(get image_id)

# ── Identity ─────────────────────────────────────────────────────────────

_head "Identity"
echo "  Hostname : $(get hostname)"
echo "  Kernel   : $(get kernel)"
echo "  Uptime   : $(get uptime)"

if [[ -n "$IMAGE_ID" ]]; then
    _pass "Image ID: $IMAGE_ID"
else
    _fail "Image ID not set — firstrun.sh may not have completed"
fi

# ── Partitions ────────────────────────────────────────────────────────────

_head "Partitions"

ROOT_GB=$(get root_total_gb)
ROOT_GB_INT="${ROOT_GB%%.*}"
ROOT_FSTYPE=$(get root_fstype)
# Pre-baked overlayroot images: root is a read-only overlayfs mount (~1 GB).
# resize2fs does not apply — systemd-growfs-root is masked at build time.
# For non-overlayroot images the root should be ≥3 GB after first boot resize.
if [[ "$ROOT_FSTYPE" == "overlay"* ]]; then
    _pass "Root filesystem: ${ROOT_GB} GB (overlayroot — read-only lower layer)"
elif [[ "${ROOT_GB_INT:-0}" -ge 3 ]]; then
    _pass "Root filesystem: ${ROOT_GB} GB total (resize2fs completed)"
else
    _fail "Root filesystem: ${ROOT_GB:-?} GB — resize2fs likely did not run (expected ≥3 GB)"
fi

ROOT_PCT=$(get root_pct)
if   [[ -n "$ROOT_PCT" && "$ROOT_PCT" -ge 90 ]]; then
    _fail "Root fs usage: ${ROOT_PCT}% (critically full)"
elif [[ -n "$ROOT_PCT" && "$ROOT_PCT" -ge 80 ]]; then
    _warn "Root fs usage: ${ROOT_PCT}% (getting full)"
elif [[ -n "$ROOT_PCT" ]]; then
    _pass "Root fs usage: ${ROOT_PCT}%"
fi

BOOT_FS=$(get boot_fs)
BOOT_MB=$(get boot_size_mb)
if [[ "$BOOT_FS" == "vfat" ]]; then
    _pass "Boot partition: FAT32, ${BOOT_MB} MB"
else
    _warn "Boot partition: ${BOOT_FS:-unknown} (expected vfat/FAT32)"
fi

# ── Services ─────────────────────────────────────────────────────────────

_head "Services"

chk_svc() {
    local key="$1" label="$2" required="${3:-yes}"
    local svc_key="svc_$(echo "$key" | tr '.-' '__')"
    local state; state=$(get "$svc_key")
    case "$state" in
        active)     _pass "$label: active" ;;
        activating) [[ "$required" == yes ]] \
                        && _warn "$label: still activating (device connected?)" \
                        || _warn "$label: activating (OK if no device connected)" ;;
        *)          [[ "$required" == yes ]] \
                        && _fail "$label: ${state:-unknown}" \
                        || _warn "$label: ${state:-unknown}" ;;
    esac
}

chk_svc mosquitto           "mosquitto (MQTT broker)"         yes
chk_svc otgw-listener       "otgw-listener"                   no
chk_svc ds18b20-listener    "ds18b20-listener (temperature)"  yes
chk_svc mqtt-logger         "mqtt-logger"                     yes
chk_svc heartbeat.timer     "heartbeat.timer"                 yes
chk_svc log-sync.timer      "log-sync.timer"                  yes

FAILED=$(get failed)
[[ "${FAILED:-0}" == "0" ]] \
    && _pass "No failed systemd units" \
    || _fail "${FAILED} failed unit(s) — run: systemctl --failed"

# ── Data Activity ─────────────────────────────────────────────────────────

_head "Data Activity"

# Note: mqtt-logger writes to RAM every 60s; log-flush.timer syncs to FAT32
# every 5 min. Log age checks read the RAM dir first for accurate results.

if [[ "$(get log_dir_exists)" == yes ]]; then
    _pass "Log directory exists: /var/log/otgw/${IMAGE_ID:-<image_id>}"
else
    _fail "Log directory /var/log/otgw/${IMAGE_ID:-?} missing — provision.sh may not have run"
fi

DS18B20_COUNT=$(get ds18b20_count)
DS18B20_IDS=$(get ds18b20_ids)
DS18B20_TEMPS=$(get ds18b20_temps)
if [[ "${DS18B20_COUNT:-0}" -gt 0 ]]; then
    _pass "DS18B20 sensors: ${DS18B20_COUNT} detected"
    # Show each sensor's temperature on its own line
    IFS=',' read -ra _sensor_entries <<< "$DS18B20_TEMPS"
    for _entry in "${_sensor_entries[@]}"; do
        _id="${_entry%%:*}"; _temp="${_entry##*:}"
        if [[ "$_temp" == "err" ]]; then
            echo -e "    ${YELLOW}⚠${NC}  ${_id}: read error"
        else
            echo "      ${_id}: ${_temp}°C"
        fi
    done
else
    _warn "DS18B20 sensors: none detected (connect sensor and check 1-Wire wiring)"
fi

chk_log() {
    local key="$1" label="$2" required="${3:-no}"
    local age; age=$(get "$key")
    if [[ "${age:--1}" -eq -1 ]]; then
        [[ "$required" == yes ]] \
            && _fail "$label: no log files found" \
            || _warn "$label: no log files yet (device not connected, or data still buffering)"
    elif [[ "$age" -le 2 ]]; then
        _pass "$label: active (last write ${age}h ago)"
    elif [[ "$age" -le 25 ]]; then
        _warn "$label: last write ${age}h ago — device connected and running?"
    else
        [[ "$required" == yes ]] \
            && _fail "$label: stale (${age}h since last write)" \
            || _warn "$label: stale (${age}h since last write)"
    fi
}

chk_log "hb_log_h"    "Heartbeat log"             yes
chk_log "otgw_log_h"  "OTGW data log"             no
chk_log "temp_log_h"  "Temperature log (DS18B20)" no
chk_log "gas_log_h"   "Gas/P1 log"                no

OTGW_HW=$(get otgw_hw)
case "$OTGW_HW" in
    connected) _pass "OTGW hardware: device connected" ;;
    retrying)  _warn "OTGW hardware: no device found (listener is retrying — connect OTGW USB)" ;;
    inactive)  _warn "OTGW hardware: otgw-listener not running" ;;
    *)         _warn "OTGW hardware: unknown state" ;;
esac

P1_MQTT=$(get p1_mqtt)
case "$P1_MQTT" in
    active) _pass "P1 MQTT: data flowing" ;;
    silent) _warn "P1 MQTT: no messages in last 3s (P1 dongle not connected or not sending)" ;;
    *)      _warn "P1 MQTT: check skipped" ;;
esac

# ── System Health ─────────────────────────────────────────────────────────

_head "System Health"

NTP=$(get ntp_sync)
if [[ "$FRESH" -eq 1 ]]; then
    [[ "$NTP" == yes ]] \
        && _fail "NTP: synchronized (unexpected — fresh Pi has no internet yet)" \
        || _pass "NTP: not synchronized (expected — no internet on fresh image)"
else
    [[ "$NTP" == yes ]] \
        && _pass "NTP: synchronized" \
        || _warn "NTP: not synchronized (clock may drift — check internet/WiFi)"
fi

MEM=$(get mem_pct)
if   [[ "${MEM:-0}" -ge 90 ]]; then _fail "Memory: ${MEM}% used"
elif [[ "${MEM:-0}" -ge 75 ]]; then _warn "Memory: ${MEM}% used (high)"
elif [[ -n "$MEM" ]];           then _pass "Memory: ${MEM}% used"
fi

UPGRADABLE=$(get upgradable)
if [[ "${UPGRADABLE:-0}" -eq 0 ]]; then
    _pass "Packages: all up to date"
else
    _warn "Packages: ${UPGRADABLE} upgradable — consider rebuilding the image"
fi

JOURNAL_KB=$(get journal_disk_kb)
if [[ "${JOURNAL_KB:-0}" -eq 0 ]]; then
    _pass "Journal: volatile only — /var/log/journal/ empty (Storage=volatile working)"
else
    human=$(awk "BEGIN{printf \"%.1fM\", ${JOURNAL_KB}/1024}")
    _fail "Journal: ${human} in /var/log/journal/ — Storage=volatile not effective (SD card wear risk)"
fi

MODE=$(get mode)
if [[ "$FRESH" -eq 1 ]]; then
    case "$MODE" in
        not-configured) _pass "Mode: not-configured (expected for fresh image)" ;;
        test)           _fail "Mode: test (unexpected — fresh image should be not-configured)" ;;
        production)     _fail "Mode: production (unexpected — fresh image should be not-configured)" ;;
        *)              _warn "Mode: ${MODE:-unknown}" ;;
    esac
else
    case "$MODE" in
        test)           _warn "Mode: TEST — data uses test/ MQTT prefix (expected during development)" ;;
        production)     _pass "Mode: production" ;;
        not-configured) _warn "Mode: not configured — run otgw-setup to set test or production" ;;
        *)              _warn "Mode: ${MODE:-unknown}" ;;
    esac
fi

# ── Network ───────────────────────────────────────────────────────────────

_head "Network"

WIFI_SSID=$(get wifi_ssid)
WIFI_IP=$(get wifi_ip)
if [[ "$FRESH" -eq 1 ]]; then
    if [[ -n "$WIFI_SSID" ]]; then
        _fail "WiFi: configured ('${WIFI_SSID}') — fresh image must not have wifi.conf"
    else
        _pass "WiFi: not configured (expected for fresh image)"
    fi
else
    if [[ -n "$WIFI_SSID" && -n "$WIFI_IP" ]]; then
        _pass "WiFi: connected to '${WIFI_SSID}' (IP: ${WIFI_IP})"
    elif [[ -n "$WIFI_SSID" ]]; then
        _warn "WiFi: associated with '${WIFI_SSID}' but no IP yet"
    else
        _warn "WiFi: not configured — run otgw-setup to add WiFi (required for log sync)"
    fi
fi

if [[ "$FRESH" -eq 1 ]]; then
    [[ "$(get internet)" == ok ]] \
        && _fail "Internet reachable (unexpected — fresh Pi should have no WiFi configured)" \
        || _pass "Internet unreachable (expected — no WiFi on fresh image)"
else
    [[ "$(get internet)" == ok ]] \
        && _pass "Internet reachable (ping 8.8.8.8)" \
        || _warn "Internet not reachable — WiFi may not be configured or connected"
fi

SC=$(get sync_count)
if [[ "$FRESH" -eq 1 ]]; then
    [[ "${SC:-0}" -gt 0 ]] \
        && _fail "Log sync: ${SC} target(s) configured (unexpected — fresh image must not have sync keys)" \
        || _pass "Log sync: no targets (expected for fresh image)"
else
    [[ "${SC:-0}" -gt 0 ]] \
        && _pass "Log sync: $SC target(s) configured" \
        || _warn "Log sync: no targets — run otgw-setup to add a VPS"
fi

# ── Verdict ───────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
if [[ "$FAILS" -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}  PASS${NC}${BOLD}  $PASSES checks passed, $WARNS warning(s)${NC}"
    VERDICT="PASS"
else
    echo -e "${RED}${BOLD}  FAIL${NC}${BOLD}  $FAILS failed, $PASSES passed, $WARNS warning(s)${NC}"
    VERDICT="FAIL"
fi
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

REPORT+=$'\n'"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"$'\n'
REPORT+="$VERDICT  passes=$PASSES fails=$FAILS warns=$WARNS"$'\n'

# ── Save report ───────────────────────────────────────────────────────────

mkdir -p "$REPORT_DIR"
REPORT_FILE="$REPORT_DIR/${IMAGE_ID:-unknown}-${TIMESTAMP}.txt"
printf '%s\n' "$REPORT" > "$REPORT_FILE"
echo "Report saved: $REPORT_FILE"

# ── Fleet log (YAML) ──────────────────────────────────────────────────────
# Append one YAML document per check to ~/.otgw/flash-log.yaml.
# YAML multi-document format (--- separator) means fields can be added in
# future without breaking reads of older records.

FLEET_LOG="${HOME}/.otgw/flash-log.yaml"
LAST_FLASH="${HOME}/.otgw/last-flash.env"
CHECK_ISO="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

if [[ -f "$LAST_FLASH" ]]; then
    # shellcheck source=/dev/null
    source "$LAST_FLASH"
fi

cat >> "$FLEET_LOG" << YAMLREC
---
flash_timestamp: "${FLASH_TIMESTAMP:-}"
image: "${FLASH_IMAGE:-}"
arch: "${FLASH_ARCH:-}"
flash_host: "${FLASH_HOST:-}"
image_id: "${IMAGE_ID:-unknown}"
pi_hostname: "$(get hostname)"
kernel: "$(get kernel)"
check_timestamp: "$CHECK_ISO"
check_mode: "$([[ "$FRESH" -eq 1 ]] && echo fresh || echo normal)"
result: $VERDICT
passes: $PASSES
fails: $FAILS
warns: $WARNS
ds18b20_count: ${DS18B20_COUNT:-0}
ds18b20_ids: "${DS18B20_IDS:-}"
code_version: "$(get code_version)"
YAMLREC

if [[ -f "$LAST_FLASH" ]]; then
    # Archive so a second check run doesn't duplicate flash fields
    mv "$LAST_FLASH" "${REPORT_DIR}/flash-${IMAGE_ID:-unknown}-${TIMESTAMP}.env"
fi

echo "Fleet log:   $FLEET_LOG"
echo ""

# ── Post-verdict notes ────────────────────────────────────────────────────
if [[ "$VERDICT" == "PASS" && "$FRESH" -eq 1 ]]; then
    echo -e "${GREEN}${BOLD}Image accepted.${NC} Configure this Pi via the portal at http://10.42.0.1/"
    echo    "  or use the hall sensor magnet after moving it to its final location."
    echo ""
elif [[ "$VERDICT" == "PASS" ]]; then
    echo -e "${YELLOW}${BOLD}⚠  Security reminder:${NC}"
    echo    "   If you added your admin key to the VPS for this installation visit,"
    echo    "   remove it now:"
    echo    "     ssh logbackup@vps1.otgw.org"
    echo    "     # delete the 'installer admin key' line from ~/.ssh/authorized_keys"
    echo    "   Pi sync keys are unaffected and keep working without the admin key."
    echo    ""
    echo    "   NOTE: the admin key is YOUR key (on your laptop), temporarily added"
    echo    "   to the VPS — it is NOT stored on the Pi.  Even if a resident extracts"
    echo    "   the Pi's own sync key (/etc/otgw/sync_key_primary), the VPS only"
    echo    "   permits write-only rsync to that Pi's own directory — no shell, no"
    echo    "   reads, no access to other Pis' data."
    echo ""
fi

[[ "$VERDICT" == "PASS" ]]
