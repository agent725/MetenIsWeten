#!/usr/bin/env python3
"""Auto-configure P1 dongle after WiFi credentials are saved.

Scans USB serial ports for a P1 dongle, reads credentials from wifi.conf
and pi-id, and sends a P1CONFIG JSON command over serial.

Called automatically by config_portal.py and wps_connect.py after WiFi
credentials are saved.  Can also be run standalone.  Results go to the
system journal via stdout.

Exit codes:
  0  Success (dongle configured)
  1  Dongle not found on USB
  2  Configuration failed (serial error, timeout, or bad response)
  3  Prerequisites missing (wifi.conf has no SSID, or pyserial absent)
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import glob
import json
import logging
import socket
import sys
import time
from pathlib import Path

# ── config ────────────────────────────────────────────────────────────────────
CONFIG_DIR      = Path('/etc/otgw')
WIFI_CONF       = CONFIG_DIR / 'wifi.conf'
PI_ID_FILE      = CONFIG_DIR / 'pi-id'
MODE_FILE       = CONFIG_DIR / 'mode'

P1_CONFIG_TOPIC = 'P1CONFIG:'
BAUD_RATE       = 115200
SERIAL_TIMEOUT  = 1        # seconds per readline
RESPONSE_WAIT   = 10       # seconds to wait for P1CONFIG: response
SETTLE_TIME     = 2        # seconds to let device settle after open

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('auto-p1-config')


# ── helpers ──────────────────────────────────────────────────────────────────

def read_wifi_conf() -> dict[str, str]:
    """Parse wifi.conf into {ssid, password}."""
    try:
        text = WIFI_CONF.read_text()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for line in text.splitlines():
        if '=' not in line:
            continue
        key, _, val = line.partition('=')
        result[key.strip().lower()] = val.strip().strip('"')
    return result


def get_image_id() -> str:
    try:
        return PI_ID_FILE.read_text().strip()
    except OSError:
        return ''


def get_mode() -> str:
    try:
        return MODE_FILE.read_text().strip()
    except OSError:
        return 'production'


def find_p1_dongle() -> str | None:
    """Return first /dev/ttyACM* or /dev/ttyUSB* device, or None."""
    for pattern in ('/dev/ttyACM*', '/dev/ttyUSB*'):
        devices = sorted(glob.glob(pattern))
        if devices:
            return devices[0]
    return None


def send_p1_config(device: str, config: dict) -> bool:
    """Open serial port and send P1CONFIG JSON. Returns True on success."""
    try:
        import serial
    except ImportError:
        log.error('pyserial not installed — cannot auto-configure P1 dongle')
        return False

    try:
        log.info('Opening %s at %d baud', device, BAUD_RATE)
        ser = serial.Serial(device, BAUD_RATE, timeout=SERIAL_TIMEOUT)
        ser.dtr = False   # prevent accidental ESP32 reset on port open
        ser.rts = False
        time.sleep(SETTLE_TIME)   # let device settle and flush startup output
        ser.reset_input_buffer()

        payload = f'{P1_CONFIG_TOPIC}{json.dumps(config)}\n'
        log.info('Sending P1CONFIG command')
        ser.write(payload.encode())
        ser.flush()

        # Read lines until we get P1CONFIG: response or timeout
        deadline = time.time() + RESPONSE_WAIT
        response = ''
        while time.time() < deadline:
            line = ser.readline().decode(errors='replace').strip()
            if line.startswith(P1_CONFIG_TOPIC):
                response = line
                break

        ser.close()

        if not response:
            log.warning('No P1CONFIG response within %ds', RESPONSE_WAIT)
            return False
        if response == 'P1CONFIG:OK':
            log.info('P1 dongle configured successfully')
            return True
        log.warning('Unexpected P1CONFIG response: %s', response)
        return False

    except Exception as exc:
        log.error('P1 dongle serial error: %s', exc)
        return False


# ── main ──────────────────────────────────────────────────────────────────────

def auto_configure_p1() -> int:
    """Detect dongle, build config, send command. Returns exit code."""
    wifi = read_wifi_conf()
    if not wifi.get('ssid'):
        log.error('wifi.conf missing or has no SSID — cannot configure P1 dongle')
        return 3

    image_id = get_image_id()
    mode  = get_mode()
    root  = 'test' if mode == 'test' else 'home'
    topic = f'{root}/{image_id}/energy/gas' if image_id else f'{root}/energy/gas'

    config = {
        'wifi_ssid':     wifi['ssid'],
        'wifi_password': wifi.get('password', ''),
        'mqtt_server':   socket.gethostname() + '.local',
        'mqtt_port':     1883,
        'mqtt_topic':    topic,
    }

    device = find_p1_dongle()
    if not device:
        log.info('No P1 dongle found on USB — skipping auto-configure')
        return 1

    log.info('P1 dongle found at %s — sending config', device)
    return 0 if send_p1_config(device, config) else 2


def main() -> None:
    sys.exit(auto_configure_p1())


if __name__ == '__main__':
    main()
