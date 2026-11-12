#!/bin/bash
# First-boot provisioning for OTGW Logger (pre-baked image)
#
# All packages are already installed and static config is baked in.
# This script only handles hardware-specific steps that need the real
# device: Pi ID generation, log directory setup, and static ethernet IP.

set -e

LOG="/var/log/otgw-first-boot.log"
exec &> >(tee -a "$LOG")

cleanup() {
    echo "Running cleanup..."
    systemctl reset-failed otgw-first-boot.service 2>/dev/null || true
    systemctl disable otgw-first-boot.service 2>/dev/null || true
    rm -f /etc/systemd/system/otgw-first-boot.service
    rm -f /opt/otgw/first-boot.sh
    systemctl daemon-reload 2>/dev/null || true
}
trap cleanup EXIT

echo "$(date): OTGW Logger first-boot (pre-baked) starting..."

# =============================================================================
# Guard: prevent re-running on every boot when overlayroot is active.
# The guard file lives on p3 (/media/root-ro/etc/otgw/.first-boot-done).
# It is visible at /etc/otgw/.first-boot-done via the overlay (p3 is lowerdir).
# IMPORTANT: the guard must be written to p3 directly (not through /etc/otgw/)
# because writes through /etc/otgw/ go to tmpfs and are wiped on reboot.
# =============================================================================
if [[ -f /etc/otgw/.first-boot-done ]] || \
   [[ -f /media/root-ro/etc/otgw/.first-boot-done ]]; then
    echo "$(date): First-boot already completed (guard file found). Exiting."
    exit 0
fi

# =============================================================================
# Layer 3 of 3: safety net — ensure otgw user exists before doing anything.
# Layer 1 (chroot useradd) and layer 2 (userconf.txt) should have handled this
# already, but if both failed for any reason we create the user here.
# =============================================================================
if ! id otgw &>/dev/null; then
    echo "$(date): Safety net: otgw user not found, creating now..."
    useradd -m -s /bin/bash otgw
    echo "otgw:otgw" | chpasswd
    for grp in sudo adm dialout cdrom audio video plugdev games users input render netdev gpio i2c spi; do
        getent group "$grp" &>/dev/null && usermod -aG "$grp" otgw || true
    done
    echo "otgw ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/otgw
    chmod 440 /etc/sudoers.d/otgw
    echo "$(date): Safety net: otgw user created successfully"
fi

cat > /dev/console << 'BANNER' 2>/dev/null || true

===============================================
  OTGW Logger — First boot setup in progress

  Configuring hardware (ID, logs, network).
  This takes about 30 seconds.

  The system will reboot automatically when done.
===============================================

BANNER

