#!/bin/bash
#
# OTGW Logger Provisioning Script
#
# This script provisions a fresh Raspberry Pi OS installation with all
# required packages and configuration for the OTGW logging system.
#
# Usage:
#   sudo ./provision.sh           # Full installation
#   sudo ./provision.sh --check   # Check what would be installed
#
# Requirements:
#   - Raspberry Pi OS Lite (Trixie or newer)
#   - Internet connection
#   - Root privileges (sudo)
#

set -euo pipefail

# Configuration
INSTALL_DIR="/opt/otgw"
CONFIG_DIR="/etc/otgw"
LOG_DIR="/var/log/otgw"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Logging functions
info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

# Check if running as root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "This script must be run as root (use sudo)"
        exit 1
    fi
}

# Parse arguments
CHECK_ONLY=false
UPGRADE=false

for arg in "$@"; do
    case "$arg" in
        --check)
            CHECK_ONLY=true
            info "Check mode - no changes will be made"
            ;;
        --upgrade)
            UPGRADE=true
            warn "Upgrade mode: will run apt-get upgrade (may change package versions)"
            ;;
        --help|-h)
            echo "Usage: $0 [--check] [--upgrade]"
            echo ""
            echo "  --check    Dry run: show what would be installed/changed"
            echo "  --upgrade  Also run apt-get upgrade (not recommended for"
            echo "             deployed Pis; test on a spare Pi first)"
            exit 0
            ;;
        *)
            error "Unknown argument: $arg"
            exit 1
            ;;
    esac
done

# ============================================================================
# Package Installation
# ============================================================================

install_packages() {
    info "Installing required packages..."

    local packages=(
        # MQTT broker and tools
        mosquitto
        mosquitto-clients

        # Python and libraries
        python3
        python3-pip
        python3-paho-mqtt
        python3-serial
        python3-lgpio

        # System utilities
        rsync
        avahi-daemon
        dosfstools      # mkfs.vfat — for FAT32 log partition setup
        parted          # partition management — for FAT32 log partition setup

        # Optional but useful
        git
        vim-tiny

        # Firewall — restores iptables rules across reboots
        iptables-persistent
    )

    if $CHECK_ONLY; then
        info "Would install: ${packages[*]}"
        return
    fi

    apt-get update
    apt-get install -y "${packages[@]}"

    # esptool: install via pip — the Debian package is incomplete (missing stub flasher files).
    # The pip version (5.x) is fully functional and needed for TUI P1 dongle firmware flash.
    if ! python3 -c "import esptool" &>/dev/null; then
        info "Installing esptool via pip..."
        pip3 install --break-system-packages esptool
    fi

    info "Packages installed successfully"
}

# ============================================================================
# System Package Upgrade (optional, not run by default)
# ============================================================================

upgrade_packages() {
    if ! $UPGRADE; then
        return
    fi

    warn "Running apt-get upgrade — this may change package versions."
    warn "Only do this on a tested image or spare Pi first."

    if $CHECK_ONLY; then
        info "Would run: apt-get upgrade --dry-run"
        apt-get upgrade --dry-run
        return
    fi

    apt-get upgrade -y
    info "System packages upgraded"
}

# ============================================================================
# Package Snapshot
# ============================================================================

save_package_snapshot() {
    local snapshot_file="$CONFIG_DIR/packages-snapshot.txt"

    if $CHECK_ONLY; then
        info "Would save package snapshot to $snapshot_file"
        return
    fi

    info "Saving package snapshot to $snapshot_file..."
    {
        echo "# OTGW package snapshot"
        echo "# Generated: $(date -Iseconds)"
        echo "# Host: $(hostname)"
        echo "#"
        dpkg -l
    } > "$snapshot_file"
    chmod 644 "$snapshot_file"
    info "Package snapshot saved"
}

