#!/bin/bash
# check_stale_pis.sh — alert on stale or unhealthy Pis
# Cron: */30 * * * * /home/backup/check_stale_pis.sh

set -euo pipefail

LOG_ROOT="/home/backup/otgw-logs"
ALERT_CONFIG="/home/backup/.alert-config"
STATE_DIR="/home/backup/alert-state"
STALE_THRESHOLD_H=6

# Source alert config
if [[ -f "$ALERT_CONFIG" ]]; then
    source "$ALERT_CONFIG"
else
    echo "No alert config found at $ALERT_CONFIG" >&2
    exit 0
fi

# Validate required config
if [[ -z "${ALERT_EMAIL:-}" ]]; then
    echo "ALERT_EMAIL not set in $ALERT_CONFIG" >&2
    exit 1
fi

# Ensure state directory exists
mkdir -p "$STATE_DIR"

now=$(date +%s)

# Helper function to send alerts with deduplication
_send_alert_once() {
    local pi_id="$1"
    local condition="$2"
    local message="$3"
    local state_file="$STATE_DIR/$pi_id/$condition"
    
    mkdir -p "$(dirname "$state_file")"
    
    # Check if alert was sent in the last 24 hours
    if [[ -f "$state_file" ]]; then
        last_alert=$(cat "$state_file" 2>/dev/null || echo "0")
        if (( now - last_alert < 86400 )); then
            return 0
        fi
    fi
    
    # Send email alert
    echo "$message" | mail -s "OTGW Alert: $pi_id — $condition" "$ALERT_EMAIL"
    
    # Update state file
    echo "$now" > "$state_file"
}

# Iterate over heartbeat files
for hb_file in "$LOG_ROOT"/*/heartbeat.json "$LOG_ROOT"/*/*/heartbeat.json; do
    [[ -f "$hb_file" ]] || continue
    
    pi_id=$(basename "$(dirname "$hb_file")")
    
    # Check file age
    mtime=$(stat -c %Y "$hb_file" 2>/dev/null) || continue
    age_h=$(( (now - mtime) / 3600 ))
    
    if (( age_h >= STALE_THRESHOLD_H )); then
        _send_alert_once "$pi_id" "stale_${STALE_THRESHOLD_H}h" \
            "Pi $pi_id has not synced for ${age_h}h (last: $(date -d @$mtime))"
    fi
    
    # Check heartbeat content (services, disk)
    if command -v jq >/dev/null 2>&1; then
        mosquitto_status=$(jq -r '.services.mosquitto // "unknown"' "$hb_file" 2>/dev/null || echo "unknown")
        if [[ "$mosquitto_status" != "active" && "$mosquitto_status" != "unknown" ]]; then
            _send_alert_once "$pi_id" "mosquitto_${mosquitto_status}" \
                "Pi $pi_id: mosquitto is $mosquitto_status"
        fi
        
        # Check log disk usage (once PLAN-2 is implemented)
        log_disk_pct=$(jq -r '.log_disk_usage_pct // 0' "$hb_file" 2>/dev/null || echo "0")
        if (( log_disk_pct > 90 )); then
            _send_alert_once "$pi_id" "log_disk_high" \
                "Pi $pi_id: log disk usage is ${log_disk_pct}%"
        fi
        
        # Check sync circuit (once PLAN-7 is implemented)
        sync_circuit_open=$(jq -r '.sync_circuit_open // false' "$hb_file" 2>/dev/null || echo "false")
        if [[ "$sync_circuit_open" == "true" ]]; then
            _send_alert_once "$pi_id" "sync_circuit_open" \
                "Pi $pi_id: sync circuit is open"
        fi
    fi
done
