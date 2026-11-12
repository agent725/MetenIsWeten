#!/usr/bin/env python3
"""
DS18B20 Simulator

Publishes synthetic DS18B20 temperature MQTT messages for testing the logging
pipeline without physical sensors.

Simulates two sensors with realistic random-walk temperatures:
- Sensor 1: indoor temperature (~20°C)
- Sensor 2: boiler return temperature (~40°C)

Publishes readings every 60 seconds, matching the real DS18B20 listener.

Usage:
    ds18b20_simulator.py [options]

Examples:
    ds18b20_simulator.py
    ds18b20_simulator.py --interval 30
    ds18b20_simulator.py -v
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


class DS18B20Simulator:
    """Publish synthetic DS18B20 readings to MQTT."""

    # Fake sensor IDs that look like real 1-Wire device IDs
    SENSORS = [
        ('28-sim000000001', 20.0),   # indoor temperature target
        ('28-sim000000002', 40.0),   # boiler return temperature target
    ]

    def __init__(self, host: str, port: int, topic: str,
                 interval: int, verbose: bool):
        self.host = host
        self.port = port
        self.topic = topic
        self.interval = interval
        self.verbose = verbose
        self.client: Optional[mqtt.Client] = None
        self.running = False
        self.count = 0
        # Initialise temperatures at their targets
        self.temps = {sid: target for sid, target in self.SENSORS}

    def _walk(self, value: float, target: float, sigma: float = 0.1) -> float:
        drift = (target - value) * 0.03
        return round(value + drift + random.gauss(0, sigma), 2)

    def on_connect(self, client, userdata, connect_flags, rc, props):
        if rc == 0:
            print(f"Connected to {self.host}:{self.port}")
        else:
            print(f"MQTT connect failed rc={rc}", file=sys.stderr)

    def on_disconnect(self, client, userdata, flags, rc, props):
        if rc != 0:
            print(f"MQTT disconnected rc={rc}", file=sys.stderr)

    def start(self):
        print("DS18B20 Simulator starting...")
        print(f"  Topic:    {self.topic}")
        print(f"  Interval: {self.interval}s")
        print(f"  Sensors:  {[s for s, _ in self.SENSORS]}")
        print()

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id='ds18b20-simulator')
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
            for sensor_id, target in self.SENSORS:
                self.temps[sensor_id] = self._walk(
                    self.temps[sensor_id], target
                )
                payload = json.dumps({
                    'sensor_id': sensor_id,
                    'temperature_c': self.temps[sensor_id],
                })
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
        print(f"Published {self.count} readings total")


def main():
    parser = argparse.ArgumentParser(
        description='DS18B20 simulator — publish synthetic temperature readings'
    )
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=1883)
    parser.add_argument('--topic', default='home/sensors/temperature')
    parser.add_argument('--image-id', default=None)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    image_id = args.image_id or read_image_id_file()
    mode = read_mode()
    topic = build_topic(args.topic, image_id, mode)
    if mode == 'test':
        print(f"Mode: TEST — publishing to {topic}")

    DS18B20Simulator(
        args.host, args.port, topic, args.interval, args.verbose
    ).start()


if __name__ == '__main__':
    main()
