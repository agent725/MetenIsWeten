#!/usr/bin/env bash
# pi-log-gateway.sh BASE_DIR
#
# SSH forced-command gateway for Pi sync keys.  Replaces the bare
# `rrsync -wo BASE_DIR` forced command so the Pi can also read
# do_not_sync.list without gaining any other access.
#
# Authorised commands (matched against SSH_ORIGINAL_COMMAND):
#   rsync --server ...     → delegated to rrsync -wo BASE_DIR/ (write-only)
#   cat do_not_sync.list   → outputs BASE_DIR/do_not_sync.list (read-only)
#
# Everything else is rejected with exit 1.
#
# Install on VPS:
#   mkdir -p /home/logbackup/bin
#   cp pi-log-gateway.sh /home/logbackup/bin/
#   chmod 755 /home/logbackup/bin/pi-log-gateway.sh
#
# authorized_keys entry format (all on one line):
#   command="/home/logbackup/bin/pi-log-gateway.sh /home/logbackup/otgw-logs/IMAGE_ID/",
#   no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty,no-user-rc PI_PUBKEY
#
# register_pi.sh generates this entry automatically.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: pi-log-gateway.sh BASE_DIR" >&2
    exit 1
fi

BASE_DIR="${1%/}"   # strip trailing slash for consistent path joins
CMD="${SSH_ORIGINAL_COMMAND:-}"

case "$CMD" in
    rsync\ --server*)
        exec rrsync -wo "${BASE_DIR}/"
        ;;
    "cat do_not_sync.list")
        list_file="${BASE_DIR}/do_not_sync.list"
        [[ -f "$list_file" ]] && cat "$list_file"
        exit 0
        ;;
    *)
        echo "pi-log-gateway: command not permitted: ${CMD:-<empty>}" >&2
        exit 1
        ;;
esac
