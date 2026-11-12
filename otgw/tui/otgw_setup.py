#!/usr/bin/env python3
"""
OTGW Logger Setup TUI - Interactive configuration for the OTGW logging system.

Features:
- Configure WiFi credentials (for P1 dongle)
- Program P1 dongle via USB serial
- Verify all data sources (OTGW, DS18B20, P1)
- Configure log synchronization to VPS
- View live MQTT data

Usage:
    otgw_setup.py           # Normal mode
    otgw_setup.py --dry-run # Test mode (no actual changes)

Requirements:
    - Python 3.7+
    - paho-mqtt (for MQTT verification)
    - pyserial (for P1 dongle configuration)
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path
from typing import Optional, Callable

from tui.lang import t, init as _lang_init, set_lang, get_lang

# Allow importing from scripts/ directory (same install tree: /opt/otgw/scripts/)
sys.path.insert(0, str(Path(__file__).parent.parent / 'scripts'))

# Configuration paths
CONFIG_DIR = Path('/etc/otgw')
LOG_DIR    = Path('/var/log/otgw')
RAM_DIR    = Path('/run/otgw')
WIFI_CONFIG       = CONFIG_DIR / 'wifi.conf'
NO_HOME_WIFI_FILE = CONFIG_DIR / 'no-home-wifi'   # presence = Case A
DEVICE_ID_FILE    = CONFIG_DIR / 'device-id'
SYNC_CONFIG = CONFIG_DIR / 'sync.conf'       # legacy single-target
SYNC_D_DIR = CONFIG_DIR / 'sync.d'           # multi-target directory
MODE_FILE = CONFIG_DIR / 'mode'
WIFI_SSH_FILE         = CONFIG_DIR / 'wifi-ssh'
CONFIG_AP_PW_FILE     = CONFIG_DIR / 'config-ap-pw'  # presence = pw-protected
CONFIG_ACTIVE         = Path('/run/otgw-config-active')
AP_TOGGLE_FLAG  = Path('/run/otgw-ap-toggle-request')
P1_CONFIG_TOPIC = 'P1CONFIG:'

# ANSI colour codes (simple, no curses dependency)
_YELLOW = '\033[1;33m'
_GREEN = '\033[0;32m'
_RED = '\033[0;31m'
_NC = '\033[0m'


def _colour(text: str, code: str) -> str:
    """Wrap text in ANSI colour if stdout is a TTY."""
    import sys
    if sys.stdout.isatty():
        return f"{code}{text}{_NC}"
    return text


def get_image_id() -> str:
    """Read Image ID from /etc/otgw/pi-id; return empty string if not found."""
    try:
        return (CONFIG_DIR / 'pi-id').read_text().strip()
    except OSError:
        return ''


# ── recent reading helpers (reads log files, no MQTT needed) ─────────────────

def _last_log_ts(image_id: str, source: str) -> float | None:
    """Return Unix timestamp of the most recent log line for source.

    Checks RAM disk first (latest data), then today's disk log, then
    yesterday's.  source is one of 'otgw', 'gas', 'temperature'.

    For 'gas', uses a glob fallback to handle variant file naming when
    the P1 dongle publishes with a different Image ID in the topic.
    """
    from datetime import datetime as _dt

    def _read_ts(path: Path) -> float | None:
        try:
            size = path.stat().st_size
            if size == 0:
                return None
            with open(path, 'rb') as f:
                f.seek(max(0, size - 512))
                tail = f.read().decode('utf-8', errors='replace')
            lines = [ln for ln in tail.splitlines() if '|' in ln]
            if not lines:
                return None
            return _dt.fromisoformat(lines[-1].split('|')[0].strip()).timestamp()
        except (OSError, ValueError):
            return None

    today     = time.strftime('%Y-%m-%d')
    yesterday = time.strftime('%Y-%m-%d', time.localtime(time.time() - 86400))

    for date in (today, yesterday):
        bases = [RAM_DIR / image_id, LOG_DIR / image_id]
        best: float | None = None

        if source == 'gas':
            # Exact name first; glob fallback for variant topic-prefix naming
            candidates: list[Path] = []
            for base in bases:
                exact = base / f'gas_{image_id}_{date}.log'
                if exact.exists():
                    candidates.append(exact)
                else:
                    candidates.extend(sorted(base.glob(f'*gas*{image_id}*{date}*.log')))
            for path in candidates:
                ts = _read_ts(path)
                if ts is not None and (best is None or ts > best):
                    best = ts
        else:
            fname = f'{source}_{image_id}_{date}.log'
            for base in bases:
                ts = _read_ts(base / fname)
                if ts is not None and (best is None or ts > best):
                    best = ts

        if best is not None:
            return best
    return None


def format_reading_age(ts: float | None) -> str:
    """Format time-since-reading as '42 s', 'HH:MM:SS', or 'DX HH:MM:SS'."""
    if ts is None:
        return '—'
    age = max(0, int(time.time() - ts))
    if age < 60:
        return f'{age} s'
    h, rem = divmod(age, 3600)
    m, s   = divmod(rem, 60)
    if age < 86400:
        return f'{h:02d}:{m:02d}:{s:02d}'
    d       = age // 86400
    h2, r2  = divmod(age % 86400, 3600)
    m2, s2  = divmod(r2, 60)
    return f'D{d} {h2:02d}:{m2:02d}:{s2:02d}'


def get_mode() -> str:
    """Read current mode: 'production' or 'test'."""
    try:
        return MODE_FILE.read_text().strip().lower()
    except OSError:
        return 'production'


def get_wifi_ssh() -> str:
    """Read WiFi SSH state: 'enabled' or 'disabled'."""
    try:
        return WIFI_SSH_FILE.read_text().strip().lower()
    except OSError:
        return 'disabled'




def get_config_ap_pw() -> bool:
    """Return True if the config AP is password-protected."""
    return CONFIG_AP_PW_FILE.exists()


def get_log_partition_info() -> dict:
    """Check whether /var/log/otgw is mounted on a FAT32 partition."""
    try:
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == '/var/log/otgw':
                    return {
                        'mounted': True,
                        'fstype': parts[2],
                        'device': parts[0],
                    }
    except Exception:
        pass
    return {'mounted': False, 'fstype': 'ext4', 'device': None}


def clear_screen():
    """Clear terminal screen."""
    os.system('clear' if os.name != 'nt' else 'cls')


def print_header(title: str):
    """Print a formatted header."""
    clear_screen()
    width = 50
    print('=' * width)
    print(f'{title:^{width}}')
    print('=' * width)
    print()


def print_menu(options: list[tuple[str, str]], prompt: str | None = None) -> str:
    """
    Print a numbered menu and get user selection.

    Args:
        options: List of (key, description) tuples
        prompt: Prompt text

    Returns:
        Selected key
    """
    if prompt is None:
        prompt = t('select_option')
    for i, (key, desc) in enumerate(options, 1):
        print(f"  {i}. {desc}")
    print()

    while True:
        try:
            choice = input(f"{prompt} [1-{len(options)}]: ").strip()
            if not choice:
                continue
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx][0]
            print(t('number_range').format(n=len(options)))
        except ValueError:
            print(t('invalid_number'))
        except (KeyboardInterrupt, EOFError):
            print("\n")
            return 'back'


def get_input(prompt: str, default: str = '', password: bool = False) -> str:
    """Get user input with optional default value."""
    if default:
        prompt = f"{prompt} [{default}]: "
    else:
        prompt = f"{prompt}: "

    try:
        if password:
            import getpass
            value = getpass.getpass(prompt)
        else:
            value = input(prompt).strip()
        return value if value else default
    except (KeyboardInterrupt, EOFError):
        print("\n")
        return default


def confirm(prompt: str, default: bool = False) -> bool:
    """Ask for yes/no confirmation."""
    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        response = input(prompt + suffix).strip().lower()
        if not response:
            return default
        return response in ('y', 'yes')
    except (KeyboardInterrupt, EOFError):
        print("\n")
        return False


def run_command(cmd: list[str], dry_run: bool = False) -> tuple[int, str, str]:
    """Run a shell command and return (returncode, stdout, stderr)."""
    if dry_run:
        print(f"[DRY-RUN] Would run: {' '.join(cmd)}")
        return 0, '', ''

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, '', 'Command timed out'
    except Exception as e:
        return -1, '', str(e)


class MQTTCache:
    """Background MQTT subscriber that caches recent messages.

    Starts on TUI launch and subscribes to all topics (#).  Keeps messages
    for up to WINDOW seconds then discards them, so memory stays bounded
    even when the TUI runs for hours.  Thread-safe.
    """

    WINDOW: float = 65.0  # seconds — discard messages older than this

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # topic -> [(monotonic_ts, payload_str), ...]
        self._data: dict[str, list[tuple[float, str]]] = {}
        # registered callbacks invoked on every new message
        self._watchers: list[Callable[[str, str], None]] = []
        self._client = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect and start background loop. Fails silently if unavailable."""
        try:
            import paho.mqtt.client as mqtt
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id='otgw-tui-cache')
            self._client.on_connect = self._on_connect
            self._client.on_message = self._on_message
            self._client.connect_async('localhost', 1883, keepalive=60)
            self._client.loop_start()
        except Exception:
            pass  # degrade gracefully — verify/live will show empty cache

    def stop(self) -> None:
        """Stop background loop."""
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Watcher registration (for live data view)
    # ------------------------------------------------------------------

    def add_watcher(self, fn: Callable[[str, str], None]) -> None:
        """Register fn(topic, payload) called on every new message."""
        with self._lock:
            self._watchers.append(fn)

    def remove_watcher(self, fn: Callable[[str, str], None]) -> None:
        with self._lock:
            try:
                self._watchers.remove(fn)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def recent(self, topic_fragment: str, within: float = 65.0) -> bool:
        """True if any message whose topic contains topic_fragment arrived
        within `within` seconds."""
        cutoff = time.monotonic() - within
        with self._lock:
            for topic, entries in self._data.items():
                if topic_fragment in topic:
                    if any(ts > cutoff for ts, _ in entries):
                        return True
        return False

    def get_history(self, within: float = 65.0) -> list[tuple[float, str, str]]:
        """Return [(monotonic_ts, topic, payload)] within window, oldest first."""
        cutoff = time.monotonic() - within
        result: list[tuple[float, str, str]] = []
        with self._lock:
            for topic, entries in self._data.items():
                for ts, payload in entries:
                    if ts > cutoff:
                        result.append((ts, topic, payload))
        result.sort(key=lambda x: x[0])
        return result

    # ------------------------------------------------------------------
    # Internal MQTT callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc, props) -> None:
        if rc == 0:
            client.subscribe('#')

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = msg.payload.decode('utf-8', errors='replace')
        except Exception:
            return
        topic = msg.topic
        now = time.monotonic()
        cutoff = now - self.WINDOW
        with self._lock:
            entries = self._data.get(topic, [])
            entries.append((now, payload))
            # keep only entries within the window — bounds memory
            entries = [(ts, p) for ts, p in entries if ts > cutoff]
            self._data[topic] = entries
            watchers = list(self._watchers)
        for fn in watchers:
            try:
                fn(topic, payload)
            except Exception:
                pass


