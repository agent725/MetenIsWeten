#!/usr/bin/env python3
"""
UART Temperature Sensor Listener

Reads temperature from a UART device (default /dev/ttyS0) and publishes
per-channel averages to MQTT.

The source sends a repeating, pipe-delimited frame every second:

    T0:20|T1:19|T0:20.1|T1:19.1|

Each 'T<n>:<value>' token is one channel's temperature in Celsius; a frame may
contain several readings per channel. Frames are newline-terminated.

Features:
- No sensor discovery: channels come from the incoming frames
- Collects every accepted reading over the interval (default 60 s, i.e. one
  minute) and publishes the average of each channel — so T0 and T1 each get
  their own one-minute average
- Discards a reading that is 30% or more different from the last accepted
  measurement on that channel (a misread)
- Publishes JSON with channel ID and temperature
- Configurable read interval
- Graceful shutdown on SIGTERM/SIGINT

Usage:
    ds18b20_listener.py [options]

Examples:
    # Default: read every 60 seconds from /dev/ttyS0, publish to localhost
    ds18b20_listener.py

    # Custom interval and broker
    ds18b20_listener.py --interval 30 --host 192.168.1.100

    # Verbose output
    ds18b20_listener.py -v

Requirements:
    - /dev/ttyS0 present and fed by the UART temperature source
    - paho-mqtt: pip install paho-mqtt
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import argparse
import json
import os
import re
import signal
import socket
import sys
import time
from typing import Optional

PI_ID_FILE = '/etc/otgw/pi-id'
MODE_FILE = '/etc/otgw/mode'
DEFAULT_DEVICE = '/dev/ttyS0'

# Watchdog ping interval; systemd kills the service if no ping within
# WatchdogSec (120 s in the unit file). Half that interval is the target.
_WATCHDOG_PING_INTERVAL = 60  # seconds

# A reading that differs by 30% or more from the last accepted measurement on
# the same channel is a misread and is discarded. The reference magnitude is
# clamped to 1 °C so that readings near 0 °C (where 30% of the reference
# vanishes) are not discarded for every tiny change.
ANOMALY_THRESHOLD = 0.30
MIN_REFERENCE = 1.0

# One reading token, e.g. 'T0:20.1'
TOKEN_RE = re.compile(r'T(\d+):(-?\d+(?:\.\d+)?)')


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


try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Error: paho-mqtt not installed. Run: pip install paho-mqtt", file=sys.stderr)
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

    home/sensors/temperature -> home/a1b2c3/sensors/temperature  (production)
    home/sensors/temperature -> test/a1b2c3/sensors/temperature   (test)
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


def parse_frame(chunk: str) -> list[tuple[int, float]]:
    """Parse a frame such as 'T0:20|T1:19|T0:20.1|T1:19.1|' into (channel, °C) pairs.

    Tolerates the trailing '|', whitespace, and any non-reading text in the chunk.
    """
    return [(int(channel), float(value)) for channel, value in TOKEN_RE.findall(chunk)]


def is_misread(temperature: float, last: Optional[float]) -> bool:
    """Return True when the reading is 30% or more different from the last accepted one."""
    if last is None:
        return False  # first reading on this channel has nothing to compare with
    return abs(temperature - last) >= ANOMALY_THRESHOLD * max(abs(last), MIN_REFERENCE)


class UARTTemperatureListener:
    """UART temperature listener with MQTT publishing of per-channel averages."""

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        host: str = 'localhost',
        port: int = 1883,
        topic: str = 'home/sensors/temperature',
        interval: int = 60,
        verbose: bool = False,
    ):
        self.device = device
        self.host = host
        self.port = port
        self.topic = topic
        self.interval = interval
        self.verbose = verbose

        self.client: Optional[mqtt.Client] = None
        self.running = False
        self.readings_count = 0
        self.serial = None

        # Last accepted reading per channel, used as the reference for the
        # misread check. Only the most recent accepted value per channel is
        # kept.
        self._last: dict[int, float] = {}

        # One-reading retry buffer per channel: if MQTT is offline when an
        # average is ready, store it here and attempt delivery on the next
        # publish cycle. Only the most recent average per channel is kept —
        # averages have interval granularity so a single retry is enough to
        # survive short broker outages without accumulating stale data.
        # (NFR-4.76)
        self._pending: dict[str, float] = {}

    def log(self, message: str) -> None:
        """Print message if verbose mode enabled."""
        if self.verbose:
            print(message)

    def open_device(self) -> None:
        """Open the UART device, waiting for it to appear after boot."""
        if not os.path.exists(self.device):
            print(f"{self.device} not available, waiting...", file=sys.stderr)
            delays = [5, 10, 30, 60]
            attempt = 0
            while not os.path.exists(self.device):
                delay = delays[min(attempt, len(delays) - 1)]
                time.sleep(delay)
                attempt += 1
        self.serial = open(self.device, 'r')
        print(f"Opened {self.device}")

    def read_frame(self) -> str:
        """Read one frame (one newline-terminated line) from the UART device."""
        line = self.serial.readline()
        if not line:
            raise IOError(f"{self.device} reached end of input (device disconnected?)")
        return line

    def on_connect(self, client: mqtt.Client, userdata, connect_flags,
                   reason_code, properties) -> None:
        """Handle MQTT connection."""
        if reason_code == 0:
            print(f"Connected to MQTT broker at {self.host}:{self.port}")
        else:
            print(f"Connection failed with code {reason_code}", file=sys.stderr)

    def on_disconnect(self, client: mqtt.Client, userdata, disconnect_flags,
                      reason_code, properties) -> None:
        """Handle MQTT disconnection."""
        if reason_code != 0:
            print(f"Unexpected disconnection (rc={reason_code}), will reconnect...",
                  file=sys.stderr)

    def _connect_mqtt(self) -> None:
        """Connect to MQTT broker with indefinite retry and backoff."""
        delays = [5, 10, 30, 60]
        attempt = 0
        while True:
            try:
                self.client.connect(self.host, self.port, keepalive=60)
                return
            except Exception as e:
                delay = delays[min(attempt, len(delays) - 1)]
                print(
                    f"MQTT connect failed ({e}), retrying in {delay}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                attempt += 1

    def publish_temperature(self, channel_id: str, temperature: float) -> None:
        """Publish a channel average to MQTT with one-reading retry. (NFR-4.76)

        If MQTT is offline the average is stored in _pending. On the next call
        (next interval cycle) the pending average is published first, then the
        fresh one. Only the most recent pending average per channel is kept.
        """
        # Attempt any pending average from the previous cycle first.
        if channel_id in self._pending:
            if self._try_publish(channel_id, self._pending[channel_id]):
                del self._pending[channel_id]
            else:
                # Still offline; replace stale pending with fresh average and bail.
                self._pending[channel_id] = temperature
                print(
                    f"MQTT offline — pending average for {channel_id} updated, "
                    "will retry next cycle (NFR-4.76)",
                    file=sys.stderr,
                )
                return

        # Publish the current average.
        if not self._try_publish(channel_id, temperature):
            self._pending[channel_id] = temperature
            print(
                f"MQTT offline — average for {channel_id} queued for retry "
                "(NFR-4.76)",
                file=sys.stderr,
            )

    def _try_publish(self, channel_id: str, temperature: float) -> bool:
        """Attempt one publish; return True on success, False if offline or failed."""
        if not (self.client and self.client.is_connected()):
            return False
        payload = json.dumps({
            'sensor_id': channel_id,
            'temperature_c': round(temperature, 2),
        })
        result = self.client.publish(self.topic, payload)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            self.readings_count += 1
            self.log(f"Published: {self.topic} -> {payload}")
            return True
        # TOCTOU: connection dropped between is_connected() and publish(). (NFR-4.74)
        print(
            f"Publish failed (rc={result.rc}), average queued for retry "
            "(NFR-4.74/4.76)",
            file=sys.stderr,
        )
        return False

    def setup_signals(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        def signal_handler(sig, frame):
            print(f"\nReceived signal {sig}, shutting down...")
            self.stop()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    def collect_averages(self) -> dict[str, float]:
        """Read frames for one interval and return the average of each channel.

        Frames arrive every second, so readline paces the loop naturally.
        Readings that fail the misread check are discarded and never update
        the reference value.
        """
        samples: dict[int, list[float]] = {}

        for _ in range(self.interval):
            if not self.running:
                break
            for channel, temperature in parse_frame(self.read_frame()):
                last = self._last.get(channel)
                if is_misread(temperature, last):
                    self.log(
                        f"Discarding T{channel}: {temperature} "
                        f"(>= {int(ANOMALY_THRESHOLD * 100)}% off last reading {last})"
                    )
                    continue
                self._last[channel] = temperature
                samples.setdefault(channel, []).append(temperature)

        if not samples:
            self.log("No readings collected in this interval")
            return {}

        averages = {}
        for channel in sorted(samples):
            temps = samples[channel]
            average = sum(temps) / len(temps)
            self.log(f"T{channel}: {len(temps)} readings, average {average:.2f} °C")
            averages[f"T{channel}"] = average
        return averages

    def start(self) -> None:
        """Start the temperature listener."""
        print("UART Temperature Listener starting...")
        print(f"  Device: {self.device}")
        print(f"  Broker: {self.host}:{self.port}")
        print(f"  Topic: {self.topic}")
        print(f"  Interval: {self.interval}s")
        print()

        # Open UART device (the port may appear a few seconds after boot).
        self.open_device()

        sys.stdout.flush()

        # Setup signal handlers
        self.setup_signals()

        # Create and configure MQTT client.
        # reconnect_on_failure=True ensures paho's background thread re-establishes
        # the connection if the broker restarts; without it the service stays active
        # but publishes nothing after a mosquitto restart. (NFR-4.69)
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            reconnect_on_failure=True,
        )
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect

        # Connect to broker (with retry)
        self._connect_mqtt()

        # Start MQTT loop in background thread
        self.client.loop_start()
        self.running = True

        _last_watchdog_ping: float = 0.0

        # Main loop: collect one interval of readings, publish averages
        try:
            while self.running:
                # Ping the systemd watchdog once per cycle. (NFR-4.80)
                _now = time.monotonic()
                if _now - _last_watchdog_ping >= _WATCHDOG_PING_INTERVAL:
                    _sd_notify('WATCHDOG=1')
                    _last_watchdog_ping = _now

                for channel_id, average in self.collect_averages().items():
                    self.publish_temperature(channel_id, average)

        except Exception as e:
            print(f"Error in main loop: {e}", file=sys.stderr)
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the listener."""
        self.running = False

        # Close UART device
        if self.serial:
            self.serial.close()

        # Disconnect MQTT
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

        print(f"Published {self.readings_count} readings total")
        sys.exit(0)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='UART temperature sensor listener with MQTT publishing',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    # Default settings
  %(prog)s --interval 30                      # Average every 30 seconds
  %(prog)s --host 192.168.1.100               # Custom MQTT broker
  %(prog)s --topic sensors/outdoor/temp       # Custom topic
  %(prog)s --device /dev/ttyUSB0              # Custom UART device
  %(prog)s -v                                 # Verbose output

