#!/usr/bin/env python3
"""
System Heartbeat Publisher

Publishes a heartbeat message to MQTT and writes it to the log directory.
Designed to be run periodically via systemd timer (every 5 minutes).

MQTT topic: home/{image_id}/system/heartbeat
Log file:   /var/log/otgw/{image_id}/heartbeat.json (overwritten each run)

Payload includes:
- timestamp (ISO 8601)
- uptime_seconds
- boot_time (ISO 8601)
- image_id, hostname
- disk_usage_pct, disk_avail_mb
- mem_avail_mb
- load_1m
- internet (bool: can reach 8.8.8.8)
- services (status of each OTGW service)
- reboot_reason (best-effort)
- code_version (git commit from /etc/otgw/version, written by provision.sh)
- python_version, os_version (for reproducibility)
- ds18b20_sensors (list of connected sensor IDs)
- otgw_device (detected USB serial device, or null)
- time_source ('ntp', 'p1', or 'unknown')
- time_synced (bool: NTP confirmed clock)
- time_reliable (bool: clock is trustworthy)

Usage:
    heartbeat.py [options]

Examples:
    heartbeat.py                        # Publish to localhost
    heartbeat.py --host 192.168.1.100   # Custom broker
    heartbeat.py --image-id a1b2c3       # Override Image ID
    heartbeat.py -v                     # Verbose output

Requirements:
    - paho-mqtt: apt install python3-paho-mqtt
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PI_ID_FILE = '/etc/otgw/pi-id'
DEVICE_ID_FILE = '/etc/otgw/device-id'
MODE_FILE = '/etc/otgw/mode'
VERSION_FILE = '/etc/otgw/version'
IMAGE_VERSION_FILE = '/etc/otgw/image-version'
LOG_DIR = '/run/otgw'
TIME_SOURCE_FILE = '/run/otgw-time-source'

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Error: paho-mqtt not installed", file=sys.stderr)
    sys.exit(1)


def read_image_id_file() -> Optional[str]:
    """Read Image ID from /etc/otgw/pi-id if it exists."""
    try:
        with open(PI_ID_FILE) as f:
            image_id = f.read().strip()
            if image_id:
                return image_id
    except FileNotFoundError:
        pass
    return None


def read_device_id_file() -> Optional[str]:
    """Read Pi device ID from /etc/otgw/device-id if it exists.

    For pre-baked images: MAC-derived short hex (e.g. '71e1af'), written by
    pi_id.sh at first boot. Identifies the physical device, not the build.
    For legacy images: same value as the Image ID (pi-id), since both are
    MAC-derived.
    """
    try:
        with open(DEVICE_ID_FILE) as f:
            device_id = f.read().strip()
            if device_id:
                return device_id
    except FileNotFoundError:
        pass
    return None


def read_mode() -> str:
    """Read operational mode from /etc/otgw/mode. Returns 'production' or 'test'."""
    try:
        with open(MODE_FILE) as f:
            mode = f.read().strip().lower()
            if mode in ('test', 'production'):
                return mode
    except FileNotFoundError:
        pass
    return 'production'


def build_topic(base_topic: str, image_id: Optional[str],
                mode: str = 'production') -> str:
    """Insert Image ID and mode prefix into topic.

    home/system/heartbeat -> home/a1b2c3/system/heartbeat  (production)
    home/system/heartbeat -> test/a1b2c3/system/heartbeat   (test)
    """
    if mode == 'test':
        parts = base_topic.split('/', 1)
        base_topic = f"test/{parts[1]}" if len(parts) == 2 else f"test/{base_topic}"

    if not image_id:
        return base_topic
    parts = base_topic.split('/', 1)
    if len(parts) == 2:
        return f"{parts[0]}/{image_id}/{parts[1]}"
    return f"{image_id}/{base_topic}"


def get_uptime() -> float:
    """Read system uptime in seconds from /proc/uptime."""
    try:
        with open('/proc/uptime') as f:
            return float(f.read().split()[0])
    except (FileNotFoundError, ValueError):
        return 0.0


def get_boot_time() -> str:
    """Calculate boot time as ISO 8601 string."""
    uptime = get_uptime()
    boot_ts = time.time() - uptime
    return datetime.fromtimestamp(
        boot_ts, tz=timezone.utc
    ).astimezone().isoformat(timespec='seconds')


def get_disk_usage(path: str = '/') -> tuple[int, int]:
    """Return (usage_percent, avail_mb) for the given mount point."""
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        used = total - (st.f_bfree * st.f_frsize)
        if total > 0:
            pct = int(100 * used / total)
        else:
            pct = 0
        avail_mb = int(avail / (1024 * 1024))
        return pct, avail_mb
    except OSError:
        return 0, 0


def get_mem_avail_mb() -> int:
    """Read available memory in MB from /proc/meminfo."""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    kb = int(line.split()[1])
                    return kb // 1024
    except (FileNotFoundError, ValueError):
        pass
    return 0


def get_load_1m() -> float:
    """Read 1-minute load average from /proc/loadavg."""
    try:
        with open('/proc/loadavg') as f:
            return float(f.read().split()[0])
    except (FileNotFoundError, ValueError):
        return 0.0


_PREBAKED_CHARS = frozenset('GHJKMNPQRSTUVWXY')


def is_prebaked_image_id(image_id: str) -> bool:
    """Return True if image_id uses the pre-baked alphabet (GHJKMNPQRSTUVWXY).

    Pre-baked IDs are random strings from the 16-char alphabet with no digits
    and no overlap with hex.  Legacy IDs are 6-char lowercase hex derived from
    the last 3 bytes of the ethernet MAC address.
    """
    return bool(image_id) and all(c in _PREBAKED_CHARS for c in image_id)


def get_mac_id() -> Optional[str]:
    """Read ethernet MAC address from sysfs.

    Tries eth0 first (Raspberry Pi OS default), then common alternatives.
    Returns the MAC address string (e.g. 'dc:a6:32:71:e1:af') or None.
    """
    for iface in ('eth0', 'end0', 'enp0s1'):
        path = Path(f'/sys/class/net/{iface}/address')
        try:
            mac = path.read_text().strip()
            if mac and mac != '00:00:00:00:00:00':
                return mac
        except OSError:
            pass
    return None


def get_reboot_reason() -> str:
    """Best-effort detection of last reboot reason."""
    try:
        result = subprocess.run(
            ['journalctl', '--list-boots', '-n', '2', '-o', 'json'],
            capture_output=True, text=True, timeout=5
        )
        # If there's only one boot, this is the first boot
        lines = [l for l in result.stdout.strip().split('\n') if l]
        if len(lines) <= 1:
            return 'first_boot'
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Check for clean shutdown vs power loss
    try:
        result = subprocess.run(
            ['journalctl', '-b', '-1', '-n', '5',
             '--output=cat', '--no-pager'],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout.lower()
        if 'reboot' in output or 'restart' in output:
            return 'reboot'
        if 'shutdown' in output or 'halt' in output:
            return 'clean_shutdown'
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return 'unknown'


def check_internet() -> bool:
    """Check internet connectivity by pinging a public DNS server."""
    try:
        result = subprocess.run(
            ['ping', '-c', '1', '-W', '3', '8.8.8.8'],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def get_service_status(service: str) -> str:
    """Get systemd service status: 'running', 'failed', 'inactive', etc."""
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', service],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 'unknown'


def get_services_status() -> dict:
    """Get status of all OTGW services (real + simulator)."""
    services = {
        'mosquitto': 'mosquitto',
        'otgw_listener': 'otgw-listener',
        'ds18b20_listener': 'ds18b20-listener',
        'mqtt_logger': 'mqtt-logger',
        'p1_listener': 'p1-listener',
        'resident_portal': 'resident-portal',
        # Simulator services (inactive unless explicitly enabled)
        'otgw_simulator': 'otgw-simulator',
        'ds18b20_simulator': 'ds18b20-simulator',
        'p1_simulator': 'p1-simulator',
    }
    return {name: get_service_status(svc) for name, svc in services.items()}


def get_code_version() -> str:
    """Read deployed code version from /etc/otgw/version (written by provision.sh)."""
    try:
        with open(VERSION_FILE) as f:
            v = f.read().strip()
            return v if v else 'unknown'
    except FileNotFoundError:
        return 'unknown'


def get_image_version() -> str:
    """Read image build version from /etc/otgw/image-version (written by build.sh).

    Format: YYYYMMDD-githash[-label] for base images,
            YYYYMMDD-githash[-label]-IMAGEID for patched images.
    Returns 'unknown' if the file is absent (pre-versioning images).
    """
    try:
        with open(IMAGE_VERSION_FILE) as f:
            v = f.read().strip()
            return v if v else 'unknown'
    except FileNotFoundError:
        return 'unknown'


def get_python_version() -> str:
    """Return running Python version string."""
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def get_os_version() -> str:
    """Read OS pretty name from /etc/os-release."""
    try:
        with open('/etc/os-release') as f:
            for line in f:
                if line.startswith('PRETTY_NAME='):
                    return line.split('=', 1)[1].strip().strip('"')
    except FileNotFoundError:
        pass
    return 'unknown'


def get_sync_circuit_state() -> bool:
    """Check if any sync circuit is open (skip_until in future)."""
    import glob
    from pathlib import Path
    now = time.time()
    for f in glob.glob('/run/otgw-sync-failures/*.skip_until'):
        try:
            skip_until = float(Path(f).read_text().strip())
            if now < skip_until:
                return True
        except (OSError, ValueError):
            pass
    return False


def get_data_freshness(
    image_id: Optional[str],
    log_dir: str = '/run/otgw',
    fat32_dir: str = '/var/log/otgw',
) -> dict:
    """Return seconds since last data for each source, or None if no data.

    Checks RAM disk first (newest data), then FAT32.
    Sources: otgw, temperature, gas.
    """
    from datetime import datetime as _dt

    def _last_ts(path: 'Path') -> Optional[float]:
        try:
            size = path.stat().st_size
            if size == 0:
                return None
            with open(path, 'rb') as fh:
                fh.seek(max(0, size - 512))
                tail = fh.read().decode('utf-8', errors='replace')
            lines = [ln for ln in tail.splitlines() if '|' in ln]
            if not lines:
                return None
            return _dt.fromisoformat(
                lines[-1].split('|')[0].strip()
            ).timestamp()
        except (OSError, ValueError):
            return None

    if not image_id:
        return {'otgw_age_s': None, 'temperature_age_s': None,
                'gas_age_s': None}

    now = time.time()
    today = time.strftime('%Y-%m-%d')
    yesterday = time.strftime('%Y-%m-%d', time.localtime(now - 86400))
    bases = [Path(log_dir) / image_id, Path(fat32_dir) / image_id]
    result: dict = {}

    # Files are stored as: <base>/<date>/<source>_<image_id>.log
    # (date-directory layout introduced 2026-04-17)
    for source in ('otgw', 'temperature'):
        best: Optional[float] = None
        for date in (today, yesterday):
            fname = f'{source}_{image_id}.log'
            for base in bases:
                ts = _last_ts(base / date / fname)
                if ts is not None and (best is None or ts > best):
                    best = ts
            if best is not None:
                break
        result[f'{source}_age_s'] = (
            round(now - best) if best is not None else None
        )

    # Gas: glob fallback for variant P1-topic naming (other-device gas topics
    # land in files like home_fb0e1c_energy_gas_<image_id>.log in same date dir)
    best = None
    for date in (today, yesterday):
        for base in bases:
            date_dir = base / date
            exact = date_dir / f'gas_{image_id}.log'
            candidates = (
                [exact] if exact.exists()
                else sorted(date_dir.glob(f'*gas*{image_id}*.log'))
                if date_dir.is_dir() else []
            )
            for path in candidates:
                ts = _last_ts(path)
                if ts is not None and (best is None or ts > best):
                    best = ts
        if best is not None:
            break
    result['gas_age_s'] = round(now - best) if best is not None else None

    return result


def get_ds18b20_sensors() -> list[str]:
    """Return sorted list of DS18B20 sensor IDs from 1-Wire bus."""
    import glob as _glob
    paths = _glob.glob('/sys/bus/w1/devices/28-*')
    return sorted(os.path.basename(p) for p in paths)


def get_otgw_device() -> Optional[str]:
    """Return first detected OTGW USB serial device path, or None."""
    import glob as _glob
    for pattern in ('/dev/ttyUSB*', '/dev/ttyACM*'):
        devices = _glob.glob(pattern)
        if devices:
            return sorted(devices)[0]
    return None


_HEALTH_WRITE_INTERVAL = 60   # must match HEALTH_WRITE_INTERVAL in otgw_listener.py
_HEALTH_STALE_AFTER    = 2 * _HEALTH_WRITE_INTERVAL   # 120 s


def get_otgw_listener_health() -> Optional[dict]:
    """Read listener health metrics from /run/otgw/otgw-listener-health.json. (NFR-4.65/4.68/4.82)

    Returns the parsed dict, or None if the file does not exist.
    Adds 'otgw_stale' key (True if last_publish_s_ago > 600).

    If the file exists but its mtime is older than _HEALTH_STALE_AFTER seconds,
    the listener has wedged without exiting — return a sentinel dict with
    health_file_stale=True instead of reporting outdated values as current.
    (NFR-4.82)

    Path is /run/otgw/ (shared tmpfs). Both otgw-listener and heartbeat have
    PrivateTmp=true, which creates isolated per-service private /tmp namespaces
    — files written to the private /tmp by one service are invisible to the
    other. (NFR-4.68)
    """
    health_file = Path('/run/otgw/otgw-listener-health.json')
    try:
        mtime = health_file.stat().st_mtime
        data = json.loads(health_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    age = time.time() - mtime
    if age > _HEALTH_STALE_AFTER:
        return {'health_file_stale': True, 'health_file_age_s': int(age)}
    # Mark stale if no message published in >10 min
    last = data.get('last_publish_s_ago')
    data['otgw_stale'] = last is None or last > 600
    return data


def get_time_info() -> dict:
    """Return time synchronisation status.

    time_source: 'ntp' | 'p1' | 'unknown'
    time_synced: True if NTP has confirmed the clock
    time_reliable: True if source is ntp or p1 and year looks sane
    """
    # Read source written by time_sync.py on boot
    source = 'unknown'
    try:
        source = Path(TIME_SOURCE_FILE).read_text().strip().lower()
        if source not in ('ntp', 'p1', 'unknown'):
            source = 'unknown'
    except OSError:
        pass

    # Cross-check with timedatectl (NTP may have synced after boot)
    ntp_synced = False
    try:
        r = subprocess.run(
            ['timedatectl', 'show', '--property=NTPSynchronized', '--value'],
            capture_output=True, text=True, timeout=5,
        )
        ntp_synced = r.stdout.strip().lower() == 'yes'
        if ntp_synced:
            source = 'ntp'
    except Exception:
        pass

    year_ok = datetime.now(tz=timezone.utc).year >= 2020
    reliable = year_ok and source in ('ntp', 'p1')

    return {
        'time_source':   source,
        'time_synced':   ntp_synced,
        'time_reliable': reliable,
    }


def build_heartbeat(image_id: Optional[str], mode: str = 'production') -> dict:
    """Build the heartbeat payload."""
    disk_pct, disk_avail = get_disk_usage('/')
    log_disk_pct, log_disk_avail = get_disk_usage('/var/log/otgw')
    tmpfs_pct, tmpfs_avail = get_disk_usage('/run')
    sensors = get_ds18b20_sensors()
    device_id = read_device_id_file()
    payload: dict = {
        'timestamp': datetime.now(
            tz=timezone.utc
        ).astimezone().isoformat(timespec='seconds'),
        'uptime_seconds': int(get_uptime()),
        'boot_time': get_boot_time(),
        'image_id': image_id or 'unknown',
        'pi_id': device_id or 'unknown',
        'hostname': socket.gethostname(),
        'mode': mode,
        'disk_usage_pct': disk_pct,
        'disk_avail_mb': disk_avail,
        'log_disk_usage_pct': log_disk_pct,
        'log_disk_avail_mb': log_disk_avail,
        'tmpfs_usage_pct': tmpfs_pct,
        'tmpfs_avail_mb': tmpfs_avail,
        'mem_avail_mb': get_mem_avail_mb(),
        'load_1m': round(get_load_1m(), 2),
        'internet': check_internet(),
        'services': get_services_status(),
        'reboot_reason': get_reboot_reason(),
        'image_version': get_image_version(),
        'code_version': get_code_version(),
        'python_version': get_python_version(),
        'os_version': get_os_version(),
        'ds18b20_sensors': sensors,
        'ds18b20_count': len(sensors),
        'otgw_device': get_otgw_device(),
        'otgw_health': get_otgw_listener_health(),
        'sync_circuit_open': get_sync_circuit_state(),
        **get_time_info(),
        **get_data_freshness(image_id),
    }
    # mac_id: included only for pre-baked Pis (random Image ID alphabet).
    # Legacy Pis derive their Image ID from the MAC address, so mac_id would
    # be redundant — and omitting it is a minor privacy saving.
    if is_prebaked_image_id(image_id or ''):
        mac = get_mac_id()
        if mac:
            payload['mac_id'] = mac
    return payload


def write_heartbeat_file(
    payload: dict,
    log_dir: str,
    image_id: Optional[str],
    verbose: bool = False,
) -> bool:
    """Write heartbeat JSON to log directory for log-sync to push to VPS."""
    subdir = os.path.join(log_dir, image_id) if image_id else log_dir
    path = os.path.join(subdir, 'heartbeat.json')
    try:
        os.makedirs(subdir, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(payload, f)
            f.write('\n')
        os.replace(tmp, path)
        if verbose:
            print(f"Wrote {path}")
        return True
    except OSError as e:
        print(f"Error writing {path}: {e}", file=sys.stderr)
        return False


def publish_heartbeat(
    host: str,
    port: int,
    topic: str,
    payload: dict,
    verbose: bool = False,
    max_attempts: int = 3,
) -> bool:
    """Connect to MQTT broker, publish heartbeat, disconnect.

    Retries up to max_attempts times with a 5-second delay between
    attempts so transient broker restarts don't silently drop a beat.
    """
    msg = json.dumps(payload)
    for attempt in range(max_attempts):
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id='heartbeat',
            )
            client.connect(host, port, keepalive=30)
            # loop_start() is required: without it paho's network loop never
            # runs, QoS=1 PUBLISH is never flushed, and wait_for_publish()
            # always times out (NFR-4.72).
            client.loop_start()
            result = client.publish(topic, msg, qos=1)
            result.wait_for_publish(timeout=10)
            client.loop_stop()
            client.disconnect()
            if verbose:
                print(f"Published to {topic}: {msg}")
            return True
        except Exception as e:
            if attempt < max_attempts - 1:
                print(
                    f"Heartbeat publish failed ({e}), "
                    f"retrying in 5s (attempt {attempt + 1}/{max_attempts})...",
                    file=sys.stderr,
                )
                time.sleep(5)
            else:
                print(f"Error publishing heartbeat: {e}", file=sys.stderr)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Publish system heartbeat to MQTT'
    )
    parser.add_argument(
        '--host', default='localhost',
        help='MQTT broker host (default: localhost)'
    )
    parser.add_argument(
        '--port', type=int, default=1883,
        help='MQTT broker port (default: 1883)'
    )
    parser.add_argument(
        '--topic', default='home/system/heartbeat',
        help='MQTT base topic (default: home/system/heartbeat)'
    )
    parser.add_argument(
        '--image-id', default=None,
        help='Image ID override (default: read from /etc/otgw/pi-id)'
    )
    parser.add_argument(
        '--log-dir', default=LOG_DIR,
        help=f'Log directory for heartbeat.json (default: {LOG_DIR})'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Verbose output'
    )
    args = parser.parse_args()

    image_id = args.image_id or read_image_id_file()
    mode = read_mode()
    topic = build_topic(args.topic, image_id, mode)
    payload = build_heartbeat(image_id, mode)

    if args.verbose:
        print(f"Image ID: {image_id}")
        print(f"Topic: {topic}")
        print(f"Payload: {json.dumps(payload, indent=2)}")

    # Write to log directory (for log-sync to push to VPS)
    write_heartbeat_file(payload, args.log_dir, image_id, args.verbose)

    # Publish to MQTT
    success = publish_heartbeat(
        args.host, args.port, topic, payload, args.verbose
    )
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
