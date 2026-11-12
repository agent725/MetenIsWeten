#!/bin/bash
#
# Device Setup — Generate device-id from ethernet MAC on every boot.
#
# Called by otgw-device-setup.service early in boot, before mqtt-logger,
# heartbeat, and log-sync start. Writes /etc/otgw/device-id (and pi-id if
# not already pre-baked) so all downstream services have a stable device
# identifier for this session.
#
# Why this runs every boot:
#   On prebake images, first-boot.sh is skipped (guard already written by
#   the QEMU prebake boot), so pi_id.sh inside first-boot.sh never runs on
#   the real Pi. This service fills that gap. See FSD §G.18 and NFR-4.23.
#
# Write path:
#   /etc/otgw/ is an overlay (p3 lowerdir, tmpfs upperdir). Writes go to
#   tmpfs and are ephemeral (wiped on reboot). That is fine — the MAC is
#   fixed hardware, so the same device-id is regenerated on every boot.
#   Persistent storage of device-id is not required.
#
# Exit codes:
#   0 — device-id written (or already present from this boot)
#   1 — no ethernet interface found; /etc/otgw/device-id not written
#

set -uo pipefail

DEVICE_ID_FILE="/etc/otgw/device-id"

# Already written this boot session (e.g. service restarted).
if [[ -f "$DEVICE_ID_FILE" ]]; then
    echo "device-id already set: $(cat "$DEVICE_ID_FILE")"
    exit 0
fi

# Delegate to pi_id.sh which handles MAC detection, device-id AND pi-id.
# pi_id.sh skips pi-id if it is already pre-baked (file exists); it always
# writes device-id from the MAC regardless.
PI_ID_SCRIPT="/opt/otgw/scripts/pi_id.sh"

if [[ ! -x "$PI_ID_SCRIPT" ]]; then
    echo "Error: $PI_ID_SCRIPT not found or not executable" >&2
    exit 1
fi

if "$PI_ID_SCRIPT"; then
    echo "device-id written: $(cat "$DEVICE_ID_FILE" 2>/dev/null || echo 'unknown')"
    exit 0
else
    echo "Warning: pi_id.sh failed (no ethernet?). device-id not set." >&2
    exit 1
fi