# ============================================================================
# Ensure otgw user exists
# ============================================================================
#
# The image build (build.sh) creates the otgw user via a 3-layer safety net.
# When provision.sh is run manually on a plain Raspberry Pi OS image (no
# pre-baked user), this function ensures the user is created the same way.
# Idempotent: does nothing if the user already exists.

ensure_otgw_user() {
    info "Checking otgw user..."

    if $CHECK_ONLY; then
        if id otgw &>/dev/null; then
            info "otgw user already exists (uid=$(id -u otgw))"
        else
            info "Would create otgw user with standard groups and NOPASSWD sudo"
        fi
        return
    fi

    if id otgw &>/dev/null; then
        info "otgw user already exists (uid=$(id -u otgw))"
        return
    fi

    info "Creating otgw user..."
    useradd -m -s /bin/bash otgw
    echo "otgw:otgw" | chpasswd

    # Add to all standard Raspberry Pi groups (skip any that don't exist)
    for grp in sudo adm dialout cdrom audio video plugdev games users \
                input render netdev gpio i2c spi; do
        getent group "$grp" &>/dev/null && usermod -aG "$grp" otgw || true
    done

    # Grant passwordless sudo — same as the image build safety net
    echo "otgw ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/otgw
    chmod 440 /etc/sudoers.d/otgw

    info "otgw user created"
}

# ============================================================================
# Directory Setup
# ============================================================================

setup_directories() {
    info "Setting up directories..."

    if $CHECK_ONLY; then
        info "Would create: $INSTALL_DIR, $CONFIG_DIR, $LOG_DIR"
        info "Would set $INSTALL_DIR and $CONFIG_DIR owner to otgw:otgw"
        return
    fi

    mkdir -p "$INSTALL_DIR"
    mkdir -p "$CONFIG_DIR"
    mkdir -p "$LOG_DIR"

    # Set permissions
    chmod 755 "$INSTALL_DIR"
    chmod 755 "$CONFIG_DIR"
    chmod 755 "$LOG_DIR"

    # Hand both directories to the otgw user:
    # - /opt/otgw: so code can be updated via scp without sudo
    # - /etc/otgw: so the setup TUI can write config files without sudo
    chown -R otgw:otgw "$INSTALL_DIR"
    chown -R otgw:otgw "$CONFIG_DIR"

    info "Directories created"
}

# ============================================================================
# Copy Files
# ============================================================================

copy_files() {
    info "Copying project files..."

    if $CHECK_ONLY; then
        info "Would copy scripts, systemd units, and configs to $INSTALL_DIR"
        return
    fi

    # Copy scripts
    cp -r "$PROJECT_DIR/scripts" "$INSTALL_DIR/"
    chmod +x "$INSTALL_DIR/scripts/"*.py 2>/dev/null || true
    chmod +x "$INSTALL_DIR/scripts/"*.sh 2>/dev/null || true

    # Write code version (git commit at install time) to /etc/otgw/version.
    # heartbeat.py and check_pi.sh read this for asset tracking.
    _git_commit=$(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null \
        || echo "unknown")
    echo "$_git_commit" > "$CONFIG_DIR/version"
    info "Code version: $_git_commit → $CONFIG_DIR/version"

    # Copy TUI
    cp -r "$PROJECT_DIR/tui" "$INSTALL_DIR/"
    chmod +x "$INSTALL_DIR/tui/"*.py 2>/dev/null || true

    # Create wrapper script for easy access.
    # A wrapper (not a symlink) is needed so that PYTHONPATH is set correctly —
    # otgw_setup.py imports from the tui/ package inside /opt/otgw.
    cat > /usr/local/bin/otgw-setup << 'WRAPPER'
#!/bin/bash
PYTHONPATH=/opt/otgw exec /usr/bin/python3 /opt/otgw/tui/otgw_setup.py "$@"
WRAPPER
    chmod +x /usr/local/bin/otgw-setup

    info "Files copied to $INSTALL_DIR"
}

# ============================================================================
# Systemd Services
# ============================================================================