class OTGWSetup:
    """Main setup TUI application."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.mqtt_client = None
        self.cache = MQTTCache()

    # Systemd services and timers managed by this app
    OTGW_SERVICES = [
        ('mosquitto',          'service'),
        ('otgw-listener',      'service'),
        ('ds18b20-listener',   'service'),
        ('mqtt-logger',        'service'),
        ('log-sync',           'timer'),
    ]

    # Required packages for OTGW Logger (apt packages only)
    # Note: esptool is installed via pip (Debian package is broken), not listed here
    REQUIRED_PACKAGES = [
        'mosquitto',
        'mosquitto-clients',
        'python3-paho-mqtt',
        'python3-serial',
        'python3-lgpio',
        'rsync',
        'avahi-daemon',
        'dosfstools',
        'parted',
    ]

    def run(self):
        """Main application loop."""
        self.cache.start()
        try:
          self._run_loop()
        finally:
            self.cache.stop()

    def _run_loop(self):
        while True:
            action = self.main_menu()
            if action == 'exit':
                print(t('goodbye'))
                break
            elif action == 'wifi':
                self.configure_wifi()
            elif action == 'p1':
                self.configure_p1_dongle()
            elif action == 'flash_p1':
                self.flash_p1_dongle()
            elif action == 'p1_usb_sync':
                self.p1_usb_sync()
            elif action == 'sync':
                self.manage_sync_targets()
            elif action == 'verify':
                self.verify_systems()
            elif action == 'data_flow':
                self.check_data_flow()
            elif action == 'live':
                self.view_live_data()
            elif action == 'install':
                self.install_packages()
            elif action == 'maintenance':
                self.system_maintenance()
            elif action == 'set_clock':
                self.set_clock()
            elif action == 'mode':
                self.set_mode()
            elif action == 'wifi_ssh':
                self.toggle_wifi_ssh()
            elif action == 'config_ap_pw':
                self.toggle_config_ap_pw()
            elif action == 'wizard':
                self.setup_wizard()
            elif action == 'portal_ap':
                self.toggle_portal_ap()
            elif action == 'simulators':
                self.manage_simulators()
            elif action == 'language':
                self.set_language()
            elif action == 'setup_log_part':
                self.setup_log_partition()
            elif action == 'factory_reset':
                self.factory_reset()

    def get_network_summary(self) -> str:
        """Get compact network status string for menu header."""
        lines = []

        # Ethernet status
        eth_ip = None
        for iface in ['eth0', 'end0', 'enp0s3']:
            path = Path(f'/sys/class/net/{iface}')
            if path.exists():
                ret, stdout, _ = run_command(
                    ['ip', '-4', '-o', 'addr', 'show', iface]
                )
                if ret == 0 and stdout.strip():
                    addr = stdout.split()[3].split('/')[0]
                    eth_ip = addr
                break
        lines.append(t('net_ethernet').format(v=eth_ip or 'down'))

        # WiFi status
        wifi = self.get_wifi_status()
        if not wifi['interface']:
            lines.append(t('net_wifi_no_adapter'))
        elif wifi['ssid']:
            ip_part = f" ({wifi['ip']})" if wifi['ip'] else ''
            lines.append(t('net_wifi_connected').format(v=f"{wifi['ssid']}{ip_part}"))
        else:
            lines.append(t('net_wifi_not_connected'))

        # Internet
        ret, _, _ = run_command(
            ['ping', '-c', '1', '-W', '3', '8.8.8.8']
        )
        lines.append(t('net_internet_ok') if ret == 0 else t('net_internet_fail'))

        # WiFi-switch state (CLIENT / AP / SWITCHING / PAUSED)
        wifi_state_file = Path('/run/otgw-wifi-state')
        try:
            state = wifi_state_file.read_text().strip()
            if state:
                lines.append(t('net_wifi_switch').format(v=state))
        except OSError:
            pass

        return '\n'.join(lines)

    def get_service_summary(self) -> str:
        """Get compact service status string for menu header."""
        total = len(self.OTGW_SERVICES)
        running = 0
        for name, kind in self.OTGW_SERVICES:
            unit = f'{name}.{kind}'
            ret, _, _ = run_command(['systemctl', 'is-active', unit])
            if ret == 0:
                running += 1
        if running == total:
            return t('svc_all_running').format(n=running, total=total)
        return t('svc_partial').format(n=running, total=total)

    def main_menu(self) -> str:
        """Display main menu."""
        print_header(t('main_title'))

        if self.dry_run:
            print(t('dry_run_banner'))

        # Network status
        print(self.get_network_summary())

        # Service status
        print(self.get_service_summary())

        # Package status
        missing = self.check_packages()
        if missing:
            print(t('pkg_missing').format(n=len(missing)))
        else:
            print(t('pkg_all_installed'))

        # Mode and WiFi SSH status
        mode = get_mode()
        wifi_ssh = get_wifi_ssh()
        if mode == 'test':
            mode_str = _colour(t('mode_test_badge'), _YELLOW)
        else:
            mode_str = _colour(t('mode_prod_badge'), _GREEN)
        wifi_ssh_str = (
            _colour(t('wifi_ssh_on_badge'), _YELLOW)
            if wifi_ssh == 'enabled'
            else t('wifi_ssh_off_badge')
        )
        print(f"  {mode_str}   {wifi_ssh_str}")

        # Log partition
        lp = get_log_partition_info()
        if lp['mounted'] and lp['fstype'] == 'vfat':
            print(t('log_part_fat32').format(v=lp['device']))
        elif lp['mounted']:
            print(t('log_part_other').format(v=lp['fstype']))
        else:
            print(t('log_part_ext4'))
        print()

        ap_active = CONFIG_ACTIVE.exists()
        ap_label  = (
            _colour('Stop config AP / portal', _YELLOW)
            if ap_active
            else 'Start config AP / portal'
        )

        options = [
            ('wizard',      t('menu_wizard')),
            ('wifi',        t('menu_wifi')),
            ('p1',          t('menu_p1')),
            ('flash_p1',    t('menu_flash_p1')),
            ('p1_usb_sync', t('menu_p1_usb_sync')),
            ('sync',        t('menu_sync')),
            ('verify',      t('menu_verify')),
            ('data_flow',   t('menu_data_flow')),
            ('live',        t('menu_live')),
            ('maintenance', t('menu_maintenance')),
            ('set_clock',  t('menu_set_clock')),
            ('mode',        t('menu_mode')),
            ('wifi_ssh',      t('menu_wifi_ssh')),
            ('config_ap_pw',  t('menu_config_ap_pw')),
            ('portal_ap',     ap_label),
            ('simulators',  t('menu_simulators')),
        ]

        # Offer log partition setup when not already on FAT32
        if not (lp['mounted'] and lp['fstype'] == 'vfat'):
            options.append(
                ('setup_log_part', t('menu_setup_log_part'))
            )

        if missing:
            options.append(
                ('install', t('menu_install_pkgs').format(n=len(missing)))
            )

        options.append(('language', t('menu_language')))
        options.append(('factory_reset', t('menu_factory_reset')))
        options.append(('exit', t('exit')))

        return print_menu(options)

    # =========================================================================
    # Mode and WiFi SSH
    # =========================================================================

    def _apply_wifi_ssh(self, enable: bool) -> bool:
        """Add or remove the iptables DROP rule for WiFi SSH. Returns success."""
        if self.dry_run:
            action = 'enable' if enable else 'disable'
            print(f"[DRY-RUN] Would {action} WiFi SSH")
            return True
        if enable:
            ret, _, err = run_command(
                ['sudo', 'iptables', '-D', 'INPUT', '-i', 'wlan+',
                 '-p', 'tcp', '--dport', '22', '-j', 'DROP']
            )
            # Not an error if rule didn't exist
        else:
            # Add rule if not present
            run_command(
                ['sudo', 'iptables', '-C', 'INPUT', '-i', 'wlan+',
                 '-p', 'tcp', '--dport', '22', '-j', 'DROP']
            )
            ret, _, _ = run_command(
                ['sudo', 'iptables', '-A', 'INPUT', '-i', 'wlan+',
                 '-p', 'tcp', '--dport', '22', '-j', 'DROP']
            )
        # Save rules if iptables-persistent is installed
        run_command(['sudo', 'iptables-save', '-c'])
        try:
            import subprocess as _sp
            _sp.run(
                ['sudo', 'sh', '-c',
                 'iptables-save > /etc/iptables/rules.v4'],
                check=False, capture_output=True
            )
        except Exception:
            pass
        # Update state file
        state = 'enabled' if enable else 'disabled'
        if not self.dry_run:
            try:
                WIFI_SSH_FILE.write_text(state + '\n')
            except OSError:
                pass
        return True

    def set_mode(self):
        """Switch between test and production mode."""
        print_header(t('mode_title'))
        current = get_mode()
        print(t('mode_current').format(v=current.upper()))
        print()
        print(t('mode_test_desc1'))
        print(t('mode_test_desc2'))
        print()
        print(t('mode_prod_desc1'))
        print(t('mode_prod_desc2'))
        print()

        options = [
            ('test',       t('mode_switch_test')),
            ('production', t('mode_switch_prod')),
            ('back',       t('back')),
        ]
        choice = print_menu(options)
        if choice == 'back':
            return
        if choice == current:
            print(t('mode_already').format(v=current.upper()))
            input(t('press_enter'))
            return

        if not confirm(t('mode_confirm_switch').format(v=choice.upper())):
            input(t('press_enter'))
            return

        if self.dry_run:
            print(t('dry_run_prefix') + f'set mode to {choice}')
        else:
            MODE_FILE.write_text(choice + '\n')

        # Automatically toggle WiFi SSH
        enable_ssh = (choice == 'test')
        self._apply_wifi_ssh(enable_ssh)
        ssh_action = t('state_enabled') if enable_ssh else t('state_disabled')

        print(t('mode_set').format(v=choice.upper()))
        print(t('mode_wifi_ssh_auto').format(v=ssh_action))
        print()
        print(t('mode_restart_hint'))
        print(t('mode_restart_cmd'))

        input(t('press_enter'))

    def set_clock(self) -> None:
        """Manually set the system clock when NTP is unavailable (Case A)."""
        import datetime as _dt
        from zoneinfo import ZoneInfo

        print_header(t('clock_title'))

        now_local = _dt.datetime.now(tz=ZoneInfo('Europe/Amsterdam'))
        print(t('clock_current').format(
            v=now_local.strftime('%Y-%m-%d %H:%M:%S')
        ))
        print()

        ntp_ret, ntp_out, _ = run_command(
            ['timedatectl', 'show', '--property=NTPSynchronized', '--value']
        )
        if ntp_ret == 0 and ntp_out.strip().lower() == 'yes':
            print(t('clock_ntp_ok'))
            input(t('press_enter'))
            return

        print(t('clock_ntp_not_synced'))
        print()

        date_default = now_local.strftime('%Y-%m-%d')
        date_str = get_input(t('clock_enter_date'), default=date_default)
        try:
            _dt.date.fromisoformat(date_str)
        except ValueError:
            print(t('clock_invalid_date'))
            input(t('press_enter'))
            return

        time_default = now_local.strftime('%H:%M:%S')
        time_str = get_input(t('clock_enter_time'), default=time_default)
        try:
            _dt.time.fromisoformat(time_str)
            if len(time_str) != 8 or time_str.count(':') != 2:
                raise ValueError
        except ValueError:
            print(t('clock_invalid_time'))
            input(t('press_enter'))
            return

        datetime_str = f'{date_str} {time_str}'

        if self.dry_run:
            print(t('clock_dry_run').format(v=datetime_str))
            input(t('press_enter'))
            return

        # timedatectl set-time expects local time. The Pi's timezone must be
        # set to Europe/Amsterdam (done during provisioning) for this to be
        # correct — which matches what the user just entered.
        ret, _, err = run_command(
            ['sudo', 'timedatectl', 'set-time', datetime_str]
        )
        if ret != 0:
            print(t('clock_set_fail').format(v=err.strip() or 'unknown error'))
            input(t('press_enter'))
            return

        run_command(['sudo', 'fake-hwclock', 'save'])
        try:
            Path('/run/otgw-time-source').write_text('manual\n')
        except OSError:
            pass

        print(t('clock_set_ok').format(v=datetime_str))
        input(t('press_enter'))

    def toggle_wifi_ssh(self):
        """Enable or disable SSH over WiFi."""
        print_header(t('wifi_ssh_title'))
        current = get_wifi_ssh()
        mode = get_mode()

        print(t('wifi_ssh_state').format(v=current.upper()))
        print(t('wifi_ssh_mode').format(v=mode.upper()))
        print()

        if mode == 'production' and current == 'disabled':
            print(_colour(t('wifi_ssh_warning'), _RED))
            print()

        if current == 'disabled':
            action_label = t('wifi_ssh_enable')
            new_state = True
        else:
            action_label = t('wifi_ssh_disable')
            new_state = False

        if not confirm(f"{action_label}?"):
            input(t('press_enter'))
            return

        self._apply_wifi_ssh(new_state)
        state_str = t('state_enabled') if new_state else t('state_disabled')
        print(t('wifi_ssh_done').format(v=state_str))
        if new_state:
            print(t('wifi_ssh_enabled_hint1'))
            print(t('wifi_ssh_enabled_hint2'))

        input(t('press_enter'))

    # =========================================================================
    # Config AP password toggle
    # =========================================================================

    def toggle_config_ap_pw(self) -> None:
        """Enable or disable WPA2 password on the config/installer AP."""
        from ap_password import derive_ap_password
        print_header(t('config_ap_pw_title'))
        enabled = get_config_ap_pw()

        if enabled:
            print(t('config_ap_pw_state_on'))
            device_id = DEVICE_ID_FILE.read_text().strip() \
                if DEVICE_ID_FILE.exists() else 'unknown'
            ssid = f'otgw-{device_id}'
            password = derive_ap_password()
            print()
            print(f'  SSID     : {ssid}')
            print(f'  Password : {password}')
            print()
            if confirm(t('config_ap_pw_disable_prompt')):
                self._set_config_ap_pw(False)
                print(t('config_ap_pw_disabled'))
        else:
            print(t('config_ap_pw_state_off'))
            print()
            print(t('config_ap_pw_enable_hint'))
            print()
            if confirm(t('config_ap_pw_enable_prompt')):
                self._set_config_ap_pw(True)
                device_id = DEVICE_ID_FILE.read_text().strip() \
                    if DEVICE_ID_FILE.exists() else 'unknown'
                ssid = f'otgw-{device_id}'
                password = derive_ap_password()
                print()
                print(t('config_ap_pw_enabled'))
                print()
                print(f'  SSID     : {ssid}')
                print(f'  Password : {password}')
                print()
                print(t('config_ap_pw_enter_in_dongle'))

        input(t('press_enter'))

    def _set_config_ap_pw(self, enable: bool) -> None:
        if self.dry_run:
            action = 'create' if enable else 'remove'
            print(f'[DRY-RUN] Would {action} {CONFIG_AP_PW_FILE}')
            return
        if enable:
            subprocess.run(
                ['sudo', 'tee', str(CONFIG_AP_PW_FILE)],
                input='', text=True,
                stdout=subprocess.DEVNULL, check=False,
            )
        else:
            subprocess.run(
                ['sudo', 'rm', '-f', str(CONFIG_AP_PW_FILE)],
                capture_output=True,
            )

    # =========================================================================
    # Portal AP toggle
    # =========================================================================

    def toggle_portal_ap(self) -> None:
        """Start or stop the config AP (portal WiFi hotspot)."""
        print_header('Config AP / Portal')
        ap_active = CONFIG_ACTIVE.exists()
        state_str = _colour('ACTIVE', _GREEN) if ap_active else 'inactive'
        print(f'  Config AP: {state_str}')
        print()

        if ap_active:
            action = 'Stop config AP (portal will close)'
        else:
            action = 'Start config AP (open portal WiFi hotspot)'

        if not confirm(f'{action}?'):
            return

        try:
            AP_TOGGLE_FLAG.write_text('toggle\n')
        except OSError as e:
            print(_colour(f'Error: {e}', _RED))
            input(t('press_enter'))
            return

        # Wait up to 6 s for config_sensor to process the toggle
        for _ in range(12):
            time.sleep(0.5)
            new_active = CONFIG_ACTIVE.exists()
            if new_active != ap_active:
                break

        new_active = CONFIG_ACTIVE.exists()
        if new_active:
            print(_colour('Config AP started — portal at http://10.42.0.1/', _GREEN))
        else:
            print('Config AP stopped.')
        input(t('press_enter'))

    # =========================================================================
    # Package Management
    # =========================================================================

    def check_packages(self) -> list[str]:
        """Check which required packages are missing."""
        missing = []
        for pkg in self.REQUIRED_PACKAGES:
            ret, _, _ = run_command(['dpkg', '-s', pkg])
            if ret != 0:
                missing.append(pkg)
        return missing

    def install_packages(self):
        """Install missing packages."""
        print_header(t('pkg_title'))

        missing = self.check_packages()
        if not missing:
            print(t('pkg_all_ok'))
            input(t('press_enter'))
            return

        print(t('pkg_missing_list'))
        for pkg in missing:
            print(f"  - {pkg}")
        print()

        if not confirm(t('pkg_install_confirm')):
            input(t('press_enter'))
            return

        if self.dry_run:
            print(t('dry_run_prefix') + 'sudo apt-get update && '
                  f'sudo apt-get install -y {" ".join(missing)}')
            input(t('press_enter'))
            return

        print(t('pkg_updating'))
        ret = subprocess.call(['sudo', 'apt-get', 'update'])
        if ret != 0:
            print(t('pkg_update_failed'))
            input(t('press_enter'))
            return

        print(t('pkg_installing').format(n=len(missing)))
        ret = subprocess.call(
            ['sudo', 'apt-get', 'install', '-y'] + missing
        )

        if ret == 0:
            print(t('pkg_install_ok'))
        else:
            print(t('pkg_install_fail'))

        input(t('press_enter'))

    def system_maintenance(self):
        """Run provision.sh to ensure installation is complete and up to date."""
        print_header(t('maint_title'))

        provision = Path('/opt/otgw/scripts/provision.sh')

        print(t('maint_desc1'))
        print(t('maint_desc2'))
        print(t('maint_desc3'))
        print()
        print(t('maint_desc4'))
        print(t('maint_desc5'))
        print()

        if not provision.exists():
            print(t('maint_not_found').format(v=provision))
            print(t('maint_not_installed'))
            input(t('press_enter'))
            return

        options = [
            ('check',    t('maint_check_only')),
            ('fix',      t('maint_check_fix')),
            ('reboot',   t('maint_reboot')),
            ('shutdown', t('maint_shutdown')),
            ('back',     t('back')),
        ]
        action = print_menu(options)

        if action == 'back':
            return

        if action in ('reboot', 'shutdown'):
            label = t('maint_reboot') if action == 'reboot' else t('maint_shutdown')
            if not confirm(f'{label}?', default=False):
                return
            if self.dry_run:
                print(t('dry_run_prefix') + f'systemctl {action}')
                input(t('press_enter'))
                return
            subprocess.run(['sudo', 'systemctl', action])
            return

        if self.dry_run:
            flag = '--check' if action == 'check' else ''
            print(t('dry_run_prefix') + f'sudo {provision} {flag}'.strip())
            input(t('press_enter'))
            return

        print()
        cmd = ['sudo', str(provision)]
        if action == 'check':
            cmd.append('--check')

        try:
            ret = subprocess.call(cmd)
            print()
            if ret == 0:
                print(t('maint_ok'))
            else:
                print(t('maint_fail'))
        except FileNotFoundError:
            print(t('maint_sudo_missing'))
            try:
                ret = subprocess.call([str(provision)] + cmd[2:])
                if ret != 0:
                    print(t('maint_fail'))
            except Exception as e:
                print(f"Error: {e}")

        input(t('press_enter'))

    # =========================================================================
    # WiFi Configuration
    # =========================================================================

    def get_wifi_status(self) -> dict:
        """Get current WiFi connection status."""
        status = {
            'interface': None,
            'ssid': None,
            'ip': None,
            'signal': None,
        }
        iface = self.find_wifi_interface()
        if not iface:
            return status
        status['interface'] = iface

        # Try nmcli for detailed info
        if shutil.which('nmcli'):
            ret, stdout, _ = run_command([
                'nmcli', '-t', '-f',
                'DEVICE,STATE,CONNECTION', 'device'
            ])
            if ret == 0:
                for line in stdout.splitlines():
                    parts = line.split(':')
                    if len(parts) >= 3 and parts[0] == iface:
                        if parts[1] == 'connected':
                            status['ssid'] = parts[2]

            ret, stdout, _ = run_command([
                'nmcli', '-t', '-f', 'IP4.ADDRESS',
                'device', 'show', iface
            ])
            if ret == 0:
                for line in stdout.splitlines():
                    if line.startswith('IP4.ADDRESS'):
                        addr = line.split(':', 1)[1].strip()
                        status['ip'] = addr.split('/')[0]

            ret, stdout, _ = run_command([
                'nmcli', '-t', '-f', 'SIGNAL',
                'device', 'wifi', 'list', 'ifname', iface
            ])
            if ret == 0:
                lines = stdout.strip().splitlines()
                if lines:
                    status['signal'] = lines[0].strip()
            return status

        # Fallback: wpa_cli + ip
        if shutil.which('wpa_cli'):
            ret, stdout, _ = run_command([
                'wpa_cli', '-i', iface, 'status'
            ])
            if ret == 0:
                for line in stdout.splitlines():
                    if line.startswith('ssid='):
                        status['ssid'] = line.split('=', 1)[1]
                    elif line.startswith('ip_address='):
                        status['ip'] = line.split('=', 1)[1]

        return status

    def print_wifi_status(self):
        """Print current WiFi connection status."""
        status = self.get_wifi_status()

        print(t('wifi_status_header'))
        if not status['interface']:
            print(t('wifi_no_iface'))
            print(t('wifi_pi1_hint'))
            return

        print(t('wifi_iface').format(v=status['interface']))
        if status['ssid']:
            print(t('wifi_connected').format(v=status['ssid']))
            if status['ip']:
                print(t('wifi_ip').format(v=status['ip']))
            if status['signal']:
                print(t('wifi_signal').format(v=status['signal']))
        else:
            print(t('wifi_not_connected'))
        print()

    def configure_wifi(self):
        """Configure WiFi credentials (home router or Case A no-home-WiFi)."""
        print_header(t('wifi_title'))
        self.print_wifi_status()

        # ── Mode selector ─────────────────────────────────────────────────────
        is_case_a = NO_HOME_WIFI_FILE.exists()
        has_wifi  = WIFI_CONFIG.exists() and WIFI_CONFIG.stat().st_size > 0
        print()
        print('  WiFi mode:')
        print('    1. Home router WiFi (2.4 GHz)')
        print('    2. No home WiFi — use Pi as access point (Case A)')
        if has_wifi or is_case_a:
            print('    F. Forget current WiFi / clear saved credentials')
        if is_case_a:
            print('  (Currently: Case A active)')
        current_default = '2' if is_case_a else '1'
        mode_choice = get_input('  Choice', current_default)

        if mode_choice.strip().lower() == 'f' and (has_wifi or is_case_a):
            self.forget_wifi()
        elif mode_choice.strip() == '2':
            self._configure_wifi_case_a()
        else:
            self._configure_wifi_home_router()

    def _configure_wifi_case_a(self) -> None:
        """Case A: no home WiFi — Pi runs its own permanent AP."""
        from ap_password import derive_ap_password

        print_header('Case A — Pi as Access Point')

        device_id = 'unknown'
        if DEVICE_ID_FILE.exists():
            device_id = DEVICE_ID_FILE.read_text().strip()
        ssid = f'otgw-{device_id}'
        password = derive_ap_password()

        print()
        print('  The Pi will broadcast its own WiFi access point.')
        print('  The P1 dongle connects directly to the Pi.')
        print()
        print(f'  SSID     : {ssid}')
        print(f'  Password : {password}')
        print()
        print('  Hall-magnet recovery: tapping the magnet opens the config AP')
        print('  (same SSID, no password) for ~5 min so you can access the')
        print('  portal. The P1 dongle reconnects automatically afterwards.')
        print()

        is_active = NO_HOME_WIFI_FILE.exists()
        if is_active:
            print('  Case A is already active.')
            if confirm('  Deactivate Case A and switch to home router WiFi?'):
                self._deactivate_case_a()
                print('  Case A deactivated. wifi-switch restarted.')
        else:
            if confirm('  Activate Case A mode?'):
                self._activate_case_a()
                print('  Case A activated. wifi-switch restarted.')
                print('  Next: use the portal or this TUI to send AP credentials')
                print('  to the P1 dongle via USB.')

        input(t('press_enter'))

    def _activate_case_a(self) -> None:
        if self.dry_run:
            print(f'[DRY-RUN] Would create {NO_HOME_WIFI_FILE}')
            return
        subprocess.run(['sudo', 'mkdir', '-p', str(CONFIG_DIR)], check=False)
        subprocess.run(
            ['sudo', 'tee', str(NO_HOME_WIFI_FILE)],
            input='', text=True,
            stdout=subprocess.DEVNULL, check=False,
        )
        subprocess.run(['systemctl', 'restart', 'wifi-switch'],
                       capture_output=True)

    def _deactivate_case_a(self) -> None:
        if self.dry_run:
            print(f'[DRY-RUN] Would remove {NO_HOME_WIFI_FILE}')
            return
        subprocess.run(['sudo', 'rm', '-f', str(NO_HOME_WIFI_FILE)],
                       capture_output=True)
        subprocess.run(['systemctl', 'restart', 'wifi-switch'],
                       capture_output=True)

    def forget_wifi(self) -> None:
        """Clear saved WiFi credentials and disconnect from the home network."""
        print_header(t('wifi_forget_title'))
        if not confirm(t('wifi_forget_confirm'), default=False):
            return
        if self.dry_run:
            print(t('dry_run_prefix') + f'nmcli con delete <ssid>; truncate {WIFI_CONFIG}')
            input(t('press_enter'))
            return
        ssid = ''
        try:
            cfg = self.load_wifi_config()
            ssid = cfg.get('ssid', '')
        except Exception:
            pass
        if ssid:
            subprocess.run(['nmcli', 'con', 'delete', ssid], capture_output=True)
        try:
            WIFI_CONFIG.write_text('')
        except OSError:
            pass
        self._deactivate_case_a()
        print(t('wifi_forgotten'))
        input(t('press_enter'))

    def _configure_wifi_home_router(self) -> None:
        """Configure home router WiFi credentials."""
        print(t('wifi_configure_intro'))

        # Load existing config
        existing = self.load_wifi_config()

        ssid = get_input(t('wifi_ssid_prompt'), existing.get('ssid', ''))
        if not ssid:
            print(t('wifi_ssid_required'))
            input(t('press_enter'))
            return

        password = get_input(t('wifi_password_prompt'), '', password=True)

        print(t('wifi_config_header'))
        print(t('wifi_config_ssid').format(v=ssid))
        pass_display = '*' * len(password) if password else '(none)'
        print(t('wifi_config_pass').format(v=pass_display))

        if confirm(t('wifi_save_confirm')):
            self.save_wifi_config(ssid, password)
            # If Case A was previously active, deactivate it
            if NO_HOME_WIFI_FILE.exists():
                self._deactivate_case_a()
            else:
                subprocess.run(['systemctl', 'restart', 'wifi-switch'],
                               capture_output=True)
            print(t('wifi_saved'))

            # Connect the Pi itself to WiFi
            if confirm(t('wifi_connect_now')):
                ok, msg = self.connect_pi_wifi(ssid, password)
                if ok:
                    print(f"\n  {msg}")
                else:
                    print(t('wifi_connect_fail').format(v=msg))

        input(t('press_enter'))

    def load_wifi_config(self) -> dict:
        """Load existing WiFi configuration."""
        if not WIFI_CONFIG.exists():
            return {}

        config = {}
        try:
            for line in WIFI_CONFIG.read_text().splitlines():
                if '=' in line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    config[key.strip().lower()] = value.strip().strip('"\'')
        except Exception:
            pass
        return config

    def save_wifi_config(self, ssid: str, password: str):
        """Save WiFi configuration."""
        if self.dry_run:
            print(f"[DRY-RUN] Would save WiFi config to {WIFI_CONFIG}")
            return

        content = f'SSID="{ssid}"\nPASSWORD="{password}"\n'
        # Write via sudo — /etc/otgw may be root-owned on non-prebake images
        subprocess.run(
            ['sudo', 'mkdir', '-p', str(CONFIG_DIR)], check=False
        )
        subprocess.run(
            ['sudo', 'tee', str(WIFI_CONFIG)],
            input=content, text=True,
            stdout=subprocess.DEVNULL, check=True,
        )
        subprocess.run(
            ['sudo', 'chmod', '600', str(WIFI_CONFIG)], check=False
        )

    def find_wifi_interface(self) -> Optional[str]:
        """Find a wireless network interface, preferring client over AP."""
        sys_net = Path('/sys/class/net')
        if not sys_net.exists():
            return None
        # uap* is the virtual AP interface (10.42.0.1); on Pi 4+ both uap0 and
        # wlan0 appear as wireless. sorted() puts uap0 first, so skip uap*
        # interfaces and only fall back to one if no client interface exists.
        fallback = None
        for iface in sorted(sys_net.iterdir()):
            if (iface / 'wireless').exists():
                if not iface.name.startswith('uap'):
                    return iface.name
                if fallback is None:
                    fallback = iface.name
        return fallback

    def connect_pi_wifi(self, ssid: str, password: str) -> tuple[bool, str]:
        """Connect the Raspberry Pi to WiFi."""
        if self.dry_run:
            print(f"[DRY-RUN] Would connect Pi to WiFi '{ssid}'")
            return True, 'Connected (dry-run)'

        iface = self.find_wifi_interface()
        if not iface:
            return False, ('No WiFi interface found. '
                           'Pi 1 needs a USB WiFi adapter.')

        # Try NetworkManager first (sudo required on Trixie even for netdev group)
        if shutil.which('nmcli'):
            ret, stdout, stderr = run_command([
                'sudo', 'nmcli', 'device', 'wifi', 'connect', ssid,
                'password', password
            ])
            if ret == 0:
                return True, f'Connected to {ssid} via NetworkManager'
            return False, stderr.strip() or 'nmcli connection failed'

        # Fall back to wpa_supplicant
        if shutil.which('wpa_passphrase'):
            ret, stdout, stderr = run_command([
                'wpa_passphrase', ssid, password
            ])
            if ret != 0:
                return False, 'wpa_passphrase failed'

            wpa_conf = Path('/etc/wpa_supplicant/wpa_supplicant.conf')
            header = (
                'ctrl_interface=DIR=/var/run/wpa_supplicant '
                'GROUP=netdev\n'
                'update_config=1\n'
                'country=NL\n\n'
            )
            # /etc/wpa_supplicant/ is root-owned — write via sudo tee
            subprocess.run(
                ['sudo', 'mkdir', '-p', str(wpa_conf.parent)], check=False
            )
            subprocess.run(
                ['sudo', 'tee', str(wpa_conf)],
                input=header + stdout, text=True,
                capture_output=True
            )
            subprocess.run(
                ['sudo', 'chmod', '600', str(wpa_conf)], check=False
            )

            # Reconfigure the interface
            run_command(['sudo', 'wpa_cli', '-i', iface, 'reconfigure'])
            # Request DHCP
            run_command(['sudo', 'dhcpcd', iface])
            time.sleep(5)

            # Verify connection
            ret, stdout, stderr = run_command([
                'wpa_cli', '-i', iface, 'status'
            ])
            if ret == 0 and 'COMPLETED' in stdout:
                return True, f'Connected to {ssid} via wpa_supplicant'
            return False, 'wpa_supplicant association failed'

        return False, 'No WiFi manager available (need nmcli or wpa_supplicant)'

    def test_wifi_connection(self, ssid: str, password: str):
        """Test WiFi connection by connecting and checking internet."""
        print(t('wifi_testing').format(v=ssid))

        ok, msg = self.connect_pi_wifi(ssid, password)
        if ok:
            print(f"  {msg}")
            # Check internet connectivity
            ret, _, _ = run_command(
                ['ping', '-c', '1', '-W', '5', '8.8.8.8']
            )
            if ret == 0:
                print(t('wifi_internet_ok'))
            else:
                print(t('wifi_internet_fail'))
        else:
            print(t('wifi_connect_fail').format(v=msg))

    # =========================================================================
    # P1 Dongle Configuration
    # =========================================================================

    def configure_p1_dongle(self):
        """Configure P1 dongle via USB serial."""
        print_header(t('p1_title'))

        print(t('p1_intro1'))
        print(t('p1_intro2'))

        # Find serial device
        device = self.find_p1_dongle()
        if not device:
            print(t('p1_not_found'))
            print(t('p1_searching'))

            device = get_input(t('p1_manual_path'))
            if not device:
                input("\nPress Enter to continue...")
                return
        else:
            print(t('p1_found').format(v=device))
            if not confirm(t('p1_use_device'), default=True):
                device = get_input(t('p1_enter_path'))
                if not device:
                    input(t('press_enter'))
                    return

        # Load WiFi config
        wifi = self.load_wifi_config()
        if not wifi.get('ssid'):
            print(t('p1_no_wifi'))
            input(t('press_enter'))
            return

        # Get MQTT broker (default to this Pi's hostname)
        hostname = self.get_hostname()
        broker = get_input(t('p1_broker_prompt'), hostname)

        # Build MQTT topic: home/{image_id}/energy/gas (or test/ prefix in test mode)
        image_id = get_image_id()
        mode = get_mode()
        root = 'test' if mode == 'test' else 'home'
        mqtt_topic = (f'{root}/{image_id}/energy/gas' if image_id
                      else f'{root}/energy/gas')

        # Build configuration
        config = {
            'wifi_ssid': wifi['ssid'],
            'wifi_password': wifi.get('password', ''),
            'mqtt_server': broker,
            'mqtt_port': 1883,
            'mqtt_topic': mqtt_topic,
        }

        print(t('p1_config_header'))
        print(f"  WiFi SSID: {config['wifi_ssid']}")
        print(f"  WiFi Password: {'*' * len(config['wifi_password'])}")
        print(f"  MQTT Server: {config['mqtt_server']}:{config['mqtt_port']}")
        print(f"  MQTT Topic: {config['mqtt_topic']}")

        if confirm(t('p1_send_confirm')):
            self.send_p1_config(device, config)

        input(t('press_enter'))

    def find_p1_dongle(self) -> Optional[str]:
        """Find P1 dongle serial device."""
        import glob

        for pattern in ['/dev/ttyACM*', '/dev/ttyUSB*']:
            devices = glob.glob(pattern)
            if devices:
                return devices[0]
        return None

    def flash_p1_dongle(self):
        """Flash P1 dongle firmware via USB serial using esptool."""
        print_header(t('p1_flash_title'))

        fw_path = Path('/opt/otgw/firmware/p1_reader.bin')
        if not fw_path.exists():
            print(t('p1_flash_not_found'))
            input(t('press_enter'))
            return

        device = self.find_p1_dongle()
        if not device:
            print(t('p1_not_found'))
            device_input = get_input(t('p1_manual_path'))
            if not device_input:
                input(t('press_enter'))
                return
            device = device_input
        else:
            print(t('p1_found').format(v=device))
            if not confirm(t('p1_use_device'), default=True):
                device_input = get_input(t('p1_enter_path'))
                if not device_input:
                    input(t('press_enter'))
                    return
                device = device_input

        if not confirm(f"Flash {fw_path.name} to {device}?", default=True):
            input(t('press_enter'))
            return

        if self.dry_run:
            print(f"[DRY-RUN] Would flash {fw_path} to {device}")
            input(t('press_enter'))
            return

        # Find esptool (prefer 'esptool' over deprecated 'esptool.py')
        esptool_cmd = None
        for cmd in ['esptool', 'esptool.py', 'python3 -m esptool']:
            try:
                result = subprocess.run(
                    cmd.split() + ['version'],
                    capture_output=True, timeout=5
                )
                if result.returncode == 0:
                    esptool_cmd = cmd.split()
                    break
            except Exception:
                continue

        if not esptool_cmd:
            print(t('p1_flash_esptool_missing'))
            input(t('press_enter'))
            return

        print(t('p1_flash_flashing').format(v=device))
        try:
            cmd = esptool_cmd + [
                '--chip', 'esp32s3',
                '--port', device,
                '--baud', '921600',
                '--before', 'default-reset',
                '--after', 'hard-reset',
                'write-flash', '-z',
                '--flash-mode', 'dio',
                '--flash-freq', '80m',
                '0x10000', str(fw_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            # Print output so the user sees progress/errors
            if result.stdout:
                print(result.stdout, end='')
            if result.stderr:
                print(result.stderr, end='')
            if result.returncode == 0:
                print(t('p1_flash_success'))
            else:
                # Detect wrong chip and give a helpful message
                combined = result.stdout + result.stderr
                import re as _re
                chip_match = _re.search(
                    r'This chip is (\S+), not ESP32-S3', combined)
                if chip_match:
                    print(t('p1_flash_wrong_chip').format(
                        port=device, chip=chip_match.group(1)))
                else:
                    print(t('p1_flash_failed').format(
                        v=f'exit code {result.returncode}'))
        except subprocess.TimeoutExpired:
            print(t('p1_flash_failed').format(v='timeout'))
        except Exception as e:
            print(t('p1_flash_failed').format(v=str(e)))

        input(t('press_enter'))

    # =========================================================================
    # Log Partition Setup
    # =========================================================================

    def setup_log_partition(self):
        """Create a 1 GB FAT32 partition at /var/log/otgw."""
        print_header(t('log_part_title'))

        lp = get_log_partition_info()
        if lp['mounted'] and lp['fstype'] == 'vfat':
            print(t('log_part_already_fat32').format(v=lp['device']))
            input(t('press_enter'))
            return

        print(t('log_part_setup_intro'))
        print()
        print(t('log_part_setup_plan'))
        print()
        print(t('log_part_setup_warning'))
        print()

        if not confirm(t('log_part_setup_confirm'), default=False):
            input(t('press_enter'))
            return

        if self.dry_run:
            print(t('dry_run_prefix') + 'setup_log_partition.sh')
            input(t('press_enter'))
            return

        # Check tools
        for tool in ('parted', 'mkfs.vfat', 'resize2fs'):
            ret, _, _ = run_command(['which', tool])
            if ret != 0:
                print(t('log_part_tool_missing').format(v=tool))
                input(t('press_enter'))
                return

        disk = '/dev/mmcblk0'
        log_part = '/dev/mmcblk0p3'
        log_dir = '/var/log/otgw'

        # Check disk size
        ret, stdout, _ = run_command(
            ['lsblk', '-b', '-dn', '-o', 'SIZE', disk])
        if ret != 0:
            print(t('log_part_disk_error').format(v=disk))
            input(t('press_enter'))
            return
        try:
            total_bytes = int(stdout.strip())
        except ValueError:
            print(t('log_part_disk_error').format(v=disk))
            input(t('press_enter'))
            return

        log_size = 1 * 1024 * 1024 * 1024   # 1 GB
        if total_bytes <= 2 * log_size:
            print(t('log_part_disk_too_small').format(
                v=total_bytes // 1024 // 1024))
            input(t('press_enter'))
            return

        new_p2_end = total_bytes - log_size

        # Stop mqtt-logger so no writes during partition ops
        print(t('log_part_stopping_logger'))
        run_command(['sudo', 'systemctl', 'stop', 'mqtt-logger'])

        # Copy existing log files before remounting
        import tempfile
        tmp_copy = None
        if Path(log_dir).is_dir():
            tmp_copy = tempfile.mkdtemp(prefix='otgw_logs_backup_')
            run_command(['sudo', 'cp', '-a', log_dir + '/.', tmp_copy + '/'])

        steps = [
            (['sudo', 'parted', '-s', disk,
              'resizepart', '2', f'{new_p2_end}B'],
             t('log_part_step_resize')),
            (['sudo', 'resize2fs', f'{disk}p2'],
             t('log_part_step_resize2fs')),
            (['sudo', 'partprobe', disk],
             t('log_part_step_partprobe')),
            (['sudo', 'parted', '-s', disk,
              'mkpart', 'primary', 'fat32',
              f'{new_p2_end}B', '100%'],
             t('log_part_step_mkpart')),
            (['sudo', 'mkfs.vfat', '-F', '32', '-n', 'OTGW-LOGS', log_part],
             t('log_part_step_format')),
        ]

        ok = True
        for cmd, step_msg in steps:
            print(step_msg)
            ret, _, err = run_command(cmd)
            if ret != 0:
                print(t('log_part_step_failed').format(v=err.strip()))
                ok = False
                break

        if ok:
            # Add fstab entry if not present
            fstab_line = (
                f'/dev/mmcblk0p3 /var/log/otgw vfat '
                f'defaults,noatime,uid=1000,gid=1000,umask=022 0 2\n'
            )
            try:
                with open('/etc/fstab') as f:
                    fstab = f.read()
            except OSError:
                fstab = ''
            if log_part not in fstab:
                print(t('log_part_step_fstab'))
                fstab_append = (
                    '\n# OTGW log partition — FAT32, readable on '
                    'Windows/macOS\n' + fstab_line
                )
                try:
                    result = subprocess.run(
                        ['sudo', 'tee', '-a', '/etc/fstab'],
                        input=fstab_append, text=True,
                        capture_output=True, timeout=10
                    )
                    if result.returncode != 0:
                        print(t('log_part_step_failed').format(
                            v=result.stderr.strip()))
                        ok = False
                except Exception as e:
                    print(t('log_part_step_failed').format(v=str(e)))
                    ok = False

        if ok:
            print(t('log_part_step_mount'))
            run_command(['sudo', 'mkdir', '-p', log_dir])
            ret, _, err = run_command(['sudo', 'mount', log_dir])
            if ret != 0:
                print(t('log_part_step_failed').format(v=err.strip()))
                ok = False

        if ok and tmp_copy:
            print(t('log_part_step_restore'))
            run_command(['sudo', 'cp', '-a', tmp_copy + '/.', log_dir + '/'])

        if tmp_copy:
            run_command(['sudo', 'rm', '-rf', tmp_copy])

        # Restart mqtt-logger
        run_command(['sudo', 'systemctl', 'start', 'mqtt-logger'])

        if ok:
            print()
            print(t('log_part_setup_done'))
        else:
            print()
            print(t('log_part_setup_failed'))

        input(t('press_enter'))

    def get_hostname(self) -> str:
        """Get this Pi's hostname."""
        try:
            import socket
            return socket.gethostname() + '.local'
        except Exception:
            return 'localhost'

    def send_p1_config(self, device: str, config: dict):
        """Send configuration to P1 dongle via serial."""
        if self.dry_run:
            print(f"[DRY-RUN] Would send config to {device}")
            return

        try:
            import serial
        except ImportError:
            print(t('p1_pyserial_missing'))
            return

        try:
            print(t('p1_opening').format(v=device))
            # dtr=False prevents accidental ESP32 reset on port open
            ser = serial.Serial(device, 115200, timeout=1)
            ser.dtr = False
            ser.rts = False
            time.sleep(2)  # Wait for device to finish any startup output
            ser.reset_input_buffer()  # Discard buffered startup/debug messages

            # Send configuration
            config_json = json.dumps(config)
            message = f"{P1_CONFIG_TOPIC}{config_json}\n"

            print(t('p1_sending').format(v=P1_CONFIG_TOPIC))
            ser.write(message.encode())
            ser.flush()

            # Read lines until P1CONFIG: response (firmware sends debug lines first)
            print(t('p1_waiting'))
            response = ''
            deadline = time.time() + 10
            while time.time() < deadline:
                line = ser.readline().decode(errors='replace').strip()
                if not line:
                    continue
                if line.startswith('P1CONFIG:'):
                    response = line
                    break

            if not response:
                print(t('p1_unexpected').format(v='(timeout — no response received)'))
            elif response == 'P1CONFIG:OK':
                print(t('p1_success').format(v=response))
            elif response.startswith('P1CONFIG:ERROR'):
                print(t('p1_error_response').format(v=response))
            else:
                print(t('p1_unexpected').format(v=response))

            ser.close()

        except serial.SerialException as e:
            print(t('p1_serial_error').format(v=e))
        except Exception as e:
            print(f"\nError: {e}")

    # =========================================================================
    # P1 USB Buffer Sync
    # =========================================================================

    def p1_usb_sync(self):
        """Retrieve buffered gas readings from P1 dongle via USB."""
        import p1_usb_sync as _usb

        print_header(t('p1_usb_sync_title'))
        print(t('p1_usb_sync_intro'))
        print()

        device = self.find_p1_dongle()
        if not device:
            print(t('p1_usb_sync_no_port'))
            input(t('press_enter'))
            return

        print(f'  Port: {device}')

        # Query status
        status = _usb.query_status(device)
        if status:
            print(t('p1_usb_sync_status').format(
                count=status.get('pending_count', '?'),
                bytes=status.get('file_bytes', '?'),
            ))
        else:
            print(t('p1_usb_sync_status_err'))
        print()

        print('  [1] Sync (undelivered records only)')
        print('  [2] Full buffer dump (from byte 0)')
        print('  [3] Mark as received on dongle (send CLEAR)')
        print('  [4] Back')
        print()

        try:
            choice = input(f'{t("select_option")} [1-4]: ').strip()
        except (KeyboardInterrupt, EOFError):
            return

        if choice == '4' or not choice:
            return

        if choice == '3':
            if not confirm(t('p1_usb_sync_clear_prompt'), default=False):
                return
            ok, msg = _usb.send_clear(device)
            if ok:
                print(t('p1_usb_sync_cleared').format(msg=msg))
            else:
                print(t('p1_usb_sync_clear_err').format(msg=msg))
            input(t('press_enter'))
            return

        full = (choice == '2')
        image_id = get_image_id()
        mode     = get_mode()
        eff_dir  = str(LOG_DIR / image_id)

        count_holder: list[int] = [0]

        def _progress(n: int) -> None:
            count_holder[0] = n
            print(f'\r  {t("p1_usb_sync_running").format(n=n)}', end='',
                  flush=True)

        print()
        result = _usb.run_sync(
            port=device,
            effective_dir=eff_dir,
            image_id=image_id,
            mode=mode,
            full=full,
            progress_cb=_progress,
        )
        print()

        if result.success:
            print(t('p1_usb_sync_done').format(
                merged=result.merged,
                skipped=result.skipped,
                malformed=result.malformed,
            ))
            if result.merged > 0 and confirm(
                t('p1_usb_sync_clear_prompt'), default=False
            ):
                ok, msg = _usb.send_clear(device)
                if ok:
                    print(t('p1_usb_sync_cleared').format(msg=msg))
                else:
                    print(t('p1_usb_sync_clear_err').format(msg=msg))
        else:
            print(t('p1_usb_sync_error').format(error=result.error))

        input(t('press_enter'))

    # Log Sync Configuration (multi-target)
    # =========================================================================

    def _is_sync_prebaked(self, name: str, config: dict) -> bool:
        """Return True if this sync target was pre-baked into the image."""
        key_path = config.get('sync_ssh_key', f'/etc/otgw/sync_key_{name}')
        return (
            Path(key_path).exists()
            and Path(str(key_path) + '.pub').exists()
            and (CONFIG_DIR / 'pi-id').exists()
        )

    def manage_sync_targets(self):
        """Manage log sync targets (multi-VPS)."""
        while True:
            print_header(t('sync_title'))
            targets = self.list_sync_targets()

            if targets:
                print(t('sync_configured'))
                for name, config in targets.items():
                    host = config.get('sync_remote_host', '(no host)')
                    user = config.get('sync_remote_user', 'logbackup')
                    prebaked = self._is_sync_prebaked(name, config)
                    status = ' [pre-registered]' if prebaked else ''
                    print(f"  {name:<20} {user}@{host}{status}")
                if any(self._is_sync_prebaked(n, c) for n, c in targets.items()):
                    print()
                    print(t('sync_prebaked_hint'))
            else:
                print(t('sync_none'))

            print()
            print(t('sync_add'))
            if targets:
                print(t('sync_remove'))
                print(t('sync_test_one'))
                print(t('sync_test_all'))
            print(t('sync_flush'))
            print(t('sync_back'))

            choice = input(f"\n{t('choice_prompt')}: ").strip().lower()

            if choice == 'a':
                self.add_sync_target()
            elif choice == 'r' and targets:
                self.remove_sync_target(targets)
            elif choice == 't' and targets:
                self.test_one_sync_target(targets)
            elif choice == 'x' and targets:
                self.test_all_sync_targets()
            elif choice == 'f':
                self.flush_and_sync()
            elif choice in ('b', ''):
                break

    def list_sync_targets(self) -> dict:
        """Return {name: config_dict} for all targets in sync.d/."""
        targets = {}
        if SYNC_D_DIR.exists():
            for conf_file in sorted(SYNC_D_DIR.glob('*.conf')):
                targets[conf_file.stem] = self._load_conf_file(conf_file)
        return targets

    def _load_conf_file(self, conf_file: Path) -> dict:
        """Load key=value pairs from a shell conf file."""
        config = {}
        try:
            for line in conf_file.read_text().splitlines():
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    key, value = line.split('=', 1)
                    config[key.strip().lower()] = value.strip().strip('"\'')
        except Exception:
            pass
        return config

    def add_sync_target(self):
        """Add a new sync target."""
        print_header(t('sync_add_title'))

        name = get_input(t('sync_name_prompt'), '').strip().lower()
        if not name:
            return
        name = re.sub(r'[^a-z0-9_-]', '-', name).strip('-')
        if not name:
            print(t('sync_invalid_name'))
            input(t('press_enter'))
            return

        host = get_input(t('sync_host_prompt'), '')
        if not host:
            input(t('press_enter'))
            return

        user = get_input(t('sync_user_prompt'), 'logbackup')
        mode = get_mode()
        default_path = 'otgw-logs/test' if mode == 'test' else 'otgw-logs'
        if mode == 'test':
            print(t('sync_mode_test_path').format(v=default_path))
        path = get_input(t('sync_path_prompt'), default_path)
        ssh_key = CONFIG_DIR / f'sync_key_{name}'

        print(t('sync_summary_target').format(v=name))
        print(t('sync_summary_host').format(v=host))
        print(t('sync_summary_user').format(v=user))
        print(t('sync_summary_path').format(v=path))
        print(t('sync_summary_key').format(v=ssh_key))

        if not confirm(t('sync_save_confirm')):
            input(t('press_enter'))
            return

        self.save_target_config(name, host, user, path, ssh_key)
        print(t('sync_saved').format(v=name))

        key_was_present = ssh_key.exists()
        if not key_was_present:
            if confirm(t('sync_gen_key')):
                self.generate_ssh_key(ssh_key)

        pub_key_path = Path(str(ssh_key) + '.pub')
        if pub_key_path.exists():
            if key_was_present:
                # Key was already there (pre-baked) — skip registration step.
                print(t('sync_key_prebaked'))
            else:
                self.show_key_registration(host, user, path, pub_key_path)

        if confirm(t('sync_test_now')):
            self._run_sync_test(name)

        input(t('press_enter'))

    def save_target_config(
        self, name: str, host: str, user: str, path: str, ssh_key: Path
    ):
        """Save a sync target config to sync.d/{name}.conf.

        SYNC_REMOTE_PATH is always written as "." because show_key_registration
        generates a per-Pi rrsync entry whose root already includes the Pi's
        own subdirectory.  log_sync.sh treats "." as per-Pi direct mode.
        """
        if self.dry_run:
            print(f"[DRY-RUN] Would save sync target '{name}'")
            return
        SYNC_D_DIR.mkdir(parents=True, exist_ok=True)
        conf_file = SYNC_D_DIR / f'{name}.conf'
        content = (
            f'SYNC_REMOTE_HOST="{host}"\n'
            f'SYNC_REMOTE_USER="{user}"\n'
            f'SYNC_REMOTE_PATH="."\n'
            f'SYNC_SSH_KEY="{ssh_key}"\n'
        )
        conf_file.write_text(content)
        conf_file.chmod(0o600)

    def remove_sync_target(self, targets: dict):
        """Remove a sync target."""
        print_header(t('sync_remove_title'))
        names = list(targets.keys())
        for i, name in enumerate(names, 1):
            host = targets[name].get('sync_remote_host', '?')
            print(f"  {i}. {name}  ({host})")

        choice = get_input(t('sync_remove_prompt'), '')
        if not choice:
            return
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(names):
                name = names[idx]
                if confirm(t('sync_remove_confirm').format(v=name)):
                    self._delete_target(name)
        except ValueError:
            pass
        input(t('press_enter'))

    def _delete_target(self, name: str):
        """Delete a sync target config file (SSH key is kept)."""
        if self.dry_run:
            print(f"[DRY-RUN] Would delete sync.d/{name}.conf")
            return
        conf_file = SYNC_D_DIR / f'{name}.conf'
        if conf_file.exists():
            conf_file.unlink()
            print(t('sync_removed').format(v=name))
            print(t('sync_key_kept'))
            print(f"  sudo rm {CONFIG_DIR}/sync_key_{name}{{,.pub}}")
        else:
            print(t('sync_not_found').format(v=name))

    def test_one_sync_target(self, targets: dict):
        """Select and test a single sync target."""
        print_header(t('sync_test_title'))
        names = list(targets.keys())
        for i, name in enumerate(names, 1):
            host = targets[name].get('sync_remote_host', '?')
            print(f"  {i}. {name}  ({host})")

        choice = get_input(t('sync_test_prompt'), '')
        if not choice:
            return
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(names):
                self._run_sync_test(names[idx])
        except ValueError:
            pass
        input(t('press_enter'))

    def test_all_sync_targets(self):
        """Test all configured sync targets."""
        print(t('sync_testing_all'))
        ret, stdout, stderr = run_command(
            ['/opt/otgw/scripts/log_sync.sh', '--test'], self.dry_run
        )
        if stdout:
            print(stdout)
        if ret == 0:
            print(t('sync_all_ok'))
        else:
            print(t('sync_some_failed'))
            if stderr:
                print(stderr)
        input(t('press_enter'))

    def flush_and_sync(self) -> None:
        """Trigger an immediate RAM→FAT32 flush followed by a VPS sync."""
        print()
        if self.dry_run:
            print(t('dry_run_prefix') + 'systemctl start log-flush.service log-sync.service')
            input(t('press_enter'))
            return
        try:
            subprocess.run(
                ['sudo', 'systemctl', 'start', 'log-flush.service',
                 'log-sync.service'],
                check=True,
            )
            print(t('sync_flush_started'))
        except subprocess.CalledProcessError as e:
            print(t('sync_flush_failed').format(err=e))
        input(t('press_enter'))

    def _run_sync_test(self, name: str):
        """Test a specific named sync target."""
        print(t('sync_testing_one').format(v=name))
        ret, stdout, stderr = run_command(
            ['/opt/otgw/scripts/log_sync.sh', '--test', '--target', name],
            self.dry_run,
        )
        if stdout:
            print(stdout)
        if ret == 0:
            print(t('sync_test_ok').format(v=name))
        else:
            print(t('sync_test_fail').format(v=name))
            if stderr:
                print(stderr)

    # Legacy single-target config (kept for backward compatibility)

    def load_sync_config(self) -> dict:
        """Load legacy sync.conf (single-target)."""
        if not SYNC_CONFIG.exists():
            return {}
        return self._load_conf_file(SYNC_CONFIG)

    def save_sync_config(self, host: str, user: str, path: str):
        """Save legacy sync.conf (single-target)."""
        if self.dry_run:
            print(f"[DRY-RUN] Would save sync config to {SYNC_CONFIG}")
            return
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        content = (
            f'SYNC_REMOTE_HOST="{host}"\n'
            f'SYNC_REMOTE_USER="{user}"\n'
            f'SYNC_REMOTE_PATH="{path}"\n'
        )
        SYNC_CONFIG.write_text(content)
        SYNC_CONFIG.chmod(0o600)

    def generate_ssh_key(self, key_path: Path):
        """Generate SSH key for sync."""
        if self.dry_run:
            print(f"[DRY-RUN] Would generate SSH key at {key_path}")
            return

        key_path.parent.mkdir(parents=True, exist_ok=True)

        ret, stdout, stderr = run_command([
            'ssh-keygen', '-t', 'ed25519', '-f', str(key_path), '-N', ''
        ])

        if ret == 0:
            print(t('ssh_key_generated').format(v=key_path))
            pub_key = Path(str(key_path) + '.pub').read_text().strip()
            print(t('ssh_public_key'))
            print("-" * 40)
            print(pub_key)
            print("-" * 40)
        else:
            print(t('ssh_key_fail').format(v=stderr))

    def show_key_registration(
        self,
        host: str,
        user: str,
        remote_path: str,
        pub_key_path: Path,
    ):
        """Show the rrsync-prefixed authorized_keys line and offer bootstrap."""
        pub_key = pub_key_path.read_text().strip()
        image_id = get_image_id()

        # Per-Pi rrsync: lock this key to the Pi's own subdirectory only.
        # rrsync root = /home/{user}/{path}/{image_id}/ — the Pi cannot write
        # outside its own directory, even if the key is compromised.
        # log_sync.sh detects SYNC_REMOTE_PATH="." and syncs directly to the
        # rrsync root without appending any extra path components.
        pi_log_dir = f'/home/{user}/{remote_path}/{image_id}' if image_id else f'/home/{user}/{remote_path}'
        rrsync_line = (
            f'command="rrsync -wo {pi_log_dir}/",'
            f'no-port-forwarding,no-X11-forwarding,'
            f'no-agent-forwarding,no-pty {pub_key}'
        )

        print(t('ssh_install_line').format(user=user, host=host))
        print("-" * 60)
        print(rrsync_line)
        print("-" * 60)
        if image_id:
            print(t('ssh_scoped_hint').format(v=image_id))

        if confirm(t('ssh_install_confirm').format(user=user, host=host)):
            self.install_sync_key(host, user, image_id, remote_path, rrsync_line)

    def install_sync_key(
        self,
        host: str,
        user: str,
        image_id: str,
        remote_path: str,
        rrsync_line: str,
    ):
        """Create Pi log dir on VPS and register the rrsync-restricted key.

        The per-Pi log directory must exist before rrsync can write to it
        (rrsync does not allow shell mkdir; rsync only creates subdirectories
        within an existing root).  We create it here, while the installer
        still has normal shell access as the backup user.
        """
        if self.dry_run:
            pi_dir = f'/home/{user}/{remote_path}/{image_id}' if image_id else f'/home/{user}/{remote_path}'
            print(
                f"[DRY-RUN] Would create {pi_dir} and append rrsync key"
                f" to {user}@{host}:~/.ssh/authorized_keys"
            )
            return

        print(t('ssh_connecting').format(v=f'{user}@{host}'))
        print(t('ssh_password_prompt'))

        pi_dir = (
            f'/home/{user}/{remote_path}/{image_id}'
            if image_id
            else f'/home/{user}/{remote_path}'
        )
        cmd = [
            'ssh', f'{user}@{host}',
            f'mkdir -p {shlex.quote(pi_dir)} && '
            f'mkdir -p ~/.ssh && chmod 700 ~/.ssh && '
            f'echo {shlex.quote(rrsync_line)} >> ~/.ssh/authorized_keys && '
            f'chmod 600 ~/.ssh/authorized_keys && '
            f'echo "Directory created and key registered successfully."',
        ]

        try:
            ret = subprocess.run(cmd).returncode
            if ret != 0:
                print(t('ssh_cmd_fail').format(v=pi_dir))
        except FileNotFoundError:
            print(t('ssh_not_found'))

    # =========================================================================
    # System Verification
    # =========================================================================

    # =========================================================================
    # Simulators
    # =========================================================================

    SIMULATOR_PAIRS = [
        ('otgw-simulator',     'otgw-listener'),
        ('ds18b20-simulator',  'ds18b20-listener'),
        ('p1-simulator',       None),
    ]

    def manage_simulators(self):
        """Enable / disable simulator services."""
        mode = get_mode()
        print_header(t('sim_title'))

        if mode == 'production':
            print(_colour(t('sim_prod_warning'), _RED))
            print()
            if not confirm(t('sim_continue_anyway')):
                return

        while True:
            print_header(t('sim_title'))
            print(t('sim_mode').format(v=mode.upper()))

            for sim, real in self.SIMULATOR_PAIRS:
                sim_active = self._service_active(sim)
                real_active = self._service_active(real) if real else False
                if sim_active:
                    state = _colour(t('sim_state_simulated'), _YELLOW)
                elif real_active:
                    state = t('sim_state_real')
                else:
                    state = t('sim_state_off')
                label = sim.replace('-simulator', '')
                print(f"  {label:<20} {state}")

            print()
            options = [
                ('enable_all',  t('sim_enable_all')),
                ('disable_all', t('sim_disable_all')),
                ('toggle',      t('sim_toggle_one')),
                ('back',        t('back')),
            ]
            choice = print_menu(options)
            if choice == 'back':
                break
            elif choice == 'enable_all':
                for sim, real in self.SIMULATOR_PAIRS:
                    self._set_simulator(sim, real, enable=True)
                print(t('sim_all_started'))
                input(t('press_enter'))
            elif choice == 'disable_all':
                for sim, real in self.SIMULATOR_PAIRS:
                    self._set_simulator(sim, None, enable=False)
                print(t('sim_all_stopped'))
                input(t('press_enter'))
            elif choice == 'toggle':
                self._toggle_individual_simulator()

    def _service_active(self, name: Optional[str]) -> bool:
        if not name:
            return False
        ret, _, _ = run_command(['systemctl', 'is-active', name])
        return ret == 0

    def _set_simulator(self, sim: str, real: Optional[str],
                       enable: bool) -> None:
        if self.dry_run:
            action = 'start' if enable else 'stop'
            print(f"[DRY-RUN] Would {action} {sim}")
            return
        if enable:
            if real:
                run_command(['sudo', 'systemctl', 'stop', real])
            run_command(['sudo', 'systemctl', 'unmask', sim])
            run_command(['sudo', 'systemctl', 'start', sim])
        else:
            run_command(['sudo', 'systemctl', 'stop', sim])
            # Re-enable real service if it should be running
            if real:
                run_command(['sudo', 'systemctl', 'start', real])

    def _toggle_individual_simulator(self) -> None:
        print()
        on_str = t('sim_state_simulated')
        off_str = t('sim_state_off')
        for i, (sim, _) in enumerate(self.SIMULATOR_PAIRS, 1):
            label = sim.replace('-simulator', '')
            state = on_str if self._service_active(sim) else off_str
            print(f"  {i}. {label:<20} [{state}]")
        choice = get_input(t('sim_toggle_prompt'), '')
        if not choice:
            return
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(self.SIMULATOR_PAIRS):
                sim, real = self.SIMULATOR_PAIRS[idx]
                enable = not self._service_active(sim)
                self._set_simulator(sim, real, enable=enable)
        except ValueError:
            pass
        input(t('press_enter'))

    # =========================================================================
    # System Verification
    # =========================================================================

    def verify_systems(self):
        """Verify all systems are working."""
        print_header(t('verify_title'))

        checks = [
            (t('check_image_id'),    self.check_image_id),
            (t('check_wifi'),        self.check_wifi),
            (t('check_internet'),    self.check_internet),
            (t('check_mqtt'),        self.check_mqtt_broker),
            (t('check_otgw'),        self.check_data_source,
             'otgw-listener', 'otgw-simulator'),
            (t('check_ds18b20'),     self.check_data_source,
             'ds18b20-listener', 'ds18b20-simulator'),
            (t('check_p1'),          self.check_data_source,
             None, 'p1-simulator'),
            (t('check_logger'),      self.check_service, 'mqtt-logger'),
            (t('check_sync_timer'),  self.check_timer, 'log-sync'),
            (t('check_log_dir'),     self.check_log_dir),
            (t('check_log_part'),    self.check_log_partition),
            (t('check_log_disk'),    self.check_log_disk),
            (t('check_sync_conf'),   self.check_sync_configured),
        ]

        results = []
        for item in checks:
            name = item[0]
            func = item[1]
            args = item[2:] if len(item) > 2 else ()

            print(t('verify_checking').format(v=name), end=' ', flush=True)
            try:
                ok, msg = func(*args)
                status = '✓' if ok else '✗'
                print(f"{status} {msg}")
                results.append((name, ok, msg))
            except Exception as e:
                print(t('check_error').format(v=e))
                results.append((name, False, str(e)))

        # Summary
        print("\n" + "=" * 40)
        passed = sum(1 for _, ok, _ in results if ok)
        print(t('verify_summary').format(ok=passed, total=len(results)))
        any_fail = any(not ok for _, ok, _ in results)

        # Finish Installation — only offered when all checks pass and AP active
        if not any_fail and CONFIG_ACTIVE.exists():
            print()
            print(_colour(
                'All checks passed. Config AP is active.', _GREEN,
            ))
            if confirm('Finish installation and stop config AP / portal?',
                       default=False):
                self._finish_installation(results)
                return
        elif not any_fail and not CONFIG_ACTIVE.exists():
            print()
            print(_colour('All checks passed.', _GREEN))

        input(t('press_enter'))

    def _write_signoff(
        self, results: list[tuple[str, bool, str]]
    ) -> 'Path | None':
        """Write timestamped sign-off JSON to LOG_DIR/<image_id>/."""
        from datetime import datetime
        image_id = get_image_id()
        ts = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        checks = [
            {'label': name, 'status': 'pass' if ok else 'fail', 'value': msg}
            for name, ok, msg in results
        ]
        total  = len(checks)
        passed = sum(1 for c in checks if c['status'] == 'pass')
        failed = total - passed
        record = {
            'timestamp': ts,
            'image_id':  image_id,
            'total':     total,
            'passed':    passed,
            'warned':    0,
            'failed':    failed,
            'overall':   'fail' if failed else 'pass',
            'checks':    checks,
        }
        tag      = ts.replace(':', '').replace('-', '')
        filename = f'signoff_{image_id}_{tag}.json'
        out_dir  = LOG_DIR / image_id
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / filename
            out_path.write_text(json.dumps(record, indent=2) + '\n')
            print(_colour(f'Sign-off written: {out_path}', _GREEN))
            return out_path
        except OSError as e:
            print(_colour(f'Sign-off write failed: {e}', _YELLOW))
            return None

    def _finish_installation(
        self,
        results: 'list[tuple[str, bool, str]] | None' = None,
    ) -> None:
        """Write sign-off record and stop the config AP."""
        if results:
            self._write_signoff(results)
        print()
        try:
            AP_TOGGLE_FLAG.write_text('stop\n')
        except OSError as e:
            print(_colour(f'Could not write toggle flag: {e}', _RED))
            input(t('press_enter'))
            return

        print('Stopping config AP…', end=' ', flush=True)
        for _ in range(12):
            time.sleep(0.5)
            if not CONFIG_ACTIVE.exists():
                break
        if CONFIG_ACTIVE.exists():
            print(_colour('(may take a few more seconds)', _YELLOW))
        else:
            print(_colour('done.', _GREEN))
        print('Installation complete. Portal and config WiFi are closed.')
        input(t('press_enter'))

    # =========================================================================
    # Setup Wizard
    # =========================================================================

    # Step definitions — each maps to a menu action and a set of check methods
    _WIZARD_STEPS: list[dict] = [
        {
            'id': 'wifi',
            'title': 'WiFi / Internet',
            'checks': ['wifi', 'internet'],
            'action': 'wifi',
        },
        {
            'id': 'p1',
            'title': 'P1 Dongle',
            'checks': ['p1'],
            'action': 'p1',
        },
        {
            'id': 'hardware',
            'title': 'Hardware sensors',
            'checks': ['otgw', 'ds18b20'],
            'action': 'verify',
        },
        {
            'id': 'services',
            'title': 'Services & logging',
            'checks': ['mqtt', 'logger', 'log_dir', 'log_part', 'log_disk'],
            'action': 'verify',
        },
        {
            'id': 'sync',
            'title': 'VPS sync',
            'checks': ['sync_conf', 'sync_timer'],
            'action': 'sync',
        },
        {
            'id': 'checks',
            'title': 'All checks',
            'checks': ['image_id', 'wifi', 'internet', 'mqtt', 'otgw',
                       'ds18b20', 'p1', 'logger', 'sync_timer',
                       'log_dir', 'log_part', 'log_disk', 'sync_conf'],
            'action': 'verify',
        },
        {
            'id': 'finish',
            'title': 'Finish & stop portal',
            'checks': [],
            'action': 'finish',
        },
    ]

    def _wizard_check_all(self) -> dict[str, tuple[bool, str]]:
        """Run all wizard checks; return {check_id: (ok, msg)}."""
        results: dict[str, tuple[bool, str]] = {}

        def _run(key: str, func, *args):
            try:
                ok, msg = func(*args)
            except Exception as e:
                ok, msg = False, str(e)
            results[key] = (ok, msg)

        _run('image_id',   self.check_image_id)
        _run('wifi',       self.check_wifi)
        _run('internet',   self.check_internet)
        _run('mqtt',       self.check_mqtt_broker)
        _run('otgw',       self.check_data_source,
             'otgw-listener', 'otgw-simulator')
        _run('ds18b20',    self.check_data_source,
             'ds18b20-listener', 'ds18b20-simulator')
        _run('p1',         self.check_data_source, None, 'p1-simulator')
        _run('logger',     self.check_service, 'mqtt-logger')
        _run('sync_timer', self.check_timer, 'log-sync')
        _run('log_dir',    self.check_log_dir)
        _run('log_part',   self.check_log_partition)
        _run('log_disk',   self.check_log_disk)
        _run('sync_conf',  self.check_sync_configured)
        return results

    def _wizard_step_status(
        self,
        step: dict,
        by_check: dict[str, tuple[bool, str]],
    ) -> str:
        """Return 'pass', 'fail', or 'pending' for a wizard step."""
        if step['id'] == 'finish':
            # Finish step: pass only when all other checks pass
            all_pass = all(ok for ok, _ in by_check.values())
            return 'pass' if all_pass else 'fail'
        keys = step['checks']
        if not keys:
            return 'pending'
        if any(k not in by_check for k in keys):
            return 'pending'
        if all(by_check[k][0] for k in keys):
            return 'pass'
        return 'fail'

    def setup_wizard(self) -> None:
        """Interactive step-by-step setup wizard."""
        while True:
            print_header(t('wizard_title'))
            print(t('wizard_intro'))
            print()

            # Run all checks
            print('Checking status…', end=' ', flush=True)
            by_check = self._wizard_check_all()
            print('done.')
            print()

            steps = self._WIZARD_STEPS
            statuses = [
                self._wizard_step_status(s, by_check) for s in steps
            ]

            # Count complete steps (exclude finish step)
            done = sum(
                1 for s, st in zip(steps, statuses)
                if s['id'] != 'finish' and st == 'pass'
            )
            total = sum(1 for s in steps if s['id'] != 'finish')

            # Render step list
            _icons = {
                'pass':    _colour('✓', _GREEN),
                'fail':    _colour('✗', _RED),
                'pending': '○',
            }
            _labels = {
                'pass':    t('wizard_step_done_label'),
                'fail':    t('wizard_step_fail_label'),
                'pending': t('wizard_step_pending_label'),
            }

            for i, (step, status) in enumerate(zip(steps, statuses), start=1):
                icon  = _icons[status]
                label = _labels[status]
                print(f"  {i}. {icon}  {step['title']}  {label}")
            print()
            print(t('wizard_progress').format(done=done, total=total))
            print()

            all_pass = all(st == 'pass' for st in statuses)
            if all_pass:
                print(_colour(t('wizard_all_done'), _GREEN))
                print()

            # Quick-controls footer
            wifi_ssh   = get_wifi_ssh()
            ap_pw      = get_config_ap_pw()
            ssh_str    = (
                _colour('ON  [s to disable]', _YELLOW)
                if wifi_ssh == 'enabled'
                else 'OFF [s to enable]'
            )
            ap_pw_str  = (
                _colour('ON  [a to disable]', _YELLOW)
                if ap_pw
                else 'OFF [a to enable]'
            )
            from ap_password import derive_ap_password
            _dev = (DEVICE_ID_FILE.read_text().strip()
                    if DEVICE_ID_FILE.exists() else 'unknown')
            _ap_ssid = f'otgw-{_dev}'
            _ap_pass = derive_ap_password()
            print('─' * 52)
            print(f'  WiFi SSH: {ssh_str}')
            print(f'  AP pwd:   {ap_pw_str}')
            print(f'    SSID:     {_ap_ssid}')
            print(f'    Password: {_ap_pass}')
            print()

            # Prompt
            print(t('wizard_step_prompt'))
            raw = input('> ').strip().lower()
            if not raw:
                break

            if raw == 's':
                enable = (wifi_ssh != 'enabled')
                self._apply_wifi_ssh(enable)
                continue

            if raw == 'a':
                self._set_config_ap_pw(not ap_pw)
                continue

            try:
                choice = int(raw)
            except ValueError:
                continue

            if not 1 <= choice <= len(steps):
                continue

            step = steps[choice - 1]
            action = step['action']

            if action == 'wifi':
                self.configure_wifi()
            elif action == 'p1':
                self.configure_p1_dongle()
            elif action == 'sync':
                self.manage_sync_targets()
            elif action == 'verify':
                self.verify_systems()
            elif action == 'finish':
                status = statuses[choice - 1]
                if status != 'pass':
                    print(_colour(
                        'Not all checks pass — complete earlier steps first.',
                        _YELLOW,
                    ))
                    input(t('press_enter'))
                elif CONFIG_ACTIVE.exists():
                    if confirm(t('wizard_finish_prompt'), default=False):
                        wizard_results = [
                            (k, ok, msg)
                            for k, (ok, msg) in by_check.items()
                        ]
                        self._finish_installation(wizard_results)
                        return
                else:
                    print(_colour('All checks passed.', _GREEN))
                    print('Config AP is not active — nothing to stop.')
                    input(t('press_enter'))

    def check_data_flow(self) -> None:
        """Subscribe to MQTT briefly and show which data streams are active."""
        import threading

        mode = get_mode()
        root = 'test' if mode == 'test' else 'home'

        print_header(t('data_flow_title'))
        print(t('data_flow_mode').format(v=mode.upper()))
        print()
        print(t('data_flow_note'))
        print()

        # Three expected data streams and the topic fragment that identifies each
        image_id = get_image_id()
        sources: dict[str, dict] = {
            'gas':  {'label': t('data_flow_source_gas'),  'key': 'energy/gas',          'log': 'gas',         'seen': False},
            'otgw': {'label': t('data_flow_source_otgw'), 'key': 'energy/otgw',         'log': 'otgw',        'seen': False},
            'temp': {'label': t('data_flow_source_temp'), 'key': 'sensors/temperature', 'log': 'temperature', 'seen': False},
        }

        # Pre-populate from log files (most reliable — no MQTT connection needed)
        for key, info in sources.items():
            ts = _last_log_ts(image_id, info['log']) if image_id else None
            info['last_ts'] = ts
            # Also check live MQTT cache (covers gap since last log flush)
            if self.cache.recent(info['key']) or (ts is not None and time.time() - ts < 65):
                info['seen'] = True

        done = threading.Event()
        if all(s['seen'] for s in sources.values()):
            done.set()

        def on_connect(client, userdata, flags, rc, props):
            if rc == 0:
                client.subscribe(f'{root}/#')

        def on_message(client, userdata, msg):
            for info in sources.values():
                if info['key'] in msg.topic and not info['seen']:
                    info['seen'] = True
            if all(s['seen'] for s in sources.values()):
                done.set()

        try:
            import paho.mqtt.client as _mqtt
            client = _mqtt.Client(_mqtt.CallbackAPIVersion.VERSION2,
                                  client_id='otgw-verify')
            client.on_connect = on_connect
            client.on_message = on_message
            client.connect('localhost', 1883, keepalive=30)
            client.loop_start()
        except Exception as e:
            print(t('data_flow_mqtt_err').format(v=e))
            input(t('press_enter'))
            return

        max_wait = 65
        try:
            for elapsed in range(max_wait):
                if done.is_set():
                    break
                sys.stdout.write(
                    f"\r{t('data_flow_waiting').format(s=elapsed, max=max_wait)}  "
                )
                sys.stdout.flush()
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            client.loop_stop()
            client.disconnect()

        sys.stdout.write('\n\n')

        # Show per-source checklist with time-since-last-reading
        for info in sources.values():
            if info['seen']:
                mark = _colour('✓', _GREEN)
            else:
                mark = _colour('✗', _RED)
            age_str = format_reading_age(info.get('last_ts'))
            label   = info['label']
            age_col = (
                _colour(age_str, _GREEN)  if info['seen']
                else _colour(age_str, _RED) if info.get('last_ts') is not None
                else _colour('no data', _RED)
            )
            print(f"  {mark}  {label:<30} {age_col}")
        print()

        all_ok = all(s['seen'] for s in sources.values())
        any_seen = any(s['seen'] for s in sources.values())
        if all_ok:
            print(_colour(t('data_flow_all_ok'), _GREEN))
        elif any_seen:
            print(_colour(t('data_flow_partial'), _YELLOW))
        else:
            print(_colour(t('data_flow_none'), _RED))

        input('\n' + t('press_enter'))

    def check_mqtt_broker(self) -> tuple[bool, str]:
        """Check if MQTT broker is running."""
        ret, stdout, stderr = run_command(['systemctl', 'is-active', 'mosquitto'])
        if ret == 0:
            return True, t('check_running')
        return False, t('check_not_running')

    def check_service(self, name: str) -> tuple[bool, str]:
        """Check if a systemd service is running."""
        ret, stdout, stderr = run_command(['systemctl', 'is-active', name])
        if ret == 0:
            return True, t('check_running')
        return False, stdout.strip() or t('check_not_running')

    def _otgw_device_present(self) -> bool:
        """Return True if otgw-listener journal shows a connected device."""
        ret, stdout, _ = run_command(
            ['journalctl', '-u', 'otgw-listener', '-n', '50', '--no-pager']
        )
        return ret == 0 and 'No serial device found' not in stdout

    def _p1_mqtt_active(self) -> bool:
        """Return True if a P1/gas message was received within the cache window."""
        image_id = get_image_id()
        fragment = f'{image_id}/energy/gas' if image_id else 'energy/gas'
        return self.cache.recent(fragment)

    def check_data_source(
        self, real: Optional[str], sim: str
    ) -> tuple[bool, str]:
        """Check a data source: reports SIMULATED, running, or WARNING."""
        sim_active = self._service_active(sim)
        real_active = self._service_active(real) if real else False

        if sim_active and real_active:
            return True, _colour(t('check_sim_conflict'), _YELLOW)
        if sim_active:
            return True, _colour(t('check_simulated'), _YELLOW)
        if real_active:
            if real == 'otgw-listener' and not self._otgw_device_present():
                return True, _colour(t('check_real_hw_retrying'), _YELLOW)
            return True, t('check_real_hw')
        if real:
            return False, t('check_not_running')
        # P1: no listener service — check live MQTT data from external dongle
        if self._p1_mqtt_active():
            return True, t('check_real_hw')
        return False, t('check_no_p1')

    def check_timer(self, name: str) -> tuple[bool, str]:
        """Check if a systemd timer is active."""
        ret, stdout, stderr = run_command(['systemctl', 'is-active', f'{name}.timer'])
        if ret == 0:
            return True, t('check_active')
        return False, t('check_not_active')

    def check_wifi(self) -> tuple[bool, str]:
        """Check if WiFi is connected."""
        status = self.get_wifi_status()
        if not status['interface']:
            return False, t('check_no_wifi_iface')
        if status['ssid']:
            ip_info = f", IP {status['ip']}" if status['ip'] else ''
            return True, f"{status['ssid']}{ip_info}"
        return False, f"{status['interface']} not connected"

    def check_internet(self) -> tuple[bool, str]:
        """Check internet connectivity."""
        ret, stdout, stderr = run_command(['ping', '-c', '1', '-W', '5', '8.8.8.8'])
        if ret == 0:
            return True, t('check_connected')
        return False, t('check_no_connection')

    def check_image_id(self) -> tuple[bool, str]:
        """Check that an Image ID has been generated."""
        image_id = get_image_id()
        if image_id:
            return True, image_id
        return False, t('check_image_id_missing')

    def check_log_dir(self) -> tuple[bool, str]:
        """Check that the log directory exists."""
        log_dir = Path('/var/log/otgw')
        if log_dir.is_dir():
            return True, str(log_dir)
        return False, t('check_log_dir_missing').format(v=log_dir)

    def check_log_partition(self) -> tuple[bool, str]:
        """Check that /var/log/otgw is mounted on a FAT32 partition."""
        lp = get_log_partition_info()
        if lp['mounted'] and lp['fstype'] == 'vfat':
            return True, t('log_part_fat32').format(v=lp['device'])
        elif lp['mounted']:
            return False, t('log_part_other').format(v=lp['fstype'])
        return False, t('log_part_ext4')

    def check_log_disk(self) -> tuple[bool, str]:
        """Check available disk space on the log partition."""
        import shutil as _shutil
        log_dir = Path('/var/log/otgw')
        if not log_dir.exists():
            return False, t('check_log_disk_missing')
        try:
            usage = _shutil.disk_usage(log_dir)
            free_mb = usage.free // (1024 * 1024)
            total_mb = usage.total // (1024 * 1024)
            pct_used = 100 * usage.used // usage.total
            msg = f'{free_mb} MB free of {total_mb} MB ({pct_used}% used)'
            # Warn if less than 50 MB free or more than 90% used
            if free_mb < 50 or pct_used >= 90:
                return False, msg + t('check_log_disk_low')
            return True, msg
        except OSError as e:
            return False, str(e)

    def check_sync_configured(self) -> tuple[bool, str]:
        """Check that at least one log sync target is configured."""
        targets = self.list_sync_targets()
        if targets:
            names = ', '.join(targets.keys())
            return True, f'{len(targets)} target(s): {names}'
        legacy = CONFIG_DIR / 'sync.conf'
        if legacy.exists():
            return True, t('check_legacy_sync')
        return False, t('check_no_sync')

    # =========================================================================
    # Live Data View
    # =========================================================================

    def view_live_data(self):
        """View live MQTT data — history first, then live stream."""
        print_header(t('live_title'))
        print(t('live_stop_hint'))
        print()

        def _fmt_payload(raw: str) -> str:
            try:
                return json.dumps(json.loads(raw), indent=2)
            except json.JSONDecodeError:
                return raw

        # --- History section -------------------------------------------------
        now = time.monotonic()
        history = self.cache.get_history()
        if history:
            print(_colour(t('live_history_header'), _YELLOW))
            for ts, topic, payload in history:
                age = int(now - ts)
                print(f"[{age:>3}s ago]  [{topic}]")
                print(_fmt_payload(payload))
            print()
        else:
            print(_colour(t('live_no_history'), _YELLOW))
            print()

        # --- Live separator --------------------------------------------------
        print(_colour(t('live_live_header'), _GREEN))
        print()

        # --- Live stream via cache watcher -----------------------------------
        def on_new(topic: str, payload: str) -> None:
            print(f"\n[{topic}]")
            print(_fmt_payload(payload))

        self.cache.add_watcher(on_new)
        try:
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                termios.tcflush(fd, termios.TCIFLUSH)  # discard buffered Enter
                tty.cbreak(fd)
                sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except Exception as e:
            print(t('live_error').format(v=e))
        finally:
            self.cache.remove_watcher(on_new)
            print(t('live_stopped'))


    # =========================================================================
    # Factory Reset
    # =========================================================================

    # Services stopped and disabled during a reset (mosquitto is left running)
    _RESET_SERVICES = [
        'otgw-listener',
        'ds18b20-listener',
        'mqtt-logger',
        'log-sync.timer',
        'heartbeat.timer',
    ]
    # Simulator services — stopped only (they are masked, not enabled)
    _RESET_SIMULATORS = [
        'otgw-simulator',
        'ds18b20-simulator',
        'p1-simulator',
    ]

    def factory_reset(self):
        """Reset Pi to post-flash state (soft reset — keeps SSH keys)."""
        import datetime

        print_header(t('reset_title'))
        print(_colour(t('reset_warning'), _YELLOW))
        print()

        if not confirm(t('reset_confirm'), default=False):
            return

        # --- Log handling choice ---
        print()
        print(t('reset_log_title'))
        print()
        log_options = [
            ('keep',   t('reset_log_keep')),
            ('backup', t('reset_log_backup')),
            ('delete', t('reset_log_delete')),
        ]
        log_choice = print_menu(log_options)
        if log_choice == 'back':
            return

        print()

        # 1. Stop and disable core services
        print(t('reset_stopping'))
        if not self.dry_run:
            run_command(
                ['sudo', 'systemctl', 'disable', '--now'] + self._RESET_SERVICES
            )
            for sim in self._RESET_SIMULATORS:
                run_command(['sudo', 'systemctl', 'stop', sim])
        else:
            print(f"[DRY-RUN] Would disable+stop: {', '.join(self._RESET_SERVICES)}")
            print(f"[DRY-RUN] Would stop simulators: {', '.join(self._RESET_SIMULATORS)}")

        # 2. Remove WiFi config
        print(t('reset_removing_wifi'))
        if not self.dry_run:
            WIFI_CONFIG.unlink(missing_ok=True)
        else:
            print(f"[DRY-RUN] Would remove {WIFI_CONFIG}")

        # 3. Remove sync targets (keep SSH keys)
        print(t('reset_removing_sync'))
        if not self.dry_run:
            if SYNC_D_DIR.exists():
                for conf in SYNC_D_DIR.glob('*.conf'):
                    conf.unlink()
            SYNC_CONFIG.unlink(missing_ok=True)
        else:
            print(f"[DRY-RUN] Would remove {SYNC_D_DIR}/*.conf and {SYNC_CONFIG}")

        # 4. Reset mode + WiFi SSH
        print(t('reset_resetting_mode'))
        if not self.dry_run:
            MODE_FILE.write_text('production\n')
        else:
            print(f"[DRY-RUN] Would write 'production' to {MODE_FILE}")
        self._apply_wifi_ssh(enable=False)

        # 5. Handle logs
        log_dir = Path('/var/log/otgw')
        if log_choice == 'keep':
            print(t('reset_logs_kept'))
        elif log_choice == 'backup':
            stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            backup = Path(f'/var/log/otgw_backup_{stamp}')
            if not self.dry_run:
                run_command(['sudo', 'mv', str(log_dir), str(backup)])
                run_command(['sudo', 'mkdir', '-p', str(log_dir)])
            else:
                print(f"[DRY-RUN] Would move {log_dir} → {backup}")
            print(t('reset_logs_backed_up').format(v=backup))
        elif log_choice == 'delete':
            if not self.dry_run:
                run_command(['sudo', 'rm', '-rf', str(log_dir)])
                run_command(['sudo', 'mkdir', '-p', str(log_dir)])
            else:
                print(f"[DRY-RUN] Would delete contents of {log_dir}")
            print(t('reset_logs_deleted'))

        print(_colour(t('reset_done'), _GREEN))
        print(t('reset_hint'))
        input(t('press_enter'))

    # =========================================================================
    # Language selection (Step 4)
    # =========================================================================

    def set_language(self):
        """Toggle between English and Dutch."""
        new_lang = 'nl' if get_lang() == 'en' else 'en'
        set_lang(new_lang)
        if not self.dry_run:
            try:
                (CONFIG_DIR / 'language').write_text(new_lang + '\n')
            except OSError:
                pass


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='OTGW Logger Setup TUI',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Test mode - no actual changes made',
    )
    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_args()

    # Initialise language from /etc/otgw/language (default: en)
    _lang_init()

    app = OTGWSetup(dry_run=args.dry_run)
    app.run()


if __name__ == '__main__':
    main()
