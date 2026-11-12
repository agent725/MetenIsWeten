#!/usr/bin/env python3
"""
OpenTherm Gateway (OTGW) Listener

Reads messages from an OpenTherm Gateway via USB serial and publishes to MQTT.

By default, pairs request/response messages (T+B or R+A) on the same data ID
within a 2-second window and publishes a single compact flat JSON message:

    {"id":25,"req":"T00190000","ack":"B40193480","Tboiler":52.5,"ms":38}

The raw 9-char strings contain the full OpenTherm message — all fields can
be reconstructed from these alone. Decoded values (Tboiler, flame, etc.)
are added for convenience.

Protocol:
    Messages are lines starting with a letter followed by 8 hex digits.
    - T = From thermostat        (pairs with B)
    - B = From boiler            (pairs with T)
    - R = Request to boiler      (gateway modified, pairs with A)
    - A = Answer to thermostat   (gateway modified, pairs with R)
    - E = Error                  (published immediately, never paired)

Usage:
    otgw_listener.py [options]

Examples:
    otgw_listener.py                        # Auto-detect device, pair messages
    otgw_listener.py --device /dev/ttyUSB0  # Specific device
    otgw_listener.py --no-pair              # Publish each message individually
    otgw_listener.py --raw                  # Publish raw 9-char lines only
    otgw_listener.py -v                     # Verbose output

References:
    - OpenTherm Gateway: https://otgw.tclcode.com/
    - OpenTherm Protocol: https://www.opentherm.eu/
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import argparse
import glob
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

PI_ID_FILE = '/etc/otgw/pi-id'
MODE_FILE = '/etc/otgw/mode'

# USB VID for the CH340G chip used on the OTGW USB-serial board. (NFR-4.63)
# Prefer /dev/serial/by-id/ entries containing this VID over first ttyUSB*.
OTGW_USB_VID = '1a86'

# Health metrics written every 60 s; heartbeat.py reads this file. (NFR-4.65)
HEALTH_FILE = '/run/otgw/otgw-listener-health.json'
HEALTH_WRITE_INTERVAL = 60  # seconds

# Seconds of silence on the serial port before stale-port recovery fires.
# OTGW sends a message every ~1 s in normal operation; 15 s allows for brief
# gaps while still recovering quickly from a port disruption.
SERIAL_STALE_TIMEOUT = 15  # seconds

# Maximum bytes to read per readline() call.  OTGW messages are at most
# 11 bytes (9 hex chars + \r\n).  This cap prevents an infinite read when the
# port floods with non-newline bytes (e.g. NULL bytes from a stuck CH340 after
# a DTR reset).  readline() with timeout=1.0 resets its deadline on every
# successful byte read, so a continuous stream of non-newline bytes means it
# never returns — blocking the stale-port recovery code forever. (NFR-4.66)
_READLINE_MAX = 50

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
    import serial
except ImportError:
    print("Error: pyserial not installed. Run: pip install pyserial",
          file=sys.stderr)
    sys.exit(1)

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Error: paho-mqtt not installed. Run: pip install paho-mqtt",
          file=sys.stderr)
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

    home/energy/otgw -> home/a1b2c3/energy/otgw  (production)
    home/energy/otgw -> test/a1b2c3/energy/otgw   (test)
    """
    # In test mode, replace the namespace prefix (first segment)
    if mode == 'test':
        parts = base_topic.split('/', 1)
        base_topic = f"test/{parts[1]}" if len(parts) == 2 else f"test/{base_topic}"

    if not image_id:
        return base_topic
    parts = base_topic.split('/', 1)
    if len(parts) == 2:
        return f"{parts[0]}/{image_id}/{parts[1]}"
    return f"{image_id}/{base_topic}"


# Request sources (pair with response) and response sources
REQUEST_SOURCES = {'T', 'R'}
RESPONSE_SOURCES = {'B', 'A'}
# T pairs with B, R pairs with A
PAIR_MAP = {'T': 'B', 'R': 'A', 'B': 'T', 'A': 'R'}

# Pairing timeout in seconds
PAIR_TIMEOUT = 2.0

# Decoded value key per data ID (from ack/response side).
# Maps data_id -> (key_name, decode_function)
# f8.8: signed 16-bit / 256.0
def _f88(val: int) -> float:
    signed = val - 65536 if val > 32767 else val
    return round(signed / 256.0, 2)

