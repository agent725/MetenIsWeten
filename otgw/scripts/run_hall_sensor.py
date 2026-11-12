#!/usr/bin/env python3
"""Hall effect sensor test script.

Monitors a hall effect sensor on a configurable GPIO pin and prints
timestamped events. Automatically stops the config-sensor service
before monitoring and restores it on exit (Ctrl-C or crash).

Supports two sensor types:
  Non-latching (default): output follows magnet presence.
    LOW  = magnet within range  → MAGNET PRESENT
    HIGH = magnet removed       → MAGNET ABSENT
    Hold duration is printed on each ABSENT event.

  Latching (--latching): bistable sensor that holds its state after
    the magnet is removed.  Each pole application flips the output.
    HIGH→LOW = LATCHED
    LOW→HIGH = UNLATCHED

Simulation mode (--sim):
  Mirrors the exact production blinking behaviour from config_sensor.py.
  On hold ≥ 100 ms: ACT double-blink + PWR rapid-blink (portal cue).
  On hold ≥ 5 s:   ACT + PWR ultra-rapid blink (shutdown warning).
  On release:       ACT heartbeat + PWR default-on.
  LED paths are printed at start so you can verify they are found.

Wiring (non-latching, e.g. OH1881 / US5881 / U58):
  Pin 1  (3.3 V) → VCC
  Pin 6  (GND)   → GND
  GPIO pin       → OUT  (pull-up enabled in software)

Wiring (latching, e.g. US1881 / AH3144):
  Same as above.

Usage:
  sudo python3 run_hall_sensor.py              # GPIO 4, non-latching
  sudo python3 run_hall_sensor.py --gpio 2     # GPIO 2
  sudo python3 run_hall_sensor.py --latching   # latching sensor
  sudo python3 run_hall_sensor.py --sim        # simulate production LEDs
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import argparse
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import lgpio

# ── constants ─────────────────────────────────────────────────────────────────
DEFAULT_GPIO    = 4       # BCM GPIO 4, physical pin 7
POLL_INTERVAL   = 0.02    # 20 ms → 50 Hz polling
DEBOUNCE_S      = 0.02    # 20 ms debounce window
PORTAL_MIN_S    = 0.1     # same threshold as production (100 ms)
SHUTDOWN_WARN_S = 5.0     # same as production shutdown-warning threshold
SERVICE         = 'config-sensor.service'


# ── service management ────────────────────────────────────────────────────────

def service_is_active(name: str) -> bool:
    result = subprocess.run(
        ['systemctl', 'is-active', '--quiet', name],
        capture_output=True
    )
    return result.returncode == 0


def stop_service(name: str) -> bool:
    """Stop service; return True if it was running (so we can restart it)."""
    was_active = service_is_active(name)
    if was_active:
        subprocess.run(['systemctl', 'stop', name], capture_output=True)
        print(f"Stopped {name} (will restart on exit)")
    else:
        print(f"{name} was not running")
    return was_active


def start_service(name: str) -> None:
    subprocess.run(['systemctl', 'start', name], capture_output=True)
    print(f"Restarted {name}")


# ── LED helpers (mirrors config_sensor.py exactly) ───────────────────────────

def _led_path() -> Path | None:
    for name in ('ACT', 'led0'):
        p = Path(f'/sys/class/leds/{name}')
        if p.exists():
            return p
    return None


def _pwr_path() -> Path | None:
    for name in ('PWR', 'led1'):
        p = Path(f'/sys/class/leds/{name}')
        if p.exists():
            return p
    return None


ACT = _led_path()
PWR = _pwr_path()

_sim_warned = False


def _led_write(led: Path | None, filename: str, value: str) -> bool:
    """Write to a sysfs LED file; return True on success."""
    global _sim_warned
    if led is None:
        return False
    try:
        (led / filename).write_text(value)
        return True
    except OSError as e:
        if not _sim_warned:
            _sim_warned = True
            print(f"  !! LED write failed ({led.name}/{filename}): {e}", file=sys.stderr)
        return False


def sim_led_rapid_blink() -> None:
    """ACT 100 ms cycle — immediate magnet-detected feedback."""
    _led_write(ACT, 'trigger', 'timer')
    _led_write(ACT, 'delay_on',  '50')
    _led_write(ACT, 'delay_off', '50')


def sim_led_double_blink() -> None:
    """ACT double-blink — portal cue (pattern trigger or timer fallback)."""
    try:
        if ACT and 'pattern' in (ACT / 'trigger').read_text():
            _led_write(ACT, 'trigger', 'pattern')
            _led_write(ACT, 'pattern', '255 200 0 200 255 200 0 800')
            _led_write(ACT, 'repeat',  '-1')
            return
    except OSError:
        pass
    # Fallback: simple 200 ms timer blink
    _led_write(ACT, 'trigger',   'timer')
    _led_write(ACT, 'delay_on',  '200')
    _led_write(ACT, 'delay_off', '200')


def sim_led_ultra_rapid_blink() -> None:
    """ACT + PWR 50 ms cycle — shutdown warning."""
    _led_write(ACT, 'trigger',   'timer')
    _led_write(ACT, 'delay_on',  '25')
    _led_write(ACT, 'delay_off', '25')
    _led_write(PWR, 'trigger',   'timer')
    _led_write(PWR, 'delay_on',  '25')
    _led_write(PWR, 'delay_off', '25')


def sim_pwr_rapid_blink() -> None:
    """PWR 200 ms cycle — portal/flush active."""
    _led_write(PWR, 'trigger',   'timer')
    _led_write(PWR, 'delay_on',  '100')
    _led_write(PWR, 'delay_off', '100')


def sim_led_heartbeat() -> None:
    """Restore ACT to kernel heartbeat."""
    _led_write(ACT, 'trigger', 'heartbeat')


def sim_pwr_default() -> None:
    """Restore PWR to always-on."""
    _led_write(PWR, 'trigger', 'default-on')


# ── monitoring ────────────────────────────────────────────────────────────────

def fmt_duration(seconds: float) -> str:
    if seconds < 1.0:
        return f"{seconds * 1000:.0f} ms"
    return f"{seconds:.2f} s"


def monitor(gpio_pin: int, latching: bool) -> None:
    chip = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_input(chip, gpio_pin, lgpio.SET_PULL_UP)

    time.sleep(DEBOUNCE_S * 2)
    state = lgpio.gpio_read(chip, gpio_pin)

    if latching:
        state_label = {0: 'LATCHED', 1: 'UNLATCHED'}
        event_label = {
            (1, 0): 'LATCHED   (field applied)',
            (0, 1): 'UNLATCHED (opposite field / field removed)',
        }
    else:
        state_label = {0: 'MAGNET PRESENT', 1: 'MAGNET ABSENT'}
        event_label = {
            (1, 0): 'MAGNET PRESENT',
            (0, 1): 'MAGNET ABSENT ',
        }

    print(f"GPIO {gpio_pin} | {'latching' if latching else 'non-latching'} | "
          f"initial state: {state_label[state]}")
    print("─" * 60)

    event_time: float = time.monotonic()
    debounce_deadline: float = 0.0

    try:
        while True:
            time.sleep(POLL_INTERVAL)
            new_state = lgpio.gpio_read(chip, gpio_pin)

            if new_state == state:
                continue

            now = time.monotonic()
            if now < debounce_deadline:
                state = new_state
                continue
            debounce_deadline = now + DEBOUNCE_S

            ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            transition = (state, new_state)
            label = event_label.get(transition, f'{state}→{new_state}')

            if not latching and transition == (0, 1):
                held = now - event_time
                print(f"[{ts}]  {label}  (held {fmt_duration(held)})")
            else:
                print(f"[{ts}]  {label}")

            if not latching and transition == (1, 0):
                event_time = now

            state = new_state

    finally:
        lgpio.gpiochip_close(chip)


def monitor_sim(gpio_pin: int) -> None:
    """Simulate exact production LED behaviour while reporting events."""
    chip = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_input(chip, gpio_pin, lgpio.SET_PULL_UP)

    time.sleep(DEBOUNCE_S * 2)
    prev = lgpio.gpio_read(chip, gpio_pin)

    hold_start:    float | None = None
    action_fired:  bool         = False
    warned_5s:     bool         = False

    state_str = 'MAGNET PRESENT' if prev == 0 else 'MAGNET ABSENT'
    print(f"GPIO {gpio_pin} | sim mode | initial state: {state_str}")
    print("─" * 60)

    sim_led_heartbeat()
    sim_pwr_default()

    try:
        while True:
            time.sleep(POLL_INTERVAL)
            curr = lgpio.gpio_read(chip, gpio_pin)
            ts   = datetime.now().strftime('%H:%M:%S.%f')[:-3]

            if curr == 0 and prev != 0:
                # Magnet just arrived
                hold_start   = time.monotonic()
                action_fired = False
                warned_5s    = False
                print(f"[{ts}]  MAGNET PRESENT — rapid-blink (immediate feedback)")
                sim_led_rapid_blink()

            elif curr == 0 and hold_start is not None:
                # Magnet still held — fire actions at thresholds
                held = time.monotonic() - hold_start

                if held >= SHUTDOWN_WARN_S and not warned_5s:
                    warned_5s = True
                    print(f"[{ts}]  Hold {fmt_duration(held)} — ultra-rapid blink "
                          f"(shutdown warning: ≥5 s)")
                    sim_led_ultra_rapid_blink()

                elif held >= PORTAL_MIN_S and not action_fired:
                    action_fired = True
                    print(f"[{ts}]  Hold {fmt_duration(held)} — ACT double-blink + "
                          f"PWR rapid-blink (portal cue)")
                    sim_led_double_blink()
                    sim_pwr_rapid_blink()

            elif curr != 0 and prev == 0 and hold_start is not None:
                # Magnet just released
                held       = time.monotonic() - hold_start
                hold_start = None
                if held < PORTAL_MIN_S:
                    print(f"[{ts}]  MAGNET ABSENT  (held {fmt_duration(held)}) "
                          f"— too short, ignored")
                else:
                    print(f"[{ts}]  MAGNET ABSENT  (held {fmt_duration(held)}) "
                          f"— restoring heartbeat / PWR-on")
                sim_led_heartbeat()
                sim_pwr_default()

            prev = curr

    finally:
        sim_led_heartbeat()
        sim_pwr_default()
        lgpio.gpiochip_close(chip)


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Test a hall effect sensor — auto-manages config-sensor service',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--gpio', type=int, default=DEFAULT_GPIO,
        metavar='N',
        help=f'BCM GPIO pin number (default: {DEFAULT_GPIO})',
    )
    parser.add_argument(
        '--latching', action='store_true',
        help='Sensor is latching/bistable (e.g. US1881, AH3144)',
    )
    parser.add_argument(
        '--sim', action='store_true',
        help='Simulate production LED behaviour (ACT double-blink + PWR blink on hold)',
    )
    args = parser.parse_args()

    mode = 'sim' if args.sim else ('latching' if args.latching else 'non-latching')
    print(f"Hall Sensor Test  —  GPIO {args.gpio}  ({mode})")
    print("=" * 60)

    if args.sim:
        print(f"  ACT LED path : {ACT or '(not found!)'}")
        print(f"  PWR LED path : {PWR or '(not found!)'}")
        if ACT is None:
            print("  WARNING: ACT LED not found — no blinking will occur", file=sys.stderr)
        print()

    was_active = stop_service(SERVICE)
    print()

    try:
        if args.sim:
            monitor_sim(args.gpio)
        else:
            monitor(args.gpio, args.latching)
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as exc:
        print(f"\nError: {exc}", file=sys.stderr)
    finally:
        print()
        if was_active:
            start_service(SERVICE)
        else:
            print(f"({SERVICE} was not running before — not restarted)")


if __name__ == '__main__':
    main()
