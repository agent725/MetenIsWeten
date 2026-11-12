#!/bin/bash
#
# Log Sync Script - Synchronizes OTGW logs to one or more remote servers
#
# Configuration:
#   /etc/otgw/sync.d/*.conf   One file per sync target (preferred)
#   /etc/otgw/sync.conf       Single-target fallback (backward compatible)
#
# Each config file sets:
#   SYNC_REMOTE_HOST    Remote hostname (required)
#   SYNC_REMOTE_USER    Remote username (default: backup)
#   SYNC_REMOTE_PATH    Remote directory (default: otgw-logs)
#   SYNC_LOCAL_PATH     Local log directory (default: /var/log/otgw)
#   SYNC_SSH_KEY        SSH private key path (default: /etc/otgw/sync_key)
#
# Usage:
#   log_sync.sh                     # Sync to all configured targets
#   log_sync.sh --test              # Test all targets without syncing
#   log_sync.sh --verbose           # Verbose rsync output
#   log_sync.sh --target primary    # Sync to specific target only
#

# -u: error on unset variables; -o pipefail: catch pipe errors
# No -e: we handle errors per-target so one failure doesn't abort others
set -uo pipefail

SYNC_D_DIR="/etc/otgw/sync.d"
LEGACY_CONF="/etc/otgw/sync.conf"
LOCAL_BASE="/var/log/otgw"
PI_ID_FILE="/etc/otgw/pi-id"
DEVICE_ID_FILE="/etc/otgw/device-id"
MODE_FILE="/etc/otgw/mode"

# Parse arguments
TEST_MODE=false
VERBOSE=false
TARGET_FILTER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --test|-t)
            TEST_MODE=true; shift ;;
        --verbose|-v)
            VERBOSE=true; shift ;;
        --target)
            TARGET_FILTER="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--test] [--verbose] [--target NAME]"
            echo ""
            echo "Options:"
            echo "  --test, -t          Test connections without syncing"
            echo "  --verbose, -v       Verbose rsync output"
            echo "  --target NAME       Only sync to the named target"
            echo "  --help, -h          Show this help"
            exit 0 ;;
        *)
            echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Collect config files
CONFIG_FILES=()

if [[ -d "$SYNC_D_DIR" ]]; then
    while IFS= read -r -d '' f; do
        CONFIG_FILES+=("$f")
    done < <(find "$SYNC_D_DIR" -maxdepth 1 -name "*.conf" -print0 2>/dev/null | sort -z)
fi

