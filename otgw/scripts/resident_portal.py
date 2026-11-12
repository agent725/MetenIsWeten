#!/usr/bin/env python3
"""Resident dashboard — always-on read-only display on port 80 (HTTP) and
port 443 (HTTPS, when a TLS certificate is present).

Shows DS18B20 temperatures, gas reading, and OTGW boiler/heating data.
No configuration controls — for residents, not installers.

The installer portal (full config) runs on port 8080, started on demand
via SSH or hall sensor.

Runs as root (required for ports 80 and 443).

HTTPS: the self-signed TLS certificate is generated at first boot by
provision.sh and stored at /etc/otgw/tls.crt + tls.key. If the files are
absent (e.g. fresh prebake before provision runs) HTTPS is silently skipped
and only HTTP is served.
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import copy
import json
import logging
import socketserver
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

# ── config ────────────────────────────────────────────────────────────────────
PORT = 80
HTTPS_PORT = 443
TLS_CERT = '/etc/otgw/tls.crt'
TLS_KEY  = '/etc/otgw/tls.key'
MQTT_HOST = 'localhost'
MQTT_PORT = 1883
MAX_OTGW_MSGS = 10
DATA_MAX_AGE_S = 300     # flag data as stale after 5 min
RELOAD_INTERVAL_S = 15   # client-side auto-reload interval

PI_ID_FILE = Path('/etc/otgw/pi-id')
CONFIG_ACTIVE = Path('/run/otgw-config-active')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('resident-portal')


# ── MQTT data cache ───────────────────────────────────────────────────────────

class DataCache:
    """Thread-safe cache of latest sensor values from MQTT."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # DS18B20: {sensor_id: {'temp': float, 'ts': float}}
        self.temps: dict[str, dict] = {}
        # Gas: {'reading': float, 'ts': float} or None
        self.gas: Optional[dict] = None
        # OTGW latest decoded scalars with per-key timestamps
        self.otgw: dict = {}
        # Recent OTGW messages: list of {'ts': float, 'summary': str}
        self.otgw_msgs: list[dict] = []

    def update_temp(self, sensor_id: str, temperature: float) -> None:
        with self._lock:
            self.temps[sensor_id] = {'temp': temperature, 'ts': time.time()}

    def update_gas(self, reading: float) -> None:
        with self._lock:
            self.gas = {'reading': reading, 'ts': time.time()}

    def update_otgw(self, payload: dict) -> None:
        now = time.time()
        summary = _summarise_otgw(payload)
        with self._lock:
            for key in ('Tboiler', 'Tset', 'Tr', 'Tdhw', 'Tout',
                        'flame', 'ch', 'dhw'):
                if key in payload:
                    self.otgw[key] = payload[key]
                    self.otgw[f'ts_{key}'] = now
            if summary:
                self.otgw_msgs.append({'ts': now, 'summary': summary})
                if len(self.otgw_msgs) > MAX_OTGW_MSGS:
                    self.otgw_msgs = self.otgw_msgs[-MAX_OTGW_MSGS:]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                'temps': copy.deepcopy(self.temps),
                'gas': dict(self.gas) if self.gas else None,
                'otgw': dict(self.otgw),
                'otgw_msgs': copy.deepcopy(self.otgw_msgs),
                'now': time.time(),
            }


def _summarise_otgw(msg: dict) -> Optional[str]:
    """Build a short human-readable summary of an OTGW message."""
    data_id = msg.get('id')
    if data_id == 0:
        parts = []
        flame = msg.get('flame')
        ch = msg.get('ch')
        if flame is not None:
            parts.append('Brander: ' + ('AAN' if flame else 'UIT'))
        if ch is not None:
            parts.append('CV: ' + ('AAN' if ch else 'UIT'))
        return ', '.join(parts) if parts else None
    pairs = [
        (1,  'Tset',    'CV setpoint: {}°C'),
        (24, 'Tr',      'Kamertemp: {}°C'),
        (25, 'Tboiler', 'Keteltemp: {}°C'),
        (26, 'Tdhw',    'Warmwater: {}°C'),
        (27, 'Tout',    'Buitentemp: {}°C'),
    ]
    for did, key, fmt in pairs:
        if data_id == did:
            val = msg.get(key)
            return fmt.format(val) if val is not None else None
    return None


_cache = DataCache()


# ── MQTT subscriber ───────────────────────────────────────────────────────────

def _read_image_id() -> Optional[str]:
    try:
        v = PI_ID_FILE.read_text().strip()
        return v or None
    except OSError:
        return None


