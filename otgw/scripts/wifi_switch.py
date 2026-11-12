#!/usr/bin/env python3
"""WiFi AP switching service.

Case B (home WiFi exists but may drop):
  Monitors wlan0 in client mode.  If home WiFi is gone for CLIENT_FAIL_SECS,
  switches wlan0 to AP mode with the same SSID/password as the home router so
  the P1 dongle can reconnect automatically without any firmware changes.
  After AP_SECS the service tries to reconnect to home WiFi.

Case A (no home WiFi ever):
  When /etc/otgw/no-home-wifi exists, immediately starts a permanent AP with
  SSID otgw-<device_id> and a deterministic password derived from Pi hardware.
  Pauses while config_sensor (hall magnet) takes over the interface.

Current state is written to /run/otgw-wifi-state for the TUI to display.
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# ── tunables ────────────────────────────────────────────────────────────────
CLIENT_FAIL_SECS = 120   # seconds without home WiFi before switching to AP
AP_SECS          = 90    # seconds in AP mode before trying to reconnect
CHECK_INTERVAL   = 10    # connectivity poll interval (seconds)

# Watchdog ping interval; systemd kills the service if no ping within
# WatchdogSec (60 s in the unit file). Half that interval is the target.
_WATCHDOG_PING_INTERVAL = 30  # seconds


def _sd_notify(msg: str) -> None:
    """Send a sd_notify message to systemd (no-op when not running under systemd)."""
    path = os.environ.get('NOTIFY_SOCKET', '')
    if not path:
        return
    if path.startswith('@'):
        path = '\0' + path[1:]  # abstract socket namespace
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(path)
            sock.sendall(msg.encode())
    except OSError:
        pass

# ── paths ────────────────────────────────────────────────────────────────────
WIFI_CONF          = Path('/etc/otgw/wifi.conf')
NO_HOME_WIFI_FILE  = Path('/etc/otgw/no-home-wifi')   # presence = Case A
DEVICE_ID_FILE     = Path('/etc/otgw/device-id')
AP_CON             = 'otgw-ap'
STATE_FILE         = Path('/run/otgw-wifi-state')
CONFIG_ACTIVE      = Path('/run/otgw-config-active')  # written by config-button

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('wifi-switch')


# ── helpers ──────────────────────────────────────────────────────────────────

def read_wifi_conf() -> tuple[str, str]:
    """Return (SSID, PASSWORD) from /etc/otgw/wifi.conf."""
    conf: dict[str, str] = {}
    for line in WIFI_CONF.read_text().splitlines():
        line = line.strip()
        if '=' in line:
            key, _, val = line.partition('=')
            conf[key.strip()] = val.strip().strip('"')
    return conf['SSID'], conf['PASSWORD']


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        log.warning('Command timed out (15 s): %s', cmd)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout='', stderr='')


def find_home_connection() -> str | None:
    """Return the NM connection name for the home WiFi.

    All Pi-managed profiles (AP and config AP) start with 'otgw-'.
    Returns the first 802-11-wireless connection that is NOT otgw-managed.
    """
    r = run(['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show'])
    for line in r.stdout.splitlines():
        parts = line.split(':')
        if (len(parts) >= 2
                and parts[1] == '802-11-wireless'
                and not parts[0].startswith('otgw-')):
            return parts[0]
    return None


def is_connected(home_con: str) -> bool:
    """Return True if the home WiFi NM connection is currently activated."""
    r = run(['nmcli', '-t', '-f', 'GENERAL.STATE', 'con', 'show', home_con])
    return 'activated' in r.stdout.lower()


def ensure_ap_profile(ssid: str, password: str) -> None:
    """Create or update the otgw-ap NM connection profile."""
    r = run(['nmcli', 'con', 'show', AP_CON])
    if r.returncode == 0:
        run(['nmcli', 'con', 'modify', AP_CON,
             'wifi.ssid', ssid,
             'wifi-sec.psk', password])
        log.info('AP profile updated: SSID=%s', ssid)
    else:
        r2 = run([
            'nmcli', 'con', 'add',
            'type', 'wifi',
            'ifname', 'wlan0',
            'con-name', AP_CON,
            'ssid', ssid,
            'wifi.mode', 'ap',
            'ipv4.method', 'shared',
            'connection.autoconnect', 'no',
            'wifi-sec.key-mgmt', 'wpa-psk',
            'wifi-sec.psk', password,
        ])
        if r2.returncode == 0:
            log.info('AP profile created: SSID=%s', ssid)
        else:
            log.error('Failed to create AP profile: %s', r2.stderr.strip())


def switch_to_ap(home_con: str) -> bool:
    run(['nmcli', 'con', 'down', home_con])
    r = run(['nmcli', 'con', 'up', AP_CON])
    if r.returncode == 0:
        log.info('AP mode active (SSID matches home router)')
        return True
    log.error('Failed to start AP: %s', r.stderr.strip())
    return False


def switch_to_client(home_con: str) -> None:
    run(['nmcli', 'con', 'down', AP_CON])
    r = run(['nmcli', 'con', 'up', home_con])
    if r.returncode != 0:
        log.warning('nmcli con up %s: %s', home_con, r.stderr.strip())


def write_state(state: str) -> None:
    try:
        STATE_FILE.write_text(state + '\n')
    except OSError:
        pass


# ── Case A helpers ────────────────────────────────────────────────────────────

def is_case_a() -> bool:
    """Return True when the 'no home WiFi' flag file is present."""
    return NO_HOME_WIFI_FILE.exists()


def _get_device_id() -> str:
    try:
        return DEVICE_ID_FILE.read_text().strip()
    except OSError:
        return 'unknown'


def ensure_case_a_ap_profile(ssid: str, password: str) -> None:
    """Create or update the otgw-ap profile for Case A (Pi's own SSID)."""
    r = run(['nmcli', 'con', 'show', AP_CON])
    if r.returncode == 0:
        run(['nmcli', 'con', 'modify', AP_CON,
             'wifi.ssid', ssid,
             'wifi-sec.key-mgmt', 'wpa-psk',
             'wifi-sec.psk', password])
        log.info('Case A AP profile updated: SSID=%s', ssid)
    else:
        r2 = run([
            'nmcli', 'con', 'add',
            'type', 'wifi',
            'ifname', 'wlan0',
            'con-name', AP_CON,
            'ssid', ssid,
            'wifi.mode', 'ap',
            'ipv4.method', 'shared',
            'connection.autoconnect', 'no',
            'wifi-sec.key-mgmt', 'wpa-psk',
            'wifi-sec.psk', password,
        ])
        if r2.returncode == 0:
            log.info('Case A AP profile created: SSID=%s', ssid)
        else:
            log.error('Failed to create Case A AP profile: %s', r2.stderr.strip())


def run_case_a() -> None:
    """Permanent AP mode for residences with no 2.4 GHz home WiFi (Case A).

    Starts Pi's own AP immediately and keeps it running indefinitely.
    Pauses when config_sensor (hall magnet) takes over the interface.
    """
    from ap_password import derive_ap_password  # local import avoids startup cost
    device_id = _get_device_id()
    ssid = f'otgw-{device_id}'
    password = derive_ap_password()

    log.info('Case A mode — permanent AP: SSID=%s', ssid)
    ensure_case_a_ap_profile(ssid, password)

    r = run(['nmcli', 'con', 'up', AP_CON])
    if r.returncode != 0:
        log.error('Failed to start Case A AP: %s', r.stderr.strip())
    else:
        log.info('Case A AP active')

    write_state('CASE_A')

    _last_wd_ping: float = 0.0
    while True:
        try:
            if CONFIG_ACTIVE.exists():
                write_state('PAUSED')
            else:
                r = run(['nmcli', '-t', '-f', 'GENERAL.STATE', 'con', 'show', AP_CON])
                if 'activated' not in r.stdout.lower():
                    log.warning('Case A AP went down — restarting')
                    run(['nmcli', 'con', 'up', AP_CON])

                write_state('CASE_A')
        except Exception as exc:
            log.error('Unexpected error in Case A: %s', exc, exc_info=True)

        time.sleep(CHECK_INTERVAL)
        _now = time.monotonic()
        if _now - _last_wd_ping >= _WATCHDOG_PING_INTERVAL:
            _sd_notify('WATCHDOG=1')
            _last_wd_ping = _now


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info('wifi-switch starting (CLIENT_FAIL=%ds AP=%ds)',
             CLIENT_FAIL_SECS, AP_SECS)

    # Case A: no home WiFi — run permanent Pi AP instead of home-WiFi loop
    if is_case_a():
        run_case_a()
        return  # run_case_a() loops forever; this line is unreachable

    # Wait for wifi.conf to appear (user configures WiFi via TUI first).
    # Ping the watchdog each iteration — the waiting loop can outlast
    # WatchdogSec (60 s) if we sleep without notifying systemd.
    if not WIFI_CONF.exists():
        log.info('Waiting for %s to appear...', WIFI_CONF)
        while not WIFI_CONF.exists():
            write_state('WAITING')
            _sd_notify('WATCHDOG=1')
            time.sleep(30)
        log.info('wifi.conf appeared')

    ssid, password = read_wifi_conf()
    log.info('Home WiFi SSID: %s', ssid)

    home_con = find_home_connection()
    if home_con:
        log.info('Home NM connection: %s', home_con)
    else:
        log.warning('No home WiFi connection found in NM yet')

    ensure_ap_profile(ssid, password)

    # Ping the watchdog after initialisation: the three nmcli calls above
    # (find_home_connection + ensure_ap_profile × 2) each have a 15 s timeout.
    # If NM is slow at boot all three can time out, consuming 45 s before the
    # main loop starts. The first loop iteration adds another nmcli call before
    # the in-loop watchdog ping, potentially exceeding WatchdogSec (60 s).
    _sd_notify('WATCHDOG=1')

    # State machine
    state = 'CLIENT'
    fail_since: float | None = None
    ap_since:   float | None = None

    write_state(state)

    _last_wd_ping: float = 0.0
    while True:
        try:
            # Pause while config-button / config-portal manages the AP.
            # Use else (not continue) so the watchdog ping at the bottom
            # always runs, even during PAUSED iterations.
            if CONFIG_ACTIVE.exists():
                write_state('PAUSED')
            else:
                # Refresh SSID/password and home connection name every cycle
                # in case wifi.conf was updated via TUI
                try:
                    ssid, password = read_wifi_conf()
                except (KeyError, OSError):
                    pass

                home_con = find_home_connection()

                if state == 'CLIENT':
                    connected = home_con is not None and is_connected(home_con)
                    if connected:
                        if fail_since is not None:
                            log.info('Home WiFi restored')
                            fail_since = None
                        write_state('CLIENT')
                    else:
                        if fail_since is None:
                            fail_since = time.monotonic()
                            log.info('Home WiFi lost — will switch to AP in %ds',
                                     CLIENT_FAIL_SECS)
                        elapsed = time.monotonic() - fail_since
                        remaining = int(CLIENT_FAIL_SECS - elapsed)
                        write_state(f'CLIENT_FAILING:{remaining}')
                        if elapsed >= CLIENT_FAIL_SECS:
                            if home_con is None:
                                home_con = find_home_connection()
                            if home_con:
                                ensure_ap_profile(ssid, password)
                                if switch_to_ap(home_con):
                                    state = 'AP'
                                    ap_since = time.monotonic()
                                    write_state('AP')
                                    fail_since = None
                            else:
                                log.warning('Still no home connection in NM — cannot switch')

                elif state == 'AP':
                    elapsed = time.monotonic() - (ap_since or 0)
                    remaining = int(AP_SECS - elapsed)
                    write_state(f'AP:{remaining}')
                    if elapsed >= AP_SECS:
                        log.info('AP timer expired — trying home WiFi reconnect')
                        write_state('SWITCHING')
                        if home_con:
                            switch_to_client(home_con)
                            time.sleep(15)  # give NM time to authenticate
                            if is_connected(home_con):
                                log.info('Reconnected to home WiFi')
                                state = 'CLIENT'
                                ap_since = None
                                fail_since = None
                                write_state('CLIENT')
                            else:
                                log.info('Home WiFi unavailable — back to AP mode')
                                switch_to_ap(home_con)
                                ap_since = time.monotonic()
                                write_state('AP')

        except Exception as exc:
            log.error('Unexpected error: %s', exc, exc_info=True)

        time.sleep(CHECK_INTERVAL)
        _now = time.monotonic()
        if _now - _last_wd_ping >= _WATCHDOG_PING_INTERVAL:
            _sd_notify('WATCHDOG=1')
            _last_wd_ping = _now


if __name__ == '__main__':
    main()