# Copy systemd units (pre-baked image still keeps units under /opt/otgw)
cp /opt/otgw/systemd/*.service /etc/systemd/system/
cp /opt/otgw/systemd/*.timer /etc/systemd/system/

# Copy configs
mkdir -p /etc/mosquitto/conf.d /etc/systemd/journald.conf.d /etc/ssh/sshd_config.d /etc/otgw
cp /opt/otgw/config/mosquitto.conf /etc/mosquitto/conf.d/otgw.conf
cp /opt/otgw/config/journald.conf /etc/systemd/journald.conf.d/otgw.conf
cp /opt/otgw/config/sshd_config.d/ethernet-only.conf /etc/ssh/sshd_config.d/
# Remove the Pi OS "SSH may not work" banner — no longer needed once user exists
rm -f /etc/ssh/sshd_config.d/rename_user.conf

# Create wrapper script for otgw-setup.
# A wrapper (not a symlink) is needed so PYTHONPATH is set correctly —
# otgw_setup.py imports from the tui/ package inside /opt/otgw.
cat > /usr/local/bin/otgw-setup << 'WRAPPER'
#!/bin/bash
PYTHONPATH=/opt/otgw exec /usr/bin/python3 /opt/otgw/tui/otgw_setup.py "$@"
WRAPPER
chmod +x /usr/local/bin/otgw-setup /opt/otgw/tui/*.py /opt/otgw/scripts/*.py /opt/otgw/scripts/*.sh

# Remove Raspberry Pi OS SSH nag banner (pam_chksshpwd checks for default pi user
# and shows "Please note SSH may not work..." before every login — irrelevant on
# our images which always use the otgw user, not the default pi user).
sed -i '/pam_chksshpwd/d' /etc/pam.d/common-session

# Silence overlayroot mount dump in SSH MOTD (implementation detail, not useful
# to operators or installers).
chmod -x /etc/update-motd.d/97-overlayroot 2>/dev/null || true

# Enable 1-Wire on GPIO17 (primary) and GPIO18 (secondary).
# GPIO17 = Pin 11 (DS18B20 primary bus).
# GPIO4 = Pin 7 is used by the hall sensor — do not enable 1-Wire there.
if ! grep -q "dtoverlay=w1-gpio" /boot/firmware/config.txt 2>/dev/null && \
   ! grep -q "dtoverlay=w1-gpio" /boot/config.txt 2>/dev/null; then
    if [[ -f /boot/firmware/config.txt ]]; then
        printf 'dtoverlay=w1-gpio,gpiopin=17\ndtoverlay=w1-gpio,gpiopin=18\n' >> /boot/firmware/config.txt
    elif [[ -f /boot/config.txt ]]; then
        printf 'dtoverlay=w1-gpio,gpiopin=17\ndtoverlay=w1-gpio,gpiopin=18\n' >> /boot/config.txt
    fi
fi

# Enable hardware watchdog — systemd drives it natively (no extra packages).
# If the kernel freezes or systemd hangs, the Pi reboots itself after 15 s.
_config_txt=""
[[ -f /boot/firmware/config.txt ]] && _config_txt=/boot/firmware/config.txt
[[ -f /boot/config.txt ]]          && _config_txt=/boot/config.txt
if [[ -n "$_config_txt" ]] && ! grep -q "dtparam=watchdog=on" "$_config_txt"; then
    echo "dtparam=watchdog=on" >> "$_config_txt"
fi
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/watchdog.conf << 'WDEOF'
[Manager]
# Pet hardware watchdog every 7.5 s; reset Pi if system freezes for >15 s.
RuntimeWatchdogSec=15
# Allow up to 10 min for a clean shutdown before watchdog fires.
ShutdownWatchdogSec=10min
WDEOF
systemctl daemon-reload

# Confirm password (idempotent — harmless if already set correctly by layer 1/2)
echo "otgw:otgw" | chpasswd

# Ensure SSH is enabled
systemctl enable ssh

# ---- Install SSH authorized keys (if staged on boot partition by flash.sh) ----
# flash.sh writes authorized_keys to the FAT32 boot partition before flashing.
# Keys baked in at build time (Option C) are already in /home/otgw/.ssh/authorized_keys;
# we append only lines not already present to avoid duplicates.
BOOT_DIR_AK=""
if [ -d /boot/firmware ]; then BOOT_DIR_AK="/boot/firmware"
elif [ -d /boot ]; then BOOT_DIR_AK="/boot"; fi
if [ -f "$BOOT_DIR_AK/authorized_keys" ]; then
    echo "Installing SSH authorized keys from flash..."
    mkdir -p /home/otgw/.ssh
    chmod 700 /home/otgw/.ssh
    touch /home/otgw/.ssh/authorized_keys
    while IFS= read -r key; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        grep -qxF "$key" /home/otgw/.ssh/authorized_keys || echo "$key" >> /home/otgw/.ssh/authorized_keys
    done < "$BOOT_DIR_AK/authorized_keys"
    chmod 600 /home/otgw/.ssh/authorized_keys
    chown -R otgw:otgw /home/otgw/.ssh
    rm -f "$BOOT_DIR_AK/authorized_keys"
    echo "SSH keys installed — passwordless login enabled"
fi

# =============================================================================
# Generate unique Image ID from ethernet MAC address
# =============================================================================
echo "$(date): Generating Image ID..."
/opt/otgw/scripts/pi_id.sh || echo "Warning: Image ID generation failed (no ethernet?)"

# =============================================================================
# Ethernet: DHCP only (no static IP)
# =============================================================================
# The Pi gets an address from whatever router it is connected to.
# It is reachable via mDNS as otgw-logger.local (avahi-daemon pre-installed).
echo "$(date): Ethernet: DHCP only (no static IP configured)"

# =============================================================================
# Partition layout: p3 (/etc/otgw) and p4 (/var/log/otgw) are pre-created
# in the image at build time and mount automatically via fstab on boot.
# No runtime repartitioning is needed or safe on a live rootfs.
# =============================================================================
echo "$(date): Log directory ready (p4 FAT32 mounts via fstab)..."
mkdir -p /var/log/otgw   # mount point; fstab mounts p4 here on boot

# =============================================================================
# Login banner (MOTD)
# =============================================================================
cat > /etc/motd << 'MOTD'

  ___  _____ _____      __  _
 / _ \|_   _/ __\ \    / / | |   ___  __ _ __ _ ___ _ _
| (_) | | || (_ |\ \/\/ /  | |__/ _ \/ _` / _` / -_) '_|
 \___/  |_| \___| \_/\_/   |____\___/\__, \__, \___|_|
                                      |___/|___/

  To configure WiFi, services, and log sync, run:

    otgw-setup

  Useful commands:
    systemctl status mosquitto      # MQTT broker status
    journalctl -u otgw-listener     # OTGW service logs
    mosquitto_sub -t 'home/#' -v    # View live MQTT messages

MOTD

# =============================================================================
# Hand /etc/otgw and /opt/otgw to the otgw user:
# - /etc/otgw: TUI can write config files without sudo
# - /opt/otgw: code can be updated via scp without sudo (dev/field updates)
# =============================================================================
chown -R otgw:otgw /etc/otgw
chown -R otgw:otgw /opt/otgw

# =============================================================================
# Generate self-signed TLS certificate for HTTPS dashboard (port 443)
# =============================================================================
if [[ ! -f /etc/otgw/tls.crt || ! -f /etc/otgw/tls.key ]]; then
    echo "$(date): Generating self-signed TLS certificate..."
    _hn=$(cat /etc/hostname 2>/dev/null | tr -d '[:space:]') || _hn='otgw-logger'
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout /etc/otgw/tls.key \
        -out    /etc/otgw/tls.crt \
        -not_before 20200101000000Z \
        -not_after  20350101000000Z \
        -subj "/CN=${_hn}.local" \
        -addext "subjectAltName=DNS:${_hn}.local,DNS:${_hn},IP:127.0.0.1" \
        2>/dev/null
    chmod 640 /etc/otgw/tls.crt /etc/otgw/tls.key
    echo "$(date): TLS certificate generated"
fi

# =============================================================================
# Harden ext4 rootfs: run fsck on every boot to catch and fix corruption early
# =============================================================================
# -c 1: check after every mount (every boot)
# -i 0: no time-based check interval (let -c 1 control it)
# This adds ~5 seconds to boot but prevents silent corruption accumulation.
tune2fs -c 1 -i 0 /dev/mmcblk0p2 2>/dev/null || true

# NM dispatcher: fire heartbeat immediately when internet connectivity restored
install -m 755 /opt/otgw/scripts/nm_heartbeat_dispatcher.sh \
    /etc/NetworkManager/dispatcher.d/99-otgw-heartbeat

# =============================================================================
# Read-only rootfs (overlayfs) is configured at build time in prebake_native().
# update-initramfs was already run in the chroot; overlayroot.conf is in place.
# =============================================================================
echo "$(date): Overlayfs was configured at build time — active on this boot."

# =============================================================================
# Reload systemd and finalize
# =============================================================================
systemctl daemon-reload

# Disable first-boot service before reboot
echo "$(date): Disabling first-boot service..."
cleanup
trap - EXIT

cat > /dev/console << 'BANNER' 2>/dev/null || true

===============================================
  OTGW Logger — First boot complete!

  Rebooting now. After reboot, connect via:

    ssh otgw@otgw-logger.local
    Password: otgw

  Then run: otgw-setup
===============================================

BANNER

echo ""
echo "$(date): OTGW Logger first-boot (pre-baked) complete!"
echo ""
echo "Connect via: ssh otgw@otgw-logger.local"
echo "Password:             otgw"
echo ""
echo "Run 'otgw-setup' to configure WiFi, P1 dongle, and sync."

# Expand p4 (FAT32 log partition) to fill all remaining SD card space.
# The image bakes p4 at a fixed ~1 GB; on real SD cards (32/64/128 GB)
# the remaining space after p4 is unallocated.  Expand it now while p4
# is still empty (no logs yet) — safe to reformat.
echo "$(date): Expanding log partition (p4) to fill SD card..."
umount /var/log/otgw 2>/dev/null || true
if parted -s /dev/mmcblk0 resizepart 4 100% 2>/dev/null; then
    partprobe /dev/mmcblk0 2>/dev/null; sleep 1
    mkfs.vfat -F 32 -n OTGW-LOGS /dev/mmcblk0p4 2>/dev/null && \
        echo "$(date): Log partition expanded to $(lsblk -b -dn -o SIZE /dev/mmcblk0p4 2>/dev/null | \
             awk '{printf "%.0f GB", $1/1024/1024/1024}')." || \
        echo "$(date): Warning: could not reformat expanded p4 (vfat)"
    mount /var/log/otgw 2>/dev/null || true
else
    echo "$(date): Warning: could not expand p4 (parted failed); logs stay on 1 GB partition"
    mount /var/log/otgw 2>/dev/null || true
fi

# Write guard file to p3 (persistent config partition) directly.
# /etc/otgw/ is an overlayfs with p3 as lowerdir and tmpfs as upperdir.
# Writing through /etc/otgw/ sends the file to tmpfs — it is wiped on reboot.
# We must write to the p3 mount point (/media/root-ro/etc/otgw/) instead,
# which persists across reboots regardless of overlayroot.
mount -o remount,rw /dev/mmcblk0p3 2>/dev/null || true
touch /media/root-ro/etc/otgw/.first-boot-done
mount -o remount,ro /dev/mmcblk0p3 2>/dev/null || true
echo "$(date): Guard file written to p3."

echo "Rebooting in 5 seconds..."
sleep 5
reboot
