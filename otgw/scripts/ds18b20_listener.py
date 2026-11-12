#!/usr/bin/env python3
"""
DS18B20 Temperature Sensor Listener

Reads temperature from DS18B20 1-Wire sensors and publishes to MQTT.

Features:
- Auto-discovers all connected DS18B20 sensors
- Publishes JSON with sensor ID and temperature
- Configurable read interval
- Graceful shutdown on SIGTERM/SIGINT

Usage:
    ds18b20_listener.py [options]

Examples:
    # Default: read every 60 seconds, publish to localhost
    ds18b20_listener.py

    # Custom interval and broker
    ds18b20_listener.py --interval 30 --host 192.168.1.100

    # Verbose output
    ds18b20_listener.py -v

Requirements:
    - Raspberry Pi with 1-Wire enabled (dtoverlay=w1-gpio in /boot/config.txt)
    - DS18B20 sensors on GPIO17 (Pin 11) or GPIO18 (Pin 12); both enabled via
      two dtoverlay=w1-gpio lines (one per pin) in /boot/config.txt
    - paho-mqtt: pip install paho-mqtt
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import argparse
import glob
import json
import os
import signal
import socket
import sys
import time
from typing import Optional

PI_ID_FILE = '/etc/otgw/pi-id'
MODE_FILE = '/etc/otgw/mode'

# Watchdog ping interval; systemd kills the service if no ping within
# WatchdogSec (120 s in the unit file). Half that interval is the target.
_WATCHDOG_PING_INTERVAL = 60  # seconds


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


# 1-Wire device path
W1_DEVICES_PATH = '/sys/bus/w1/devices'
W1_SENSOR_PREFIX = '28-'  # DS18B20 sensors start with 28-


class DS18B20Listener:
    """DS18B20 temperature sensor listener with MQTT publishing."""

    def __init__(
        self,
        host: str = 'localhost',
        port: int = 1883,
        topic: str = 'home/sensors/temperature',
        interval: int = 60,
        verbose: bool = False,
    ):
        self.host = host
        self.port = port
        self.topic = topic
        self.interval = interval
        self.verbose = verbose

        self.client: Optional[mqtt.Client] = None
        self.running = False
        self.readings_count = 0

        # One-reading retry buffer per sensor: if MQTT is offline when a
        # reading is ready, store it here and attempt delivery on the next
        # publish cycle. Only the most recent reading per sensor is kept —
        # temperature data has 15-s granularity so a single retry is enough
        # to survive short broker outages without accumulating stale data.
        # (NFR-4.76)
        self._pending: dict[str, float] = {}

    def log(self, message: str) -> None:
        """Print message if verbose mode enabled."""
        if self.verbose:
            print(message)

    def discover_sensors(self) -> list[str]:
        """
        Discover all connected DS18B20 sensors.

        Returns list of sensor IDs (e.g., ['28-00000abcdef1', '28-00000abcdef2'])
        """
        pattern = os.path.join(W1_DEVICES_PATH, f'{W1_SENSOR_PREFIX}*')
        sensor_paths = glob.glob(pattern)

        sensors = []
        for path in sensor_paths:
            sensor_id = os.path.basename(path)
            sensors.append(sensor_id)

        return sorted(sensors)

    def read_temperature(self, sensor_id: str) -> Optional[float]:
        """
        Read temperature from a DS18B20 sensor.

        Args:
            sensor_id: Sensor ID (e.g., '28-00000abcdef1')

        Returns:
            Temperature in Celsius, or None if read failed
        """
        # The temperature is in the 'temperature' file (newer kernels)
        # or in 'w1_slave' file (older kernels)
        temp_file = os.path.join(W1_DEVICES_PATH, sensor_id, 'temperature')
        slave_file = os.path.join(W1_DEVICES_PATH, sensor_id, 'w1_slave')

        try:
            # Try new-style temperature file first (raw millidegrees)
            if os.path.exists(temp_file):
                with open(temp_file, 'r') as f:
                    content = f.read().strip()
                    temp_millic = int(content)
                    return temp_millic / 1000.0

            # Fall back to w1_slave file
            if os.path.exists(slave_file):
                with open(slave_file, 'r') as f:
                    lines = f.readlines()

                # Check CRC validity (first line ends with YES)
                if len(lines) < 2 or 'YES' not in lines[0]:
                    self.log(f"CRC check failed for {sensor_id}")
                    return None

                # Extract temperature from second line (t=XXXXX)
                temp_pos = lines[1].find('t=')
                if temp_pos == -1:
                    self.log(f"Temperature not found in response for {sensor_id}")
                    return None

                temp_millic = int(lines[1][temp_pos + 2:])
                return temp_millic / 1000.0

            self.log(f"No temperature file found for {sensor_id}")
            return None

        except (IOError, ValueError) as e:
            self.log(f"Error reading {sensor_id}: {e}")
            return None

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

    def publish_temperature(self, sensor_id: str, temperature: float) -> None:
        """Publish temperature reading to MQTT with one-reading retry. (NFR-4.76)

        If MQTT is offline the reading is stored in _pending. On the next call
        (next interval cycle) the pending reading is published first, then the
        fresh reading. Only the most recent pending reading per sensor is kept.
        """
        # Attempt any pending reading from the previous cycle first.
        if sensor_id in self._pending:
            if self._try_publish(sensor_id, self._pending[sensor_id]):
                del self._pending[sensor_id]
            else:
                # Still offline; replace stale pending with fresh reading and bail.
                self._pending[sensor_id] = temperature
                print(
                    f"MQTT offline — pending reading for {sensor_id} updated, "
                    "will retry next cycle (NFR-4.76)",
                    file=sys.stderr,
                )
                return

        # Publish the current reading.
        if not self._try_publish(sensor_id, temperature):
            self._pending[sensor_id] = temperature
            print(
                f"MQTT offline — reading for {sensor_id} queued for retry "
                "(NFR-4.76)",
                file=sys.stderr,
            )

    def _try_publish(self, sensor_id: str, temperature: float) -> bool:
        """Attempt one publish; return True on success, False if offline or failed."""
        if not (self.client and self.client.is_connected()):
            return False
        payload = json.dumps({
            'sensor_id': sensor_id,
            'temperature_c': round(temperature, 2),
        })
        result = self.client.publish(self.topic, payload)
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            self.readings_count += 1
            self.log(f"Published: {self.topic} -> {payload}")
            return True
        # TOCTOU: connection dropped between is_connected() and publish(). (NFR-4.74)
        print(
            f"Publish failed (rc={result.rc}), reading queued for retry "
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

    def start(self) -> None:
        """Start the temperature listener."""
        print("DS18B20 Listener starting...")
        print(f"  Broker: {self.host}:{self.port}")
        print(f"  Topic: {self.topic}")
        print(f"  Interval: {self.interval}s")
        print()

        # Wait for 1-Wire support (may take a few seconds after boot,
        # or may be absent if dtoverlay=w1-gpio is not configured).
        if not os.path.exists(W1_DEVICES_PATH):
            print(
                f"1-Wire not yet available ({W1_DEVICES_PATH}), waiting...",
                file=sys.stderr,
            )
            print(
                "  If permanently absent: "
                "add 'dtoverlay=w1-gpio' to /boot/config.txt and reboot",
                file=sys.stderr,
            )
            delays = [5, 10, 30, 60]
            attempt = 0
            while not os.path.exists(W1_DEVICES_PATH):
                delay = delays[min(attempt, len(delays) - 1)]
                time.sleep(delay)
                attempt += 1
            print("1-Wire available, continuing...")

        # Discover sensors — log per-bus-master breakdown for hardware diagnosis
        for master in sorted(glob.glob(
                os.path.join(W1_DEVICES_PATH, 'w1_bus_master*'))):
            slaves_file = os.path.join(master, 'w1_master_slaves')
            try:
                slaves = open(slaves_file).read().split()
                ds = [s for s in slaves if s.startswith(W1_SENSOR_PREFIX)]
                name = os.path.basename(master)
                print(f"  {name}: {', '.join(ds) if ds else 'no DS18B20'}")
            except OSError:
                pass

        sensors = self.discover_sensors()
        if not sensors:
            print("Warning: No DS18B20 sensors found", file=sys.stderr)
            print(f"Looking in: {W1_DEVICES_PATH}/{W1_SENSOR_PREFIX}*", file=sys.stderr)
        else:
            print(f"Found {len(sensors)} sensor(s): {', '.join(sensors)}")

        print()
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

        # Main loop: periodic temperature reads
        try:
            while self.running:
                # Ping the systemd watchdog once per cycle. (NFR-4.80)
                _now = time.monotonic()
                if _now - _last_watchdog_ping >= _WATCHDOG_PING_INTERVAL:
                    _sd_notify('WATCHDOG=1')
                    _last_watchdog_ping = _now

                # Re-discover sensors each cycle (hot-plug support)
                sensors = self.discover_sensors()

                for sensor_id in sensors:
                    temp = self.read_temperature(sensor_id)
                    if temp is not None:
                        self.publish_temperature(sensor_id, temp)
                    else:
                        self.log(f"Skipping {sensor_id} (read failed)")

                # Sleep for interval
                for _ in range(self.interval):
                    if not self.running:
                        break
                    time.sleep(1)

        except Exception as e:
            print(f"Error in main loop: {e}", file=sys.stderr)
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the listener."""
        self.running = False

        # Disconnect MQTT
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()

        print(f"Published {self.readings_count} readings total")
        sys.exit(0)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='DS18B20 temperature sensor listener with MQTT publishing',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    # Default settings
  %(prog)s --interval 30                      # Read every 30 seconds
  %(prog)s --host 192.168.1.100               # Custom MQTT broker
  %(prog)s --topic sensors/outdoor/temp       # Custom topic
  %(prog)s -v                                 # Verbose output

Setup:
  1. Enable 1-Wire (add both lines to /boot/config.txt):
        echo 'dtoverlay=w1-gpio,gpiopin=17' >> /boot/config.txt
        echo 'dtoverlay=w1-gpio,gpiopin=18' >> /boot/config.txt
  2. Reboot
  3. Connect DS18B20 to GPIO17 (Pin 11) or GPIO18 (Pin 12) with 4.7kΩ pull-up to 3.3V
  4. Verify: ls /sys/bus/w1/devices/28-*
        """,
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
        help='Read interval in seconds (default: 60)',
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Print each reading as it happens',
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

    listener = DS18B20Listener(
        host=args.host,
        port=args.port,
        topic=topic,
        interval=args.interval,
        verbose=args.verbose,
    )

    listener.start()


if __name__ == '__main__':
    main()
