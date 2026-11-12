#!/bin/bash
# deploy-vps.sh — deploy VPS-side components for OTGW monitoring

set -euo pipefail

# Ensure scripts directory exists
mkdir -p "$HOME/otgw-logs"
mkdir -p "$HOME/alert-state"

# Install check_stale_pis.sh
cp "$(dirname "$0")/check_stale_pis.sh" "$HOME/check_stale_pis.sh"
chmod +x "$HOME/check_stale_pis.sh"

# Install cron entry for stale Pi monitoring
echo "Installing cron entry for check_stale_pis.sh..."
(crontab -l 2>/dev/null || true; echo "*/30 * * * * $HOME/check_stale_pis.sh") | crontab -

# Create example alert config
ALERT_CONFIG="$HOME/.alert-config"
if [[ ! -f "$ALERT_CONFIG" ]]; then
    cat > "$ALERT_CONFIG" <<EOF
# OTGW Alert Configuration
# Set the email address to receive alerts
ALERT_EMAIL="your-email@example.com"
EOF
    echo "Created example alert config at $ALERT_CONFIG"
    echo "Please edit $ALERT_CONFIG to set your email address."
fi

echo "VPS deployment complete. Please verify:"
echo "1. Edit $ALERT_CONFIG to set ALERT_EMAIL"
echo "2. Verify cron entry: crontab -l"