def _start_mqtt() -> None:
    """Start MQTT subscriber in a background daemon thread."""

    def run() -> None:
        image_id = _read_image_id()
        topic = 'home/#'

        while True:
            try:
                import paho.mqtt.client as mqtt

                def on_connect(client, userdata, flags, rc, props):
                    if rc == 0:
                        client.subscribe(topic)
                        log.info('MQTT subscribed to %s', topic)
                    else:
                        log.warning('MQTT connect rc=%d', rc)

                def on_message(client, userdata, msg):
                    try:
                        payload = json.loads(
                            msg.payload.decode('utf-8', errors='replace')
                        )
                    except Exception:
                        return
                    t = msg.topic
                    if '/sensors/temperature' in t:
                        sid = payload.get('sensor_id', 'unknown')
                        temp = payload.get('temperature_c')
                        if temp is not None:
                            _cache.update_temp(sid, float(temp))
                    elif '/energy/gas' in t:
                        reading = (payload.get('current') or {}).get(
                            'value_m3'
                        )
                        if reading is not None:
                            _cache.update_gas(float(reading))
                    elif '/energy/otgw' in t:
                        _cache.update_otgw(payload)

                client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
                    client_id='resident-portal',
                )
                client.on_connect = on_connect
                client.on_message = on_message
                client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                client.loop_forever()
            except Exception as exc:
                log.warning('MQTT error: %s — retrying in 10 s', exc)
                time.sleep(10)

    threading.Thread(target=run, daemon=True).start()


# ── HTML rendering ────────────────────────────────────────────────────────────

def _age_str(ts: float, now: float) -> str:
    age = int(now - ts)
    if age < 60:
        return f'{age}s geleden'
    elif age < 3600:
        return f'{age // 60}m geleden'
    else:
        return f'{age // 3600}u geleden'


_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0 }
body { font-family: system-ui, sans-serif; background: #f2f2f2;
       color: #222; padding: 14px; max-width: 700px; margin: 0 auto }
h1 { font-size: 1.15em; color: #555; margin-bottom: 14px }
.grid { display: grid; gap: 10px;
        grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)) }
.card { background: #fff; border-radius: 10px; padding: 14px;
        box-shadow: 0 1px 4px rgba(0,0,0,.08) }
.card h2 { font-size: .7em; text-transform: uppercase; color: #999;
           letter-spacing: .06em; margin-bottom: 8px }
