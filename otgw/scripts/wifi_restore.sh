#!/bin/bash
# wifi_restore.sh — recreate the home WiFi NM connection from /etc/otgw/wifi.conf
#
# Runs at boot (via otgw-wifi-restore.service) after NetworkManager starts.
# With the p3 initramfs fix (NFR-4.67), /etc/otgw is persistent across reboots
# but NM stores its connection files in the tmpfs root overlay — they are lost
# on every reboot.  This script reads wifi.conf from p3 and recreates the NM
# connection so that WiFi reconnects automatically without re-running the portal.
#
# No-op conditions (exits 0 quietly):
#   - /etc/otgw/wifi.conf does not exist (WiFi not configured yet)
#   - SSID field is empty
#   - An NM connection with that name already exists

WIFI_CONF="/etc/otgw/wifi.conf"

[ -f "$WIFI_CONF" ] || exit 0

# Parse key="value" pairs from wifi.conf
SSID=""
PASSWORD=""
while IFS='=' read -r key val; do
    val="${val#\"}"   # strip leading "
    val="${val%\"}"   # strip trailing "
    key="${key%%[[:space:]]*}"  # strip trailing whitespace
    case "$key" in
        SSID)     SSID="$val" ;;
        PASSWORD) PASSWORD="$val" ;;
    esac
done < "$WIFI_CONF"

[ -z "$SSID" ] && exit 0

# If the connection already exists in NM, nothing to do.
if nmcli -t -f NAME connection show 2>/dev/null | grep -qxF "$SSID"; then
    exit 0
fi

echo "wifi_restore: creating NM connection for '$SSID'"
nmcli connection add \
    type wifi \
    ifname wlan0 \
    con-name "$SSID" \
    ssid "$SSID" \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$PASSWORD" \
    ipv4.method auto \
    ipv6.method auto \
    connection.autoconnect yes

echo "wifi_restore: activating '$SSID'"
nmcli connection up "$SSID" || true