Input format (one frame per second, newline-terminated):
  T0:20|T1:19|T0:20.1|T1:19.1|
        """,
    )

    parser.add_argument(
        '--device',
        default=DEFAULT_DEVICE,
        help=f'UART device to read from (default: {DEFAULT_DEVICE})',
    )
    parser.add_argument(
        '--host',
        default='localhost',
        help='MQTT broker hostname (default: localhost)',
    )
    parser.add_argument(
        '--port',
        type=int,
        default=1883,
        help='MQTT broker port (default: 1883)',
    )
    parser.add_argument(
        '--topic',
        default='home/sensors/temperature',
        help='MQTT topic to publish to (default: home/sensors/temperature)',
    )
    parser.add_argument(
        '--image-id',
        default=None,
        help='Image identifier for MQTT topics (default: read from /etc/otgw/pi-id)',
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=60,
        help='Average interval in seconds (default: 60)',
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Print each reading, discard and average as it happens',
    )

    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Resolve Image ID: explicit arg > file > None
    image_id = args.image_id if args.image_id is not None else read_image_id_file()
    mode = read_mode()
    topic = build_topic(args.topic, image_id, mode)
    if mode == 'test':
        print(f"Mode: TEST — publishing to {topic}")

    listener = UARTTemperatureListener(
        device=args.device,
        host=args.host,
        port=args.port,
        topic=topic,
        interval=args.interval,
        verbose=args.verbose,
    )

    listener.start()


if __name__ == '__main__':
    main()
