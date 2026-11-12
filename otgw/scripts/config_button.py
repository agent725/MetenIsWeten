#!/usr/bin/env python3
"""Config button service — GPIO 14 monitor.

Press durations:
  1–3 s  → WPS PBC (press router WPS button within 60 s)
  5 s+   → Toggle AP mode (phone can connect to Pi's AP and use web portal)

ACT LED patterns (no extra hardware needed):
  Normal          heartbeat (kernel default)
  WPS active      fast blink (100 ms on / 400 ms off)
  AP mode active  double-blink repeating
  Success         solid 3 s then heartbeat
  Failure         rapid 10× blink then heartbeat
"""

import logging
import os
import socket
import subprocess
import sys
import time
import threading
from pathlib import Path

# ── config ───────────────────────────────────────────────────────────────────
GPIO_BUTTON    = 14          # GPIO number (BCM); pin 8 on the 40-pin header
DEBOUNCE_S     = 0.05        # seconds

# Ping watchdog every _WD_PING_EVERY iterations; DEBOUNCE_S × count ≈ 30 s.
# Counter-based (no time.monotonic) to keep the inner idle loop side-effect-free.
_WATCHDOG_PING_INTERVAL = 30  # seconds (informational — see _WD_PING_EVERY)
_WD_PING_EVERY = max(1, int(_WATCHDOG_PING_INTERVAL / DEBOUNCE_S))  # ~600


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
WPS_MIN_S      = 1.0         # minimum press for WPS
WPS_MAX_S      = 4.0         # maximum press for WPS (longer = AP mode)
AP_MIN_S       = 5.0         # minimum press for AP mode toggle
AP_TIMEOUT_S   = 600         # auto-exit AP mode after 10 minutes

# ── paths ────────────────────────────────────────────────────────────────────
CONFIG_ACTIVE  = Path('/run/otgw-config-active')
WPS_SCRIPT     = Path('/opt/otgw/scripts/wps_connect.py')

# ── ACT LED ──────────────────────────────────────────────────────────────────
def _led_path() -> Path | None:
    for name in ('ACT', 'led0', 'led1'):
        p = Path(f'/sys/class/leds/{name}')
        if p.exists():
            return p
    return None

ACT = _led_path()

def _led_write(filename: str, value: str) -> None:
    if ACT is None:
        return
    try:
        (ACT / filename).write_text(value)
    except OSError:
        pass

def led_heartbeat() -> None:
    _led_write('trigger', 'heartbeat')

def led_fast_blink() -> None:
    _led_write('trigger', 'timer')
    _led_write('delay_on', '100')
    _led_write('delay_off', '400')

def led_double_blink() -> None:
    """Double-blink via pattern trigger if available, else fast blink."""
    # Try pattern trigger (Pi OS Bookworm+)
    try:
        if ACT and 'pattern' in (ACT / 'trigger').read_text():
            _led_write('trigger', 'pattern')
            _led_write('pattern', '255 200 0 200 255 200 0 800')
            return
    except OSError:
        pass
    _led_write('trigger', 'timer')
    _led_write('delay_on', '200')
    _led_write('delay_off', '200')

def led_solid_then_heartbeat(seconds: float = 3.0) -> None:
    _led_write('trigger', 'none')
    _led_write('brightness', '1')
    time.sleep(seconds)
    led_heartbeat()

def led_rapid_fail() -> None:
    for _ in range(10):
        _led_write('trigger', 'none')
        _led_write('brightness', '1')
        time.sleep(0.05)
        _led_write('brightness', '0')
        time.sleep(0.05)
    led_heartbeat()

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('config-button')

# ── helpers ──────────────────────────────────────────────────────────────────

def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)

def is_ap_active() -> bool:
    r = run(['nmcli', '-t', '-f', 'GENERAL.STATE', 'con', 'show', 'otgw-ap'])
    return 'activated' in r.stdout.lower()

def enter_ap_mode() -> bool:
    """Stop wifi-switch, activate otgw-ap, write state file."""
    log.info('Entering AP mode for config portal')
    run(['systemctl', 'stop', 'wifi-switch'])
    # Find and bring down the current home WiFi connection
    r = run(['nmcli', '-t', '-f', 'NAME,TYPE,DEVICE', 'con', 'show', '--active'])
    for line in r.stdout.splitlines():
        parts = line.split(':')
        if len(parts) >= 2 and parts[1] == '802-11-wireless' and parts[0] != 'otgw-ap':
            run(['nmcli', 'con', 'down', parts[0]])
            break
    r = run(['nmcli', 'con', 'up', 'otgw-ap'])
    if r.returncode == 0:
        CONFIG_ACTIVE.write_text('ap\n')
        log.info('AP mode active — portal accessible at http://10.42.0.1/')
        return True
    log.error('Failed to start AP mode: %s', r.stderr.strip())
    run(['systemctl', 'start', 'wifi-switch'])
    return False

