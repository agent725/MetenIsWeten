#!/usr/bin/env python3
"""LED status service — drives the Pi PWR (red) LED to show system health.

The two onboard Pi 4 LEDs each serve a distinct role:

  ACT (green, /sys/class/leds/ACT):
    Managed by config_sensor.py for transient states:
      • heartbeat          — idle / normal (kernel default)
      • fast blink         — WPS active (60 s window)
      • double-blink       — config AP active (installer connected)
      • rapid blink        — shutdown warning (hold magnet > 8 s)
    When config_sensor.py is idle ACT reverts to 'heartbeat'.
    This script does NOT touch ACT.

  PWR (red, /sys/class/leds/PWR):
    Managed by this service to show ongoing system health:

      State                          Pattern
      ─────────────────────────────  ─────────────────────────────────────────
      All OK (WiFi + logging)        Solid on   (normal healthy state)
      Config AP active (installer)   Solid on   (health is fine; ACT shows it)
      WiFi degraded (AP cycling)     Slow blink  1 Hz (500 ms / 500 ms)
      WiFi not configured            3-pulse repeating
      Key service down               Double-blink repeating
      Powering off / critical        Off

The patterns are chosen so that PWR = solid means "all fine" and any blink
pattern means "attention needed", with faster / more complex = more urgent.
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── config ────────────────────────────────────────────────────────────────────
POLL_INTERVAL_S  = 10    # check state every N seconds
CRITICAL_SERVICES = ('mosquitto', 'mqtt-logger')


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
WIFI_STATE_FILE = Path('/run/otgw-wifi-state')
WIFI_CONF       = Path('/etc/otgw/wifi.conf')
CONFIG_ACTIVE   = Path('/run/otgw-config-active')

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('led-status')


# ── PWR LED ──────────────────────────────────────────────────────────────────

def _pwr_path() -> Path | None:
    for name in ('PWR', 'led1'):
        p = Path(f'/sys/class/leds/{name}')
        if p.exists():
            return p
    return None


PWR = _pwr_path()

# Background blink thread state
_blink_stop = threading.Event()
_blink_thread: threading.Thread | None = None


def _pwr_write(filename: str, value: str) -> None:
    if PWR is None:
        return
    try:
        (PWR / filename).write_text(value)
    except OSError:
        pass


def _stop_blink_thread() -> None:
    global _blink_thread
    _blink_stop.set()
    if _blink_thread and _blink_thread.is_alive():
        _blink_thread.join(timeout=1)
    _blink_thread = None


def _start_blink_thread(
    pattern: list[tuple[int, float]],
) -> None:
    """Drive PWR LED with a repeating pattern using a background thread.

    pattern: list of (brightness, duration_seconds) steps.
    Kernel trigger must already be set to 'none' before calling.
    """
    global _blink_thread, _blink_stop
    _stop_blink_thread()
    _blink_stop = threading.Event()
    stop = _blink_stop

    def run() -> None:
        while not stop.is_set():
            for brightness, duration in pattern:
                if stop.is_set():
                    return
                _pwr_write('brightness', str(brightness))
                stop.wait(duration)

    _blink_thread = threading.Thread(target=run, daemon=True)
    _blink_thread.start()


def _pwr_solid_on() -> None:
    _stop_blink_thread()
    _pwr_write('trigger',    'none')
    _pwr_write('brightness', '1')


def _pwr_off() -> None:
    _stop_blink_thread()
    _pwr_write('trigger',    'none')
    _pwr_write('brightness', '0')


def _pwr_slow_blink() -> None:
    """1 Hz blink — WiFi degraded."""
    _stop_blink_thread()
    _pwr_write('trigger',   'timer')
    _pwr_write('delay_on',  '500')
    _pwr_write('delay_off', '500')


def _pwr_double_blink() -> None:
    """Double-blink repeating — service down.

    Pattern: on 200 ms, off 200 ms, on 200 ms, off 800 ms.
    """
    _pwr_write('trigger', 'none')
    _start_blink_thread([
        (1, 0.20), (0, 0.20),
        (1, 0.20), (0, 0.80),
    ])


def _pwr_triple_pulse() -> None:
    """3-pulse repeating — WiFi not configured.

    Pattern: three 150 ms pulses, then 1200 ms pause.
    """
    _pwr_write('trigger', 'none')
    _start_blink_thread([
        (1, 0.15), (0, 0.15),
        (1, 0.15), (0, 0.15),
        (1, 0.15), (0, 1.20),
    ])


# ── state detection ───────────────────────────────────────────────────────────

def _wifi_state() -> str:
    try:
        return WIFI_STATE_FILE.read_text().strip()
    except OSError:
        return 'UNKNOWN'


def _services_ok() -> bool:
    for svc in CRITICAL_SERVICES:
        try:
            r = subprocess.run(
                ['systemctl', 'is-active', '--quiet', svc],
                capture_output=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            return False
        if r.returncode != 0:
            return False
    return True


def get_status() -> str:
    """Classify current system state as a short string."""
    if not WIFI_CONF.exists():
        return 'no_config'

    wifi = _wifi_state()

    # Config AP active (installer mode) — health is fine
    if CONFIG_ACTIVE.exists() or wifi == 'PAUSED':
        if _services_ok():
            return 'ok'
        return 'service_down'

    if not _services_ok():
        return 'service_down'

    if wifi == 'CLIENT':
        return 'ok'
    elif wifi in ('WAITING', 'UNKNOWN'):
        return 'no_config'
    elif wifi.startswith('CLIENT_FAILING') or wifi.startswith('AP') or wifi == 'SWITCHING':
        return 'wifi_degraded'

    return 'ok'


# ── LED driver ────────────────────────────────────────────────────────────────

_last_status: str = ''


def apply_status(status: str) -> None:
    global _last_status
    if status == _last_status:
        return   # no change — avoid redundant LED writes

    log.info('System status: %s', status)
    _last_status = status

    if status == 'ok':
        _pwr_solid_on()
    elif status == 'wifi_degraded':
        _pwr_slow_blink()
    elif status == 'no_config':
        _pwr_triple_pulse()
    elif status == 'service_down':
        _pwr_double_blink()
    else:
        _pwr_slow_blink()   # unknown — treat as degraded


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if PWR is None:
        log.warning('No controllable PWR LED found — running in monitor-only mode')

    log.info('led-status starting (poll every %ds)', POLL_INTERVAL_S)

    # Start with solid on to show the service is running
    _pwr_solid_on()

    while True:
        try:
            status = get_status()
            apply_status(status)
        except Exception as exc:
            log.error('Unexpected error: %s', exc, exc_info=True)
        _sd_notify('WATCHDOG=1')  # ping every POLL_INTERVAL_S — well within WatchdogSec=60. (NFR-4.80)
        time.sleep(POLL_INTERVAL_S)


if __name__ == '__main__':
    main()
