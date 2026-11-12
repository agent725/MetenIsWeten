#!/bin/bash
#
# Pi ID Detection Script
#
# Reads the ethernet MAC address, extracts the last 3 bytes as a 6-character
# lowercase hex string, and saves it to /etc/otgw/pi-id.
#
# This provides a unique identifier for each Raspberry Pi, used in:
# - Log file names (gas_a1b2c3_2026-01-30.log)
# - MQTT topics (otgw/a1b2c3/energy/gas)
# - Remote sync paths (otgw-logs/a1b2c3/)
#
# Usage:
#   pi_id.sh              # Detect and save Pi ID (skips if already exists)
#   pi_id.sh --force      # Regenerate even if file exists
#   pi_id.sh --print      # Print current Pi ID (or detect if not saved)
#
# The ID is derived from the last 3 bytes of the ethernet MAC address.
# Example: MAC dc:a6:32:a1:b2:c3 -> Pi ID a1b2c3
#

set -euo pipefail

PI_ID_FILE="/etc/otgw/pi-id"
PI_ID_ENV_FILE="/etc/otgw/pi-id.env"
DEVICE_ID_FILE="/etc/otgw/device-id"
FORCE=false
PRINT_ONLY=false

# Overridable for testing
_IP_CMD="${_IP_CMD:-ip}"
_CPUINFO="${_CPUINFO:-/proc/cpuinfo}"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force|-f)
            FORCE=true
            shift
            ;;
        --print|-p)
            PRINT_ONLY=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [--force] [--print]"
            echo ""
            echo "Options:"
            echo "  --force, -f   Regenerate Pi ID even if file exists"
            echo "  --print, -p   Print current Pi ID and exit"
            echo "  --help, -h    Show this help"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# Detect ethernet MAC address
detect_mac() {
    local mac=""

    # Try common ethernet interface names
    for iface in eth0 end0 enp0s3; do
        if $_IP_CMD link show "$iface" &>/dev/null; then
            mac=$($_IP_CMD link show "$iface" | grep -oE 'link/ether [0-9a-f:]+' | awk '{print $2}')
            if [[ -n "$mac" ]]; then
                echo "$mac"
                return 0
            fi
        fi
    done

    # Fallback: first non-loopback, non-wireless interface
    for iface in $($_IP_CMD -o link show | awk -F': ' '{print $2}' | grep -v '^lo$' | grep -v '^wlan'); do
        mac=$($_IP_CMD link show "$iface" | grep -oE 'link/ether [0-9a-f:]+' | awk '{print $2}')
        if [[ -n "$mac" ]]; then
            echo "$mac"
            return 0
        fi
    done

    return 1
}

# Detect WiFi MAC address
detect_wifi_mac() {
    local mac=""
    for iface in wlan0 wlan1; do
        if $_IP_CMD link show "$iface" &>/dev/null; then
            mac=$($_IP_CMD link show "$iface" | grep -oE 'link/ether [0-9a-f:]+' | awk '{print $2}')
            if [[ -n "$mac" ]]; then
                echo "$mac"
                return 0
            fi
        fi
    done
    return 1
}

# Detect CPU serial number from /proc/cpuinfo (last 6 hex chars of Serial field)
detect_cpu_serial() {
    local serial
    serial=$(grep -m1 '^Serial' "$_CPUINFO" 2>/dev/null | awk '{print $3}')
    if [[ -n "$serial" && "${#serial}" -ge 6 ]]; then
        echo "${serial: -6}"
        return 0
    fi
    return 1
}

# Derive device ID using fallback chain: ethernet → WiFi → CPU serial
# Outputs "<id>:<source>" on success, exits 1 on failure.
detect_device_id() {
    local mac id

    if mac=$(detect_mac 2>/dev/null); then
        id=$(mac_to_image_id "$mac")
        echo "${id}:ethernet"
        return 0
    fi

    if mac=$(detect_wifi_mac 2>/dev/null); then
        id=$(mac_to_image_id "$mac")
        echo "${id}:wifi"
        return 0
    fi

    if id=$(detect_cpu_serial 2>/dev/null); then
        echo "${id}:cpuserial"
        return 0
    fi

    return 1
}

# Extract last 3 bytes from MAC address
# Input:  dc:a6:32:a1:b2:c3
# Output: a1b2c3
mac_to_image_id() {
    local mac="$1"
    echo "$mac" | awk -F: '{printf "%s%s%s", $4, $5, $6}'
}

# When sourced (for testing), stop here — functions above are available.
[[ "${BASH_SOURCE[0]}" != "${0}" ]] && return 0

# Print mode: show current ID or detect
if [[ "$PRINT_ONLY" == "true" ]]; then
    if [[ -f "$PI_ID_FILE" ]]; then
        cat "$PI_ID_FILE"
    else
        result=$(detect_device_id) || { echo "Error: no hardware ID source found" >&2; exit 1; }
        echo "${result%%:*}"
    fi
    exit 0
fi

# Detect device ID using fallback chain (always needed — written even if pi-id already set)
id_result=$(detect_device_id) || {
    echo "Error: could not derive device ID" >&2
    echo "No ethernet, WiFi, or CPU serial found" >&2
    exit 1
}

device_id="${id_result%%:*}"
id_source="${id_result##*:}"

echo "Device ID derived from: ${id_source} (${device_id})"

# Always write device-id (per-device unique identifier, derived from MAC).
# This is separate from pi-id which may hold the pre-baked Image ID (YGTTWW).
mkdir -p "$(dirname "$DEVICE_ID_FILE")"
printf '%s' "$device_id" > "$DEVICE_ID_FILE"
chmod 644 "$DEVICE_ID_FILE"
echo "Saved to: $DEVICE_ID_FILE"

# Skip pi-id if already set by build time (pre-baked image ID) and not --force
if [[ -f "$PI_ID_FILE" && "$FORCE" != "true" ]]; then
    echo "Image ID already set (pre-baked): $(cat "$PI_ID_FILE") — skipping pi-id write"
    exit 0
fi

# Legacy / dev mode: pi-id not pre-set, write MAC-derived ID
image_id="$device_id"
echo "Image ID: $image_id"

# Save to file
printf '%s' "$image_id" > "$PI_ID_FILE"
chmod 644 "$PI_ID_FILE"
echo "Saved to: $PI_ID_FILE"

# Generate env file for systemd EnvironmentFile
printf 'IMAGE_ID=%s\n' "$image_id" > "$PI_ID_ENV_FILE"
chmod 644 "$PI_ID_ENV_FILE"
echo "Saved to: $PI_ID_ENV_FILE"