.val { font-size: 1.65em; font-weight: 600 }
.on  { color: #d94f00 }
.off { color: #bbb }
.age { font-size: .68em; color: #bbb; margin-top: 4px }
.stale { color: #c00; font-size: .75em; margin-left: 5px }
.msgs { margin-top: 10px; background: #fff; border-radius: 10px;
        padding: 14px; box-shadow: 0 1px 4px rgba(0,0,0,.08) }
.msgs h2 { font-size: .7em; text-transform: uppercase; color: #999;
           letter-spacing: .06em; margin-bottom: 8px }
.msgs table { width: 100%; border-collapse: collapse; font-size: .83em }
.msgs td { padding: 4px 6px; border-bottom: 1px solid #f0f0f0 }
.msgs td:first-child { color: #bbb; white-space: nowrap; width: 65px }
footer { margin-top: 12px; text-align: center;
         font-size: .68em; color: #ccc }
"""

_PAGE = """\
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Energie &amp; Verwarming</title>
<style>{css}</style>
</head>
<body>
<h1>Energie &amp; Verwarming</h1>
<div class="grid">
{cards}
</div>
{msgs}
<footer>Bijgewerkt: {updated}</footer>
<script>setTimeout(()=>location.reload(),{reload}000);</script>
</body>
</html>"""


def _stale_tag(ts: float, now: float) -> str:
    return (
        ' <span class="stale">oud</span>'
        if now - ts > DATA_MAX_AGE_S else ''
    )


def _card(title: str, value: str, age_str: str,
          stale: str = '', css: str = '') -> str:
    css_attr = f' class="val {css}"' if css else ' class="val"'
    return (
        f'<div class="card"><h2>{title}{stale}</h2>'
        f'<div{css_attr}>{value}</div>'
        f'<div class="age">{age_str}</div></div>'
    )


def _render(data: dict) -> str:
    now = data['now']
    cards: list[str] = []

    # DS18B20 temperatures — both labeled as pipe (supply or return);
    # we cannot reliably determine which sensor is on which pipe.
    # Skip sensors not seen within 2× DATA_MAX_AGE_S — their IDs stay in the
    # in-memory cache until restart, which would show ghost 0.0°C cards.
    temp_items = [
        (sid, info) for sid, info in sorted(data['temps'].items())
        if now - info['ts'] < DATA_MAX_AGE_S * 2
    ]
    for sid, info in temp_items:
        cards.append(_card(
            'Leiding (aanvoer of retour)',
            f'{info["temp"]:.1f}°C',
            _age_str(info['ts'], now),
            stale=_stale_tag(info['ts'], now),
        ))

    # Gas reading
    if data['gas']:
        g = data['gas']
        cards.append(_card(
            'Gas',
            f'{g["reading"]:.3f} m³',
            _age_str(g['ts'], now),
            stale=_stale_tag(g['ts'], now),
        ))

    # OTGW scalar values
    otgw = data['otgw']

    def _otgw_card(key: str, label: str, fmt: str = '.1f',
                   unit: str = '°C') -> str:
        if key not in otgw:
            return ''
        val = otgw[key]
        ts = otgw.get(f'ts_{key}', now)
        return _card(
            label,
            f'{val:{fmt}}{unit}',
            _age_str(ts, now),
            stale=_stale_tag(ts, now),
        )

    # Flame status card
    if 'flame' in otgw:
        ts = otgw.get('ts_flame', now)
        flame = otgw['flame']
        cards.append(_card(
            'Brander',
            'AAN 🔥' if flame else 'UIT',
            _age_str(ts, now),
            stale=_stale_tag(ts, now),
            css='on' if flame else 'off',
        ))

    cards.append(_otgw_card('Tr',      'Kamertemp'))
    cards.append(_otgw_card('Tset',    'CV setpoint'))
    cards.append(_otgw_card('Tboiler', 'Keteltemp'))
    cards.append(_otgw_card('Tdhw',    'Warmwater'))

    cards_html = '\n'.join(c for c in cards if c)

    # Empty-state placeholder when no data has arrived yet (e.g. startup
    # race or sensors not yet connected)
    if not cards_html:
        cards_html = (
            '<div style="grid-column:1/-1;text-align:center;'
            'color:#bbb;padding:24px 0;font-size:.9em">'
            'Wachten op sensordata\u2026</div>'
        )

    # Recent OTGW messages table
    msgs_html = ''
    if data['otgw_msgs']:
        rows = ''.join(
            f'<tr><td>{time.strftime("%H:%M:%S", time.localtime(m["ts"]))}</td>'
            f'<td>{m["summary"]}</td></tr>'
            for m in reversed(data['otgw_msgs'])
        )
        msgs_html = (
            '<div class="msgs"><h2>Recente berichten</h2>'
            f'<table><tbody>{rows}</tbody></table></div>'
        )

    return _PAGE.format(
        css=_CSS,
        cards=cards_html,
        msgs=msgs_html,
        updated=time.strftime('%H:%M:%S'),
        reload=RELOAD_INTERVAL_S,
    )


# ── HTTP handler ──────────────────────────────────────────────────────────────

class ResidentHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args) -> None:
        pass  # suppress per-request access log

    def do_GET(self) -> None:
        # Redirect to installer portal when config AP is active.
        # Use the Host header so the redirect works regardless of which IP
        # the client used (AP gateway 10.42.0.1, eth0, wlan0, or mDNS name).
        if CONFIG_ACTIVE.exists():
            host = (self.headers.get('Host') or '10.42.0.1').split(':')[0]
            new_url = f'http://{host}:8080{self.path}'
            self.send_response(302)
            self.send_header('Location', new_url)
            self.end_headers()
            return
        
        if self.path in ('/', '/index.html'):
            body = _render(_cache.snapshot()).encode()
            self._send(200, 'text/html; charset=utf-8', body)
        elif self.path == '/data.json':
            body = json.dumps(_cache.snapshot(), default=str).encode()
            self._send(200, 'application/json', body)
        else:
            self._send(404, 'text/plain', b'Not found')

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── main ─────────────────────────────────────────────────────────────────────

def _start_https(handler_class: type) -> None:
    """Start HTTPS server on port 443 in a daemon thread.

    Uses the self-signed certificate generated at first boot by provision.sh
    (/etc/otgw/tls.crt + tls.key). If the files are absent, logs a notice
    and returns without starting HTTPS — HTTP on port 80 remains available.
    """
    cert = Path(TLS_CERT)
    key  = Path(TLS_KEY)
    if not cert.exists() or not key.exists():
        log.info(
            'TLS cert not found (%s) — HTTPS disabled; HTTP only on port %d',
            TLS_CERT, PORT,
        )
        return

    class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        https_server = ThreadedHTTPServer(('', HTTPS_PORT), handler_class)
        https_server.socket = ctx.wrap_socket(
            https_server.socket, server_side=True
        )
        threading.Thread(
            target=https_server.serve_forever,
            daemon=True,
            name='https-server',
        ).start()
        log.info('resident-portal HTTPS started on port %d', HTTPS_PORT)
    except Exception as exc:
        log.warning('HTTPS setup failed (%s) — HTTP only on port %d', exc, PORT)


def main() -> None:
    _start_mqtt()

    class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True

    _start_https(ResidentHandler)

    server = ThreadedHTTPServer(('', PORT), ResidentHandler)
    log.info('resident-portal starting on port %d (HTTP)', PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
