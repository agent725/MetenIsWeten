#!/bin/bash
# NetworkManager dispatcher: fire an immediate heartbeat when connectivity is
# established.  Installed at /etc/NetworkManager/dispatcher.d/99-otgw-heartbeat
#
# Called by NM with args: INTERFACE ACTION
# We trigger on 'connectivity-change' when connectivity is 'full'.

INTERFACE="$1"
ACTION="$2"

# Only act on connectivity events, not every interface state change
if [[ "$ACTION" != "connectivity-change" ]]; then
    exit 0
fi

CONNECTIVITY=$(nmcli -t -f CONNECTIVITY general 2>/dev/null | head -1)
if [[ "$CONNECTIVITY" == "full" ]]; then
    # Trigger heartbeat without waiting for the 15-minute timer
    systemctl start heartbeat.service 2>/dev/null || true
fi