install_services() {
    info "Installing systemd services..."

    if $CHECK_ONLY; then
        info "Would install systemd services and timers"
        return
    fi

    # Copy service files
    cp "$PROJECT_DIR/systemd/"*.service /etc/systemd/system/
    cp "$PROJECT_DIR/systemd/"*.timer /etc/systemd/system/

    # Reload systemd
    systemctl daemon-reload

    # Enable services (but don't start yet - need configuration first)
    systemctl enable mosquitto
    systemctl enable ds18b20-listener
    systemctl enable otgw-listener
    systemctl enable mqtt-logger
    systemctl enable log-flush.timer
    systemctl enable log-sync.timer
    systemctl enable wifi-switch
    systemctl enable config-sensor
    systemctl enable config-portal
    systemctl enable led-status
    systemctl enable time-sync
    # Simulator units: installed but NOT enabled (enabled manually via TUI)
    # Units are already copied by the cp above; no systemctl enable here.

    info "Systemd services installed and enabled"
}

# ============================================================================
# System Configuration
# ============================================================================

configure_system() {
    info "Configuring system..."

    if $CHECK_ONLY; then
        info "Would configure mosquitto, journald, SSH, and 1-Wire"
        return
    fi

    # Remove the Raspberry Pi OS "SSH may not work" banner — it is shown until
    # the first-boot user wizard runs, which we bypass entirely with our own
    # user setup. The file is safe to delete; it has no other purpose.
    if [[ -f /etc/ssh/sshd_config.d/rename_user.conf ]]; then
        rm /etc/ssh/sshd_config.d/rename_user.conf
        systemctl restart ssh || true
        info "Removed SSH banner (rename_user.conf)"
    fi

    # Mosquitto configuration
    if [[ -d /etc/mosquitto/conf.d ]]; then
        cp "$PROJECT_DIR/config/mosquitto.conf" /etc/mosquitto/conf.d/otgw.conf
        info "Mosquitto configured"
    fi

    # Journald configuration (reduce SD card writes)
    mkdir -p /etc/systemd/journald.conf.d
    cp "$PROJECT_DIR/config/journald.conf" /etc/systemd/journald.conf.d/otgw.conf
    info "Journald configured for volatile storage"

    # SSH configuration
    mkdir -p /etc/ssh/sshd_config.d
    cp "$PROJECT_DIR/config/sshd_config.d/ethernet-only.conf" /etc/ssh/sshd_config.d/
    cp "$PROJECT_DIR/config/sshd_banner" /etc/ssh/otgw_banner
    info "SSH configured"

    # Disable UART serial console to free GPIO 14 (TXD) and GPIO 15 (RXD)
    # for general use (config button on GPIO 14). Safe on headless Pi with SSH.
    local cmdline_txt=""
    [[ -f /boot/firmware/cmdline.txt ]] && cmdline_txt=/boot/firmware/cmdline.txt
    [[ -f /boot/cmdline.txt ]] && cmdline_txt=/boot/cmdline.txt
    if [[ -n "$cmdline_txt" ]] && grep -q "console=serial0" "$cmdline_txt"; then
        sed -i 's/console=serial0,[0-9]* //g' "$cmdline_txt"
        info "Disabled UART serial console (GPIO 14/15 now free for GPIO use)"
    else
        info "UART serial console already disabled or not found"
    fi

    # Enable 1-Wire on GPIO17 (Pin 11) and GPIO18 (Pin 12).
    # GPIO17 is the primary DS18B20 bus; GPIO18 is the secondary (alternative).
    # GPIO4 (Pin 7) is used by the hall sensor — do not enable 1-Wire there.
    # Two separate overlay lines are required — gpiopin1= is not a valid parameter.
    local _w1cfg=""
    [[ -f /boot/firmware/config.txt ]] && _w1cfg=/boot/firmware/config.txt
    [[ -z "$_w1cfg" && -f /boot/config.txt ]] && _w1cfg=/boot/config.txt
    if [[ -n "$_w1cfg" ]]; then
        local _changed=0
        if ! grep -q "gpiopin=17" "$_w1cfg"; then
            # Remove any legacy lines (old GPIO4 setup), then add both lines cleanly
            sed -i '/dtoverlay=w1-gpio/d' "$_w1cfg"
            printf 'dtoverlay=w1-gpio,gpiopin=17\ndtoverlay=w1-gpio,gpiopin=18\n' >> "$_w1cfg"
            _changed=1
        elif ! grep -q "gpiopin=18" "$_w1cfg"; then
            echo "dtoverlay=w1-gpio,gpiopin=18" >> "$_w1cfg"
            _changed=1
        fi
        if [[ $_changed -eq 1 ]]; then
            warn "1-Wire enabled on GPIO17+GPIO18 - REBOOT REQUIRED"
        else
            info "1-Wire already enabled on GPIO17+GPIO18"
        fi
    fi

    # Enable hardware watchdog
    # systemd drives it natively — no extra packages needed.
    # If the system freezes for >15 s, hardware resets the Pi.
    local config_txt=""
    [[ -f /boot/firmware/config.txt ]] && config_txt=/boot/firmware/config.txt
    [[ -f /boot/config.txt ]] && config_txt=/boot/config.txt
    if [[ -n "$config_txt" ]] && ! grep -q "dtparam=watchdog=on" "$config_txt"; then
        echo "dtparam=watchdog=on" >> "$config_txt"
        info "Hardware watchdog enabled in $config_txt"
    else
        info "Hardware watchdog already enabled"
    fi

    # Configure systemd to use the hardware watchdog
    local sysconfd=/etc/systemd/system.conf.d
    mkdir -p "$sysconfd"
    cat > "$sysconfd/watchdog.conf" << 'EOF'
[Manager]
# Ping hardware watchdog every 7.5 s (half of RuntimeWatchdogSec).
# If the system freezes completely, hardware resets the Pi after 15 s.
RuntimeWatchdogSec=15
# Allow up to 10 min for a clean shutdown before watchdog fires.
ShutdownWatchdogSec=10min
EOF
    systemctl daemon-reload
    info "systemd watchdog configured (RuntimeWatchdogSec=15)"

    # NM dispatcher: fire heartbeat immediately when internet connectivity restored
    install -m 755 /opt/otgw/scripts/nm_heartbeat_dispatcher.sh \
        /etc/NetworkManager/dispatcher.d/99-otgw-heartbeat
    info "NM dispatcher installed (heartbeat on connectivity-change)"
}

