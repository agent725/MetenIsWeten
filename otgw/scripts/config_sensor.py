#!/usr/bin/env python3
"""Config sensor service — hall effect sensor on GPIO 4.

Hardware:
  Digital hall effect sensor — U58 / US5881 / OH1881 (omnipolar, TO-92).
  The sensor output is LOW when a magnet is held within range, HIGH when
  removed — identical to a momentary pushbutton from the code's perspective.
  Omnipolar: works with either pole of the magnet.

  Pull-up is required — lgpio.SET_PULL_UP is used.

  Wiring:
    Pin 1  (3.3 V)  → VCC
    Pin 6  (GND)    → GND
    Pin 7  (GPIO 4) → OUT

Hold / tap behaviour:
  < 100 ms      → Ignored (accidental / debounce)
  ≥ 100 ms hold → Immediately opens config AP + triggers log flush in background.
                   ACT double-blinks; PWR red blinks.
                   WiFi SSH opens with the AP and closes when the AP exits.
                   Flush is rate-limited to one per 10 s.
  Triple-tap    → 3 tap completions (magnet on then off = 1 tap) within an 8 s
                   rolling window AND first-to-last tap span ≥ 5 s triggers a
                   flush + sync (waits to complete) then safe shutdown.
                   This prevents accidental rapid tapping from causing shutdown.

Note on WPS: WPS push-button pairing was considered but ultimately not used.
The P1 dongle has no WPS functionality and adding WPS key exchange to the
dongle firmware is complex. Instead, WiFi credentials are entered via the
installer web portal (the dongle is auto-configured over USB).

Config AP mode — dual-mode (Pi 3B+ / Pi 4, brcmfmac driver):
  Creates a virtual uap0 interface for the open AP while wlan0 stays
  connected to home WiFi.  The installer can configure credentials and
  immediately verify the connection without losing portal access.
  SSID: otgw-<device_id>  (open, no password)

Config AP mode — single-mode fallback (if uap0 not supported):
  wlan0 is used directly as AP; home WiFi is disconnected during the
  config session.

Auto-exit (config AP):
  • 2 min after last activation or last portal HTTP interaction, whichever is later
  • Any page load in the portal resets the 2-minute clock

ACT LED patterns (green) + PWR LED (red):
  Idle                ACT heartbeat (kernel default); PWR default-on
  Magnet held < 5 s   ACT double-blink cue; PWR 200 ms blink
  Magnet held ≥ 5 s   ACT + PWR 100 ms blink (double-speed, shutdown warning)
  Config AP active    ACT double-blink repeating
  Flush triggered     ACT rapid blink, PWR rapid blink (during flush)
  Success             ACT solid 3 s → heartbeat; PWR default-on
  Failure             ACT 10 × rapid blink → heartbeat
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import threading
from pathlib import Path

# ── config ────────────────────────────────────────────────────────────────────
GPIO_SENSOR           = 4            # BCM GPIO; pin 7 on 40-pin header
DEBOUNCE_S            = 0.05         # polling interval (seconds)

# Ping watchdog every _WD_PING_EVERY iterations; DEBOUNCE_S × count ≈ 30 s.
# Counter-based (no time.monotonic) to keep the tight poll loop side-effect-free.
_WATCHDOG_PING_INTERVAL = 30  # seconds (informational — see _WD_PING_EVERY)


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
PORTAL_MIN_S          = 0.1          # act at 100 ms during hold (not on release)
FLUSH_RATE_LIMIT_S    = 10.0         # min seconds between flush triggers
SHUTDOWN_TAPS         = 3            # tap completions needed for shutdown
SHUTDOWN_WINDOW_S     = 8.0          # rolling window for tap counting
SHUTDOWN_MIN_SPAN_S   = 5.0          # first-to-last tap must span this long

CONFIG_AP_CON     = 'otgw-config-ap'   # NM connection name
VIRTUAL_IFACE     = 'uap0'             # Virtual AP interface (dual-mode)

AP_INACTIVITY_S       = 3600  # Exit 1 hour after last activation or portal interaction
FIRST_BOOT_AP_DELAY_S = 60    # Seconds after boot before auto-opening AP (no wifi.conf)

# ── paths ────────────────────────────────────────────────────────────────────
CONFIG_ACTIVE     = Path('/run/otgw-config-active')
WIFI_CONF         = Path('/etc/otgw/wifi.conf')
PI_ID_FILE        = Path('/etc/otgw/pi-id')
DEVICE_ID_FILE    = Path('/etc/otgw/device-id')
DNS_HIJACK_CONF   = Path(
    '/etc/NetworkManager/dnsmasq-shared.d/captive-portal.conf'
)
AP_TOGGLE_FLAG        = Path('/run/otgw-ap-toggle-request')
ACTIVITY_FILE         = Path('/run/otgw-portal-activity')
CONFIG_AP_PW_FILE     = Path('/etc/otgw/config-ap-pw')  # presence = pw-protected

# ── runtime state ─────────────────────────────────────────────────────────────
_ap_ifname: str | None = None
_last_toggle_time: float = 0.0
_toggle_lock = threading.Lock()
TOGGLE_COOLDOWN_S = 5.0   # minimum seconds between AP toggles
_wlan0_forced_band_conn: str | None = None  # NM conn modified to lock 2.4 GHz

_last_flush_time: float = 0.0
_flush_lock = threading.Lock()
_tap_times: list[float] = []


# ── Image ID / Device ID ──────────────────────────────────────────────────────
def _get_image_id() -> str:
    try:
        return PI_ID_FILE.read_text().strip()
    except OSError:
        return 'xxxxxx'


def _get_device_id() -> str:
    try:
        return DEVICE_ID_FILE.read_text().strip()
    except OSError:
        return 'xxxxxx'


def config_ap_ssid() -> str:
    return f'otgw-{_get_device_id()}'


# ── MQTT event publishing ─────────────────────────────────────────────────────

MQTT_BROKER   = 'localhost'
MQTT_PORT     = 1883
MQTT_TOPIC    = 'home/{image_id}/system/sensor'


def _publish_event(event: str, **extra) -> None:
    """Publish a hall-sensor event to MQTT.

    Fire-and-forget: runs in a daemon thread so the sensor loop is never
    blocked.  Failures are logged as warnings and silently dropped.

    Payload (JSON):
        event      — event name (see below)
        image_id   — Pi identifier
        + any keyword args passed in as extra fields (e.g. held_s, reason)

    Events published:
        portal_open         — config AP opened; held_s = magnet hold duration
        portal_timer_reset  — re-trigger while AP active; timer restarted
        portal_closed       — config AP closed; reason = 'inactivity'|'button'
        flush_started       — log flush triggered (≥100 ms hold)
        shutdown_triggered  — shutdown triggered by triple-tap
    """
    def _do_publish() -> None:
        try:
            import paho.mqtt.client as mqtt  # type: ignore[import]
        except ImportError:
            return   # paho not installed — skip silently

        image_id = _get_image_id()
        topic    = MQTT_TOPIC.format(image_id=image_id)
        payload  = json.dumps({'event': event, 'image_id': image_id, **extra})
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=10)
            # loop_start() is required: without it paho's network loop never
            # runs and QoS=0 packets are never flushed to the socket before
            # disconnect() closes it, causing silent message loss.
            client.loop_start()
            result = client.publish(topic, payload, qos=0)
            result.wait_for_publish(timeout=5)
            client.loop_stop()
            client.disconnect()
            log.debug('MQTT %s → %s', topic, payload)
        except Exception as exc:
            log.warning('MQTT publish failed (%s): %s', event, exc)

    threading.Thread(target=_do_publish, daemon=True, name=f'mqtt-{event}').start()


# ── ACT LED ──────────────────────────────────────────────────────────────────
def _led_path() -> Path | None:
    for name in ('ACT', 'led0'):
        p = Path(f'/sys/class/leds/{name}')
        if p.exists():
            return p
    return None


ACT = _led_path()


def _pwr_led_path() -> Path | None:
    for name in ('PWR', 'led1'):
        p = Path(f'/sys/class/leds/{name}')
        if p.exists():
            return p
    return None


PWR = _pwr_led_path()


def _pwr_write(filename: str, value: str) -> None:
    if PWR is None:
        return
    try:
        (PWR / filename).write_text(value)
    except OSError:
        pass


def pwr_led_rapid_blink() -> None:
    """200 ms cycle blink — magnet held / flush active."""
    log.debug('pwr_led_rapid_blink() called - PWR=%s', PWR)
    _pwr_write('trigger', 'timer')
    _pwr_write('delay_on',  '100')
    _pwr_write('delay_off', '100')


def pwr_led_ultra_rapid_blink() -> None:
    """100 ms cycle blink — shutdown sequence active."""
    _pwr_write('trigger', 'timer')
    _pwr_write('delay_on',  '50')
    _pwr_write('delay_off', '50')


def pwr_led_default() -> None:
    """Restore PWR LED to always-on (power indicator)."""
    _pwr_write('trigger', 'default-on')


_led_warned: bool = False


def _led_write(filename: str, value: str) -> None:
    global _led_warned
    if ACT is None:
        return
    try:
        (ACT / filename).write_text(value)
    except OSError as e:
        if not _led_warned:
            _led_warned = True
            log.warning(
                'LED write failed (%s/%s): %s — is 99-act-led.rules '
                'missing from this image?', ACT.name, filename, e,
            )


def led_heartbeat() -> None:
    _led_write('trigger', 'heartbeat')


def led_rapid_blink() -> None:
    """100 ms cycle blink on ACT."""
    _led_write('trigger', 'timer')
    _led_write('delay_on',  '50')
    _led_write('delay_off', '50')


def led_ultra_rapid_blink() -> None:
    """50 ms cycle blink on ACT — shutdown warning (≥ 5 s hold)."""
    _led_write('trigger', 'timer')
    _led_write('delay_on',  '25')
    _led_write('delay_off', '25')


def _led_double_blink_manual_loop() -> None:
    """Manual double-blink loop for kernels without the pattern trigger.

    Runs in a daemon thread while CONFIG_ACTIVE exists.  Exits cleanly when the
    portal closes (CONFIG_ACTIVE is unlinked before exit_config_ap() sleeps its
    grace period, so the thread is done before led_solid_then_heartbeat runs).
    """
    while CONFIG_ACTIVE.exists():
        _led_write('brightness', '1')
        time.sleep(0.2)
        _led_write('brightness', '0')
        time.sleep(0.2)
        _led_write('brightness', '1')
        time.sleep(0.2)
        _led_write('brightness', '0')
        time.sleep(0.8)


def led_double_blink(loop: bool = False) -> None:
    """Double-blink ACT LED.

    loop=True  — repeat indefinitely while the portal is active.
                 Uses pattern trigger with repeat=-1 (Bookworm+), or a
                 manual brightness loop thread on older kernels.
    loop=False — one-shot confirmation blink (default).
    """
    log.debug('led_double_blink(loop=%s) called - ACT=%s', loop, ACT)
    try:
        if ACT and 'pattern' in (ACT / 'trigger').read_text():
            log.debug('Using pattern trigger for double-blink')
            _led_write('trigger', 'pattern')
            _led_write('pattern', '255 200 0 200 255 200 0 800')
            if loop:
                _led_write('repeat', '-1')
            return
    except OSError as e:
        log.warning('Pattern trigger failed: %s', e)

    if loop:
        log.debug('pattern trigger unavailable — using manual brightness loop')
        _led_write('trigger', 'none')
        threading.Thread(target=_led_double_blink_manual_loop,
                         daemon=True, name='led-double-blink').start()
    else:
        log.debug('Using timer fallback for one-shot double-blink')
        _led_write('trigger', 'timer')
        _led_write('delay_on',  '200')
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
log = logging.getLogger('config-sensor')


# ── helpers ──────────────────────────────────────────────────────────────────

def run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning('Command timed out (%ds): %s', timeout, cmd)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout='', stderr='')


# ── virtual interface (dual-mode concurrent STA+AP) ──────────────────────────

def _virtual_iface_exists() -> bool:
    return Path(f'/sys/class/net/{VIRTUAL_IFACE}').exists()


def _get_wlan0_channel() -> tuple[str, int] | None:
    """Return (nmcli_band, channel_number) of the active wlan0 connection.

    nmcli_band is 'a' (5 GHz) or 'bg' (2.4 GHz).  Returns None if wlan0 is
    not connected or the channel cannot be determined.

    The brcmfmac driver supports at most one channel across all concurrent
    interfaces (#channels <= 1 in the phy0 interface combination).  The config
    AP always uses 2.4 GHz; this function tells us which 2.4 GHz channel
    wlan0 settled on after _force_wlan0_2ghz() locked it to band=bg.
    """
    r = run(['/sbin/iw', 'dev', 'wlan0', 'info'])
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        m = re.search(r'channel\s+(\d+)\s+\((\d+)\s*MHz\)', line)
        if m:
            channel = int(m.group(1))
            freq_mhz = int(m.group(2))
            band = 'a' if freq_mhz >= 5000 else 'bg'
            return band, channel
    return None


def _create_virtual_iface() -> bool:
    """Create uap0 on top of wlan0 for concurrent STA+AP.

    Requires brcmfmac driver (Pi 3B+, Pi 4, Pi Zero 2W).  Both wlan0 and
    uap0 share the same radio and MUST be on the same channel — the AP NM
    profile is created with the wlan0 channel explicitly to satisfy the
    driver's #channels <= 1 constraint.

    Returns True if the interface is ready.
    """
    if _virtual_iface_exists():
        return True
    # Use 'iw phy phy0 interface add' (not 'iw dev wlan0 interface add').
    # The phy-based form works even while wlan0 is scanning or connecting;
    # the dev-based form returns EBUSY in those states.
    r = run(['iw', 'phy', 'phy0', 'interface', 'add', VIRTUAL_IFACE, 'type', '__ap'])
    if r.returncode != 0:
        log.warning(
            'Cannot create %s (dual-mode not supported): %s',
            VIRTUAL_IFACE, r.stderr.strip(),
        )
        return False
    time.sleep(0.5)
    return _virtual_iface_exists()


def _destroy_virtual_iface() -> None:
    if _virtual_iface_exists():
        run(['iw', 'dev', VIRTUAL_IFACE, 'del'])


# ── config AP firewall isolation ──────────────────────────────────────────────

def _apply_ap_firewall(ifname: str) -> None:
    """Isolate the config AP interface with iptables rules.

    Applied at AP start; removed by _remove_ap_firewall() at AP exit.

    Blocks:
      - All IP forwarding from/to the AP interface.  NetworkManager's
        ipv4.method shared enables ip_forward and inserts a MASQUERADE rule,
        which would otherwise let AP clients route through the Pi to eth0 and
        the home LAN.  The FORWARD DROP rules prevent this regardless of the
        nat table.
      - All INPUT on the AP interface except ports needed for the portals:
          80/tcp   — HTTP resident dashboard (captive-portal popup)
          8080/tcp — HTTP installer portal (config; started on demand)
          53/tcp  — DNS (captive-portal popup on iOS/Android)
          53/udp  — DNS
          67/udp  — DHCP (NM shared-mode dnsmasq)
        This blocks SSH (22), MQTT (1883), and every other service that
        binds to 0.0.0.0 on the Pi.

    Note: the existing `wlan+` SSH-drop rule covers wlan0/wlan1/… but NOT
    uap0 (the virtual AP interface whose name starts with 'uap'). These rules
    fill that gap unconditionally for any AP interface name.

    REJECT vs DROP: the catch-all uses REJECT (TCP RST / ICMP unreachable)
    rather than DROP.  iOS sends HTTPS probes (port 443) alongside its HTTP
    captive-portal probe; a silent DROP causes a ~30-second TCP timeout that
    blocks the captive-portal popup from appearing.  REJECT lets iOS fail
    fast and proceed to show the portal immediately.
    """
    # Block all forwarding from/to the AP — prevents AP clients from routing
    # through the Pi to eth0 or the home network.
    run(['iptables', '-I', 'FORWARD', '-i', ifname, '-j', 'DROP'])
    run(['iptables', '-I', 'FORWARD', '-o', ifname, '-j', 'DROP'])

    # Allow only the services the captive portal needs on the AP interface.
    # Rules are inserted at position 1 (top) so they are evaluated before any
    # existing chain rules; the catch-all REJECT is appended after them.
    run(['iptables', '-I', 'INPUT', '-i', ifname,
         '-p', 'udp', '--dport', '67', '-j', 'ACCEPT'])
    run(['iptables', '-I', 'INPUT', '-i', ifname,
         '-p', 'udp', '--dport', '53', '-j', 'ACCEPT'])
    run(['iptables', '-I', 'INPUT', '-i', ifname,
         '-p', 'tcp', '--dport', '53', '-j', 'ACCEPT'])
    run(['iptables', '-I', 'INPUT', '-i', ifname,
         '-p', 'tcp', '--dport', '80', '-j', 'ACCEPT'])
    run(['iptables', '-I', 'INPUT', '-i', ifname,
         '-p', 'tcp', '--dport', '8080', '-j', 'ACCEPT'])
    # Catch-all REJECT: TCP gets RST (instant), UDP/other gets ICMP unreachable.
    # Do NOT use DROP here — silent drops cause iOS HTTPS probes to hang for
    # ~30 seconds, delaying the captive-portal popup.
    run(['iptables', '-A', 'INPUT', '-i', ifname,
         '-p', 'tcp', '-j', 'REJECT', '--reject-with', 'tcp-reset'])
    run(['iptables', '-A', 'INPUT', '-i', ifname, '-j', 'REJECT'])
    log.info('AP firewall applied on %s (ports 80, 8080, 53, 67)', ifname)


def _remove_ap_firewall(ifname: str) -> None:
    """Remove the iptables rules added by _apply_ap_firewall."""
    cmds = [
        ['iptables', '-D', 'FORWARD', '-i', ifname, '-j', 'DROP'],
        ['iptables', '-D', 'FORWARD', '-o', ifname, '-j', 'DROP'],
        ['iptables', '-D', 'INPUT', '-i', ifname,
         '-p', 'udp', '--dport', '67', '-j', 'ACCEPT'],
        ['iptables', '-D', 'INPUT', '-i', ifname,
         '-p', 'udp', '--dport', '53', '-j', 'ACCEPT'],
        ['iptables', '-D', 'INPUT', '-i', ifname,
         '-p', 'tcp', '--dport', '53', '-j', 'ACCEPT'],
        ['iptables', '-D', 'INPUT', '-i', ifname,
         '-p', 'tcp', '--dport', '80', '-j', 'ACCEPT'],
        ['iptables', '-D', 'INPUT', '-i', ifname,
         '-p', 'tcp', '--dport', '8080', '-j', 'ACCEPT'],
        ['iptables', '-D', 'INPUT', '-i', ifname,
         '-p', 'tcp', '-j', 'REJECT', '--reject-with', 'tcp-reset'],
        ['iptables', '-D', 'INPUT', '-i', ifname, '-j', 'REJECT'],
    ]
    for cmd in cmds:
        run(cmd)   # returncode ignored — rule may not exist if AP start failed
    log.info('AP firewall removed for %s', ifname)


# ── WiFi SSH gate (temporary open during portal session) ─────────────────────

def _wifi_ssh_open() -> None:
    """Temporarily allow new SSH connections on wlan* for the portal session.

    Inserts an ACCEPT rule in front of the persistent DROP rule that
    provision.sh installs on wlan+ port 22 (--ctstate NEW).  The ACCEPT
    is removed again by _wifi_ssh_close() when the portal exits.

    Existing SSH sessions are never affected (they are ESTABLISHED, not NEW).
    """
    run(['iptables', '-I', 'INPUT', '1', '-i', 'wlan+', '-p', 'tcp',
         '--dport', '22', '-m', 'conntrack', '--ctstate', 'NEW', '-j', 'ACCEPT'])
    log.info('WiFi SSH opened for portal session')


def _wifi_ssh_close() -> None:
    """Re-block new SSH connections on wlan* after the portal session ends."""
    run(['iptables', '-D', 'INPUT', '-i', 'wlan+', '-p', 'tcp',
         '--dport', '22', '-m', 'conntrack', '--ctstate', 'NEW', '-j', 'ACCEPT'])
    log.info('WiFi SSH closed')


# ── config AP (open WiFi for installer) ──────────────────────────────────────

def _force_wlan0_2ghz() -> str | None:
    """Lock wlan0's active NM connection to 2.4 GHz (band=bg).

    Why: brcmfmac allows only one channel across all concurrent interfaces
    (#channels <= 1).  If the home router uses band steering, it repeatedly
    pushes wlan0 back to 5 GHz while uap0 holds the radio on a 2.4 GHz
    channel — causing wlan0 to disconnect/reconnect every few seconds,
    which disrupts the shared radio and drops clients from the config AP.
    Locking wlan0 to band=bg prevents the router from steering it away.

    Returns the NM connection name modified (for later restoration by
    _restore_wlan0_band), or None if wlan0 has no active connection.
    """
    global _wlan0_forced_band_conn
    r = run(['nmcli', '-t', '-f', 'NAME,DEVICE,TYPE', 'con', 'show', '--active'])
    conn_name = None
    for line in r.stdout.splitlines():
        parts = line.split(':')
        if (len(parts) >= 3
                and parts[1] == 'wlan0'
                and parts[2] == '802-11-wireless'):
            conn_name = parts[0]
            break
    if conn_name is None:
        log.info('No active wlan0 connection — skipping band=bg lock')
        return None

    log.info(
        'Locking wlan0 to 2.4 GHz (band=bg) to prevent band steering: %s',
        conn_name,
    )
    r = run(['nmcli', 'con', 'modify', conn_name, '802-11-wireless.band', 'bg'])
    if r.returncode != 0:
        log.warning(
            'Could not set band=bg on %s: %s', conn_name, r.stderr.strip(),
        )
        return None

    # Bounce wlan0 so the band restriction takes effect before we read the
    # channel and create the AP profile on it.
    run(['nmcli', 'dev', 'disconnect', 'wlan0'])
    time.sleep(1)
    run(['nmcli', 'con', 'up', conn_name], timeout=20)
    # Wait up to 8 s for wlan0 to associate on a 2.4 GHz channel.
    for _ in range(8):
        if _get_wlan0_channel() is not None:
            break
        time.sleep(1)

    _wlan0_forced_band_conn = conn_name
    log.info('wlan0 locked to 2.4 GHz: %s', conn_name)
    return conn_name


def _restore_wlan0_band() -> None:
    """Undo the band=bg lock set by _force_wlan0_2ghz."""
    global _wlan0_forced_band_conn
    conn_name = _wlan0_forced_band_conn
    if conn_name is None:
        return
    _wlan0_forced_band_conn = None
    log.info('Restoring wlan0 band setting on %s', conn_name)
    run(['nmcli', 'con', 'modify', conn_name, '802-11-wireless.band', ''])
    # Bounce wlan0 so it can roam back to 5 GHz if the router prefers it.
    run(['nmcli', 'dev', 'disconnect', 'wlan0'])
    time.sleep(0.5)
    run(['nmcli', 'con', 'up', conn_name], timeout=20)


def _ensure_config_ap_profile(ifname: str) -> bool:
    """Create a fresh NM profile for the config AP bound to ifname.

    Always recreates to guarantee no leftover security settings.
    When CONFIG_AP_PW_FILE exists the AP uses WPA2-PSK with the derived
    hardware password; otherwise it is an open AP (no password).

    Channel matching: the profile is set to the same band and channel as
    wlan0 so the brcmfmac driver's #channels<=1 constraint is satisfied.
    Without this, the driver lets clients associate to uap0 but marks them
    as 'authorized: no' and blocks all data traffic.
    """
    run(['nmcli', 'con', 'delete', CONFIG_AP_CON])  # ignore errors

    # Always use 2.4 GHz for the config AP: better range, no DFS radar scan,
    # and no band-steering fights.  Match the exact channel wlan0 settled on
    # (the driver requires both interfaces on the same channel).  If wlan0
    # isn't connected, fall back to ch 6 — universally available on 2.4 GHz.
    ch_info = _get_wlan0_channel()
    if ch_info is not None and ch_info[0] == 'bg':
        band, channel = ch_info
        log.info('AP profile: band=bg channel=%d (matched to wlan0)', channel)
    else:
        # wlan0 unknown, disconnected, or on 5 GHz — use 2.4 GHz ch 6
        band, channel = 'bg', 6
        if ch_info is not None:
            log.info(
                'wlan0 on 5 GHz (ch %d) — AP forced to 2.4 GHz ch 6',
                ch_info[1],
            )
        else:
            log.info('wlan0 channel unknown — AP defaulting to 2.4 GHz ch 6')
    extra: list[str] = [
        '802-11-wireless.band', band,
        '802-11-wireless.channel', str(channel),
    ]

    if CONFIG_AP_PW_FILE.exists():
        from ap_password import derive_ap_password
        pw = derive_ap_password()
        extra += [
            'wifi-sec.key-mgmt', 'wpa-psk',
            'wifi-sec.psk', pw,
        ]
        log.info('Config AP profile: WPA2-PSK enabled')
    else:
        log.info('Config AP profile: open (no password)')

    r = run([
        'nmcli', 'con', 'add',
        'type', 'wifi',
        'ifname', ifname,
        'con-name', CONFIG_AP_CON,
        'ssid', config_ap_ssid(),
        'wifi.mode', 'ap',
        'ipv4.method', 'shared',
        'connection.autoconnect', 'no',
    ] + extra)
    if r.returncode != 0:
        log.error('Failed to create config AP NM profile: %s', r.stderr.strip())
        return False
    return True


def _get_ap_client_count(ifname: str) -> int:
    """Count stations associated with the AP interface via iw."""
    r = run(['iw', 'dev', ifname, 'station', 'dump'])
    if r.returncode != 0:
        return 0
    return sum(1 for line in r.stdout.splitlines() if line.startswith('Station'))


def enter_config_ap() -> bool:
    """Start the config AP.

    Tries dual-mode (uap0 + wlan0-as-client) first.
    Falls back to single-mode (wlan0-as-AP, home WiFi suspended).
    """
    global _ap_ifname

    # Clean up any stale uap0 left from a previous session so NM
    # starts from a known-good state.
    if _virtual_iface_exists():
        log.info('Removing stale %s before starting AP', VIRTUAL_IFACE)
        _destroy_virtual_iface()
        time.sleep(2)

    # If wifi-switch has put wlan0 into AP mode (otgw-ap), bring it down first.
    # iw dev wlan0 interface add uap0 fails with EBUSY if wlan0 is already an AP.
    r = run(['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show', '--active'])
    for line in r.stdout.splitlines():
        parts = line.split(':')
        if len(parts) >= 2 and parts[0] == 'otgw-ap' and parts[1] == '802-11-wireless':
            log.info('wifi-switch AP (otgw-ap) is active on wlan0 — bringing it down for dual-mode')
            run(['nmcli', 'con', 'down', 'otgw-ap'])
            time.sleep(2)
            break

    if _create_virtual_iface():
        ifname = VIRTUAL_IFACE
        run(['nmcli', 'dev', 'set', ifname, 'managed', 'yes'])
        # Wait for wlan0 to (re)connect to home WiFi after otgw-ap teardown,
        # then lock it to 2.4 GHz.  We need it connected first so we have a
        # live NM connection to modify; 8 s covers the typical 5–7 s reconnect.
        log.info('Waiting for wlan0 to reconnect before locking band…')
        for _ in range(8):
            time.sleep(1)
            r2 = run(['nmcli', '-t', '-f', 'NAME,DEVICE,TYPE',
                      'con', 'show', '--active'])
            if any(
                p.split(':')[1:3] == ['wlan0', '802-11-wireless']
                for p in r2.stdout.splitlines()
                if len(p.split(':')) >= 3
            ):
                break
        _force_wlan0_2ghz()
    else:
        log.info('Falling back to single-mode (wlan0 AP, home WiFi suspended)')
        ifname = 'wlan0'
        # Bring down the current home WiFi connection
        r = run(['nmcli', '-t', '-f', 'NAME,TYPE', 'con', 'show', '--active'])
        for line in r.stdout.splitlines():
            parts = line.split(':')
            if (len(parts) >= 2
                    and parts[1] == '802-11-wireless'
                    and parts[0] not in (CONFIG_AP_CON, 'otgw-ap')):
                run(['nmcli', 'con', 'down', parts[0]])
                break

    if not _ensure_config_ap_profile(ifname):
        if ifname == VIRTUAL_IFACE:
            _destroy_virtual_iface()
        return False

    # Write DNS hijack config before NM starts dnsmasq.  NetworkManager's
    # shared-mode dnsmasq reads --conf-dir=/etc/NetworkManager/dnsmasq-shared.d
    # at startup, so this file must exist before `nmcli con up`.
    # address=/#/10.42.0.1 makes ALL DNS queries resolve to the portal IP,
    # which causes iOS/Android captive-portal probes to hit our server and
    # trigger the automatic popup without the user needing to type an address.
    try:
        DNS_HIJACK_CONF.parent.mkdir(parents=True, exist_ok=True)
        DNS_HIJACK_CONF.write_text('address=/#/10.42.0.1\n')
    except OSError as e:
        log.warning('Could not write DNS hijack config: %s', e)

    # Use a longer timeout: NM + hostapd bring-up can take 10–20 s on 5 GHz.
    r = run(['nmcli', 'con', 'up', CONFIG_AP_CON], timeout=30)
    if r.returncode != 0:
        log.error('Failed to bring up config AP on %s: %s',
                  ifname, r.stderr.strip())
        if ifname == VIRTUAL_IFACE:
            _destroy_virtual_iface()
        return False

    _ap_ifname = ifname
    _apply_ap_firewall(ifname)
    _wifi_ssh_open()
    CONFIG_ACTIVE.write_text('config-ap\n')
    mode = 'dual' if ifname == VIRTUAL_IFACE else 'single'
    log.info(
        'Config AP active (%s-mode) — SSID "%s" open, portal at http://10.42.0.1/',
        mode, config_ap_ssid(),
    )
    return True


def exit_config_ap(delay_s: float = 3.0) -> None:
    """Stop config AP and restore normal networking.

    delay_s: grace period so the portal can deliver its closing response.
    """
    global _ap_ifname

    CONFIG_ACTIVE.unlink(missing_ok=True)
    DNS_HIJACK_CONF.unlink(missing_ok=True)
    if delay_s > 0:
        time.sleep(delay_s)

    ifname = _ap_ifname or VIRTUAL_IFACE
    _remove_ap_firewall(ifname)
    _wifi_ssh_close()
    ACTIVITY_FILE.unlink(missing_ok=True)
    run(['nmcli', 'con', 'down', CONFIG_AP_CON])
    time.sleep(0.5)

    if ifname == VIRTUAL_IFACE:
        _destroy_virtual_iface()
        # Restore wlan0 band setting so it can roam back to 5 GHz.
        _restore_wlan0_band()
    else:
        # Single-mode: restart wifi-switch to reconnect home WiFi
        run(['systemctl', 'start', 'wifi-switch'])

    _ap_ifname = None
    log.info('Config AP stopped')


def _portal_last_activity() -> float | None:
    """Return mtime of ACTIVITY_FILE, or None if it does not exist."""
    try:
        return ACTIVITY_FILE.stat().st_mtime
    except OSError:
        return None


def _ap_monitor() -> None:
    """Background thread: enforce inactivity timeout.

    The AP exits AP_INACTIVITY_S seconds after the later of:
      - AP activation (start time)
      - Last portal HTTP request (ACTIVITY_FILE mtime)

    Every portal page-load touches ACTIVITY_FILE, so any interaction
    resets the 5-minute clock.

    Uses time.monotonic() throughout to avoid NTP clock jumps (which happen
    on Pi boot when the clock corrects from its last-known timestamp to the
    real time) from immediately triggering the timeout.  ACTIVITY_FILE carries
    a wall-clock mtime, so a reference (wall, mono) pair captured at start is
    used to convert it to a monotonic offset.
    """
    start_mono = time.monotonic()
    ref_mono = start_mono
    ref_wall = time.time()
    log.info('Config AP will auto-exit %ds after last interaction', AP_INACTIVITY_S)

    while CONFIG_ACTIVE.exists():
        last_act_wall = _portal_last_activity()
        now_mono = time.monotonic()

        if last_act_wall is not None:
            # Convert wall-clock mtime to a monotonic estimate using the
            # reference pair.  If the file predates this session (mtime <
            # ref_wall), last_act_mono ends up less than start_mono and is
            # clamped away by max(), so stale files are correctly ignored.
            last_act_mono = ref_mono + (last_act_wall - ref_wall)
            last_event = max(start_mono, last_act_mono)
        else:
            last_event = start_mono

        idle_for = now_mono - last_event
        if idle_for >= AP_INACTIVITY_S:
            if not WIFI_CONF.exists():
                # First boot: keep the AP open until wifi.conf is written so
                # the user can configure WiFi without knowing the magnet trick.
                time.sleep(5)
                continue
            log.info(
                'Config AP: no interaction for %.0fs — auto-exit', idle_for,
            )
            _publish_event('portal_closed', reason='inactivity')
            break
        time.sleep(5)

    exit_config_ap()
    led_solid_then_heartbeat(2)


def _software_toggle_monitor() -> None:
    """Background thread: watch AP_TOGGLE_FLAG for software-triggered toggles.

    Any process (TUI, portal) can write AP_TOGGLE_FLAG to request an AP
    toggle.  Write 'stop' to explicitly stop the AP; any other content (or
    empty) behaves as a plain toggle (start if inactive, reset timer if active).
    """
    while True:
        if AP_TOGGLE_FLAG.exists():
            try:
                content = AP_TOGGLE_FLAG.read_text().strip()
                AP_TOGGLE_FLAG.unlink()
            except OSError:
                content = ''
            if content == 'stop' and CONFIG_ACTIVE.exists():
                log.info('Software AP stop requested via flag file')
                _publish_event('portal_closed', reason='button')
                threading.Thread(
                    target=exit_config_ap, daemon=True,
                    name='ap-stop-sw',
                ).start()
            else:
                log.info('Software AP toggle requested via flag file')
                threading.Thread(
                    target=toggle_config_ap, daemon=True,
                    name='ap-toggle-sw',
                ).start()
        time.sleep(2)


def toggle_config_ap() -> None:
    """Toggle config AP on/off — called from sensor medium-hold handler."""
    global _last_toggle_time
    if not _toggle_lock.acquire(blocking=False):
        log.info('Toggle already in progress — ignoring')
        led_heartbeat()
        return
    try:
        _toggle_config_ap_locked()
    finally:
        _toggle_lock.release()


def _toggle_config_ap_locked() -> None:
    """Start the config AP — only called while _toggle_lock is held.

    If the AP is already active, the sensor press is ignored (the AP stays
    up until the inactivity timer fires or the portal's Finish button is used).
    """
    global _last_toggle_time
    now = time.monotonic()
    if now - _last_toggle_time < TOGGLE_COOLDOWN_S:
        log.info('Toggle ignored — cooldown (%ds between toggles)', TOGGLE_COOLDOWN_S)
        led_heartbeat()
        return

    if CONFIG_ACTIVE.exists():
        # AP already running — re-trigger resets the inactivity timer instead
        # of toggling off.  Touch ACTIVITY_FILE so _ap_monitor() restarts its
        # 5-minute countdown from now.
        ACTIVITY_FILE.touch()
        log.info('Config AP already active — inactivity timer reset')
        _publish_event('portal_timer_reset')
        led_double_blink()   # brief blink so the user knows the reset happened
        return

    if enter_config_ap():
        _publish_event('portal_open')
        led_double_blink(loop=True)
        t = threading.Thread(target=_ap_monitor, daemon=True, name='ap-monitor')
        t.start()
    else:
        led_rapid_fail()

    _last_toggle_time = time.monotonic()  # stamp AFTER action completes


# ── safe flush + shutdown ─────────────────────────────────────────────────────

def _flush_logs() -> None:
    """Flush RAM-disk logs to SD and attempt VPS sync (best-effort)."""
    subprocess.run(['systemctl', 'start', 'log-flush.service'],
                   capture_output=True, timeout=30)
    subprocess.run(['systemctl', 'start', 'log-sync.service'],
                   capture_output=True, timeout=60)


def _do_shutdown() -> None:
    """Shutdown after a brief pause to let logs settle."""
    log.info('Shutting down now')
    time.sleep(1)
    subprocess.run(['systemctl', 'poweroff'])


# ── GPIO monitor ──────────────────────────────────────────────────────────────

def _trigger_portal_and_flush() -> None:
    """Open config AP and start a background log flush (rate-limited).

    Called immediately when hold reaches PORTAL_MIN_S (100 ms), not on
    release.  A second call within FLUSH_RATE_LIMIT_S is silently ignored
    so a long hold doesn't hammer the flush service.
    """
    global _last_flush_time

    # Toggle config AP (non-blocking via thread)
    threading.Thread(target=toggle_config_ap, daemon=True,
                     name='ap-toggle-hold').start()
    log.info('Triggering LED patterns: ACT double-blink, PWR rapid-blink')
    led_double_blink()
    pwr_led_rapid_blink()
    log.info('LED patterns triggered')

    # Rate-limited flush — at most one per FLUSH_RATE_LIMIT_S
    if not _flush_lock.acquire(blocking=False):
        log.info('Flush already running — skipping')
        return
    try:
        now = time.monotonic()
        if now - _last_flush_time < FLUSH_RATE_LIMIT_S:
            log.info('Flush rate-limited (%.1fs since last)', now - _last_flush_time)
            return
        _last_flush_time = now
        _publish_event('flush_started')
        log.info('Starting background log flush')
        threading.Thread(target=_flush_logs, daemon=True,
                         name='flush-hold').start()
    finally:
        _flush_lock.release()


def _flush_and_shutdown() -> None:
    """Flush logs to SD/VPS then power off (sequential, best-effort)."""
    led_ultra_rapid_blink()
    pwr_led_ultra_rapid_blink()
    log.info('Flushing logs before shutdown …')
    _flush_logs()
    log.info('Flush complete — powering off')
    _do_shutdown()


def _first_boot_ap_watch() -> None:
    """Auto-open config AP on first boot when wifi.conf is absent.

    Waits FIRST_BOOT_AP_DELAY_S then opens the AP so the user is not left
    wondering why WiFi never came up.  The AP inactivity timer is suppressed
    while wifi.conf is absent (see _ap_monitor), so the AP stays open until
    the user saves credentials via the portal.
    """
    if WIFI_CONF.exists():
        return
    log.info(
        'wifi.conf absent — auto-opening config AP in %ds (first-boot)',
        FIRST_BOOT_AP_DELAY_S,
    )
    time.sleep(FIRST_BOOT_AP_DELAY_S)
    if WIFI_CONF.exists():
        log.info('wifi.conf appeared before delay elapsed — skipping auto-AP')
        return
    log.info('First-boot: auto-opening config AP (wifi.conf absent)')
    toggle_config_ap()


def monitor_gpio() -> None:
    global _tap_times

    try:
        import lgpio
    except ImportError:
        log.error('lgpio not available — install python3-lgpio')
        sys.exit(1)

    chip = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_input(chip, GPIO_SENSOR, lgpio.SET_PULL_UP)
    log.info(
        'Monitoring GPIO %d for U58 omnipolar hall effect sensor '
        '(≥100ms hold → portal+flush; triple-tap with ≥5s span → shutdown)',
        GPIO_SENSOR,
    )
    led_heartbeat()

    prev              = lgpio.gpio_read(chip, GPIO_SENSOR)
    hold_start: float | None = None
    action_fired      = False   # prevent repeated action_fire during one hold
    shutting_down     = False
    # Counter-based watchdog ping: avoids extra time.monotonic() calls that
    # would interfere with the existing time-mocked tests. (NFR-4.81)
    _wd_counter = 0
    _WD_PING_EVERY = max(1, int(_WATCHDOG_PING_INTERVAL / DEBOUNCE_S))  # ~600

    while True:
        time.sleep(DEBOUNCE_S)

        if shutting_down:
            continue

        curr = lgpio.gpio_read(chip, GPIO_SENSOR)

        if curr == 0 and prev != 0:
            # Magnet just arrived — rapid blink gives immediate feedback (FSD §G.34)
            hold_start   = time.monotonic()
            action_fired = False
            log.debug('Magnet detected on GPIO %d', GPIO_SENSOR)
            if not CONFIG_ACTIVE.exists():
                led_rapid_blink()

        elif curr == 0 and hold_start is not None:
            # Magnet still held — fire action once when threshold reached
            held = time.monotonic() - hold_start
            if held >= PORTAL_MIN_S and not action_fired:
                action_fired = True
                log.info('Hold %.0fms — opening portal + triggering flush', held * 1000)
                _trigger_portal_and_flush()

        elif curr != 0 and prev == 0 and hold_start is not None:
            # Magnet just released — record tap and check shutdown condition
            held       = time.monotonic() - hold_start
            hold_start = None
            pwr_led_default()

            if held < PORTAL_MIN_S:
                log.debug('Hold too short (%.0fms) — ignored', held * 1000)
                led_heartbeat()
                prev = curr
                continue

            # Record tap completion time
            now = time.monotonic()
            _tap_times.append(now)

            # Prune taps outside the rolling window
            _tap_times = [t for t in _tap_times
                          if now - t <= SHUTDOWN_WINDOW_S]

            log.debug('Tap recorded; %d tap(s) in %.0fs window',
                      len(_tap_times), SHUTDOWN_WINDOW_S)

            if len(_tap_times) >= SHUTDOWN_TAPS:
                span = _tap_times[-1] - _tap_times[0]
                if span >= SHUTDOWN_MIN_SPAN_S:
                    shutting_down = True
                    _tap_times = []
                    log.info(
                        'Triple-tap shutdown: %d taps, span=%.1fs',
                        SHUTDOWN_TAPS, span,
                    )
                    _publish_event('shutdown_triggered')
                    threading.Thread(
                        target=_flush_and_shutdown, daemon=True,
                        name='triple-tap-shutdown',
                    ).start()
                else:
                    log.info(
                        'Triple-tap ignored — span %.1fs < %.1fs required',
                        span, SHUTDOWN_MIN_SPAN_S,
                    )

        prev = curr

        # Ping the systemd watchdog every ~_WATCHDOG_PING_INTERVAL seconds. (NFR-4.81)
        _wd_counter += 1
        if _wd_counter >= _WD_PING_EVERY:
            _sd_notify('WATCHDOG=1')
            _wd_counter = 0


def main() -> None:
    log.info(
        'config-sensor starting (GPIO %d) — '
        'U58 omnipolar: ≥100ms hold → portal+flush; '
        'triple-tap (≥5s span) → shutdown; SSID: "%s"',
        GPIO_SENSOR, config_ap_ssid(),
    )

    # Clean up stale state from a previous crash or reboot
    if CONFIG_ACTIVE.exists():
        log.warning('Stale config-active file found — cleaning up')
        CONFIG_ACTIVE.unlink(missing_ok=True)
    AP_TOGGLE_FLAG.unlink(missing_ok=True)  # discard any pending toggle request
    ACTIVITY_FILE.unlink(missing_ok=True)

    # Remove any leftover virtual interface from a previous crash
    if _virtual_iface_exists():
        log.warning('Stale %s interface found — removing', VIRTUAL_IFACE)
        _destroy_virtual_iface()

    # Initialize LEDs to known good state
    led_heartbeat()
    pwr_led_default()

    # Watch for software-triggered AP toggles from portal/TUI
    threading.Thread(
        target=_software_toggle_monitor, daemon=True,
        name='ap-toggle-monitor',
    ).start()

    # Auto-open AP on first boot if wifi.conf is absent
    threading.Thread(
        target=_first_boot_ap_watch, daemon=True,
        name='first-boot-ap-watch',
    ).start()

    monitor_gpio()


if __name__ == '__main__':
    main()