def _f88_1(val: int) -> float:
    signed = val - 65536 if val > 32767 else val
    return round(signed / 256.0, 1)

def _s16(val: int) -> int:
    """Signed 16-bit integer (for exhaust temp)."""
    return val - 65536 if val > 32767 else val


DECODE_MAP: dict[int, tuple[str, callable]] = {
    1:  ('Tset', _f88),
    7:  ('cool', _f88_1),
    8:  ('Tset2', _f88),
    9:  ('TrOverride', _f88),
    14: ('maxMod', _f88_1),
    16: ('TrSet', _f88),
    17: ('mod', _f88_1),
    18: ('bar', _f88),
    19: ('lpm', _f88),
    23: ('TrSet2', _f88),
    24: ('Tr', _f88),
    25: ('Tboiler', _f88),
    26: ('Tdhw', _f88),
    27: ('Tout', _f88),
    28: ('Tret', _f88),
    31: ('Tch2', _f88),
    33: ('Texhaust', _s16),
    56: ('TdhwSet', _f88),
    57: ('TmaxSet', _f88),
    77: ('vent', _f88_1),
    116: ('starts', None),  # None = use raw unsigned value
    117: ('starts', None),
    118: ('starts', None),
    119: ('starts', None),
    120: ('hours', None),
    121: ('hours', None),
    122: ('hours', None),
    123: ('hours', None),
}


def parse_raw(line: str) -> Optional[tuple[str, int, int]]:
    """Parse a raw OTGW line into (source, data_id, data_value).

    Returns None if the line is not a valid OTGW message.
    """
    if len(line) != 9:
        return None
    src = line[0]
    if src not in ('T', 'B', 'R', 'A', 'E'):
        return None
    try:
        hex_data = line[1:]
        byte1 = int(hex_data[2:4], 16)
        data_val = int(hex_data[4:8], 16)
        return src, byte1, data_val
    except ValueError:
        return None


def decode_status_flags(data_value: int, is_request: bool) -> dict:
    """Decode data ID 0 status flags. Returns dict with 0/1 values."""
    if is_request:
        # Master status (high byte)
        hb = (data_value >> 8) & 0xFF
        return {
            'ch': (hb >> 0) & 1,
            'dhw': (hb >> 1) & 1,
            'cool': (hb >> 2) & 1,
            'otc': (hb >> 3) & 1,
        }
    else:
        # Slave status (low byte)
        lb = data_value & 0xFF
        return {
            'fault': (lb >> 0) & 1,
            'ch': (lb >> 1) & 1,
            'dhw': (lb >> 2) & 1,
            'flame': (lb >> 3) & 1,
        }


def build_paired_msg(
    data_id: int,
    req_raw: Optional[str],
    ack_raw: Optional[str],
    ms: Optional[int],
) -> dict:
    """Build a compact flat JSON message from a paired request/response."""
    msg: dict = {'id': data_id}
    msg['req'] = req_raw
    msg['ack'] = ack_raw

    # Decode value from ack side (the actual measured/reported value)
    # For status (id 0), decode both sides
    if data_id == 0:
        if req_raw:
            parsed = parse_raw(req_raw)
            if parsed:
                # Don't add req-side flags to keep it minimal —
                # only ack (boiler state) is decoded at top level
                pass
        if ack_raw:
            parsed = parse_raw(ack_raw)
            if parsed:
                _, _, val = parsed
                msg.update(decode_status_flags(val, is_request=False))
    elif data_id in DECODE_MAP:
        # Decode from ack if available, else from req
        raw = ack_raw or req_raw
        if raw:
            parsed = parse_raw(raw)
            if parsed:
                _, _, val = parsed
                key, func = DECODE_MAP[data_id]
                msg[key] = func(val) if func else val

    msg['ms'] = ms
    return msg


def build_unpaired_msg(raw: str) -> Optional[dict]:
    """Build a compact flat JSON for a single unpaired message."""
    parsed = parse_raw(raw)
    if not parsed:
        return None
    src, data_id, data_val = parsed
    is_req = src in REQUEST_SOURCES

    msg: dict = {'id': data_id}
    if is_req:
        msg['req'] = raw
        msg['ack'] = None
    else:
        msg['req'] = None
        msg['ack'] = raw

    # Decode value
    if data_id == 0:
        msg.update(decode_status_flags(data_val, is_request=is_req))
    elif data_id in DECODE_MAP:
        key, func = DECODE_MAP[data_id]
        msg[key] = func(data_val) if func else data_val

    msg['ms'] = None
    return msg


