#!/bin/bash
# Apply WiFi SSH iptables rule at boot from /etc/otgw/wifi-ssh state file.
# Default: disabled (blocked) for all Pis. WiFi SSH is opened automatically
# by config_sensor.py while the installer portal is active and closed again
# on portal exit — no persistent state change, no reboot required.
#
# To grant permanent WiFi SSH, use the portal toggle or TUI menu item, which
# writes "enabled" to /etc/otgw/wifi-ssh (p3, persistent across reboots).

WIFI_SSH_FILE=/etc/otgw/wifi-ssh

RULE_ARGS=(-A INPUT -i wlan+ -p tcp --dport 22 -m conntrack --ctstate NEW -j DROP)

if [[ ! -f "$WIFI_SSH_FILE" ]]; then
    printf 'disabled\n' > "$WIFI_SSH_FILE"
fi

state=$(tr -d '[:space:]' < "$WIFI_SSH_FILE" 2>/dev/null \
        | tr '[:upper:]' '[:lower:]')

if [[ "$state" == "enabled" ]]; then
    iptables -D INPUT -i wlan+ -p tcp --dport 22 \
        -m conntrack --ctstate NEW -j DROP 2>/dev/null || true
    echo "wifi-ssh-init: WiFi SSH enabled (DROP rule removed)"
else
    iptables -C INPUT -i wlan+ -p tcp --dport 22 \
        -m conntrack --ctstate NEW -j DROP 2>/dev/null \
        || iptables "${RULE_ARGS[@]}"
    echo "wifi-ssh-init: WiFi SSH disabled (DROP rule applied)"
fi