# ============================================================================
# Hostname Configuration
# ============================================================================

configure_hostname() {
    info "Configuring hostname..."

    local current_hostname
    current_hostname=$(hostname)

    if [[ "$current_hostname" == "raspberrypi" ]]; then
        if $CHECK_ONLY; then
            info "Would change hostname from 'raspberrypi' to 'otgw-logger'"
            return
        fi

        hostnamectl set-hostname otgw-logger
        sed -i 's/raspberrypi/otgw-logger/g' /etc/hosts
        info "Hostname changed to 'otgw-logger'"
    else
        info "Hostname is already set to '$current_hostname'"
    fi
}

# ============================================================================
# WiFi Country Configuration
# ============================================================================

configure_wifi_country() {
    info "Setting WiFi regulatory domain to NL..."

    if $CHECK_ONLY; then
        info "Would set WiFi country to NL"
        return
    fi

    if command -v raspi-config &>/dev/null; then
        raspi-config nonint do_wifi_country NL
        info "WiFi country set to NL via raspi-config"
    else
        iw reg set NL 2>/dev/null || true
        if [[ -f /etc/default/crda ]]; then
            sed -i 's/^REGDOMAIN=.*/REGDOMAIN=NL/' /etc/default/crda
        else
            echo "REGDOMAIN=NL" > /etc/default/crda
        fi
        info "WiFi country set to NL via iw/crda"
    fi
    rfkill unblock wifi 2>/dev/null || true
}

