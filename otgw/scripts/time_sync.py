#!/usr/bin/env python3
"""time_sync.py — set system clock from P1 smart meter if NTP is unavailable.

Run as a one-shot service at boot (after mosquitto, before heartbeat).

Flow:
  1. Check if NTP is already synchronised  → write "ntp" and exit cleanly.
  2. Check if current year looks sane (>= 2020).  If sane but no NTP, write
     "unknown" — time may be from a previous boot's fake-hwclock or similar.
  3. Subscribe to the P1 gas MQTT topic.  The DSMR telegram timestamp embedded
     in the payload comes from the smart meter's own clock (grid-operator
     synchronised, highly accurate).  Use it to set the system clock.
  4. Write /run/otgw-time-source with the result so heartbeat.py can report it.

Exit codes:
  0  time set from P1, or NTP was already synchronised
  1  P1 not available within timeout, time source unknown
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import paho.mqtt.client as mqtt

# ── config ────────────────────────────────────────────────────────────────────
MQTT_HOST         = 'localhost'
MQTT_PORT         = 1883
WAIT_TIMEOUT_S    = 300      # wait up to 5 minutes for a P1 message
MIN_VALID_YEAR    = 2020     # years below this are clearly wrong
TIME_SOURCE_FILE  = Path('/run/otgw-time-source')
PI_ID_FILE        = Path('/etc/otgw/pi-id')
MODE_FILE         = Path('/etc/otgw/mode')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('time-sync')


def is_ntp_synced() -> bool:
    """Return True if systemd-timesyncd (or chrony) has synced the clock."""
    try:
        r = subprocess.run(
            ['timedatectl', 'show', '--property=NTPSynchronized', '--value'],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip().lower() == 'yes'
    except Exception:
        return False


def current_year() -> int:
    return datetime.now().year


def write_time_source(source: str) -> None:
    try:
        TIME_SOURCE_FILE.write_text(source + '\n')
    except OSError as exc:
        log.warning('Cannot write %s: %s', TIME_SOURCE_FILE, exc)


def get_image_id() -> str:
    try:
        return PI_ID_FILE.read_text().strip()
    except OSError:
        return ''


def get_mode() -> str:
    try:
        m = MODE_FILE.read_text().strip().lower()
        return m if m in ('test', 'production') else 'production'
    except OSError:
        return 'production'


def parse_dsmr_ts(ts_str: str) -> datetime | None:
    """Parse DSMR timestamp YYMMDDHHmmssW/S → datetime (local time aware).

    Example: '260321143000W' → 2026-03-21 14:30:00 CET
    'W' = winter (UTC+1), 'S' = summer (UTC+2).
    """
    s = ts_str.strip()
    if len(s) < 12:
        return None
    try:
        year   = 2000 + int(s[0:2])
        month  = int(s[2:4])
        day    = int(s[4:6])
        hour   = int(s[6:8])
        minute = int(s[8:10])
        second = int(s[10:12])
        dst    = s[12] if len(s) > 12 else 'W'
        offset = timedelta(hours=2 if dst == 'S' else 1)
        tz     = timezone(offset)
        return datetime(year, month, day, hour, minute, second, tzinfo=tz)
    except (ValueError, IndexError):
        return None


def parse_iso_ts(ts_str: str) -> datetime | None:
    """Try to parse an ISO 8601 timestamp string."""
    for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%d %H:%M:%S'):
        try:
            dt = datetime.strptime(ts_str.strip()[:19], fmt[:len(fmt)])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def extract_timestamp(payload: str) -> datetime | None:
    """Try to extract a smart-meter timestamp from a P1 MQTT JSON payload."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None

    # DSMR-format fields (raw telegram timestamp from meter)
    for field in ('meter_ts', 'meter_timestamp', 'dsmr_ts'):
        if field in data and isinstance(data[field], str):
            dt = parse_dsmr_ts(data[field])
            if dt and dt.year >= MIN_VALID_YEAR:
                return dt

    # ISO-format fields
    for field in ('timestamp', 'ts', 'time', 'datetime'):
        if field in data and isinstance(data[field], str):
            dt = parse_iso_ts(data[field])
            if dt and dt.year >= MIN_VALID_YEAR:
                return dt

    return None


def set_system_time(dt: datetime) -> bool:
    """Set system clock using 'date' command.  Requires root."""
    ts_str = dt.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    try:
        r = subprocess.run(
            ['date', '-u', '-s', ts_str],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception as exc:
        log.error('date command failed: %s', exc)
        return False


def wait_for_p1_timestamp(topic: str) -> datetime | None:
    """Subscribe to MQTT gas topic; return first valid timestamp or None."""
    found: list[datetime] = []

    def on_message(_client, _userdata, message):
        dt = extract_timestamp(message.payload.decode(errors='replace'))
        if dt:
            found.append(dt)

    def on_connect(client, _userdata, _flags, _rc, _props=None):
        client.subscribe(topic)
        log.info('Subscribed to %s', topic)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='time-sync')
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    except Exception as exc:
        log.error('Cannot connect to MQTT broker: %s', exc)
        return None

    deadline = time.monotonic() + WAIT_TIMEOUT_S
    while not found and time.monotonic() < deadline:
        client.loop(timeout=1.0)

    client.disconnect()
    return found[0] if found else None


def main() -> int:
    # ── 1. NTP already synced? ───────────────────────────────────────────────
    if is_ntp_synced():
        log.info('NTP synchronised — no action needed')
        write_time_source('ntp')
        return 0

    # ── 2. Check if year looks sane ──────────────────────────────────────────
    year = current_year()
    if year >= MIN_VALID_YEAR:
        log.info(
            'NTP not synced but year=%d looks plausible — '
            'marking as unknown, waiting for P1', year,
        )
        # Still try P1 below; it will give us a better source tag
    else:
        log.warning(
            'Year=%d looks wrong (< %d) — NTP not synced, '
            'trying P1 for time set', year, MIN_VALID_YEAR,
        )

    # ── 3. Try P1 MQTT ───────────────────────────────────────────────────────
    image_id = get_image_id()
    mode  = get_mode()
    prefix = 'test' if mode == 'test' else 'home'
    # Subscribe broadly — catch any gas message regardless of image_id
    topic = f'{prefix}/+/energy/gas'

    log.info('Waiting up to %ds for P1 message on %s...', WAIT_TIMEOUT_S, topic)
    dt = wait_for_p1_timestamp(topic)

    if dt is None:
        if is_ntp_synced():
            log.info('P1 not available but NTP synced during wait — OK')
            write_time_source('ntp')
            return 0
        log.warning('No P1 timestamp received within %ds', WAIT_TIMEOUT_S)
        write_time_source('unknown')
        return 1

    log.info('P1 timestamp: %s', dt.isoformat())

    if dt.year < MIN_VALID_YEAR:
        log.warning('P1 timestamp year=%d looks wrong — ignoring', dt.year)
        write_time_source('unknown')
        return 1

    if set_system_time(dt):
        log.info('System time set from P1 meter: %s', dt.isoformat())
        write_time_source('p1')
        return 0
    else:
        log.error('Failed to set system time')
        write_time_source('unknown')
        return 1


if __name__ == '__main__':
    sys.exit(main())