def exit_ap_mode() -> None:
    """Deactivate otgw-ap, restart wifi-switch, remove state file."""
    log.info('Exiting AP mode')
    run(['nmcli', 'con', 'down', 'otgw-ap'])
    CONFIG_ACTIVE.unlink(missing_ok=True)
    run(['systemctl', 'start', 'wifi-switch'])
    led_solid_then_heartbeat(2)
    log.info('Returned to client mode')

def trigger_wps() -> None:
    log.info('WPS PBC triggered')
    led_fast_blink()
    if not WPS_SCRIPT.exists():
        log.error('wps_connect.py not found at %s', WPS_SCRIPT)
        led_rapid_fail()
        return
    r = subprocess.run(
        [sys.executable, str(WPS_SCRIPT)],
        capture_output=False,   # let it log to journal
    )
    if r.returncode == 0:
        log.info('WPS succeeded')
        led_solid_then_heartbeat(3)
    else:
        log.warning('WPS failed or timed out')
        led_rapid_fail()

# ── AP mode monitor ──────────────────────────────────────────────────────────

_ap_timer: threading.Timer | None = None

def _ap_timeout() -> None:
    if CONFIG_ACTIVE.exists():
        log.info('AP mode timeout — returning to client mode')
        exit_ap_mode()

def toggle_ap_mode() -> None:
    global _ap_timer
    if is_ap_active() or CONFIG_ACTIVE.exists():
        # Cancel any pending timeout and exit
        if _ap_timer:
            _ap_timer.cancel()
            _ap_timer = None
        exit_ap_mode()
    else:
        if enter_ap_mode():
            led_double_blink()
            _ap_timer = threading.Timer(AP_TIMEOUT_S, _ap_timeout)
            _ap_timer.daemon = True
            _ap_timer.start()
            log.info('AP mode timeout in %d s', AP_TIMEOUT_S)
        else:
            led_rapid_fail()

# ── GPIO monitor ──────────────────────────────────────────────────────────────

def monitor_gpio() -> None:
    try:
        import lgpio
    except ImportError:
        log.error('lgpio not available — install python3-lgpio')
        sys.exit(1)

    chip = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_input(chip, GPIO_BUTTON, lgpio.SET_PULL_UP)
    log.info('Monitoring GPIO %d (pull-up, active LOW)', GPIO_BUTTON)
    led_heartbeat()

    _wd_counter = 0
    while True:
        # Wait for button press (LOW); ping watchdog while idle. (NFR-4.81)
        while lgpio.gpio_read(chip, GPIO_BUTTON) == 1:
            time.sleep(DEBOUNCE_S)
            _wd_counter += 1
            if _wd_counter >= _WD_PING_EVERY:
                _sd_notify('WATCHDOG=1')
                _wd_counter = 0

        press_start = time.monotonic()
        log.debug('Button pressed')

        # Wait for release
        while lgpio.gpio_read(chip, GPIO_BUTTON) == 0:
            time.sleep(DEBOUNCE_S)

        duration = time.monotonic() - press_start
        log.info('Button released after %.2f s', duration)

        if duration < WPS_MIN_S:
            pass  # too short — ignore
        elif duration <= WPS_MAX_S:
            threading.Thread(target=trigger_wps, daemon=True).start()
        elif duration >= AP_MIN_S:
            threading.Thread(target=toggle_ap_mode, daemon=True).start()
        # gap between WPS_MAX_S and AP_MIN_S is intentional dead zone

        time.sleep(DEBOUNCE_S)  # debounce after release


def main() -> None:
    log.info('config-button starting (GPIO %d)', GPIO_BUTTON)
    # If AP was active before a crash/restart, clean up
    if CONFIG_ACTIVE.exists():
        log.warning('Stale config-active file found — cleaning up')
        CONFIG_ACTIVE.unlink(missing_ok=True)
    monitor_gpio()


if __name__ == '__main__':
    main()