# ============================================================================
# Mode and WiFi SSH State Files
# ============================================================================

setup_mode_defaults() {
    info "Setting up mode defaults..."

    if $CHECK_ONLY; then
        info "Would create /etc/otgw/mode (production) and /etc/otgw/wifi-ssh (disabled) if missing"
        return
    fi

    if [[ ! -f "$CONFIG_DIR/mode" ]]; then
        echo "production" > "$CONFIG_DIR/mode"
        info "Created /etc/otgw/mode = production"
    else
        info "Mode already set: $(cat "$CONFIG_DIR/mode")"
    fi

    if [[ ! -f "$CONFIG_DIR/wifi-ssh" ]]; then
        echo "disabled" > "$CONFIG_DIR/wifi-ssh"
        info "Created /etc/otgw/wifi-ssh = disabled"
    else
        info "WiFi SSH already set: $(cat "$CONFIG_DIR/wifi-ssh")"
    fi

    # Apply the current state so iptables reflects the config file
    if [[ "$(cat "$CONFIG_DIR/wifi-ssh")" == "enabled" ]]; then
        wifi_ssh_enable
    else
        wifi_ssh_disable
    fi
}

# ============================================================================
# WiFi SSH Toggle
# ============================================================================

# wifi_ssh_enable / wifi_ssh_disable: rewrite iptables + save state file.
# Call from TUI or manually; idempotent.

wifi_ssh_enable() {
    if $CHECK_ONLY; then
        info "Would enable WiFi SSH (remove DROP rule on wlan+:22)"
        return
    fi
    iptables -D INPUT -i wlan+ -p tcp --dport 22 \
        -m conntrack --ctstate NEW -j DROP 2>/dev/null || true
    if [[ -d /etc/iptables ]]; then
        iptables-save > /etc/iptables/rules.v4
    fi
    echo "enabled" > "$CONFIG_DIR/wifi-ssh"
    info "WiFi SSH enabled"
}

wifi_ssh_disable() {
    if $CHECK_ONLY; then
        info "Would disable WiFi SSH (add DROP rule on wlan+:22)"
        return
    fi
    # --ctstate NEW: only new connections are blocked; established SSH sessions
    # (e.g. started during a portal window) survive until they are closed normally.
    iptables -C INPUT -i wlan+ -p tcp --dport 22 \
        -m conntrack --ctstate NEW -j DROP 2>/dev/null || \
        iptables -A INPUT -i wlan+ -p tcp --dport 22 \
            -m conntrack --ctstate NEW -j DROP
    if [[ -d /etc/iptables ]]; then
        iptables-save > /etc/iptables/rules.v4
    fi
    echo "disabled" > "$CONFIG_DIR/wifi-ssh"
    info "WiFi SSH disabled"
}

# ============================================================================
# Pi ID Generation
# ============================================================================

generate_image_id() {
    info "Generating Image ID from ethernet MAC..."

    if $CHECK_ONLY; then
        info "Would generate Image ID and save to /etc/otgw/pi-id"
        return
    fi

    "$INSTALL_DIR/scripts/pi_id.sh" || warn "Image ID generation failed (no ethernet?)"
}

# ============================================================================

