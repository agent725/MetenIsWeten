#!/bin/bash
#
# OTGW Logger Unprovisioning Script
#
# Reverses everything done by provision.sh:
#   - Stops and disables all OTGW services
#   - Removes systemd unit files
#   - Removes installed files and configuration
#   - Optionally removes packages and log data
#
# Usage:
#   sudo ./unprovision.sh           # Remove services, files, configs
#   sudo ./unprovision.sh --check   # Show what would be removed
#   sudo ./unprovision.sh --purge   # Also remove packages and log data
#

set -euo pipefail

# Configuration (must match provision.sh)
INSTALL_DIR="/opt/otgw"
CONFIG_DIR="/etc/otgw"
LOG_DIR="/var/log/otgw"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)"
        exit 1
    fi
}

# Parse arguments
CHECK_ONLY=false
PURGE=false
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=true ;;
        --purge) PURGE=true ;;
        *)
            error "Unknown option: $arg"
            echo "Usage: sudo ./unprovision.sh [--check] [--purge]"
            exit 1
            ;;
    esac
done

if $CHECK_ONLY; then
    info "Check mode - no changes will be made"
fi

# ============================================================================
# Stop and Disable Services
# ============================================================================

remove_services() {
    info "Stopping and disabling services..."

    local services=(
        otgw-listener
        ds18b20-listener
        mqtt-logger
        resident-portal
        log-sync.timer
        log-sync
    )

    if $CHECK_ONLY; then
        info "Would stop and disable: ${services[*]}"
        return
    fi

    for svc in "${services[@]}"; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            systemctl stop "$svc"
            info "Stopped $svc"
        fi
        if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
            systemctl disable "$svc"
            info "Disabled $svc"
        fi
    done

    # Remove unit files
    local unit_files=(
        /etc/systemd/system/otgw-listener.service
        /etc/systemd/system/ds18b20-listener.service
        /etc/systemd/system/mqtt-logger.service
        /etc/systemd/system/resident-portal.service
        /etc/systemd/system/log-sync.service
        /etc/systemd/system/log-sync.timer
    )

    for f in "${unit_files[@]}"; do
        if [[ -f "$f" ]]; then
            rm "$f"
            info "Removed $f"
        fi
    done

    systemctl daemon-reload
    info "Services removed"
}

# ============================================================================
# Stop Mosquitto
# ============================================================================

stop_mosquitto() {
    info "Stopping mosquitto..."

    if $CHECK_ONLY; then
        info "Would stop and disable mosquitto"
        return
    fi

    if systemctl is-active --quiet mosquitto 2>/dev/null; then
        systemctl stop mosquitto
        info "Stopped mosquitto"
    fi
    if systemctl is-enabled --quiet mosquitto 2>/dev/null; then
        systemctl disable mosquitto
        info "Disabled mosquitto"
    fi
}

# ============================================================================
# Remove Configuration Files
# ============================================================================

remove_configs() {
    info "Removing configuration files..."

    local config_files=(
        /etc/mosquitto/conf.d/otgw.conf
        /etc/systemd/journald.conf.d/otgw.conf
        /etc/ssh/sshd_config.d/ethernet-only.conf
    )

    if $CHECK_ONLY; then
        for f in "${config_files[@]}"; do
            [[ -f "$f" ]] && info "Would remove $f"
        done
        return
    fi

    for f in "${config_files[@]}"; do
        if [[ -f "$f" ]]; then
            rm "$f"
            info "Removed $f"
        fi
    done

    # Reload journald after removing its config
    if systemctl is-active --quiet systemd-journald 2>/dev/null; then
        systemctl restart systemd-journald
        info "Restarted journald with default settings"
    fi
}

# ============================================================================
# Remove Installed Files
# ============================================================================

remove_files() {
    info "Removing installed files..."

    if $CHECK_ONLY; then
        [[ -d "$INSTALL_DIR" ]] && info "Would remove $INSTALL_DIR"
        [[ -L /usr/local/bin/otgw-setup ]] && info "Would remove /usr/local/bin/otgw-setup"
        return
    fi

    if [[ -d "$INSTALL_DIR" ]]; then
        rm -rf "$INSTALL_DIR"
        info "Removed $INSTALL_DIR"
    fi

    if [[ -L /usr/local/bin/otgw-setup ]]; then
        rm /usr/local/bin/otgw-setup
        info "Removed /usr/local/bin/otgw-setup"
    fi
}

# ============================================================================
# Remove Packages (only with --purge)
# ============================================================================

remove_packages() {
    if ! $PURGE; then
        info "Skipping package removal (use --purge to remove packages)"
        return
    fi

    info "Removing packages installed by provision.sh..."

    local packages=(
        mosquitto
        mosquitto-clients
        python3-paho-mqtt
        python3-serial
    )

    if $CHECK_ONLY; then
        info "Would remove: ${packages[*]}"
        return
    fi

    apt-get remove -y "${packages[@]}"
    apt-get autoremove -y
    info "Packages removed"
}

# ============================================================================
# Remove Log Data (only with --purge)
# ============================================================================

remove_logs() {
    if ! $PURGE; then
        if [[ -d "$LOG_DIR" ]]; then
            warn "Log data preserved in $LOG_DIR (use --purge to remove)"
        fi
        return
    fi

    info "Removing log data..."

    if $CHECK_ONLY; then
        [[ -d "$LOG_DIR" ]] && info "Would remove $LOG_DIR"
        [[ -d "$CONFIG_DIR" ]] && info "Would remove $CONFIG_DIR"
        return
    fi

    if [[ -d "$LOG_DIR" ]]; then
        rm -rf "$LOG_DIR"
        info "Removed $LOG_DIR"
    fi

    if [[ -d "$CONFIG_DIR" ]]; then
        rm -rf "$CONFIG_DIR"
        info "Removed $CONFIG_DIR"
    fi
}

# ============================================================================
# Main
# ============================================================================

main() {
    echo "=============================================="
    echo "  OTGW Logger Unprovisioning Script"
    echo "=============================================="
    echo ""

    check_root

    remove_services
    stop_mosquitto
    remove_configs
    remove_files
    remove_packages
    remove_logs

    if ! $CHECK_ONLY; then
        echo ""
        echo "=============================================="
        echo "  Unprovisioning Complete"
        echo "=============================================="
        echo ""
        if $PURGE; then
            info "All OTGW components, packages, and data removed."
        else
            info "OTGW services, files, and configs removed."
            [[ -d "$LOG_DIR" ]] && warn "Log data preserved in $LOG_DIR"
            [[ -d "$CONFIG_DIR" ]] && warn "Config data preserved in $CONFIG_DIR"
            echo ""
            echo "To also remove packages and data:"
            echo "  sudo ./unprovision.sh --purge"
        fi
        echo ""
        echo "Note: hostname was NOT reverted. Change manually if needed:"
        echo "  sudo hostnamectl set-hostname <your-hostname>"
        echo ""
        echo "Note: 1-Wire dtoverlay in boot config was NOT removed."
        echo "  Remove 'dtoverlay=w1-gpio' from /boot/firmware/config.txt if desired."
        echo ""
    else
        echo ""
        info "Check complete. Run without --check to apply changes."
    fi
}

main "$@"
