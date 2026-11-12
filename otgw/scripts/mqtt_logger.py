#!/usr/bin/env python3
"""
MQTT Logger - Subscribes to MQTT topics and logs messages to files.

Features:
- Writes each message immediately when output_dir is on a RAM filesystem
  (tmpfs/ramfs) — no buffering, reads always see the latest data.
- Buffers messages in memory and flushes at --write-interval when
  output_dir is on a real disk — reduces SD card / flash wear.
- Graceful shutdown on SIGTERM/SIGINT (flushes buffer)
- Split files by topic prefix or single combined file
- ISO8601 timestamps with timezone offset for unambiguous logging
- Configurable timezone (default: Europe/Amsterdam)

Log format:
    2026-01-30T14:30:00+01:00 | otgw/energy/gas : {"reading": 1234.5}

File layout:
    <output_dir>/YYYY-MM-DD/gas_<image_id>.log
    <output_dir>/YYYY-MM-DD/otgw_<image_id>.log
    ...
    Date uses the configured timezone (default: Europe/Amsterdam).
    Unknown topics are captured with the full sanitized topic as filename prefix.

Usage:
    mqtt_logger.py [options]

Examples:
    # Default: log home/# topics to /var/log/otgw, split by prefix
    mqtt_logger.py

    # Custom output directory and write interval
    mqtt_logger.py --output-dir /home/pi/logs --write-interval 1800

    # Single file mode
    mqtt_logger.py --single-file

    # Custom timezone
    mqtt_logger.py --timezone UTC

    # Custom topics
    mqtt_logger.py --topics home/energy/gas,home/energy/otgw,home/sensors/temperature
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import argparse
import datetime
import os
import signal
import socket
import sys
import time
from collections import defaultdict
from typing import Optional
from zoneinfo import ZoneInfo

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


def is_ram_disk(path: str) -> bool:
    """Return True if path (or its closest existing ancestor) is on tmpfs/ramfs."""
    try:
        # Walk up to find an existing path to check
        p = os.path.realpath(path)
        while p and not os.path.exists(p):
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent

        # Find the longest matching mountpoint in /proc/mounts
        best_mountpoint = '/'
        best_fstype = ''
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mountpoint, fstype = parts[1], parts[2]
                if (p.startswith(mountpoint)
                        and len(mountpoint) >= len(best_mountpoint)):
                    best_mountpoint = mountpoint
                    best_fstype = fstype
        return best_fstype in ('tmpfs', 'ramfs')
    except OSError:
        return False


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


class MqttLogger:
    """MQTT message logger with buffered writes."""

    # Topic suffix -> file prefix mapping (Image ID segment is inserted dynamically)
    # Full topic format: home/{image_id}/suffix  e.g. home/a1b2c3/energy/gas
    # In test mode:      test/{image_id}/suffix  e.g. test/a1b2c3/energy/gas
    TOPIC_SUFFIXES = {
        'energy/gas': 'gas',
        'energy/otgw': 'otgw',
        'sensors/temperature': 'temperature',
        'system/heartbeat': 'heartbeat',
        'system/sensor': 'system_sensor',
    }

    def __init__(
        self,
        host: str = 'localhost',
        port: int = 1883,
        output_dir: str = '/var/log/otgw',
        write_interval: int = 900,
        split_files: bool = True,
        topics: Optional[list[str]] = None,
        timezone: str = 'Europe/Amsterdam',
        image_id: Optional[str] = None,
        mode: str = 'production',
        verbose: bool = False,
    ):
        self.host = host
        self.port = port
        self.output_dir = output_dir
        self.write_interval = write_interval
        self.split_files = split_files
        self.mode = mode
        self.timezone = ZoneInfo(timezone)
        self.timezone_name = timezone
        self.image_id = image_id
        self.verbose = verbose

        # In test mode subscribe to test/# and log to a separate subdirectory.
        self.root = 'test' if mode == 'test' else 'home'
        self.topics = topics or [f'{self.root}/#']

        # Build topic prefix -> file prefix mapping, inserting Image ID if known.
        # Topics are {root}/{image_id}/suffix (with Image ID) or {root}/suffix (without).
        if image_id:
            self.topic_prefixes = {
                f'{self.root}/{image_id}/{s}': n for s, n in self.TOPIC_SUFFIXES.items()
            }
        else:
            self.topic_prefixes = {
                f'{self.root}/{s}': n for s, n in self.TOPIC_SUFFIXES.items()
            }

        # Effective output directory — date subdirectory is added per message.
        # Layout: <output_dir>/<image_id>/YYYY-MM-DD/<prefix>_<image_id>.log
        # The image_id subdir matches what log_sync.sh rsyncs (LOCAL_PATH={dir}/{IMAGE_ID}).
        # Without it, log files would land outside log_sync's LOCAL_PATH and never reach VPS.
        base = os.path.join(output_dir, 'test') if mode == 'test' else output_dir
        self.effective_dir: str = os.path.join(base, image_id) if image_id else base

        # Write immediately when on RAM disk — no buffering needed
        self.immediate_write: bool = is_ram_disk(output_dir)

        # Message buffer: {file_key: [messages]} — only used when not immediate_write
        self.buffer: dict[str, list[str]] = defaultdict(list)
        self.message_count = 0
        self.last_write_time = time.time()
        # Count of messages dropped due to insufficient log-partition space (NFR-4.52)
        self._disk_full_drops: int = 0

        # MQTT client
        self.client: Optional[mqtt.Client] = None
        self.running = False

    def log(self, message: str) -> None:
        """Print message if verbose mode enabled."""
        if self.verbose:
            print(message)

    def _build_file_key(self, prefix: str, date_str: str) -> str:
        """Build file key: date_dir/prefix_imageid or date_dir/prefix."""
        name = f"{prefix}_{self.image_id}" if self.image_id else prefix
        return f"{date_str}/{name}"

    def get_file_key(self, topic: str, date_str: str) -> str:
        """
        Determine the file key for a given topic and date.

        In split mode, maps topics to prefixes (e.g., otgw/energy/gas -> gas_a1b2c3_2026-01-29)
        In single mode, all messages go to one file (e.g., mqtt_a1b2c3_2026-01-29)
        """
        if not self.split_files:
            return self._build_file_key("mqtt", date_str)

        # Find matching prefix
        for topic_prefix, file_prefix in self.topic_prefixes.items():
            if topic.startswith(topic_prefix):
                return self._build_file_key(file_prefix, date_str)

        # Default: strip the known root prefix before sanitizing so the image_id
        # doesn't appear twice (once in the path, once added by _build_file_key).
        root_prefix = (f'{self.root}/{self.image_id}/'
                       if self.image_id else f'{self.root}/')
        remainder = (topic[len(root_prefix):]
                     if topic.startswith(root_prefix) else topic)
        sanitized = remainder.replace('/', '_').replace('#', 'all').replace('+', 'any')
        return self._build_file_key(sanitized, date_str)

    def format_message(self, topic: str, payload: str, timestamp: datetime.datetime) -> str:
        """Format a log message with ISO8601 timestamp including timezone offset."""
        # Format: 2026-01-30T14:30:00+01:00 | topic : payload
        iso_timestamp = timestamp.strftime('%Y-%m-%dT%H:%M:%S%z')
        # Insert colon in timezone offset (Python's %z gives +0100, we want +01:00)
        iso_timestamp = iso_timestamp[:-2] + ':' + iso_timestamp[-2:]
        return f"{iso_timestamp} | {topic} : {payload}"

    def on_connect(self, client: mqtt.Client, userdata, connect_flags,
                   reason_code, properties) -> None:
        """Handle MQTT connection."""
        if reason_code == 0:
            print(f"Connected to MQTT broker at {self.host}:{self.port}")
            # Subscribe to all configured topics
            for topic in self.topics:
                client.subscribe(topic)
                print(f"Subscribed to: {topic}")
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

    def on_message(self, client: mqtt.Client, userdata, msg: mqtt.MQTTMessage) -> None:
        """Handle incoming MQTT message."""
        try:
            # Get current time in configured timezone
            now = datetime.datetime.now(self.timezone)
            date_str = now.strftime('%Y-%m-%d')

            # Decode payload
            try:
                payload = msg.payload.decode('utf-8')
            except UnicodeDecodeError:
                payload = msg.payload.hex()

            # Format message
            file_key = self.get_file_key(msg.topic, date_str)
            log_message = self.format_message(msg.topic, payload, now)
            self.message_count += 1
            self.log(f"{file_key} -> {log_message}")

            if self.immediate_write:
                self._write_immediately(file_key, log_message)
            else:
                self.buffer[file_key].append(log_message)

        except Exception as e:
            print(f"Error processing message: {e}", file=sys.stderr)

    def _check_log_disk_space(self) -> bool:
        """Return True if enough space to write. Log warning/error if low.

        Returns False (and increments _disk_full_drops) when < 5 MB free.
        Logs LOG_DISK_LOW warning when < 50 MB free. NFR-4.52.
        """
        check_path = (self.effective_dir
                      if os.path.exists(self.effective_dir) else self.output_dir)
        try:
            st = os.statvfs(check_path)
            avail_mb = (st.f_bavail * st.f_frsize) // (1024 * 1024)
        except OSError:
            return True  # can't check; let write_buffer fail naturally if needed
        if avail_mb < 5:
            pending = sum(len(v) for v in self.buffer.values())
            self._disk_full_drops += pending
            print(
                f"LOG_DISK_FULL: {avail_mb} MB free, "
                f"skipping write, dropped {self._disk_full_drops} messages total",
                file=sys.stderr,
            )
            return False
        if avail_mb < 50:
            print(
                f"LOG_DISK_LOW: {avail_mb} MB free on log partition",
                file=sys.stderr,
            )
        return True

    def _write_immediately(self, file_key: str, log_message: str) -> None:
        """Write a single message directly to file — used on RAM disks."""
        file_path = os.path.join(self.effective_dir, f"{file_key}.log")
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        try:
            with open(file_path, 'a') as f:
                f.write(log_message + '\n')
                # flush() is enough for tmpfs — no physical seek penalty,
                # no need for fsync() which would be a no-op anyway.
                f.flush()
        except OSError as e:
            print(f"Error writing to {file_path}: {e}", file=sys.stderr)

    def write_buffer(self) -> None:
        """Write buffered messages to files (used on real disks)."""
        if not self.buffer:
            self.log("Buffer empty, nothing to write")
            return

        if not self._check_log_disk_space():
            self.buffer.clear()
            return

        os.makedirs(self.effective_dir, exist_ok=True)

        files_written = 0
        messages_written = 0

        for file_key, messages in self.buffer.items():
            if not messages:
                continue

            file_path = os.path.join(self.effective_dir, f"{file_key}.log")
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            try:
                with open(file_path, 'a') as f:
                    for message in messages:
                        f.write(message + '\n')
                    # Flush Python buffers and fsync — protects against data
                    # loss on power failure, especially on FAT32.
                    f.flush()
                    os.fsync(f.fileno())
                files_written += 1
                messages_written += len(messages)
            except OSError as e:
                # Log partition may be full or temporarily unavailable.
                # Drop this batch and continue — losing one write interval is
                # better than crashing the logger.
                print(
                    f"Error writing to {file_path}: {e} — "
                    f"dropping {len(messages)} messages",
                    file=sys.stderr,
                )

        self.buffer.clear()
        self.last_write_time = time.time()

        print(f"Wrote {messages_written} messages to {files_written} file(s)")

    def setup_signals(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        def signal_handler(sig, frame):
            print(f"\nReceived signal {sig}, shutting down...")
            self.stop()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    def start(self) -> None:
        """Start the MQTT logger."""
        print(f"MQTT Logger starting...")
        print(f"  Mode: {self.mode.upper()}")
        print(f"  Broker: {self.host}:{self.port}")
        print(f"  Output: {self.output_dir}")
        if self.immediate_write:
            print(f"  Write mode: immediate (RAM disk detected)")
        else:
            print(f"  Write interval: {self.write_interval}s (buffered)")
        print(f"  Split files: {self.split_files}")
        print(f"  Image ID: {self.image_id or '(none)'}")
        print(f"  Timezone: {self.timezone_name}")
        print(f"  Topics: {', '.join(self.topics)}")
        print()

        # Setup signal handlers
        self.setup_signals()

        # Create and configure MQTT client.
        # reconnect_on_failure=True ensures paho's background thread re-establishes
        # the connection if the broker restarts; without it the logger stays active
        # but receives nothing after a mosquitto restart, silently dropping all data.
        # (NFR-4.69)
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            reconnect_on_failure=True,
        )
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

        # Connect to broker (with retry)
        self._connect_mqtt()

        # Start MQTT loop in background thread
        self.client.loop_start()
        self.running = True
        _last_watchdog_ping: float = 0.0

        # Main loop: periodic buffer writes
        try:
            while self.running:
                time.sleep(1)

                # Check if it's time to write
                elapsed = time.time() - self.last_write_time
                if elapsed >= self.write_interval:
                    self.write_buffer()

                # Ping the systemd watchdog periodically. (NFR-4.80)
                _now = time.monotonic()
                if _now - _last_watchdog_ping >= _WATCHDOG_PING_INTERVAL:
                    _sd_notify('WATCHDOG=1')
                    _last_watchdog_ping = _now

        except Exception as e:
            print(f"Error in main loop: {e}", file=sys.stderr)
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the logger and flush buffer.

        loop_stop() is called first to drain paho's background thread —
        any on_message() callbacks in flight will complete before the thread
        exits. write_buffer() is then called to catch those final messages.
        Reversing this order risks missing messages that arrive after
        write_buffer() returns but before the paho thread is stopped. (NFR-4.77)
        """
        self.running = False

        # Stop paho thread first so no more on_message() callbacks can fire
        # after the final write_buffer() call. (NFR-4.77)
        if self.client:
            self.client.loop_stop()

        # Flush whatever arrived up to and including the last callback.
        print("Flushing buffer...")
        self.write_buffer()

        if self.client:
            self.client.disconnect()

        print(f"Logged {self.message_count} messages total")
        sys.exit(0)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='MQTT message logger with buffered writes',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    # Default settings
  %(prog)s --output-dir /var/log/otgw         # Custom output directory
  %(prog)s --write-interval 1800              # Write every 30 minutes
  %(prog)s --single-file                      # All messages in one file
  %(prog)s --topics otgw/energy/#             # Custom topic filter
  %(prog)s -v                                 # Verbose output
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
        '--output-dir',
        default='/var/log/otgw',
        help='Log file output directory (default: /var/log/otgw)',
    )
    parser.add_argument(
        '--write-interval',
        type=int,
        default=900,
        help='Write interval in seconds (default: 900 = 15 minutes)',
    )

    # File mode: split vs single
    file_mode = parser.add_mutually_exclusive_group()
    file_mode.add_argument(
        '--split-files',
        action='store_true',
        default=True,
        help='Separate files per topic prefix (default)',
    )
    file_mode.add_argument(
        '--single-file',
        action='store_true',
        help='All topics in one file per day',
    )

    parser.add_argument(
        '--topics',
        help='Comma-separated list of topics to subscribe (default: otgw/#)',
    )
    parser.add_argument(
        '--image-id',
        default=None,
        help='Image identifier for log filenames (default: read from /etc/otgw/pi-id)',
    )
    parser.add_argument(
        '--timezone',
        default='Europe/Amsterdam',
        help='Timezone for timestamps and file dates (default: Europe/Amsterdam)',
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Print each message as it arrives',
    )

    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Parse topics
    topics = None
    if args.topics:
        topics = [t.strip() for t in args.topics.split(',')]

    # Determine split mode
    split_files = not args.single_file

    # Resolve Image ID: explicit arg > file > None
    image_id = args.image_id if args.image_id is not None else read_image_id_file()

    # Read mode from config file
    mode = read_mode()
    if mode == 'test':
        print("Mode: TEST — subscribing to test/# and logging to test/ subdirectory")

    # Create and start logger
    logger = MqttLogger(
        host=args.host,
        port=args.port,
        output_dir=args.output_dir,
        write_interval=args.write_interval,
        split_files=split_files,
        topics=topics,
        timezone=args.timezone,
        image_id=image_id,
        mode=mode,
        verbose=args.verbose,
    )

    logger.start()


if __name__ == '__main__':
    main()
