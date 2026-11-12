#!/usr/bin/env python3
"""
OTGW Simulator

Publishes synthetic OpenTherm Gateway MQTT messages for testing the logging
pipeline without a real OTGW device.

Simulates paired T/B messages (thermostat/boiler) with realistic values:
- Status (id 0): flame on/off based on boiler setpoint vs room temp
- Boiler water temp (id 25): random walk around 55°C
- CH setpoint (id 1): random walk around 60°C
- Room temp (id 24): slow random walk around 20°C
- DHW temp (id 26): random walk around 55°C

Publishes one pair of messages every ~4 seconds, matching a real OTGW.

Usage:
    otgw_simulator.py [options]

Examples:
    otgw_simulator.py
    otgw_simulator.py --host 192.168.1.100
    otgw_simulator.py -v
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import argparse
import json
import random
import signal
import sys
import time
from typing import Optional

PI_ID_FILE = '/etc/otgw/pi-id'
MODE_FILE = '/etc/otgw/mode'

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


class OTGWSimulator:
    """Publish synthetic OTGW paired messages to MQTT."""

    INTERVAL = 4.0  # seconds between message pairs

    def __init__(self, host: str, port: int, topic: str, verbose: bool):
        self.host = host
        self.port = port
        self.topic = topic
        self.verbose = verbose
        self.client: Optional[mqtt.Client] = None
        self.running = False
        self.count = 0

        # State variables for random walks
        self.tboiler = 55.0
        self.tset = 60.0
        self.troom = 20.0
        self.tdhw = 55.0
        self.flame = 0

    def _walk(self, value: float, target: float, sigma: float = 0.3,
              lo: float = -999, hi: float = 999) -> float:
        """Small random walk biased back toward target."""
        drift = (target - value) * 0.05
        return max(lo, min(hi, value + drift + random.gauss(0, sigma)))

    def _build_payload(self) -> dict:
        """Build one simulated paired OTGW message payload."""
        self.tboiler = self._walk(self.tboiler, 55.0, sigma=0.5, lo=30, hi=90)
        self.tset = self._walk(self.tset, 60.0, sigma=0.2, lo=20, hi=80)
        self.troom = self._walk(self.troom, 20.0, sigma=0.05, lo=10, hi=30)
        self.tdhw = self._walk(self.tdhw, 55.0, sigma=0.3, lo=30, hi=70)
        # Flame on when boiler setpoint > room temp + margin
        self.flame = 1 if self.tset > self.troom + 2.0 else 0

        return {
            'id': 25,
            'req': 'T00190000',
            'ack': 'B40193480',
            'Tboiler': round(self.tboiler, 2),
            'ms': random.randint(20, 60),
        }

    def _build_status_payload(self) -> dict:
        return {
            'id': 0,
            'req': f'T0000{"01" if self.flame else "00"}00',
            'ack': f'B4000000{"A" if self.flame else "8"}',
            'flame': self.flame,
            'ch': 1,
            'dhw': 0,
            'fault': 0,
            'ms': random.randint(20, 60),
        }

    def on_connect(self, client, userdata, connect_flags, rc, props):
        if rc == 0:
            print(f"Connected to {self.host}:{self.port}")
        else:
            print(f"MQTT connect failed rc={rc}", file=sys.stderr)

    def on_disconnect(self, client, userdata, flags, rc, props):
        if rc != 0:
            print(f"MQTT disconnected rc={rc}", file=sys.stderr)

    def start(self):
        print("OTGW Simulator starting...")
        print(f"  Topic:  {self.topic}")
        print(f"  Interval: {self.INTERVAL}s")
        print()

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id='otgw-simulator')
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
            for payload in [self._build_status_payload(), self._build_payload()]:
                msg = json.dumps(payload, separators=(',', ':'))
                self.client.publish(self.topic, msg)
                self.count += 1
                if self.verbose:
                    print(f"Published: {msg}")
            time.sleep(self.INTERVAL)

        self.client.loop_stop()
        self.client.disconnect()
        print(f"Published {self.count} messages total")


def main():
    parser = argparse.ArgumentParser(
        description='OTGW simulator — publish synthetic OTGW messages to MQTT'
    )
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=1883)
    parser.add_argument('--topic', default='home/energy/otgw')
    parser.add_argument('--image-id', default=None)
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    image_id = args.image_id or read_image_id_file()
    mode = read_mode()
    topic = build_topic(args.topic, image_id, mode)
    if mode == 'test':
        print(f"Mode: TEST — publishing to {topic}")

    OTGWSimulator(args.host, args.port, topic, args.verbose).start()


if __name__ == '__main__':
    main()
