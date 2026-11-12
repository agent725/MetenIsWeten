#!/usr/bin/env python3
"""
P1 Gas Meter Simulator

Publishes synthetic P1 gas meter MQTT messages for testing the logging
pipeline without a real P1 meter dongle.

Simulates a slowly incrementing gas counter:
- Starts from persisted value in /etc/otgw/sim-p1-reading (or 0.0)
- Increments by ~0.01 m³ every 60 seconds (~14 m³/day — realistic for a
  home heating system in winter)
- Persists the current reading to survive restarts

Publishes every 60 seconds, matching the real P1 dongle MQTT interval.

Usage:
    p1_simulator.py [options]

Examples:
    p1_simulator.py
    p1_simulator.py --interval 30
    p1_simulator.py --start 1234.567
    p1_simulator.py -v
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import argparse
import json
import os
import random
import signal
import sys
import time
from typing import Optional

PI_ID_FILE = '/etc/otgw/pi-id'
MODE_FILE = '/etc/otgw/mode'
SIM_STATE_FILE = '/etc/otgw/sim-p1-reading'

# Increment per interval: ~0.01 m³/min → ~14.4 m³/day
INCREMENT_PER_INTERVAL = 0.01

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Error: paho-mqtt not installed", file=sys.stderr)
    sys.exit(1)


def read_image_id_file() -> Optional[str]:
    try:
        with open(PI_ID_FILE) as f:
            v = f.read().strip()
            return v if v else None
    except FileNotFoundError:
        return None


def read_mode() -> str:
    try:
        with open(MODE_FILE) as f:
            v = f.read().strip().lower()
            return v if v in ('test', 'production') else 'production'
    except FileNotFoundError:
        return 'production'


def build_topic(base_topic: str, image_id: Optional[str],
                mode: str = 'production') -> str:
    if mode == 'test':
        parts = base_topic.split('/', 1)
        base_topic = f"test/{parts[1]}" if len(parts) == 2 else f"test/{base_topic}"
    if not image_id:
        return base_topic
    parts = base_topic.split('/', 1)
    if len(parts) == 2:
        return f"{parts[0]}/{image_id}/{parts[1]}"
    return f"{image_id}/{base_topic}"


def load_reading(state_file: str, default: float) -> float:
    try:
        with open(state_file) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return default


def save_reading(state_file: str, reading: float) -> None:
    try:
        tmp = state_file + '.tmp'
        with open(tmp, 'w') as f:
            f.write(f"{reading:.3f}\n")
        os.replace(tmp, state_file)
    except OSError:
        pass


class P1Simulator:
    """Publish synthetic P1 gas meter readings to MQTT."""

    def __init__(self, host: str, port: int, topic: str,
                 interval: int, start: Optional[float],
                 state_file: str, verbose: bool):
        self.host = host
        self.port = port
        self.topic = topic
        self.interval = interval
        self.state_file = state_file
        self.verbose = verbose
        self.client: Optional[mqtt.Client] = None
        self.running = False
        self.count = 0
        initial = start if start is not None else load_reading(state_file, 0.0)
        self.reading = initial

    def on_connect(self, client, userdata, connect_flags, rc, props):
        if rc == 0:
            print(f"Connected to {self.host}:{self.port}")
        else:
            print(f"MQTT connect failed rc={rc}", file=sys.stderr)

    def on_disconnect(self, client, userdata, flags, rc, props):
        if rc != 0:
            print(f"MQTT disconnected rc={rc}", file=sys.stderr)

    def start(self):
        print("P1 Gas Meter Simulator starting...")
        print(f"  Topic:    {self.topic}")
        print(f"  Interval: {self.interval}s")
        print(f"  Reading:  {self.reading:.3f} m³")
        print()

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id='p1-simulator')
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect

        try:
            self.client.connect(self.host, self.port, keepalive=60)
        except Exception as e:
            print(f"Failed to connect: {e}", file=sys.stderr)
            sys.exit(1)

        self.client.loop_start()
        self.running = True

        def _stop(sig, frame):
            self.running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        while self.running:
            # Increment reading with small jitter
            self.reading += INCREMENT_PER_INTERVAL * (
                1 + random.gauss(0, 0.1)
            )
            self.reading = round(self.reading, 3)
            save_reading(self.state_file, self.reading)

            payload = json.dumps({'reading': self.reading})
            self.client.publish(self.topic, payload)
            self.count += 1
            if self.verbose:
                print(f"Published: {payload}")

            for _ in range(self.interval):
                if not self.running:
                    break
                time.sleep(1)

        self.client.loop_stop()
        self.client.disconnect()
        print(f"Published {self.count} readings. Final: {self.reading:.3f} m³")


def main():
    parser = argparse.ArgumentParser(
        description='P1 gas meter simulator — publish synthetic readings to MQTT'
    )
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=1883)
    parser.add_argument('--topic', default='home/energy/gas')
    parser.add_argument('--image-id', default=None)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument(
        '--start', type=float, default=None,
        help='Starting gas reading in m³ (default: restore from state file or 0)'
    )
    parser.add_argument(
        '--state-file', default=SIM_STATE_FILE,
        help=f'File to persist reading across restarts (default: {SIM_STATE_FILE})'
    )
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    image_id = args.image_id or read_image_id_file()
    mode = read_mode()
    topic = build_topic(args.topic, image_id, mode)
    if mode == 'test':
        print(f"Mode: TEST — publishing to {topic}")

    P1Simulator(
        args.host, args.port, topic, args.interval,
        args.start, args.state_file, args.verbose
    ).start()


if __name__ == '__main__':
    main()
