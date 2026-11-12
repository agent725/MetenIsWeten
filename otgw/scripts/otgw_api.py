#!/usr/bin/env python3
"""Shared OTGW business-logic API.

Pure functions — no UI, no HTTP.  Import from both the TUI and the web portal.
New features go here first; both frontends call the same implementation.

Functions:
  flash_p1_dongle()         Flash P1 reader firmware via USB serial (esptool)
  install_packages()        Install missing apt packages
  get_missing_packages()    Return list of missing required packages
  find_esptool()            Locate esptool binary / module
  find_p1_firmware()        Return Path to bundled P1 firmware binary
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import subprocess
import threading
import time
from pathlib import Path
from typing import Iterator

# ── constants ─────────────────────────────────────────────────────────────────

FIRMWARE_PATH = Path('/opt/otgw/firmware/p1_reader.bin')

# Baud rate for esptool flash.  921600 and 115200 both drop ~31% through the
# write on Raspberry Pi due to marginal USB power delivery under bulk transfer
# load.  9600 keeps burst sizes within what the Pi USB stack can sustain
# reliably (confirmed on Pi4 2026-04-17, 877 KB firmware, exit 0).
# --after no-reset avoids a second USB re-enumeration that can also fail on Pi.
FLASH_BAUD = 9600
FLASH_MAX_ATTEMPTS = 3     # At 9600 baud failures are rare; 3 is sufficient headroom.
FLASH_RETRY_DELAY_S = 2    # Short pause between attempts to let USB settle.

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

# ── P1 dongle flash ───────────────────────────────────────────────────────────

def find_p1_firmware() -> Path | None:
    """Return Path to bundled P1 firmware binary, or None if not found."""
    return FIRMWARE_PATH if FIRMWARE_PATH.exists() else None


def find_esptool() -> list[str] | None:
    """Return esptool command as list, or None if not installed."""
    for cmd in (['esptool'], ['esptool.py'], ['python3', '-m', 'esptool']):
        try:
            r = subprocess.run(
                cmd + ['version'],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                return cmd
        except Exception:
            continue
    return None


def find_p1_dongle() -> str | None:
    """Return first /dev/ttyACM* or /dev/ttyUSB* device, or None."""
    import glob as _glob
    for pattern in ('/dev/ttyACM*', '/dev/ttyUSB*'):
        devices = sorted(_glob.glob(pattern))
        if devices:
            return devices[0]
    return None


# ── LED feedback during flash ─────────────────────────────────────────────────
# Best-effort: silently ignored on dev machines without the Pi LED sysfs paths.

def _led_write(led: str, filename: str, value: str) -> None:
    try:
        Path(f'/sys/class/leds/{led}/{filename}').write_text(value)
    except OSError:
        pass


_flash_blink_stop: threading.Event | None = None
_flash_blink_thread: threading.Thread | None = None


def _led_flash_start() -> None:
    """Fast-blink the ACT LED while flashing is in progress."""
    global _flash_blink_stop, _flash_blink_thread
    _led_write('ACT', 'trigger', 'none')
    stop = threading.Event()
    _flash_blink_stop = stop

    def _blink() -> None:
        while not stop.is_set():
            _led_write('ACT', 'brightness', '1')
            stop.wait(0.1)
            if stop.is_set():
                break
            _led_write('ACT', 'brightness', '0')
            stop.wait(0.1)

    _flash_blink_thread = threading.Thread(target=_blink, daemon=True)
    _flash_blink_thread.start()


def _led_flash_end(ok: bool) -> None:
    """Stop the flash blink; solid 2 s on success, 10× rapid blink on failure."""
    global _flash_blink_stop, _flash_blink_thread
    if _flash_blink_stop is not None:
        _flash_blink_stop.set()
    if _flash_blink_thread is not None:
        _flash_blink_thread.join(timeout=1)
    _flash_blink_stop = None
    _flash_blink_thread = None

    if ok:
        _led_write('ACT', 'trigger', 'none')
        _led_write('ACT', 'brightness', '1')
        time.sleep(2)
    else:
        for _ in range(10):
            _led_write('ACT', 'brightness', '1')
            time.sleep(0.05)
            _led_write('ACT', 'brightness', '0')
            time.sleep(0.05)
    # Restore heartbeat
    _led_write('ACT', 'trigger', 'heartbeat')


def flash_p1_dongle(device: str | None = None) -> Iterator[str]:
    """Flash P1 dongle firmware.  Yields output lines as they are produced.

    Caller iterates the generator to consume output in real time.
    The last yielded line starts with 'OK:' on success or 'ERROR:' on failure.
    Up to FLASH_MAX_ATTEMPTS are made; a short pause separates retries.

    Example:
        for line in flash_p1_dongle():
            print(line)
    """
    fw = find_p1_firmware()
    if fw is None:
        yield f'ERROR: Firmware not found at {FIRMWARE_PATH}'
        return

    if device is None:
        device = find_p1_dongle()
    if device is None:
        yield 'ERROR: No P1 dongle found (check USB connection)'
        return

    esptool = find_esptool()
    if esptool is None:
        yield 'ERROR: esptool not installed (pip install esptool)'
        return

    cmd = esptool + [
        '--chip', 'esp32s3',
        '--port', device,
        '--baud', str(FLASH_BAUD),
        '--before', 'default-reset',
        '--after', 'no-reset',
        'write-flash', '--no-compress',
        '--flash-mode', 'dio',
        '--flash-freq', '20m',
        '0x10000', str(fw),
    ]
    yield f'Flashing {fw.name} to {device}…'
    yield f'$ {" ".join(cmd)}'

    _led_flash_start()
    ok = False
    last_error = 'Flash did not start'
    try:
        for attempt in range(1, FLASH_MAX_ATTEMPTS + 1):
            if attempt > 1:
                yield f'Retry {attempt}/{FLASH_MAX_ATTEMPTS} (waiting {FLASH_RETRY_DELAY_S} s)…'
                time.sleep(FLASH_RETRY_DELAY_S)
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    yield line.rstrip()
                proc.wait(timeout=600)

                if proc.returncode == 0:
                    ok = True
                    yield 'OK: Flash successful'
                    return
                else:
                    last_error = f'esptool exited with code {proc.returncode}'
                    yield f'Attempt {attempt}/{FLASH_MAX_ATTEMPTS} failed: {last_error}'
            except subprocess.TimeoutExpired:
                last_error = 'Flash timed out after 600 s'
                yield f'Attempt {attempt}/{FLASH_MAX_ATTEMPTS} failed: {last_error}'
            except Exception as exc:
                last_error = str(exc)
                yield f'Attempt {attempt}/{FLASH_MAX_ATTEMPTS} failed: {last_error}'
        yield f'ERROR: {last_error}'
    finally:
        _led_flash_end(ok)


def flash_p1_dongle_collect(device: str | None = None) -> tuple[bool, str]:
    """Convenience wrapper: run flash_p1_dongle and return (ok, output_str)."""
    lines = list(flash_p1_dongle(device))
    ok = bool(lines) and lines[-1].startswith('OK:')
    return ok, '\n'.join(lines)


# ── package management ────────────────────────────────────────────────────────

def get_missing_packages() -> list[str]:
    """Return list of required apt packages that are not installed."""
    missing = []
    for pkg in REQUIRED_PACKAGES:
        r = subprocess.run(
            ['dpkg', '-s', pkg],
            capture_output=True,
        )
        if r.returncode != 0:
            missing.append(pkg)
    return missing


def install_packages(packages: list[str] | None = None) -> tuple[bool, str]:
    """Install missing packages via apt-get.

    If packages is None, installs all missing required packages.
    Returns (ok, output_string).
    """
    if packages is None:
        packages = get_missing_packages()
    if not packages:
        return True, 'All required packages already installed.'

    lines: list[str] = []
    lines.append(f'Installing: {", ".join(packages)}')
    lines.append('$ apt-get update')
    r = subprocess.run(
        ['apt-get', 'update'],
        capture_output=True, text=True, timeout=120,
    )
    lines.append((r.stdout + r.stderr).strip())
    if r.returncode != 0:
        return False, '\n'.join(lines)

    lines.append(f'$ apt-get install -y {" ".join(packages)}')
    r = subprocess.run(
        ['apt-get', 'install', '-y'] + packages,
        capture_output=True, text=True, timeout=300,
    )
    lines.append((r.stdout + r.stderr).strip())
    ok = (r.returncode == 0)
    if ok:
        lines.append('OK: Packages installed successfully.')
    else:
        lines.append(f'ERROR: apt-get exited with code {r.returncode}')
    return ok, '\n'.join(lines)