generate_tls_cert() {
    local cert="$CONFIG_DIR/tls.crt"
    local key="$CONFIG_DIR/tls.key"

    if $CHECK_ONLY; then
        if [[ -f "$cert" && -f "$key" ]]; then
            info "TLS cert already present: $cert"
        else
            info "Would generate self-signed TLS cert: $cert"
        fi
        return
    fi

    if [[ -f "$cert" && -f "$key" ]]; then
        info "TLS cert already present — skipping"
        return
    fi

    info "Generating self-signed TLS certificate..."

    # Read hostname for the CN; fall back to 'otgw-logger'
    local hostname
    hostname=$(cat /etc/hostname 2>/dev/null | tr -d '[:space:]') || hostname='otgw-logger'

    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$key" \
        -out "$cert" \
        -days 3650 \
        -subj "/CN=${hostname}.local" \
        -addext "subjectAltName=DNS:${hostname}.local,DNS:${hostname},IP:127.0.0.1" \
        2>/dev/null

    chmod 640 "$cert" "$key"
    chown root:root "$cert" "$key"
    info "TLS cert generated: $cert (valid 10 years)"
}

# ============================================================================
# Final Steps
# ============================================================================

print_summary() {
    echo ""
    echo "=============================================="
    echo "  OTGW Logger Provisioning Complete"
    echo "=============================================="
    echo ""
    echo "Installed to: $INSTALL_DIR"
    echo ""
    echo "Next steps:"
    echo "  1. Reboot if 1-Wire was just enabled"
    echo "  2. Run 'otgw-setup' to configure WiFi and services"
    echo "  3. Connect OTGW via USB"
    echo "  4. Connect DS18B20 sensors to GPIO17 (pin 11)"
    echo ""
    echo "Services will start automatically after configuration."
    echo ""
    echo "Useful commands:"
    echo "  otgw-setup                    # Run setup TUI"
    echo "  systemctl status mosquitto    # Check MQTT broker"
    echo "  journalctl -u otgw-listener   # View OTGW logs"
    echo "  mosquitto_sub -t 'otgw/#' -v  # View MQTT messages"
    echo ""
}

start_services() {
    info "Starting services..."

    if $CHECK_ONLY; then
        info "Would start mosquitto service"
        return
    fi

    # Start mosquitto (needed for other services)
    systemctl start mosquitto

    # Start timers — these are always safe to run regardless of configuration
    systemctl start log-flush.timer
    systemctl start log-sync.timer
    systemctl start heartbeat.timer

    # wifi-switch waits internally for wifi.conf — safe to start now
    systemctl start wifi-switch
    systemctl start config-sensor
    systemctl start config-portal
    systemctl start led-status

    # Other services will fail until properly configured, so don't start them
    info "Mosquitto, timers, and wifi-switch started. Listener services will start after configuration."
}

# ============================================================================
# Main
# ============================================================================
# Log Partition Check
# ============================================================================

check_log_partition() {
    info "Checking log partition..."

    # Detect the SD card device
    local disk="/dev/mmcblk0"
    [ -b "$disk" ] || disk="/dev/sda"
    local p3="${disk}p3"
    [ -b "/dev/mmcblk0p3" ] || p3="${disk}3"

    if findmnt -n "$LOG_DIR" | grep -q vfat; then
        info "Log partition: FAT32 mounted at $LOG_DIR"
    elif [ -b "$p3" ]; then
        warn "Log partition ($p3) exists but is not mounted at $LOG_DIR"
        warn "  Run first-boot.sh or mount manually: mount $p3 $LOG_DIR"
    else
        warn "Log partition (FAT32 p3) not found — logs are on the root filesystem"
        warn "  This Pi was not provisioned via the prebake first-boot path."
        warn "  Reflash using 'build.sh --prebake' to get a dedicated FAT32 log partition."
    fi
}

# ============================================================================

main() {
    echo "=============================================="
    echo "  OTGW Logger Provisioning Script"
    echo "=============================================="
    echo ""

    check_root

    install_packages
    upgrade_packages
    ensure_otgw_user
    setup_directories
    copy_files
    install_services
    configure_system
    configure_hostname
    configure_wifi_country
    generate_image_id
    generate_tls_cert
    setup_mode_defaults
    check_log_partition
    start_services
    save_package_snapshot

    if ! $CHECK_ONLY; then
        print_summary
    else
        echo ""
        info "Check complete. Run without --check to apply changes."
    fi
}

main "$@"