# Fall back to legacy sync.conf if sync.d/ has no files
if [[ ${#CONFIG_FILES[@]} -eq 0 && -f "$LEGACY_CONF" ]]; then
    CONFIG_FILES=("$LEGACY_CONF")
fi

if [[ ${#CONFIG_FILES[@]} -eq 0 ]]; then
    echo "Log sync not configured. Run 'otgw-setup' to add a sync target."
    exit 0
fi

# Get Image ID (empty string if not set)
IMAGE_ID=""
if [[ -f "$PI_ID_FILE" ]]; then
    IMAGE_ID=$(cat "$PI_ID_FILE")
fi

# Get Device ID — MAC-derived short hex, unique per physical Pi.
# For pre-baked images this differs from IMAGE_ID (e.g. device=71e1af, image=YGTTWW).
# For legacy images they are the same value.
DEVICE_ID=""
if [[ -f "$DEVICE_ID_FILE" ]]; then
    DEVICE_ID=$(cat "$DEVICE_ID_FILE")
elif [[ -n "$IMAGE_ID" ]]; then
    DEVICE_ID="$IMAGE_ID"  # fallback: legacy Pi where both are the same
fi

# Get mode (production or test)
MODE="production"
if [[ -f "$MODE_FILE" ]]; then
    MODE=$(cat "$MODE_FILE")
fi

# sync_one_target CONF_FILE
# Sources the config, syncs (or tests) one target.
# Returns 0 on success, 1 on failure.
sync_one_target() {
    local conf_file="$1"
    local target_name
    target_name=$(basename "$conf_file" .conf)

    # Apply --target filter
    if [[ -n "$TARGET_FILTER" && "$target_name" != "$TARGET_FILTER" ]]; then
        return 0
    fi

    # Circuit breaker: check if target is skipped due to consecutive failures
    # /run is root-owned; use /tmp which is always writable by the otgw user.
    local FAILURE_DIR="/tmp/otgw-sync-failures"
    local FAIL_FILE="$FAILURE_DIR/${target_name}"
    local FAIL_SKIP_FILE="$FAILURE_DIR/${target_name}.skip_until"
    
    mkdir -p "$FAILURE_DIR"
    
    # Check if circuit is open (skip_until timestamp in future)
    if [[ -f "$FAIL_SKIP_FILE" ]]; then
        local skip_until
        skip_until=$(cat "$FAIL_SKIP_FILE" 2>/dev/null || echo "0")
        local now
        now=$(date +%s)
        if [[ "$skip_until" -gt "$now" ]]; then
            echo "$(date -Iseconds) SYNC_CIRCUIT_OPEN: [$target_name] skipping until $(date -d @$skip_until)"
            return 0
        fi
        # Circuit period ended, remove skip file
        rm -f "$FAIL_SKIP_FILE"
    fi

    # Reset to defaults before sourcing (prevents leaking between targets)
    local SYNC_REMOTE_HOST=""
    local SYNC_REMOTE_USER="logbackup"
    local SYNC_REMOTE_PATH="otgw-logs"
    local SYNC_LOCAL_PATH="$LOCAL_BASE"
    local SYNC_SSH_KEY="/etc/otgw/sync_key"

    # shellcheck source=/dev/null
    source "$conf_file"

    if [[ -z "$SYNC_REMOTE_HOST" ]]; then
        echo "[$target_name] Skipping: SYNC_REMOTE_HOST not set in $conf_file"
        return 0
    fi

    # Build paths.
    # Per-Pi rrsync mode: SYNC_REMOTE_PATH="." means the rrsync root on the
    # VPS is already scoped to this Pi's directory.  Send files directly to
    # the rrsync root ("." in test mode → "test/"); never append the Pi ID.
    #
    # Legacy shared-root mode: SYNC_REMOTE_PATH="otgw-logs" (or similar).
    # Append mode subdirectory and Pi ID so files land in the correct place.
    local LOCAL_PATH="$SYNC_LOCAL_PATH"
    local REMOTE_PATH
    if [[ "$SYNC_REMOTE_PATH" == "." || -z "$SYNC_REMOTE_PATH" ]]; then
        # Per-Pi rrsync: VPS root is already scoped to this image's directory.
        # Append device-id as a subdirectory so two Pis flashed from the same
        # image land in separate VPS directories (YGTTWW/71e1af/ vs YGTTWW/dc32ab/).
        if [[ "$MODE" == "test" ]]; then
            REMOTE_PATH="test"
        else
            REMOTE_PATH="${DEVICE_ID:-unknown}"
        fi
        if [[ -n "$IMAGE_ID" ]]; then
            LOCAL_PATH="${LOCAL_PATH}/${IMAGE_ID}"
        fi
    else
        # Legacy shared-root: append mode subdir and Image ID
        REMOTE_PATH="$SYNC_REMOTE_PATH"
        if [[ "$MODE" == "test" ]]; then
            REMOTE_PATH="${REMOTE_PATH}/test"
        fi
        if [[ -n "$IMAGE_ID" ]]; then
            LOCAL_PATH="${LOCAL_PATH}/${IMAGE_ID}"
            REMOTE_PATH="${REMOTE_PATH}/${IMAGE_ID}"
        fi
    fi

    if [[ ! -d "$LOCAL_PATH" ]]; then
        echo "[$target_name] Error: local path does not exist: $LOCAL_PATH" >&2
        return 1
    fi

    if [[ ! -f "$SYNC_SSH_KEY" ]]; then
        echo "[$target_name] Error: SSH key not found: $SYNC_SSH_KEY" >&2
        echo "[$target_name] Generate with: ssh-keygen -t ed25519 -f $SYNC_SSH_KEY -N ''" >&2
        return 1
    fi

    # UserKnownHostsFile must point to a writable path. /root/.ssh/ is on the
    # overlayroot read-only layer — SSH cannot create it or write known_hosts.
    # /etc/otgw/ writes fail at runtime because the /etc/otgw overlay upperdir
    # is a tmpfs that is not writable via the overlay mount path. /tmp/ is
    # always writable (tmpfs, no overlay complications). The key is not
    # persistent across reboots but StrictHostKeyChecking=accept-new means the
    # host is re-accepted cleanly each boot with no error. See NFR-4.21 / §G.16.
    local SSH_CMD="ssh -i ${SYNC_SSH_KEY} -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=30 -o UserKnownHostsFile=/tmp/known_hosts_vps"

    if [[ "$TEST_MODE" == "true" ]]; then
        echo "[$target_name] Testing ${SYNC_REMOTE_USER}@${SYNC_REMOTE_HOST}..."
        # Use a dry-run rsync to test the connection — plain SSH shell commands
        # are blocked when the key is restricted by rrsync.
        if rsync -az --dry-run --timeout=30 \
            -e "$SSH_CMD" \
            "${LOCAL_PATH}/" \
            "${SYNC_REMOTE_USER}@${SYNC_REMOTE_HOST}:${REMOTE_PATH}/" \
            2>&1 | head -5; then
            echo "[$target_name] OK"
            return 0
        else
            echo "[$target_name] FAILED" >&2
            return 1
        fi
    fi

    # Build rsync options
    local RSYNC_OPTS="-az --partial --timeout=60"
    if [[ "$VERBOSE" == "true" ]]; then
        RSYNC_OPTS="$RSYNC_OPTS -v --progress"
    fi

    # Fetch do_not_sync.list from VPS before syncing.
    # The list (written by compress_logs.sh) names files the VPS has already
    # compressed; passing it as --exclude-from prevents the Pi from re-uploading
    # originals that were removed after compression.  The list is append-only on
    # the VPS, so archived files stay excluded even after their .gz is deleted.
    # Failure is non-fatal: if the fetch fails (old key format, network, VPS down)
    # we log a note and sync everything — no data is ever permanently excluded by
    # a transient error.
    local EXCLUDE_FILE="/tmp/do_not_sync_${target_name}.list"
    rm -f "$EXCLUDE_FILE"
    if ssh -i "${SYNC_SSH_KEY}" \
           -o StrictHostKeyChecking=accept-new \
           -o BatchMode=yes \
           -o ConnectTimeout=10 \
           -o UserKnownHostsFile=/tmp/known_hosts_vps \
           "${SYNC_REMOTE_USER}@${SYNC_REMOTE_HOST}" \
           "cat do_not_sync.list" > "$EXCLUDE_FILE" 2>/dev/null; then
        if [[ -s "$EXCLUDE_FILE" ]]; then
            RSYNC_OPTS="$RSYNC_OPTS --exclude-from=${EXCLUDE_FILE}"
        fi
    else
        echo "$(date -Iseconds) [$target_name] NOTE: do_not_sync.list unavailable; syncing all files"
        rm -f "$EXCLUDE_FILE"
    fi

    echo "$(date -Iseconds) [$target_name] Syncing to ${SYNC_REMOTE_USER}@${SYNC_REMOTE_HOST}:${REMOTE_PATH}"

    # shellcheck disable=SC2086
    if rsync $RSYNC_OPTS \
        -e "$SSH_CMD" \
        "${LOCAL_PATH}/" \
        "${SYNC_REMOTE_USER}@${SYNC_REMOTE_HOST}:${REMOTE_PATH}/"; then
        echo "$(date -Iseconds) [$target_name] Sync completed successfully"
        
        # Circuit breaker: reset failure counter on success
        if [[ -f "$FAIL_FILE" ]]; then
            echo "$(date -Iseconds) SYNC_CIRCUIT_RESET: [$target_name]"
        fi
        rm -f "$FAIL_FILE" "$FAIL_SKIP_FILE"
        rm -f "$EXCLUDE_FILE"
        return 0
    else
        echo "$(date -Iseconds) [$target_name] Sync FAILED" >&2
        
        # Circuit breaker: increment failure counter
        local fail_count=0
        if [[ -f "$FAIL_FILE" ]]; then
            fail_count=$(cat "$FAIL_FILE" 2>/dev/null || echo "0")
        fi
        fail_count=$((fail_count + 1))
        echo "$fail_count" > "$FAIL_FILE"
        
        if [[ "$fail_count" -ge 10 ]]; then
            local skip_until
            skip_until=$(( $(date +%s) + 3300 ))  # 55 minutes
            echo "$skip_until" > "$FAIL_SKIP_FILE"
            echo "$(date -Iseconds) SYNC_CIRCUIT_OPEN: [$target_name] $fail_count consecutive failures — next retry $(date -d @$skip_until)"
        fi
        
        rm -f "$EXCLUDE_FILE"
        return 1
    fi
}

# Sync to all targets; track overall result
OVERALL=0
SYNCED=0

for conf in "${CONFIG_FILES[@]}"; do
    if sync_one_target "$conf"; then
        SYNCED=$((SYNCED + 1))
    else
        OVERALL=1
    fi
done

if [[ $OVERALL -ne 0 ]]; then
    echo "One or more sync targets failed." >&2
fi

exit $OVERALL