class OTGWListener:
    """OpenTherm Gateway listener with MQTT publishing and message pairing."""

    MSG_PATTERN = re.compile(r'^[TBRAE][0-9A-Fa-f]{8}$')

    def __init__(
        self,
        device: Optional[str] = None,
        baud: int = 9600,
        host: str = 'localhost',
        port: int = 1883,
        topic: str = 'home/energy/otgw',
        verbose: bool = False,
        raw: bool = False,
        pair: bool = True,
        buffer_size: int = 200,
        device_vid: str = OTGW_USB_VID,
        flush_delay_s: float = 3.0,
    ):
        self.device = device
        self.device_vid = device_vid
        self.baud = baud
        self.host = host
        self.port = port
        self.topic = topic
        self.verbose = verbose
        self.raw = raw
        self.pair = pair

        self.serial: Optional[serial.Serial] = None
        self.mqtt_client: Optional[mqtt.Client] = None
        self.running = False
        self.message_count = 0

        # Pairing buffer: {data_id: (raw_line, timestamp)}
        self.pending_requests: dict[int, tuple[str, float]] = {}

        # Timestamp of last valid OTGW message received (for stale-port detection)
        self.last_message_time: Optional[float] = None
        # Timestamp when the current serial port was opened; set in the outer loop.
        # Combined with last_message_time to detect silence from boot. (NFR-4.27)
        self._port_open_time: Optional[float] = None
        # Recovery phase: 0 = no reset sent, 1 = one DTR reset sent,
        # 2 = two DTR resets sent, 3+ = gave up (log only).
        # Cleared to 0 by any valid message or a fresh device open. (NFR-4.27)
        self._reset_phase: int = 0

        # Offline message buffer: holds messages when MQTT is disconnected. (NFR-4.61)
        # Oldest messages are dropped when the buffer is full (deque maxlen).
        self._msg_buffer: Optional[deque] = (
            deque(maxlen=buffer_size) if buffer_size > 0 else None
        )
        # _offline_count: messages that entered the offline path (buffered OR lost).
        # Used only for log-spam throttling (log on first, then every 10th). (NFR-4.61)
        self._offline_count: int = 0
        # _truly_dropped_count: messages genuinely lost — either the buffer is
        # disabled (buffer_size=0) or the buffer was full and the oldest was evicted.
        # This is the value reported in the health file as 'drop_count'. (NFR-4.75)
        self._truly_dropped_count: int = 0

        # Delay (seconds) before flushing the offline buffer after reconnect.
        # Gives mqtt_logger time to reconnect and re-subscribe so QoS=0 flushed
        # messages are not discarded by the broker. (NFR-4.73)
        self._flush_delay_s: float = flush_delay_s
        self._flush_timer: Optional[threading.Timer] = None

        # systemd watchdog: track last ping time to avoid pinging too often.
        self._last_watchdog_ping: float = 0.0

        # MQTT watchdog: 30-s ticks while disconnected. (NFR-4.62)
        # After 3 ticks (90 s) a forced reconnect() is attempted.
        self._mqtt_disconnect_ticks: int = 0

        # Set True by SIGUSR1 handler to break the inner loop and release the
        # serial port without stopping the process. (NFR-4.64)
        self._release_port: bool = False

        # Health metrics. (NFR-4.65)
        self._last_publish_time: Optional[float] = None
        self._last_health_write: float = 0.0

    def log(self, message: str) -> None:
        """Print message if verbose mode enabled."""
        if self.verbose:
            print(message)

    def find_serial_device(self, vid: str = OTGW_USB_VID) -> Optional[str]:
        """Auto-detect OTGW serial device, preferring by-id VID match. (NFR-4.63)

        Priority:
          1. /dev/serial/by-id/ entry whose name contains `vid` (e.g. '1a86').
             Resolves the symlink so the caller gets a stable /dev/ttyUSBn path.
          2. First /dev/ttyUSB* only (ttyACM* is reserved for the P1 dongle).

        The OTGW is always a USB-to-serial adapter (CH340/CP210x) → ttyUSB*.
        Never fall back to ttyACM* — that is the ESP32-S3 P1 dongle's port.
        """
        # Priority 1: VID-matched by-id entry
        by_id = Path('/dev/serial/by-id')
        if by_id.exists():
            matches = sorted(p for p in by_id.iterdir() if vid in p.name)
            if matches:
                try:
                    resolved = str(matches[0].resolve())
                    print(f"Auto-detected OTGW by VID {vid}: {resolved}")
                    return resolved
                except OSError:
                    pass

        # Priority 2: first ttyUSB* only — never grab ttyACM* (P1 dongle)
        devices = glob.glob('/dev/ttyUSB*')
        if devices:
            device = devices[0]
            print(f"Auto-detected serial device: {device}")
            return device
        return None

    def wait_for_serial_device(self, timeout: int = 30) -> Optional[str]:
        """Auto-detect OTGW serial device, waiting up to timeout seconds.

        USB devices can take a few seconds to enumerate after boot.
        Retrying here avoids relying on systemd restart cycling.
        """
        deadline = time.monotonic() + timeout
        while True:
            device = self.find_serial_device(self.device_vid)
            if device:
                return device
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            print(f"Serial device not found yet, retrying in 5 s "
                  f"({int(remaining)} s remaining)...", file=sys.stderr)
            time.sleep(5)

    def _attempt_dtr_reset(self) -> None:
        """Toggle DTR on the open serial port to reset a hung firmware.

        Works on ESP32 and other boards that treat a DTR falling edge as a
        hardware reset signal (same mechanism as esptool uses for flashing).
        No-op if the serial port is not currently open.
        """
        if self.serial is None:
            return
        try:
            self.serial.dtr = False
            time.sleep(0.1)
            self.serial.dtr = True
            print("DTR reset sent — waiting for firmware to restart",
                  file=sys.stderr)
            # Clear last_message_time so the 5-min stale clock restarts from
            # now, giving the firmware time to boot before the next warning.
            self.last_message_time = time.monotonic()
        except Exception as e:
            print(f"DTR reset failed: {e}", file=sys.stderr)

    def flush_stale(self) -> None:
        """Publish and remove requests older than PAIR_TIMEOUT."""
        now = time.monotonic()
        stale_ids = [
            did for did, (_, ts) in self.pending_requests.items()
            if now - ts > PAIR_TIMEOUT
        ]
        for did in stale_ids:
            raw_line, _ = self.pending_requests.pop(did)
            msg = build_unpaired_msg(raw_line)
            if msg:
                self.publish_json(msg)

    def handle_message(self, line: str) -> None:
        """Process a validated OTGW message line with pairing logic."""
        self.last_message_time = time.monotonic()
        self._reset_phase = 0  # any valid message resets the recovery phase
        if self.raw:
            self.publish_raw(line)
            return

        if not self.pair:
            msg = build_unpaired_msg(line)
            if msg:
                self.publish_json(msg)
            return

        parsed = parse_raw(line)
        if not parsed:
            return
        src, data_id, _ = parsed

        # Error messages: publish immediately, never pair
        if src == 'E':
            msg = build_unpaired_msg(line)
            if msg:
                self.publish_json(msg)
            return

        now = time.monotonic()

        if src in REQUEST_SOURCES:
            # Buffer the request, replacing any existing one for this data_id
            if data_id in self.pending_requests:
                # Flush the old unpaired request first
                old_raw, _ = self.pending_requests.pop(data_id)
                msg = build_unpaired_msg(old_raw)
                if msg:
                    self.publish_json(msg)
            self.pending_requests[data_id] = (line, now)

        elif src in RESPONSE_SOURCES:
            # Try to pair with a pending request
            if data_id in self.pending_requests:
                req_raw, req_ts = self.pending_requests.pop(data_id)
                elapsed_ms = int((now - req_ts) * 1000)
                msg = build_paired_msg(data_id, req_raw, line, elapsed_ms)
                self.publish_json(msg)
            else:
                # Orphan response (no matching request)
                msg = build_unpaired_msg(line)
                if msg:
                    self.publish_json(msg)

    def publish_json(self, msg: dict) -> None:
        """Publish a JSON message to MQTT."""
        payload = json.dumps(msg, separators=(',', ':'))
        self._publish(payload)

    def publish_raw(self, line: str) -> None:
        """Publish a raw line to MQTT."""
        self._publish(line)

    def _publish(self, payload: str) -> None:
        """Publish payload string to MQTT, buffering when disconnected.

        Uses is_connected() as a fast path, but treats a failed publish()
        return code the same as not-connected: the message is buffered rather
        than silently discarded. This closes the TOCTOU race where the
        connection drops between is_connected() (True) and publish().
        (NFR-4.61 / NFR-4.74)
        """
        if self.mqtt_client and self.mqtt_client.is_connected():
            result = self.mqtt_client.publish(self.topic, payload)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.message_count += 1
                self._last_publish_time = time.time()
                self.log(f"Published: {payload}")
                return  # success — do not fall through to buffer
            # publish() failed (TOCTOU: connection dropped after is_connected check)
            print(f"Publish failed (rc={result.rc}), buffering message",
                  file=sys.stderr)
        # Offline or publish failed: buffer the message.
        self._offline_count += 1
        if self._msg_buffer is not None:
            # Detect eviction: if the buffer is already full, the oldest entry
            # will be silently discarded by the deque on append. (NFR-4.75)
            if len(self._msg_buffer) == self._msg_buffer.maxlen:
                self._truly_dropped_count += 1
            self._msg_buffer.append(payload)
        else:
            # No buffer configured — this message is truly lost.
            self._truly_dropped_count += 1
        # Log on first offline event then every 10 thereafter to avoid spam.
        if self._offline_count == 1 or self._offline_count % 10 == 0:
            buffered = len(self._msg_buffer) if self._msg_buffer else 0
            print(
                f"MQTT offline — {self._offline_count} message(s) offline "
                f"({buffered} buffered, {self._truly_dropped_count} truly lost)",
                file=sys.stderr,
            )

    def _flush_buffer(self) -> None:
        """Drain the offline buffer to MQTT.

        Called via a threading.Timer _flush_delay_s seconds after reconnect so
        that mqtt_logger has time to re-subscribe before messages arrive.
        If the connection dropped again in the interim, the flush is skipped and
        messages remain buffered for the next reconnect. (NFR-4.73)
        """
        self._flush_timer = None
        if not self._msg_buffer:
            return
        if not (self.mqtt_client and self.mqtt_client.is_connected()):
            return
        count = len(self._msg_buffer)
        while self._msg_buffer:
            buffered_payload = self._msg_buffer.popleft()
            self.mqtt_client.publish(self.topic, buffered_payload)
            self.message_count += 1
        print(f"Flushed {count} buffered message(s) after reconnect",
              file=sys.stderr)

    def on_mqtt_connect(self, client, userdata, connect_flags,
                        reason_code, properties) -> None:
        """Handle MQTT connection; schedule delayed buffer flush. (NFR-4.61/4.73)"""
        if reason_code == 0:
            print(f"Connected to MQTT broker at {self.host}:{self.port}")
            if self._msg_buffer:
                # Schedule flush after _flush_delay_s so mqtt_logger can
                # reconnect and re-subscribe before messages arrive. Without
                # this delay, QoS=0 messages published to a broker with no
                # subscriber yet are silently discarded. (NFR-4.73)
                if self._flush_timer is not None:
                    self._flush_timer.cancel()
                self._flush_timer = threading.Timer(
                    self._flush_delay_s, self._flush_buffer
                )
                self._flush_timer.daemon = True
                self._flush_timer.start()
            self._offline_count = 0
        else:
            print(f"MQTT connection failed with code {reason_code}",
                  file=sys.stderr)

    def on_mqtt_disconnect(self, client, userdata, disconnect_flags,
                           reason_code, properties) -> None:
        """Handle MQTT disconnection; cancel any pending buffer flush. (NFR-4.73)"""
        if reason_code != 0:
            print(f"MQTT disconnected (rc={reason_code}), will reconnect...",
                  file=sys.stderr)
        # Cancel pending flush: the buffer is still intact and will be
        # flushed after the next successful reconnect. (NFR-4.73)
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None

    def _mqtt_watchdog_loop(self) -> None:
        """Background thread: force reconnect if paho's auto-reconnect stalls.

        paho's exponential back-off can delay reconnect by up to 120 s.
        If the client is still disconnected after 3 × 30 s = 90 s, call
        reconnect() directly to bypass the back-off timer. (NFR-4.62)
        """
        while self.running:
            time.sleep(30)
            if not self.running:
                break
            if self.mqtt_client and not self.mqtt_client.is_connected():
                self._mqtt_disconnect_ticks += 1
                if self._mqtt_disconnect_ticks >= 3:
                    print("MQTT watchdog: forcing reconnect after 90 s stall",
                          file=sys.stderr)
                    try:
                        self.mqtt_client.reconnect()
                    except Exception as e:
                        print(f"MQTT watchdog reconnect failed: {e}",
                              file=sys.stderr)
                    # Reset so next stall triggers after another 90 s
                    self._mqtt_disconnect_ticks = 0
            else:
                self._mqtt_disconnect_ticks = 0

    def _write_health(self, device: Optional[str]) -> None:
        """Write health metrics to HEALTH_FILE for heartbeat to read. (NFR-4.65)"""
        now = time.time()
        if now - self._last_health_write < HEALTH_WRITE_INTERVAL:
            return
        self._last_health_write = now
        connected = bool(self.mqtt_client and self.mqtt_client.is_connected())
        last_pub_s_ago = (
            int(now - self._last_publish_time)
            if self._last_publish_time is not None else None
        )
        data = {
            'ts': int(now),
            'last_publish_s_ago': last_pub_s_ago,
            'drop_count': self._truly_dropped_count,
            'mqtt_connected': connected,
            'serial_device': device,
            'serial_open': self.serial is not None,
            'message_count': self.message_count,
        }
        try:
            os.makedirs(os.path.dirname(HEALTH_FILE), exist_ok=True)
            with open(HEALTH_FILE, 'w') as f:
                json.dump(data, f)
        except OSError as e:
            print(f"Health file write failed: {e}", file=sys.stderr)

    def _connect_mqtt(self) -> None:
        """Connect to MQTT broker with indefinite retry and backoff."""
        delays = [5, 10, 30, 60]
        attempt = 0
        while True:
            try:
                self.mqtt_client.connect(self.host, self.port, keepalive=60)
                return
            except Exception as e:
                delay = delays[min(attempt, len(delays) - 1)]
                print(
                    f"MQTT connect failed ({e}), retrying in {delay}s...",
                    file=sys.stderr,
                )
                time.sleep(delay)
                attempt += 1

    def setup_signals(self) -> None:
        """Setup signal handlers for graceful shutdown and port release."""
        def signal_handler(sig, frame):
            print(f"\nReceived signal {sig}, shutting down...")
            self.stop()

        def release_handler(sig, frame):
            """SIGUSR1: release serial port so another process can use it.

            Sets _release_port flag; the inner read loop checks this flag and
            breaks back to the outer loop, which closes the port and then waits
            for it to reappear (auto-reconnect). The MQTT connection and message
            buffer are preserved. (NFR-4.64)
            """
            print("SIGUSR1: releasing serial port for external operation",
                  file=sys.stderr)
            self._release_port = True

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGUSR1, release_handler)

    def start(self) -> None:
        """Start the OTGW listener.

        Runs two nested loops for robust hot-plug support:
        - Outer loop: scans for a serial device every 5 s indefinitely.
          Handles plug-in after boot and re-plug after removal.
        - Inner loop: reads lines from the open port until a SerialException
          signals the device was removed, then breaks back to the outer loop.

        The process never exits on its own (only on SIGINT/SIGTERM), so
        systemd does not need to restart it for plug/unplug events.
        """
        print("OTGW Listener starting...")
        print(f"  MQTT Broker: {self.host}:{self.port}")
        print(f"  Topic: {self.topic}")
        print(f"  Baud rate: {self.baud}")
        print(f"  Pairing: {'on' if self.pair else 'off'}")
        print()

        self.setup_signals()

        # Connect to MQTT broker once; loop_start() keeps it alive. (NFR-4.60)
        # reconnect_on_failure=True is the default in paho 2.x but we set it
        # explicitly so behaviour is not version-dependent.
        self.mqtt_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            reconnect_on_failure=True,
        )
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_disconnect = self.on_mqtt_disconnect

        self._connect_mqtt()
        self.mqtt_client.loop_start()
        self.running = True

        # Watchdog thread: forces reconnect if paho's back-off stalls. (NFR-4.62)
        threading.Thread(
            target=self._mqtt_watchdog_loop,
            daemon=True,
            name='mqtt-watchdog',
        ).start()

        # ── Outer loop: find device ──────────────────────────────────────────
        while self.running:
            device = self.device or self.find_serial_device(self.device_vid)

            if not device:
                print("No serial device found, retrying in 5 s...",
                      file=sys.stderr)
                time.sleep(5)
                continue

            print(f"Opening serial port: {device}")
            self._reset_phase = 0  # fresh connection — reset recovery phase
            try:
                self.serial = serial.Serial(
                    port=device,
                    baudrate=self.baud,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=1.0,
                )
            except serial.SerialException as e:
                print(f"Failed to open {device}: {e}", file=sys.stderr)
                self.serial = None
                time.sleep(5)
                continue

            print("Serial port opened successfully")
            print()
            self._port_open_time = time.monotonic()
            self._release_port = False  # clear any pending release flag

            # ── Inner loop: read until device lost ───────────────────────────
            try:
                while self.running:
                    # SIGUSR1 port-release: break to outer loop, which closes
                    # the port and waits for it to reappear. (NFR-4.64)
                    if self._release_port:
                        print("Releasing serial port (SIGUSR1 requested)",
                              file=sys.stderr)
                        break
                    try:
                        # Use a size cap so readline() returns even when the
                        # port floods with non-newline bytes (e.g. NULL bytes
                        # from a stuck CH340 after a DTR reset). Without this
                        # cap, pyserial's readline() resets its 1-s deadline on
                        # every incoming byte and never returns, which blocks the
                        # stale-port recovery code below. (NFR-4.66)
                        line = self.serial.readline(_READLINE_MAX)
                        if line:
                            try:
                                line_str = line.decode('ascii',
                                                       errors='replace').strip()
                            except Exception:
                                line_str = ''
                            if line_str and self.MSG_PATTERN.match(line_str):
                                self.handle_message(line_str)
                            else:
                                if line_str:
                                    self.log(f"Non-message line: {line_str}")
                                # Garbage bytes (e.g. all NULLs): clear the
                                # UART receive buffer so they don't accumulate.
                                if any(b < 0x20 for b in line
                                       if b not in (0x0A, 0x0D)):
                                    try:
                                        self.serial.reset_input_buffer()
                                    except Exception:
                                        pass

                        # Flush stale paired requests and write health every
                        # iteration so these run even when data flows
                        # continuously (no readline timeout). (NFR-4.27/4.65/4.66)
                        if self.pair:
                            self.flush_stale()
                        self._write_health(device)

                        # Ping the systemd watchdog periodically. (NFR-4.80)
                        _now = time.monotonic()
                        if _now - self._last_watchdog_ping >= _WATCHDOG_PING_INTERVAL:
                            _sd_notify('WATCHDOG=1')
                            self._last_watchdog_ping = _now

                        # Auto-recover if no valid messages for >SERIAL_STALE_TIMEOUT s.
                        # Checked on EVERY iteration — not only on readline timeout —
                        # so it fires even when the port floods with garbage bytes.
                        # Use last_message_time if any message was received; fall back
                        # to port-open time so recovery fires from the very start
                        # (last_message_time is None). (NFR-4.27 / NFR-4.66)
                        _silence_ref = (
                            self.last_message_time
                            if self.last_message_time is not None
                            else self._port_open_time
                        )
                        if time.monotonic() - _silence_ref > SERIAL_STALE_TIMEOUT:
                            if self._reset_phase == 0:
                                # Phase 1: first DTR reset
                                print(
                                    f"Warning: no valid OTGW messages for "
                                    f">{SERIAL_STALE_TIMEOUT}s "
                                    f"on {device} — attempting DTR reset",
                                    file=sys.stderr,
                                )
                                self._attempt_dtr_reset()
                                self._reset_phase = 1
                            elif self._reset_phase == 1:
                                # Phase 2: second DTR reset
                                print(
                                    f"Warning: OTGW still silent after first DTR "
                                    f"reset on {device} — attempting second reset",
                                    file=sys.stderr,
                                )
                                self._attempt_dtr_reset()
                                self._reset_phase = 2
                            elif self._reset_phase == 2:
                                # Phase 3: close and reopen serial port
                                print(
                                    f"OTGW_FIRMWARE_UNRECOVERABLE: {device} still "
                                    f"silent after two DTR resets — reopening port",
                                    file=sys.stderr,
                                )
                                self._reset_phase = 3
                                break  # outer loop will close and reopen device
                            else:
                                # Phase 4+: log only, await human intervention
                                print(
                                    f"Warning: OTGW still not responding on "
                                    f"{device} — human intervention may be needed",
                                    file=sys.stderr,
                                )
                            # Restart silence clock (skipped if we broke in phase 3)
                            self.last_message_time = time.monotonic()

                    except serial.SerialException as e:
                        print(f"Serial device lost ({device}): {e}",
                              file=sys.stderr)
                        break  # Back to outer loop to re-scan

            except Exception as e:
                print(f"Unexpected error: {e}", file=sys.stderr)
            finally:
                if self.serial:
                    try:
                        self.serial.close()
                    except Exception:
                        pass
                    self.serial = None

            if self.running:
                print("Waiting for serial device to reconnect...",
                      file=sys.stderr)
                time.sleep(1)

        self.stop()

    def stop(self) -> None:
        """Stop the listener and flush pending messages."""
        self.running = False

        # Cancel any pending delayed buffer flush (NFR-4.73)
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None

        # Flush any remaining pending requests
        for did, (raw_line, _) in self.pending_requests.items():
            msg = build_unpaired_msg(raw_line)
            if msg:
                self.publish_json(msg)
        self.pending_requests.clear()

        if self.serial:
            self.serial.close()

        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

        print(f"Published {self.message_count} messages total")
        sys.exit(0)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='OpenTherm Gateway listener with MQTT publishing',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    # Auto-detect, pair messages
  %(prog)s --device /dev/ttyUSB0              # Specific device
  %(prog)s --no-pair                          # Disable pairing
  %(prog)s --raw                              # Publish raw 9-char lines
  %(prog)s -v                                 # Verbose output

