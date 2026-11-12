#!/usr/bin/env bash
# Deploy updated scripts to a running Pi and restart affected services.
#
# Usage:
#   ./deploy.sh [host]          # default: otgw-logger.local
#   ./deploy.sh 192.168.9.51
#
# Run from the otgw_pi repo root or the scripts/ directory.

set -euo pipefail

HOST="${1:-otgw-logger.local}"
PIUSER="otgw"
PASS="otgw"
REMOTE="/opt/otgw/scripts"

SCP="sshpass -p $PASS scp -o StrictHostKeyChecking=no"
SSH="sshpass -p $PASS ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 $PIUSER@$HOST"

# Locate the scripts/ directory regardless of where this script is called from
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ "$(basename "$SCRIPT_DIR")" == "scripts" ]]; then
    SRC="$SCRIPT_DIR"
else
    SRC="$SCRIPT_DIR/scripts"
fi

echo "=== Deploy otgw_pi scripts to $PIUSER@$HOST ==="
echo ""

# 1. Connectivity check
echo "[1/3] Checking SSH..."
$SSH "echo '    OK: connected as $(id -un) on $(hostname)'"

# 2. Copy scripts
echo "[2/3] Copying scripts..."
$SCP "$SRC/otgw_listener.py"    "$PIUSER@$HOST:$REMOTE/otgw_listener.py"
$SCP "$SRC/config_portal.py"    "$PIUSER@$HOST:$REMOTE/config_portal.py"
$SCP "$SRC/mqtt_logger.py"      "$PIUSER@$HOST:$REMOTE/mqtt_logger.py"
$SCP "$SRC/ds18b20_listener.py" "$PIUSER@$HOST:$REMOTE/ds18b20_listener.py"
$SCP "$SRC/heartbeat.py"        "$PIUSER@$HOST:$REMOTE/heartbeat.py"
$SCP "$SRC/p1_usb_sync.py"      "$PIUSER@$HOST:$REMOTE/p1_usb_sync.py"
echo "    Done"

# 3. Restart services
echo "[3/3] Restarting services..."
$SSH "sudo systemctl restart otgw-listener mqtt-logger ds18b20-listener"
sleep 2
$SSH "systemctl is-active otgw-listener mqtt-logger ds18b20-listener" | \
    paste - - - | \
    awk '{printf "    otgw-listener: %s  mqtt-logger: %s  ds18b20-listener: %s\n", $1, $2, $3}'

echo ""
echo "=== Deploy complete ==="