Paired message format (default):
  {"id":25,"req":"T00190000","ack":"B40193480","Tboiler":52.5,"ms":38}
  {"id":0,"req":"T00000100","ack":"B4000000A","flame":1,"ch":1,"dhw":0,"fault":0,"ms":25}
        """,
    )

    parser.add_argument(
        '--device',
        help='Serial device path (default: auto-detect /dev/ttyUSB*)',
    )
    parser.add_argument(
        '--baud', type=int, default=9600,
        help='Serial baud rate (default: 9600)',
    )
    parser.add_argument(
        '--host', default='localhost',
        help='MQTT broker hostname (default: localhost)',
    )
    parser.add_argument(
        '--port', type=int, default=1883,
        help='MQTT broker port (default: 1883)',
    )
    parser.add_argument(
        '--topic', default='home/energy/otgw',
        help='MQTT topic to publish to (default: home/energy/otgw)',
    )
    parser.add_argument(
        '--image-id', default=None,
        help='Image identifier for MQTT topics '
             '(default: read from /etc/otgw/pi-id)',
    )
    parser.add_argument(
        '--no-pair', action='store_true',
        help='Disable request/response pairing (publish each message '
             'individually)',
    )
    parser.add_argument(
        '--raw', action='store_true',
        help='Publish raw 9-char message lines instead of JSON',
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Print each message as it is processed',
    )
    parser.add_argument(
        '--buffer-size', type=int, default=200,
        metavar='N',
        help='Messages to buffer during MQTT disconnect (default: 200, 0=disable)',
    )
    parser.add_argument(
        '--device-vid', default=OTGW_USB_VID,
        metavar='VID',
        help=f'USB VID to prefer when auto-detecting device (default: {OTGW_USB_VID})',
    )

    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    image_id = args.image_id if args.image_id is not None else read_image_id_file()
    mode = read_mode()
    topic = build_topic(args.topic, image_id, mode)
    if mode == 'test':
        print(f"Mode: TEST — publishing to {topic}")

    listener = OTGWListener(
        device=args.device,
        baud=args.baud,
        host=args.host,
        port=args.port,
        topic=topic,
        verbose=args.verbose,
        raw=args.raw,
        pair=not args.no_pair,
        buffer_size=args.buffer_size,
        device_vid=args.device_vid,
    )

    listener.start()


if __name__ == '__main__':
    main()
