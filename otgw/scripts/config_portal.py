#!/usr/bin/env python3
"""Config portal — installer HTTP server on port 8080.

Started on demand (hall sensor or SSH); the always-on resident dashboard
runs separately on port 80 (resident_portal.py).

Provides a phone-friendly web interface equivalent to the TUI:
  /          Dashboard: WiFi status, recent readings, service status
  /wifi      WiFi config: scan networks, select SSID, enter password
  /mode      Toggle test / production mode
  /checks    Full system checks (matches TUI verify-systems)
  /checks/signoff  POST — run checks, write timestamped sign-off record to log dir
  /p1        P1 dongle configuration (broker, topic prefix, auto-configure)
  /sync      Log sync target management (list, add, delete, keygen, test)
  /live      Live MQTT data viewer (auto-refresh every 3 s)
  /simulators  Simulator service management (start/stop per simulator)
  /factory-reset  Factory reset with log handling options
  /maintenance    System maintenance (run provision.sh, check packages)
  /status.json   JSON status for JS auto-refresh (includes reading ages, MQTT)
  /messages.json  JSON list of last 20 MQTT messages
  /scan.json     JSON list of visible WiFi networks
  /simulators/toggle  POST — toggle a simulator service
  /wifi/case-a        GET  — Case A WiFi view (no home WiFi; Pi as AP)
  /wifi/forget        POST — clear saved WiFi credentials and delete nmcli profile
  /wifi-ssh/toggle          POST — toggle WiFi SSH iptables rule
  /config-ap-pw/toggle      POST — toggle WPA2 password on config AP
  /p1/send-case-a-wifi  POST — send Pi AP credentials to P1 dongle (Case A)
  /lang               POST — set language cookie (en/nl)
  /sync/keygen        POST — generate a new SSH sync key
  /sync/add           POST — add a new sync target
  /sync/delete        POST — delete a sync target
  /sync/test          POST — test a sync target
  /maintenance/run              POST — run provision.sh
  /maintenance/install-packages POST — apt-get install missing packages
  /p1/flash                     GET  — P1 firmware flash page
  /p1/flash                     POST — run esptool flash
  /sync/test-all                POST — test all configured sync targets
  /sync/flush-now               POST — trigger log-flush + log-sync (rate-limited)
  /ap/exit   Exit config AP mode (POST)
  /log-partition       Log partition status; shown in nav only when not FAT32
  /log-partition/setup POST — shrink root, create + format FAT32 log partition
  /reboot    Reboot Pi (POST)
  /shutdown  Shutdown Pi (POST)
  /*         Redirect to / (captive portal catch-all)

Language: Dutch or English, selected by cookie first, then Accept-Language header.
Runs as root (required for port 8080 and nmcli / system commands).
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import collections
import glob as _glob
import html
import json
import logging
import os
import re
import serial
import socket
import subprocess
import sys
import threading
import time
# Ensure otgw_api (sibling module) is importable regardless of WorkingDirectory
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))
import otgw_api
import p1_usb_sync
from ap_password import derive_ap_password as _derive_ap_password
from datetime import datetime, timezone
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── config ────────────────────────────────────────────────────────────────────
PORT          = 8080
CONFIG_DIR    = Path('/etc/otgw')
WIFI_CONF     = CONFIG_DIR / 'wifi.conf'
MODE_FILE     = CONFIG_DIR / 'mode'
PI_ID_FILE     = CONFIG_DIR / 'pi-id'
DEVICE_ID_FILE = CONFIG_DIR / 'device-id'
VERSION_FILE   = CONFIG_DIR / 'version'
LANG_FILE     = CONFIG_DIR / 'language'
CONFIG_ACTIVE  = Path('/run/otgw-config-active')
NO_HOME_WIFI_FILE = CONFIG_DIR / 'no-home-wifi'   # presence = Case A
WIFI_STATE     = Path('/run/otgw-wifi-state')
WIFI_SSH_FILE         = CONFIG_DIR / 'wifi-ssh'
CONFIG_AP_PW_FILE     = CONFIG_DIR / 'config-ap-pw'  # presence = pw-protected
SYNC_D_DIR            = CONFIG_DIR / 'sync.d'
LOG_DIR        = Path('/var/log/otgw')
RAM_DIR        = Path('/run/otgw')
VIRTUAL_IFACE  = 'uap0'
AP_TOGGLE_FLAG = Path('/run/otgw-ap-toggle-request')
ACTIVITY_FILE  = Path('/run/otgw-portal-activity')
AP_INACTIVITY_S = 3600  # must match config_sensor.py AP_INACTIVITY_S
FLUSH_RATE_LIMIT_S = 10.0  # must match config_sensor.py FLUSH_RATE_LIMIT_S

_portal_last_flush_time: float = 0.0
_p1_sync_busy: bool = False

# Endpoints that must NOT touch the inactivity timer — they are polled
# automatically by JS and would otherwise keep the timer permanently near 5:00.
_NO_ACTIVITY_PATHS = frozenset({
    '/status.json', '/messages.json', '/scan.json',
    # /timer.json is handled separately (returns early before _touch_activity)
})

# OS captive-portal probe paths — respond immediately without full page render.
# iOS/macOS expect a non-"Success" 200 to trigger the CNA popup.
# Android expects non-204 to detect captive portal.
# Return a tiny page linking to the portal; avoids redirect round-trip.
_CAPTIVE_PROBE_PATHS = frozenset({
    '/hotspot-detect.html',        # iOS / macOS
    '/library/test/success.html',  # iOS older
    '/generate_204',               # Android / Chrome
    '/gen_204',                    # Android fallback
    '/ncsi.txt',                   # Windows NCSI
    '/connecttest.txt',            # Windows newer NCSI
    '/redirect',                   # Windows NCSI redirect step
})
_CAPTIVE_PROBE_BODY = (
    '<html><body>'
    '<a href="http://10.42.0.1/">Open Setup Portal</a>'
    '</body></html>'
)

MAX_POST_BODY  = 65536   # 64 KB — reject larger POST bodies

SERVICES = [
    'mosquitto',
    'otgw-listener',
    'ds18b20-listener',
    'mqtt-logger',
    'log-flush.timer',
    'log-sync.timer',
    'heartbeat.timer',
    'wifi-switch',
    'config-sensor',
    'config-portal',
    'led-status',
]

SIMULATOR_SERVICES = [
    'otgw-simulator',
    'ds18b20-simulator',
    'p1-simulator',
]

# Age thresholds (seconds): (recent_ok, warn)
# Reading is green if < ok, orange if < warn, red if >= warn
READING_THRESHOLDS = {
    'otgw':        (300,  900),   # every few min from boiler; ok<5m, warn<15m
    'gas':         (600, 1800),   # every 5 min from P1;       ok<10m, warn<30m
    'temperature': (120,  600),   # every ~1 min from sensors; ok<2m, warn<10m
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger('config-portal')

# ── i18n ─────────────────────────────────────────────────────────────────────
STRINGS: dict[str, dict[str, str]] = {
    'title':          {'en': 'OTGW Logger',           'nl': 'OTGW Logger'},
    'nav_home':       {'en': 'Dashboard',              'nl': 'Dashboard'},
    'nav_wifi':       {'en': 'WiFi',                   'nl': 'WiFi'},
    'nav_mode':       {'en': 'Mode',                   'nl': 'Modus'},
    'nav_checks':     {'en': 'Checks',                 'nl': 'Controles'},
    'nav_p1':         {'en': 'P1 Dongle',              'nl': 'P1-dongle'},
    'nav_sync':       {'en': 'Sync',                   'nl': 'Sync'},
    'nav_live':       {'en': 'Live',                   'nl': 'Live'},
    'nav_simulators': {'en': 'Simulators',             'nl': 'Simulatoren'},
    'image_id':       {'en': 'Image ID',                'nl': 'Image ID'},
    'version':        {'en': 'Firmware',               'nl': 'Firmware'},
    'uptime':         {'en': 'Uptime',                 'nl': 'Uptime'},
    'mode':           {'en': 'Mode',                   'nl': 'Modus'},
    'mode_prod':      {'en': 'Production',             'nl': 'Productie'},
    'mode_test':      {'en': 'Test',                   'nl': 'Test'},
    'services':       {'en': 'Services',               'nl': 'Services'},
    'active':         {'en': 'active',                 'nl': 'actief'},
    'inactive':       {'en': 'inactive',               'nl': 'inactief'},
    'wifi_status':    {'en': 'WiFi',                   'nl': 'WiFi'},
    'wifi_ssid':      {'en': 'SSID',                   'nl': 'SSID'},
    'wifi_ip':        {'en': 'IP address',             'nl': 'IP-adres'},
    'wifi_rssi':      {'en': 'Signal',                 'nl': 'Signaal'},
    'wifi_config':    {'en': 'Configure WiFi',         'nl': 'WiFi instellen'},
    'wifi_select':    {'en': 'Select network',         'nl': 'Netwerk kiezen'},
    'wifi_manual':    {'en': 'Or enter manually',      'nl': 'Of handmatig invoeren'},
    'wifi_password':  {'en': 'Password',               'nl': 'Wachtwoord'},
    'wifi_connect':   {'en': 'Connect',                'nl': 'Verbinden'},
    'wifi_scanning':  {'en': 'Scanning…',              'nl': 'Scannen…'},
    'wifi_forget':    {'en': 'Forget saved WiFi',      'nl': 'Vergeet opgeslagen WiFi'},
    'wifi_forgotten': {'en': 'WiFi credentials cleared.', 'nl': 'WiFi-gegevens gewist.'},
    'wifi_confirm_forget': {'en': 'Clear saved WiFi credentials?',
                            'nl': 'Opgeslagen WiFi-gegevens wissen?'},
    'wifi_saved':     {'en': 'WiFi saved. Connecting to home network…',
                       'nl': 'WiFi opgeslagen. Verbinden met thuisnetwerk…'},
    'wifi_connecting_title': {
        'en': 'Connecting to WiFi…',
        'nl': 'Verbinden met WiFi…',
    },
    'wifi_channel_note': {
        'en': ('The Pi radio may switch channels. '
               'Your phone will reconnect to the installer AP automatically.'),
        'nl': ('De Pi-radio wisselt mogelijk van kanaal. '
               'Uw telefoon verbindt automatisch opnieuw met de installatie-AP.'),
    },
    'wifi_24ghz_note': {
        'en': 'Only 2.4 GHz networks are compatible (ESP32 limitation).',
        'nl': 'Alleen 2,4 GHz-netwerken zijn compatibel (ESP32-beperking).',
    },
    'wifi_reload': {
        'en': 'This page will reload automatically…',
        'nl': 'Deze pagina wordt automatisch herladen…',
    },
    'wifi_home':      {'en': 'Home network',           'nl': 'Thuisnetwerk'},
    'wifi_ap':        {'en': 'Installer AP',           'nl': 'Installatie-AP'},
    # Case A — no home WiFi
    'wifi_mode_title': {'en': 'WiFi Mode',             'nl': 'WiFi-modus'},
    'wifi_mode_home':  {
        'en': 'Home router WiFi (2.4 GHz)',
        'nl': 'Thuis-router WiFi (2,4 GHz)',
    },
    'wifi_mode_case_a': {
        'en': 'No home WiFi — use Pi as access point',
        'nl': 'Geen thuis-WiFi — gebruik Pi als accesspoint',
    },
    'wifi_case_a_title': {
        'en': 'Pi Access Point (Case A)',
        'nl': 'Pi-accesspoint (situatie A)',
    },
    'wifi_case_a_desc': {
        'en': ('No 2.4 GHz home router detected. The Pi creates its own WiFi access '
               'point. The P1 dongle connects directly to the Pi — no home router needed.'),
        'nl': ('Geen 2,4 GHz thuisrouter gevonden. De Pi maakt zelf een WiFi-toegangspunt '
               'aan. De P1-dongle verbindt rechtstreeks met de Pi — geen thuisrouter nodig.'),
    },
    'wifi_case_a_ssid':   {'en': 'Pi AP SSID',         'nl': 'Pi AP SSID'},
    'wifi_case_a_pw':     {'en': 'Pi AP password',      'nl': 'Pi AP wachtwoord'},
    'wifi_case_a_copy':   {'en': 'Copy',                'nl': 'Kopieer'},
    'wifi_case_a_copied': {'en': 'Copied!',             'nl': 'Gekopieerd!'},
    'wifi_case_a_activate': {
        'en': 'Activate Case A mode',
        'nl': 'Situatie A activeren',
    },
    'wifi_case_a_active': {
        'en': 'Case A mode is active. The Pi is broadcasting its own AP.',
        'nl': 'Situatie A is actief. De Pi zendt zijn eigen AP uit.',
    },
    'wifi_case_a_deactivate': {
        'en': 'Switch to home router WiFi',
        'nl': 'Overschakelen naar thuisrouter WiFi',
    },
    'wifi_case_a_recovery': {
        'en': ('During the hall-magnet recovery window the AP temporarily becomes open '
               '(no password). The P1 dongle disconnects until the window closes '
               '(~5 min), then reconnects automatically.'),
        'nl': ('Tijdens het halmagneet-herstelvenster wordt de AP tijdelijk open (geen '
               'wachtwoord). De P1-dongle verbreekt de verbinding totdat het venster '
               'sluit (~5 min) en maakt daarna automatisch opnieuw verbinding.'),
    },
    'wifi_case_a_send_p1': {
        'en': 'Send AP credentials to P1 dongle via USB',
        'nl': 'AP-gegevens via USB naar P1-dongle sturen',
    },
    'mode_title':     {'en': 'Operating Mode',         'nl': 'Bedrijfsmodus'},
    'mode_desc_prod': {'en': 'Production: real data, live VPS sync.',
                       'nl': 'Productie: echte data, live VPS-sync.'},
    'mode_desc_test': {'en': 'Test: simulated data, separate log path.',
                       'nl': 'Test: gesimuleerde data, apart logpad.'},
    'mode_switch':    {'en': 'Switch to',              'nl': 'Schakel naar'},
    'checks_title':   {'en': 'System Checks',          'nl': 'Systeemcontroles'},
    'checks_pass':    {'en': 'PASS',                   'nl': 'OK'},
    'checks_fail':    {'en': 'FAIL',                   'nl': 'FOUT'},
    'checks_warn':    {'en': 'WARN',                   'nl': 'WAARSCHUWING'},
    'checks_signoff_btn':  {'en': 'Sign off installation',
                            'nl': 'Installatie afmelden'},
    'checks_signoff_blocked': {
        'en': 'Fix all failing checks before signing off.',
        'nl': 'Los alle mislukte controles op voor het afmelden.',
    },
    'checks_signoff_ok':   {'en': 'Signed off at {ts} — record saved to log.',
                            'nl': 'Afgemeld om {ts} — record opgeslagen in log.'},
    'checks_signoff_err':  {'en': 'Sign-off failed: {err}',
                            'nl': 'Afmelden mislukt: {err}'},
    'checks_finish_btn':   {'en': 'Finish & Stop Portal',
                            'nl': 'Afronden & portaal stoppen'},
    'checks_finish_disabled': {
        'en': 'Fix failing checks before finishing',
        'nl': 'Los mislukte controles op voor het afronden',
    },
    'stop_ap_title':       {'en': 'Stopping Portal…',
                            'nl': 'Portaal stoppen…'},
    'stop_ap_body':        {'en': 'Installation complete. The portal and config WiFi '
                                  'will close in a few seconds.',
                            'nl': 'Installatie voltooid. Het portaal en de '
                                  'configuratie-WiFi worden over enkele seconden '
                                  'gesloten.'},
    'checks_data_flow':    {'en': 'Data Flow',         'nl': 'Datastroom'},
    'ap_mode':        {'en': 'Config AP active',       'nl': 'Config-AP actief'},
    'ap_dual':        {'en': 'Dual-mode — phone: installer AP / Pi: home WiFi',
                       'nl': 'Dual-modus — telefoon: installatie-AP / Pi: thuis-WiFi'},
    'ap_single':      {'en': 'Single-mode — home WiFi suspended during config',
                       'nl': 'Enkelvoudige modus — thuis-WiFi onderbroken'},
    'ap_exit':        {'en': 'Exit config mode',       'nl': 'Config-modus verlaten'},
    'ap_timer_label': {'en': 'Portal closes in',       'nl': 'Portaal sluit over'},
    'ap_timer_reset': {'en': 'Swipe magnet to reset',  'nl': 'Magneet wissen om te resetten'},
    'reboot':         {'en': 'Reboot',                 'nl': 'Herstart'},
    'shutdown':       {'en': 'Shutdown',               'nl': 'Afsluiten'},
    'confirm_reboot': {'en': 'Reboot the Pi?',         'nl': 'Pi herstarten?'},
    'confirm_shutdown':{'en':'Shutdown the Pi?',       'nl': 'Pi afsluiten?'},
    'ds18b20':        {'en': 'Temperature sensors',    'nl': 'Temperatuursensoren'},
    'no_sensors':     {'en': 'No sensors found',       'nl': 'Geen sensoren gevonden'},
    'recent_data':    {'en': 'Recent data',            'nl': 'Recente data'},
    'last_otgw':      {'en': 'OTGW',                   'nl': 'OTGW'},
    'last_gas':       {'en': 'Gas (P1 meter)',          'nl': 'Gas (P1-meter)'},
    'last_temp':      {'en': 'Temperature',            'nl': 'Temperatuur'},
    'no_data':        {'en': 'No data yet',            'nl': 'Nog geen data'},
    'log_space':      {'en': 'Log storage',            'nl': 'Logopslag'},
    'sync_targets':   {'en': 'Sync targets',           'nl': 'Synchronisatiedoelen'},
    'none':           {'en': 'none',                   'nl': 'geen'},
    # Live MQTT
    'live_mqtt':      {'en': 'Live MQTT',              'nl': 'Live MQTT'},
    'live_title':     {'en': 'Live MQTT Data',         'nl': 'Live MQTT-data'},
    'live_connected': {'en': 'Connected',              'nl': 'Verbonden'},
    'live_disconnected': {'en': 'Disconnected',        'nl': 'Niet verbonden'},
    'live_topic':     {'en': 'Topic',                  'nl': 'Onderwerp'},
    'live_payload':   {'en': 'Payload',                'nl': 'Payload'},
    'live_age':       {'en': 'Age',                    'nl': 'Leeftijd'},
    'live_no_msgs':   {'en': 'No messages yet',        'nl': 'Nog geen berichten'},
    # P1 page
    'p1_title':       {'en': 'P1 Dongle Configuration', 'nl': 'P1-dongle instellen'},
    'p1_broker':      {'en': 'MQTT Broker',            'nl': 'MQTT-broker'},
    'p1_topic_prefix':{'en': 'Topic Prefix',           'nl': 'Onderwerpprefix'},
    'p1_save':        {'en': 'Save & Configure',       'nl': 'Opslaan & instellen'},
    'p1_dongle_found':{'en': 'P1 dongle found at',     'nl': 'P1-dongle gevonden op'},
    'p1_dongle_none': {'en': 'No P1 dongle detected',  'nl': 'Geen P1-dongle gevonden'},
    'p1_send_config_title': {'en': 'Send WiFi Configuration', 'nl': 'WiFi-configuratie verzenden'},
    'p1_send_config_desc': {'en': 'Send WiFi and MQTT settings to the P1 dongle via USB serial.', 
                           'nl': 'Verstuur WiFi- en MQTT-instellingen naar de P1-dongle via USB-serieel.'},
    'p1_config_sent': {'en': 'Configuration sent successfully!', 'nl': 'Configuratie succesvol verzonden!'},
    'p1_config_error': {'en': 'Failed to send configuration: {error}', 'nl': 'Configuratie verzenden mislukt: {error}'},
    'p1_send_config': {'en': 'Send Configuration', 'nl': 'Configuratie verzenden'},
    'p1_wifi_ssid': {'en': 'WiFi SSID:', 'nl': 'WiFi SSID:'},
    'p1_wifi_password': {'en': 'WiFi Password:', 'nl': 'WiFi-wachtwoord:'},
    'p1_wifi_password_placeholder': {'en': 'Enter WiFi password (required for connection)', 'nl': 'Voer WiFi-wachtwoord in (vereist voor verbinding)'},
    'p1_wifi_ssid_note': {'en': 'Note: Current WiFi SSID not found in config. Enter the SSID manually.', 
                         'nl': 'Opmerking: Huidige WiFi-SSID niet gevonden in configuratie. Voer de SSID handmatig in.'},
    'p1_wifi_password_hint': {'en': 'Current password hint: {hint}', 'nl': 'Huidige wachtwoord hint: {hint}'},
    'p1_flash_warning_title': {'en': '⚠️ Flashing P1 Dongle', 'nl': '⚠️ P1-dongle flashen'},
    'p1_flash_warning_slow': {'en': 'WARNING: Flashing from Raspberry Pi is slow and experimental. The process may fail due to USB power or timing issues.', 
                             'nl': 'WAARSCHUWING: Flashen vanaf Raspberry Pi is langzaam en experimenteel. Het proces kan falen door USB-voeding of timing-problemen.'},
    'p1_flash_recommend_pc': {'en': 'For best results, flash the P1 dongle directly from a PC or Mac using esptool. See instructions below.',
                             'nl': 'Voor beste resultaten, flash de P1-dongle rechtstreeks vanaf een PC of Mac met esptool. Zie instructies hieronder.'},
    'p1_flash_manual_title': {'en': 'Manual Flashing Instructions (Recommended)', 'nl': 'Handmatige flash-instructies (aanbevolen)'},
    'p1_flash_step1': {'en': 'Download the latest P1 reader firmware binary for ESP32-S3 from the project repository.',
                       'nl': 'Download de nieuwste P1-reader firmware binary voor ESP32-S3 uit de projectrepository.'},
    'p1_flash_step2': {'en': 'Install esptool on your computer: <code>pip install esptool</code>',
                       'nl': 'Installeer esptool op je computer: <code>pip install esptool</code>'},
    'p1_flash_step3': {'en': 'Connect P1 dongle via USB and flash with: <code>esptool.py -p /dev/ttyUSB0 -b 115200 write_flash 0x10000 firmware.bin</code>',
                       'nl': 'Sluit P1-dongle aan via USB en flash met: <code>esptool.py -p /dev/ttyUSB0 -b 115200 write_flash 0x10000 firmware.bin</code>'},
    'p1_flash_step4': {'en': 'After flashing, configure WiFi using the "Send WiFi Configuration" form above or the dongle\'s built-in web portal.',
                       'nl': 'Na het flashen, configureer WiFi met het "Send WiFi Configuration" formulier hierboven of via de ingebouwde webportal van de dongle.'},
    'p1_flash_download': {'en': 'Download here', 'nl': 'Download hier'},
    'p1_mqtt_broker': {'en': 'MQTT Broker:', 'nl': 'MQTT-broker:'},
    'p1_mqtt_port': {'en': 'MQTT Port:', 'nl': 'MQTT-poort:'},
    'p1_mqtt_topic': {'en': 'MQTT Topic:', 'nl': 'MQTT-onderwerp:'},
    'p1_saved':       {'en': 'P1 config saved.',       'nl': 'P1-configuratie opgeslagen.'},
    'p1_auto_output': {'en': 'Auto-configure output:', 'nl': 'Auto-configuratie uitvoer:'},
    # Sync page
    'sync_title':     {'en': 'Log Sync Targets',       'nl': 'Logsynchronisatiedoelen'},
    'sync_public_key':   {'en': 'Pi Public SSH Key',      'nl': 'Openbare SSH-sleutel Pi'},
    'sync_no_key':       {'en': 'No key generated yet',   'nl': 'Nog geen sleutel gegenereerd'},
    'sync_gen_key':      {'en': 'Generate Key',           'nl': 'Sleutel genereren'},
    'sync_prebaked_badge':{'en': 'pre-registered',        'nl': 'vooraf geregistreerd'},
    'sync_prebaked_key': {'en': 'SSH key was baked into this image — VPS is pre-registered, no manual setup needed.',
                          'nl': 'SSH-sleutel is in dit image gebakken — VPS is vooraf geregistreerd, geen handmatige instelling nodig.'},
    'sync_add_target':{'en': 'Add Target',             'nl': 'Doel toevoegen'},
    'sync_name':      {'en': 'Name',                   'nl': 'Naam'},
    'sync_host':      {'en': 'Host',                   'nl': 'Host'},
    'sync_user':      {'en': 'User',                   'nl': 'Gebruiker'},
    'sync_path':      {'en': 'Remote path',            'nl': 'Extern pad'},
    'sync_delete':    {'en': 'Delete',                 'nl': 'Verwijderen'},
    'sync_test':      {'en': 'Test',                   'nl': 'Testen'},
    'sync_saved':     {'en': 'Sync target saved.',     'nl': 'Synchronisatiedoel opgeslagen.'},
    'sync_deleted':   {'en': 'Sync target deleted.',   'nl': 'Synchronisatiedoel verwijderd.'},
    'sync_key_gen_ok':{'en': 'SSH key generated.',     'nl': 'SSH-sleutel gegenereerd.'},
    'sync_key_gen_err':{'en': 'Key generation failed: {err}',
                        'nl': 'Sleutelgeneratie mislukt: {err}'},
    'sync_test_result':{'en': 'Test result:',          'nl': 'Testresultaat:'},
    # Simulators page
    'sim_title':      {'en': 'Simulator Management',   'nl': 'Simulatorenbeheer'},
    'sim_service':    {'en': 'Service',                'nl': 'Dienst'},
    'sim_status':     {'en': 'Status',                 'nl': 'Status'},
    'sim_toggle':     {'en': 'Toggle',                 'nl': 'Schakelen'},
    'sim_started':    {'en': 'started',                'nl': 'gestart'},
    'sim_stopped':    {'en': 'stopped',                'nl': 'gestopt'},
    # WiFi SSH
    'wifi_ssh_title': {'en': 'WiFi SSH',               'nl': 'WiFi SSH'},
    'wifi_ssh_enabled':{'en': 'WiFi SSH enabled',      'nl': 'WiFi SSH ingeschakeld'},
    'wifi_ssh_disabled':{'en': 'WiFi SSH disabled',    'nl': 'WiFi SSH uitgeschakeld'},
    'wifi_ssh_toggle':{'en': 'Toggle WiFi SSH',        'nl': 'WiFi SSH schakelen'},
    'wifi_ssh_on':    {'en': 'ON',                     'nl': 'AAN'},
    'wifi_ssh_off':   {'en': 'OFF',                    'nl': 'UIT'},
    # Config AP password
    'config_ap_pw_label': {'en': 'Config AP password',   'nl': 'AP-wachtwoord'},
    'config_ap_pw_on':    {'en': 'WPA2 (password-protected)', 'nl': 'WPA2 (met wachtwoord)'},
    'config_ap_pw_off':   {'en': 'Open (no password)',   'nl': 'Open (geen wachtwoord)'},
    'config_ap_pw_enable': {'en': 'Enable password',     'nl': 'Wachtwoord inschakelen'},
    'config_ap_pw_disable':{'en': 'Disable password',    'nl': 'Wachtwoord uitschakelen'},
    # Factory reset
    'reset_title':    {'en': 'Factory Reset',          'nl': 'Fabrieksinstellingen'},
    'reset_warning':  {'en': 'This will remove all configuration from this Pi.',
                       'nl': 'Hiermee wordt alle configuratie van deze Pi verwijderd.'},
    'reset_keep_logs':{'en': 'Keep logs',              'nl': 'Logs bewaren'},
    'reset_delete_logs':{'en': 'Delete logs',          'nl': 'Logs verwijderen'},
    'reset_btn':      {'en': 'Factory Reset',          'nl': 'Fabrieksinstellingen'},
    'reset_done':     {'en': 'Factory reset complete. Rebooting…',
                       'nl': 'Fabrieksinstellingen hersteld. Herstarten…'},
    # Maintenance
    'maint_title':    {'en': 'System Maintenance',     'nl': 'Systeemonderhoud'},
    'maint_run_btn':  {'en': 'Run System Maintenance', 'nl': 'Systeemonderhoud uitvoeren'},
    'maint_output':   {'en': 'Output:',                'nl': 'Uitvoer:'},
    'maint_pkgs':     {'en': 'Package Status',         'nl': 'Pakketstatus'},
    'maint_pkg_ok':   {'en': 'All required packages installed',
                       'nl': 'Alle vereiste pakketten geïnstalleerd'},
    'maint_pkg_missing':{'en': 'Missing packages: {pkgs}',
                         'nl': 'Ontbrekende pakketten: {pkgs}'},
    # Language toggle
    'lang_toggle':    {'en': 'NL',                     'nl': 'EN'},
    # P1 flash
    'p1_flash_title':  {'en': 'Flash P1 Firmware',     'nl': 'P1-firmware flashen'},
    'p1_flash_btn':    {'en': 'Flash Firmware',         'nl': 'Firmware flashen'},
    'p1_flash_running':{'en': 'Flashing…',              'nl': 'Flashen…'},
    'p1_flash_no_fw':  {'en': 'Firmware file not found on Pi',
                        'nl': 'Firmwarebestand niet gevonden op Pi'},
    'p1_flash_no_esptool': {'en': 'esptool not installed',
                            'nl': 'esptool niet geïnstalleerd'},
    'p1_flash_ok':     {'en': 'Flash successful.',      'nl': 'Flash geslaagd.'},
    'p1_flash_err':    {'en': 'Flash failed.',          'nl': 'Flashen mislukt.'},
    'p1_flash_output': {'en': 'Flash output',           'nl': 'Flash-uitvoer'},
    # Package install
    'pkg_install_btn': {'en': 'Install Missing Packages',
                        'nl': 'Ontbrekende pakketten installeren'},
    'pkg_install_ok':  {'en': 'All packages installed.','nl': 'Alle pakketten geïnstalleerd.'},
    'pkg_install_err': {'en': 'Package installation failed.',
                        'nl': 'Pakketinstallatie mislukt.'},
    'pkg_install_output': {'en': 'Install output',      'nl': 'Installatie-uitvoer'},
    # Sync: test all / flush now
    'sync_test_all':   {'en': 'Test All Targets',       'nl': 'Alle doelen testen'},
    'sync_flush_now':  {'en': 'Flush & Sync Now',       'nl': 'Nu flushen & synchroniseren'},
    'sync_flush_started':     {'en': 'Flush & sync started in background.',
                               'nl': 'Flush & synchronisatie gestart op achtergrond.'},
    'sync_flush_rate_limited':{'en': 'Flush already running — wait a moment.',
                               'nl': 'Flush loopt al — even wachten.'},
    # P1 USB sync
    'p1_sync_title':   {'en': 'P1 Dongle USB Sync',
                        'nl': 'P1-dongle USB-synchronisatie'},
    'p1_sync_btn':     {'en': 'Sync from P1 dongle',
                        'nl': 'Ophalen van P1-dongle'},
    'p1_sync_full_btn':{'en': 'Full buffer dump',
                        'nl': 'Volledig bufferdump'},
    'p1_sync_clear_btn':{'en': 'Mark as received on dongle',
                         'nl': 'Markeren als ontvangen op dongle'},
    'p1_sync_busy':    {'en': 'Sync already in progress — please wait.',
                        'nl': 'Synchronisatie al bezig — even geduld.'},
    'p1_sync_no_port': {'en': 'P1 dongle not found. Connect dongle via USB and retry.',
                        'nl': 'P1-dongle niet gevonden. Sluit de dongle aan via USB en probeer opnieuw.'},
    'p1_sync_done':    {'en': 'Sync complete: {merged} new records, {skipped} duplicate, {malformed} malformed.',
                        'nl': 'Sync voltooid: {merged} nieuwe records, {skipped} duplicaat, {malformed} ongeldig.'},
    'p1_sync_error':   {'en': 'Sync failed: {error}',
                        'nl': 'Sync mislukt: {error}'},
    'p1_sync_cleared': {'en': 'Dongle MQTT offset cleared: {msg}',
                        'nl': 'Dongle MQTT-offset gewist: {msg}'},
    'p1_sync_clear_err':{'en': 'Clear failed: {msg}',
                         'nl': 'Wissen mislukt: {msg}'},
    'p1_sync_status':  {'en': 'Dongle buffer: {count} records pending ({bytes} bytes)',
                        'nl': 'Dongle-buffer: {count} records in wachtrij ({bytes} bytes)'},
    'p1_sync_status_err': {'en': 'Could not query dongle buffer status.',
                           'nl': 'Kan status van dongle-buffer niet ophalen.'},
    # Log partition page
    'nav_log_part':   {'en': 'Log Partition',          'nl': 'Logpartitie'},
    'logpart_title':  {'en': 'Log Partition Setup',    'nl': 'Logpartitie instellen'},
    'logpart_already':{'en': 'Log partition already mounted as FAT32 on {dev}.',
                       'nl': 'Logpartitie al als FAT32 gekoppeld op {dev}.'},
    'logpart_status_ok':   {'en': 'FAT32 on {dev}',   'nl': 'FAT32 op {dev}'},
    'logpart_status_other':{'en': 'Mounted ({fs}), not FAT32',
                            'nl': 'Gekoppeld ({fs}), geen FAT32'},
    'logpart_status_none': {'en': 'Not mounted (ext4 / tmpfs)',
                            'nl': 'Niet gekoppeld (ext4 / tmpfs)'},
    'logpart_intro':  {'en': 'This will shrink the root partition and create a 1 GB FAT32 partition at /var/log/otgw. The partition is readable on Windows and macOS without extra software.',
                       'nl': 'Dit verkleint de rootpartitie en maakt een FAT32-partitie van 1 GB aan op /var/log/otgw. De partitie is leesbaar op Windows en macOS.'},
    'logpart_warning':{'en': 'Warning: this operation modifies the partition table. Existing log files will be preserved.',
                       'nl': 'Waarschuwing: deze actie wijzigt de partitietabel. Bestaande logbestanden worden bewaard.'},
    'logpart_btn':    {'en': 'Set up FAT32 log partition',
                       'nl': 'FAT32-logpartitie instellen'},
    'logpart_running':{'en': 'Setting up partition…',
                       'nl': 'Partitie instellen…'},
    'logpart_done':   {'en': 'Log partition set up successfully.',
                       'nl': 'Logpartitie succesvol ingesteld.'},
    'logpart_failed': {'en': 'Setup failed: {v}',     'nl': 'Instellen mislukt: {v}'},
    'logpart_output': {'en': 'Output',                'nl': 'Uitvoer'},
    # Setup wizard
    'nav_wizard':       {'en': 'Wizard',              'nl': 'Wizard'},
    'wizard_title':         {'en': 'Setup Wizard',        'nl': 'Installatiewizard'},
    'wizard_controls_title':{'en': 'Quick Controls',      'nl': 'Snelle instellingen'},
    'wizard_subtitle':  {'en': 'Step-by-step installation guide',
                         'nl': 'Stapsgewijze installatiegids'},
    'wizard_progress':  {'en': '{done} of {total} steps complete',
                         'nl': '{done} van {total} stappen klaar'},
    'wizard_go':        {'en': 'Go',                  'nl': 'Ga'},
    'wizard_redo':      {'en': 'Redo',                'nl': 'Opnieuw'},
    'wizard_optional':  {'en': 'optional',            'nl': 'optioneel'},
    'wizard_step_wifi':     {'en': 'WiFi Connection',       'nl': 'WiFi-verbinding'},
    'wizard_step_internet': {'en': 'Internet Access',       'nl': 'Internettoegang'},
    'wizard_step_p1':       {'en': 'P1 Dongle (Gas meter)', 'nl': 'P1-dongle (gasmeter)'},
    'wizard_step_otgw':     {'en': 'OTGW (Gateway)',           'nl': 'OTGW (gateway)'},
    'wizard_step_sensors':  {'en': 'Temperature Sensors',      'nl': 'Temperatuursensoren'},
    'wizard_step_sync':     {'en': 'Log Sync (VPS)',        'nl': 'Logsync (VPS)'},
    'wizard_step_services': {'en': 'Services',              'nl': 'Diensten'},
    'wizard_step_finish':   {'en': 'Finish Installation',   'nl': 'Installatie afronden'},
    'wizard_finish_hint':   {'en': 'All required steps passed — ready to finish.',
                             'nl': 'Alle verplichte stappen geslaagd — klaar om af te ronden.'},
    'wizard_finish_blocked': {'en': 'Complete required steps before finishing.',
                              'nl': 'Voltooi de verplichte stappen voor het afronden.'},
}


def tr(key: str, lang: str) -> str:
    entry = STRINGS.get(key, {})
    return entry.get(lang) or entry.get('en', key)


def detect_lang(accept: str, cookie: str = '') -> str:
    """Return 'nl' or 'en'. Cookie overrides Accept-Language."""
    if cookie in ('nl', 'en'):
        return cookie
    if not accept:
        return 'en'
    for part in re.split(r'[,;]', accept):
        code = part.strip().split('-')[0].lower()
        if code == 'nl':
            return 'nl'
        if code == 'en':
            return 'en'
    return 'en'


def _parse_cookie(raw: str) -> dict[str, str]:
    """Parse Cookie header into a dict."""
    result: dict[str, str] = {}
    if not raw:
        return result
    for part in raw.split(';'):
        part = part.strip()
        if '=' in part:
            k, v = part.split('=', 1)
            result[k.strip()] = v.strip()
    return result


# ── MQTT live message cache ───────────────────────────────────────────────────

class PortalMQTTCache:
    """Thread-safe MQTT subscriber caching the last 20 messages.

    Subscribes to # on localhost:1883 in a background daemon thread.
    Reconnects automatically on disconnect (5 s retry).

    Also maintains _latest_reading_ts: a per-category timestamp that is
    never evicted by high-frequency topics.  The deque (MAX_MESSAGES=20)
    is only used for the live /messages view; reading ages use the
    dedicated dict so gas (30 s interval) is never pushed out by OTGW
    (every 2-3 s) or temperature (every 8 s).
    """

    MAX_MESSAGES = 20

    # topic suffix → reading category key
    _READING_TOPICS: dict[str, str] = {
        'energy/otgw':          'otgw',
        'energy/gas':           'gas',
        'sensors/temperature':  'temperature',
    }

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages: collections.deque[tuple[float, str, str]] = (
            collections.deque(maxlen=self.MAX_MESSAGES)
        )
        # Per-category latest timestamp — never evicted
        self._latest_reading_ts: dict[str, float] = {}
        self._connected = False
        self._client = None

    def get_latest_reading_ts(self) -> dict[str, float | None]:
        """Return {otgw, gas, temperature} → latest Unix timestamp or None."""
        with self._lock:
            return {k: self._latest_reading_ts.get(k)
                    for k in ('otgw', 'gas', 'temperature')}

    def start(self) -> None:
        """Start background MQTT loop in a daemon thread."""
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self) -> None:
        while True:
            try:
                import paho.mqtt.client as mqtt
                client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
                    client_id='otgw-portal-cache',
                )
                client.on_connect = self._on_connect
                client.on_disconnect = self._on_disconnect
                client.on_message = self._on_message
                self._client = client
                client.connect('localhost', 1883, keepalive=60)
                client.loop_forever()
            except Exception as exc:
                log.debug('MQTT cache connect error: %s', exc)
            self._connected = False
            time.sleep(5)

    def _on_connect(self, client, userdata, flags, rc, props=None) -> None:
        if rc == 0:
            self._connected = True
            client.subscribe('#')
        else:
            self._connected = False

    def _on_disconnect(self, client, userdata, rc, props=None,
                       rcode=None) -> None:
        self._connected = False

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = msg.payload.decode('utf-8', errors='replace')
        except Exception:
            return
        now = time.time()
        with self._lock:
            self._messages.append((now, msg.topic, payload))
            for suffix, key in self._READING_TOPICS.items():
                if suffix in msg.topic:
                    self._latest_reading_ts[key] = now
                    break

    def get_messages(self) -> list[dict]:
        """Return list of {ts, topic, payload, age} newest-first."""
        now = time.time()
        with self._lock:
            msgs = list(self._messages)
        msgs.reverse()
        return [
            {'ts': ts, 'topic': topic, 'payload': payload,
             'age': round(now - ts, 1)}
            for ts, topic, payload in msgs
        ]

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def message_count(self) -> int:
        with self._lock:
            return len(self._messages)


# Module-level MQTT cache instance (started in main())
_mqtt_cache = PortalMQTTCache()


class ChecksCache:
    """Background thread that runs run_checks() every 30 s and caches results.

    Also caches get_wifi_info() and service statuses so that build_dashboard()
    and /status.json return instantly without any subprocess calls per request.

    The cache is warmed once at startup before the HTTP server opens, so the
    very first request is fast.
    """

    REFRESH_INTERVAL = 30  # seconds between background refreshes

    def __init__(self) -> None:
        self._lock     = threading.Lock()
        self._results:  list[dict]            = []
        self._wifi:     dict                  = {}
        self._services: dict[str, str]        = {}
        self._sensors:  list[dict]            = []
        self._log_usage: str                  = '…'
        self._readings: dict[str, float|None] = {
            'otgw': None, 'gas': None, 'temperature': None,
        }
        self._last_run: float                 = 0.0

    def _collect(self) -> None:
        """Run all slow calls and store results under the lock."""
        all_svcs = SERVICES + SIMULATOR_SERVICES
        results   = run_checks()
        wifi      = get_wifi_info()
        services  = {s: get_service_status(s) for s in all_svcs}
        sensors   = get_ds18b20()
        log_usage = get_log_usage()
        readings  = get_recent_readings()
        with self._lock:
            self._results   = results
            self._wifi      = wifi
            self._services  = services
            self._sensors   = sensors
            self._log_usage = log_usage
            self._readings  = readings
            self._last_run  = time.time()

    def start(self) -> None:
        """Start background refresh thread; first collect runs immediately."""
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self) -> None:
        # First collect happens right away — no initial sleep.
        # The HTTP server is already accepting connections by now, so pages
        # served before _collect() finishes will show '…' / 'unknown'.
        try:
            self._collect()
        except Exception as exc:
            log.debug('checks cache initial collect error: %s', exc)
        while True:
            time.sleep(self.REFRESH_INTERVAL)
            try:
                self._collect()
            except Exception as exc:
                log.debug('checks cache refresh error: %s', exc)

    def get(self) -> tuple[list[dict], float]:
        """Return (results, last_run_timestamp) immediately from cache."""
        with self._lock:
            return list(self._results), self._last_run

    def get_wifi(self) -> dict:
        """Return cached wifi info (instant, no subprocess)."""
        with self._lock:
            return dict(self._wifi)

    def get_services(self) -> dict[str, str]:
        """Return cached {service_name: status} dict (instant, no subprocess)."""
        with self._lock:
            return dict(self._services)

    def get_sensors(self) -> list[dict]:
        """Return cached DS18B20 sensor readings."""
        with self._lock:
            return list(self._sensors)

    def get_log_usage(self) -> str:
        """Return cached log partition usage string."""
        with self._lock:
            return self._log_usage

    def get_readings(self) -> dict[str, float | None]:
        """Return cached last-reading timestamps for each data source."""
        with self._lock:
            return dict(self._readings)


# Module-level checks cache instance (warmed + started in main())
_checks_cache = ChecksCache()


# ── system helpers ────────────────────────────────────────────────────────────

def run(cmd: list[str], timeout: int = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ''


def get_image_id() -> str:
    try:
        return PI_ID_FILE.read_text().strip()
    except OSError:
        return 'unknown'


def get_device_id() -> str:
    """Return the per-device ID written by pi_id.sh at first boot.

    Differs from Image ID on prebaked images: Image ID identifies the build
    (baked in at image creation); Device ID identifies the physical board
    (derived from MAC address at first boot).
    """
    try:
        return DEVICE_ID_FILE.read_text().strip()
    except OSError:
        return 'unknown'


def get_version() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return 'unknown'


def get_mode() -> str:
    try:
        return MODE_FILE.read_text().strip()
    except OSError:
        return 'production'


def get_uptime() -> str:
    raw = run(['uptime', '-p'])
    return raw.replace('up ', '') if raw else '?'


def get_service_status(name: str) -> str:
    out = run(['systemctl', 'is-active', name])
    return out or 'unknown'


def get_wifi_info() -> dict:
    """Return WiFi status including dual-mode detection."""
    info: dict = {}

    # Check dual-mode (uap0 virtual interface exists = concurrent STA+AP)
    info['dual_mode'] = Path(f'/sys/class/net/{VIRTUAL_IFACE}').exists()
    info['ap_mode'] = CONFIG_ACTIVE.exists()

    # Active wlan0 connection (home WiFi in client mode).
    # Primary: nmcli wifi list ACTIVE=yes — only set when fully connected.
    # Fallback: nmcli con show --active on wlan0 — also set while connecting.
    # The fallback handles the brief reconnection phase when wlan0 is locked
    # to 2.4 GHz at config-AP start and hasn't yet completed the handshake.
    out = run(['nmcli', '-t', '-f', 'ACTIVE,SSID,SIGNAL,DEVICE',
               'device', 'wifi', 'list'])
    for line in out.splitlines():
        parts = line.split(':')
        if len(parts) >= 4 and parts[0] == 'yes' and parts[3].startswith('wlan'):
            info['ssid']   = parts[1]
            info['signal'] = parts[2] + ' %'
            break
    if not info.get('ssid'):
        # wlan0 not yet connected — check for an active (connecting) NM connection
        out2 = run(['nmcli', '-t', '-f', 'NAME,DEVICE,TYPE', 'con', 'show', '--active'])
        for line in out2.splitlines():
            parts = line.split(':')
            if (len(parts) >= 3
                    and parts[1] == 'wlan0'
                    and parts[2] == '802-11-wireless'):
                info['ssid'] = parts[0]   # connection name, e.g. "casa25g"
                info['connecting'] = True
                break

    # wlan0 IP (home WiFi)
    out = run(['nmcli', '-t', '-f', 'IP4.ADDRESS', 'dev', 'show', 'wlan0'])
    for line in out.splitlines():
        if line.startswith('IP4.ADDRESS'):
            info['ip'] = line.split(':')[-1].split('/')[0]
            break

    # uap0 IP (installer AP, only in dual-mode)
    if info['dual_mode']:
        out = run(['nmcli', '-t', '-f', 'IP4.ADDRESS', 'dev', 'show', VIRTUAL_IFACE])
        for line in out.splitlines():
            if line.startswith('IP4.ADDRESS'):
                info['ap_ip'] = line.split(':')[-1].split('/')[0]
                break

    # wifi-switch state
    try:
        info['wifi_state'] = WIFI_STATE.read_text().strip()
    except OSError:
        info['wifi_state'] = ''

    return info


def scan_wifi(rescan: bool = False) -> list[dict]:
    """Return list of {ssid, signal, security, band} sorted by signal desc.

    band is '2.4', '5', or 'unknown'.  Only 2.4 GHz networks are compatible
    with the ESP32-S3 P1 dongle — 5 GHz networks are surfaced so the user
    can see them but they are marked as incompatible in the UI.

    rescan=False uses nmcli's cached results (instant).
    rescan=True triggers an active WiFi rescan first (~4 s) — used by
    /scan.json so the page loads fast and JS refreshes in the background.
    """
    if rescan:
        run(['nmcli', 'device', 'wifi', 'rescan'], timeout=15)
        time.sleep(2)
    out = run(['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY,FREQ',
               'device', 'wifi', 'list'])
    seen: set[str] = set()
    networks: list[dict] = []
    for line in out.splitlines():
        parts = line.split(':')
        if len(parts) < 2:
            continue
        ssid = parts[0].strip()
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        freq_str = parts[3].strip() if len(parts) > 3 else ''
        try:
            freq_mhz = int(freq_str.split()[0])
        except (ValueError, IndexError):
            freq_mhz = 0
        band = 'unknown' if freq_mhz == 0 else ('2.4' if freq_mhz < 3000 else '5')
        networks.append({
            'ssid':     ssid,
            'signal':   int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0,
            'security': parts[2] if len(parts) > 2 else '',
            'band':     band,
        })
    return sorted(networks, key=lambda x: x['signal'], reverse=True)


def get_ds18b20() -> list[dict]:
    sensors = []
    base = Path('/sys/bus/w1/devices')
    if not base.exists():
        return sensors
    for dev in base.iterdir():
        if not dev.name.startswith('28-'):
            continue
        try:
            raw = int((dev / 'temperature').read_text().strip())
            sensors.append({'id': dev.name, 'temp': raw / 1000})
        except (OSError, ValueError):
            pass
    return sensors


def get_log_usage() -> str:
    out = run(['df', '-h', str(LOG_DIR)])
    lines = out.splitlines()
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 5:
            return f'{parts[2]} used / {parts[1]} total ({parts[4]})'
    return '?'


def get_sync_targets() -> list[str]:
    """Return names of configured VPS sync targets."""
    return [k.stem.removeprefix('sync_key_')
            for k in sorted(CONFIG_DIR.glob('sync_key_*'))]


def get_sync_targets_full() -> dict[str, dict]:
    """Return {name: config_dict} for all targets in sync.d/."""
    targets: dict[str, dict] = {}
    if SYNC_D_DIR.exists():
        for conf_file in sorted(SYNC_D_DIR.glob('*.conf')):
            targets[conf_file.stem] = _load_conf_file(conf_file)
    return targets


def is_prebaked_sync_target(name: str) -> bool:
    """Return True if this sync target was pre-baked (key pair exists + SYNC_REMOTE_PATH=.)."""
    key_path = CONFIG_DIR / f'sync_key_{name}'
    pub_path = CONFIG_DIR / f'sync_key_{name}.pub'
    if not (key_path.exists() and pub_path.exists()):
        return False
    conf_file = SYNC_D_DIR / f'{name}.conf'
    if conf_file.exists():
        cfg = _load_conf_file(conf_file)
        return cfg.get('sync_remote_path', '') == '.'
    return False


def _load_conf_file(conf_file: Path) -> dict:
    config: dict[str, str] = {}
    try:
        for line in conf_file.read_text().splitlines():
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                key, value = line.split('=', 1)
                config[key.strip().lower()] = value.strip().strip('"\'')
    except Exception:
        pass
    return config


def load_wifi_conf() -> dict[str, str]:
    """Load all key=value pairs from wifi.conf."""
    config: dict[str, str] = {}
    if not WIFI_CONF.exists():
        return config
    try:
        for line in WIFI_CONF.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                config[k.strip()] = v.strip().strip('"\'')
    except Exception:
        pass
    return config


def save_wifi_conf_fields(fields: dict[str, str]) -> None:
    """Update/add specific key=value fields in wifi.conf, preserving others."""
    existing: dict[str, str] = {}
    lines_order: list[str] = []
    if WIFI_CONF.exists():
        for line in WIFI_CONF.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                existing[k.strip()] = v.strip()
                lines_order.append(k.strip())
            else:
                lines_order.append(line)
    # Update or add fields
    for k, v in fields.items():
        existing[k] = f'"{v}"'
        if k not in lines_order:
            lines_order.append(k)
    # Rebuild file
    out_lines = []
    seen_keys: set[str] = set()
    for item in lines_order:
        if item in existing and item not in seen_keys:
            out_lines.append(f'{item}={existing[item]}')
            seen_keys.add(item)
        elif item not in existing:
            out_lines.append(item)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    WIFI_CONF.write_text('\n'.join(out_lines) + '\n')
    WIFI_CONF.chmod(0o600)


def get_wifi_ssh_state() -> str:
    """Return 'enabled' or 'disabled'."""
    try:
        return WIFI_SSH_FILE.read_text().strip().lower()
    except OSError:
        return 'disabled'


def toggle_wifi_ssh() -> str:
    """Toggle WiFi SSH iptables rule. Returns new state.

    Rule uses --ctstate NEW (matching provision.sh and wifi_ssh_init.sh) so
    that toggling disable does not drop established SSH sessions.  Persistence
    is via /etc/otgw/wifi-ssh (p3) read by wifi_ssh_init.service at boot.
    """
    current = get_wifi_ssh_state()
    enable = (current == 'disabled')
    _drop = ['iptables', '-D', 'INPUT', '-i', 'wlan+', '-p', 'tcp',
             '--dport', '22', '-m', 'conntrack', '--ctstate', 'NEW', '-j', 'DROP']
    _check = ['iptables', '-C', 'INPUT', '-i', 'wlan+', '-p', 'tcp',
              '--dport', '22', '-m', 'conntrack', '--ctstate', 'NEW', '-j', 'DROP']
    _add = ['iptables', '-A', 'INPUT', '-i', 'wlan+', '-p', 'tcp',
            '--dport', '22', '-m', 'conntrack', '--ctstate', 'NEW', '-j', 'DROP']
    if enable:
        subprocess.run(_drop, capture_output=True)
    else:
        r = subprocess.run(_check, capture_output=True)
        if r.returncode != 0:
            subprocess.run(_add, capture_output=True)
    new_state = 'enabled' if enable else 'disabled'
    try:
        WIFI_SSH_FILE.write_text(new_state + '\n')
    except OSError:
        pass
    return new_state



def get_config_ap_pw() -> bool:
    """Return True if the config AP is password-protected."""
    return CONFIG_AP_PW_FILE.exists()


def set_config_ap_pw(enable: bool) -> None:
    """Enable or disable WPA2 password on the config AP."""
    if enable:
        try:
            CONFIG_AP_PW_FILE.touch()
        except OSError:
            pass
    else:
        try:
            CONFIG_AP_PW_FILE.unlink(missing_ok=True)
        except OSError:
            pass


# USB VIDs that identify P1 dongle hardware (Espressif native USB).
_P1_DONGLE_VIDS = frozenset({'303a'})  # Espressif Systems (ESP32-S3, ESP32-C3 native USB)

# USB VIDs that identify OTGW USB-to-serial adapters.
_OTGW_ADAPTER_VIDS = frozenset({
    '1a86',  # QinHeng Electronics CH340/CH341
    '10c4',  # Silicon Labs CP2102/CP2104
    '0403',  # FTDI FT232
    '067b',  # Prolific PL2303
})


def _usb_vid(path: str) -> str:
    """Return lower-case USB vendor ID for a /dev/tty* device, or ''."""
    try:
        out = subprocess.run(
            ['udevadm', 'info', '--query=property', f'--name={path}'],
            capture_output=True, text=True, timeout=2,
        ).stdout
        for line in out.splitlines():
            if line.startswith('ID_VENDOR_ID='):
                return line.split('=', 1)[1].strip().lower()
    except Exception:
        pass
    return ''


def classify_serial_device(path: str) -> str:
    """Return 'p1_dongle', 'otgw', or 'unknown' for a /dev/tty* device.

    Uses USB VID from udevadm as the primary signal; falls back to the
    device-node name (ttyACM → P1 dongle, ttyUSB → OTGW adapter).
    """
    vid = _usb_vid(path)
    if vid in _P1_DONGLE_VIDS:
        return 'p1_dongle'
    if vid in _OTGW_ADAPTER_VIDS:
        return 'otgw'
    # Fallback heuristic: USB CDC (ttyACM) → P1 dongle; USB-serial (ttyUSB) → OTGW
    name = os.path.basename(path)
    if name.startswith('ttyACM'):
        return 'p1_dongle'
    if name.startswith('ttyUSB'):
        return 'otgw'
    return 'unknown'


def find_p1_dongle() -> str:
    """Return first serial device classified as a P1 dongle, or ''.

    Prefers /dev/ttyACM* (ESP32 native USB) and uses USB VID to confirm.
    Falls back to /dev/ttyUSB* only if classified as p1_dongle (not OTGW).
    """
    for pattern in ['/dev/ttyACM*', '/dev/ttyUSB*']:
        for dev in sorted(_glob.glob(pattern)):
            if classify_serial_device(dev) in ('p1_dongle', 'unknown'):
                return dev
    return ''


def find_otgw_device() -> str:
    """Return first serial device classified as an OTGW adapter, or ''.

    Prefers /dev/ttyUSB* (CH341/CP210x) and uses USB VID to confirm.
    Falls back to /dev/ttyACM* only if classified as otgw.
    """
    for pattern in ['/dev/ttyUSB*', '/dev/ttyACM*']:
        for dev in sorted(_glob.glob(pattern)):
            if classify_serial_device(dev) == 'otgw':
                return dev
    return ''


def send_p1_config(serial_port: str, config: dict, timeout: int = 2) -> tuple[bool, str]:
    """Send WiFi/MQTT config to P1 dongle via serial.
    
    Args:
        serial_port: Path to serial device (e.g., '/dev/ttyACM0')
        config: Dictionary with config parameters
        timeout: Serial timeout in seconds
    
    Returns:
        (success: bool, message: str)
    """
    try:
        # Open serial connection at 115200 baud (USB CDC default)
        with serial.Serial(serial_port, 115200, timeout=timeout) as ser:
            # Build P1CONFIG command
            command = f"P1CONFIG:{json.dumps(config)}"
            
            # Send command with newline
            ser.write(command.encode() + b'\n')
            
            # Wait for response
            time.sleep(0.5)
            response = ser.readline().decode().strip()
            
            if response == "P1CONFIG:OK":
                return True, "Configuration sent successfully"
            elif response.startswith("P1CONFIG:ERROR:"):
                error_msg = response[15:]  # Extract error message
                return False, f"Configuration error: {error_msg}"
            elif response:
                return False, f"Unexpected response: {response}"
            else:
                return False, "No response from dongle"
                
    except serial.SerialException as e:
        return False, f"Serial communication error: {str(e)}"
    except (TypeError, ValueError) as e:
        return False, f"Configuration encoding error: {str(e)}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"


def validate_p1_config(config: dict) -> tuple[bool, str]:
    """Validate P1 configuration before sending.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        (valid: bool, message: str)
    """
    # Required field
    if 'wifi_ssid' not in config or not config['wifi_ssid']:
        return False, "WiFi SSID is required"
    
    # Validate SSID length
    if len(config['wifi_ssid']) > 32:
        return False, "WiFi SSID too long (max 32 characters)"
    
    # Validate password if provided
    if 'wifi_password' in config and config['wifi_password']:
        if len(config['wifi_password']) > 64:
            return False, "WiFi password too long (max 64 characters)"
    
    # Validate MQTT server if provided
    if 'mqtt_server' in config and config['mqtt_server']:
        if len(config['mqtt_server']) > 64:
            return False, "MQTT server too long (max 64 characters)"
    
    # Validate MQTT port if provided
    if 'mqtt_port' in config:
        try:
            port = int(config['mqtt_port'])
            if not (1 <= port <= 65535):
                return False, "MQTT port must be between 1 and 65535"
        except ValueError:
            return False, "MQTT port must be a valid number"
    
    # Validate MQTT topic if provided
    if 'mqtt_topic' in config and config['mqtt_topic']:
        if len(config['mqtt_topic']) > 128:
            return False, "MQTT topic too long (max 128 characters)"
    
    return True, "Configuration is valid"


def handle_send_p1_config(form_data: dict) -> tuple[str, str]:
    """Handle P1 config send request from web form.
    
    Returns:
        (status: 'success'|'error', message: str)
    """
    # Get P1 dongle device
    p1_device = find_p1_dongle()
    
    if not p1_device:
        # Try to find any USB device as fallback
        all_devs = sorted(_glob.glob('/dev/ttyACM*') + _glob.glob('/dev/ttyUSB*'))
        if all_devs:
            p1_device = all_devs[0]
        else:
            return 'error', 'No P1 dongle or USB device detected'
    
    # Get current WiFi config from Pi
    wifi_conf = load_wifi_conf()
    image_id = get_image_id() or 'unknown'
    
    # Build config to send
    config = {
        'wifi_ssid': form_data.get('ssid', wifi_conf.get('ssid', ''))
    }
    
    # Add password if provided
    wifi_password = form_data.get('password', '')
    if wifi_password:
        config['wifi_password'] = wifi_password
    
    # Add MQTT config
    config['mqtt_server'] = form_data.get('broker', wifi_conf.get('P1_BROKER', socket.gethostname() + '.local'))
    config['mqtt_port'] = int(form_data.get('port', 1883))
    config['mqtt_topic'] = form_data.get('topic', f'home/{image_id}/energy/gas')
    
    # Validate config
    valid, val_msg = validate_p1_config(config)
    if not valid:
        return 'error', val_msg
    
    # Send config to dongle
    success, message = send_p1_config(p1_device, config)
    
    if success:
        return 'success', message
    else:
        return 'error', message


def get_sync_pub_key() -> str:
    """Return the first public key content found in /etc/otgw/sync_key_*.pub."""
    for pub in sorted(CONFIG_DIR.glob('sync_key_*.pub')):
        try:
            return pub.read_text().strip()
        except OSError:
            pass
    return ''


def get_sync_pub_key_paths() -> list[Path]:
    return sorted(CONFIG_DIR.glob('sync_key_*.pub'))


REQUIRED_PACKAGES = [
    'mosquitto',
    'mosquitto-clients',
    'python3-paho-mqtt',
    'python3-serial',
    'python3-lgpio',
    'rsync',
    'avahi-daemon',
    'dosfstools',
    'parted',
]


def check_missing_packages() -> list[str]:
    missing = []
    for pkg in REQUIRED_PACKAGES:
        r = subprocess.run(
            ['dpkg', '-s', pkg],
            capture_output=True,
        )
        if r.returncode != 0:
            missing.append(pkg)
    return missing


# ── log partition ─────────────────────────────────────────────────────────────

def get_log_partition_info() -> dict:
    """Check whether /var/log/otgw is mounted on a FAT32 partition."""
    try:
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == '/var/log/otgw':
                    return {
                        'mounted': True,
                        'fstype': parts[2],
                        'device': parts[0],
                    }
    except Exception:
        pass
    return {'mounted': False, 'fstype': 'unknown', 'device': None}


def is_log_partition_fat32() -> bool:
    lp = get_log_partition_info()
    return lp['mounted'] and lp['fstype'] == 'vfat'


# ── recent readings from log files ───────────────────────────────────────────

def _read_last_line(path: Path) -> str:
    """Read the last non-empty line of a file without loading the whole thing."""
    try:
        size = path.stat().st_size
        if size == 0:
            return ''
        with open(path, 'rb') as f:
            f.seek(max(0, size - 512))
            tail = f.read().decode('utf-8', errors='replace')
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        return lines[-1] if lines else ''
    except OSError:
        return ''


def _parse_log_ts(line: str) -> float | None:
    """Parse Unix timestamp from log line 'ISO8601 | topic : payload'."""
    if '|' not in line:
        return None
    ts_str = line.split('|')[0].strip()
    try:
        return datetime.fromisoformat(ts_str).timestamp()
    except (ValueError, TypeError):
        return None


def _find_gas_files(
    image_id: str, date: str, bases: list[Path]
) -> list[Path]:
    """Return gas log file paths for image_id + date.

    Tries exact name first; falls back to glob *gas*{image_id}*{date}*.log
    to handle variant naming like home_<image_id>_energy_gas_<image_id>_<date>.log.
    """
    found: list[Path] = []
    exact_name = f'gas_{image_id}_{date}.log'
    for base in bases:
        exact = base / exact_name
        if exact.exists():
            found.append(exact)
            continue
        # Glob fallback
        for p in sorted(base.glob(f'*gas*{image_id}*{date}*.log')):
            found.append(p)
        # Also try *{date}*gas*.log pattern
        if not found:
            for p in sorted(base.glob(f'*{date}*gas*.log')):
                found.append(p)
    return found


def get_recent_readings() -> dict[str, float | None]:
    """Return Unix timestamp of last reading for each data source.

    Checks the live MQTT cache first (instant, no file I/O), then falls back
    to RAM-disk log files, then today's/yesterday's disk log files.

    For 'gas', uses a glob fallback to handle variant file naming.
    """
    result: dict[str, float | None] = {
        'otgw': None, 'gas': None, 'temperature': None,
    }

    # Fast path: per-category timestamp dict — updated on every message,
    # never evicted by high-frequency topics (unlike the 20-msg deque).
    for key, ts in _mqtt_cache.get_latest_reading_ts().items():
        if ts is not None:
            result[key] = ts
    if all(v is not None for v in result.values()):
        return result

    # Slow path: log files (for keys not found in MQTT cache)
    image_id = get_image_id()
    if image_id == 'unknown':
        return result

    today     = time.strftime('%Y-%m-%d')
    yesterday = time.strftime('%Y-%m-%d', time.localtime(time.time() - 86400))

    for key in result:
        if result[key] is not None:
            continue  # already found via MQTT cache
        for date in (today, yesterday):
            bases = [RAM_DIR / image_id, LOG_DIR / image_id]
            best: float | None = None

            if key == 'gas':
                files = _find_gas_files(image_id, date, bases)
                for fpath in files:
                    ts = _parse_log_ts(_read_last_line(fpath))
                    if ts is not None and (best is None or ts > best):
                        best = ts
            else:
                fname = f'{key}_{image_id}_{date}.log'
                for base in bases:
                    ts = _parse_log_ts(_read_last_line(base / fname))
                    if ts is not None and (best is None or ts > best):
                        best = ts

            if best is not None:
                result[key] = best
                break   # found today's data; no need for yesterday

    return result


def format_age(ts: float | None) -> str:
    """Format seconds-since-reading as '42 s', 'HH:MM:SS', or 'DX HH:MM:SS'."""
    if ts is None:
        return '—'
    age = max(0, int(time.time() - ts))
    if age < 60:
        return f'{age} s'
    h, rem = divmod(age, 3600)
    m, s   = divmod(rem, 60)
    if age < 86400:
        return f'{h:02d}:{m:02d}:{s:02d}'
    d      = age // 86400
    h2, r2 = divmod(age % 86400, 3600)
    m2, s2 = divmod(r2, 60)
    return f'D{d} {h2:02d}:{m2:02d}:{s2:02d}'


def age_css(ts: float | None, ok_s: int, warn_s: int) -> str:
    if ts is None:
        return 'age-none'
    age = time.time() - ts
    if age < ok_s:
        return 'age-ok'
    if age < warn_s:
        return 'age-warn'
    return 'age-old'


def run_checks() -> list[dict]:
    """Run full system checks — matches TUI verify-systems output."""
    results = []

    def chk(name: str, ok: bool, detail: str = '', warn: bool = False) -> None:
        results.append({
            'name':   name,
            'status': 'pass' if ok else ('warn' if warn else 'fail'),
            'detail': detail,
        })

    # Core services
    for svc in ['mosquitto', 'mqtt-logger', 'otgw-listener', 'ds18b20-listener']:
        chk(f'Service: {svc}', get_service_status(svc) == 'active')

    # Support services
    for svc in ['wifi-switch', 'config-sensor', 'led-status']:
        active = get_service_status(svc) == 'active'
        chk(f'Service: {svc}', active, warn=not active)

    # WiFi connectivity
    wifi = get_wifi_info()
    ssid = wifi.get('ssid', '')
    wifi_state_val = wifi.get('wifi_state', '')
    if wifi_state_val == 'CASE_A' or NO_HOME_WIFI_FILE.exists():
        # Case A: Pi is its own AP — no home WiFi by design
        device_id = (DEVICE_ID_FILE.read_text().strip()
                     if DEVICE_ID_FILE.exists() else 'unknown')
        chk('WiFi connected', True, f'Case A — Pi AP: otgw-{device_id}')
        chk('Internet via WiFi', False, 'Case A — no internet (expected)', warn=True)
    elif wifi_state_val == 'WAITING':
        # No credentials configured — not an error, just not set up yet
        chk('WiFi connected', False, 'not configured', warn=True)
        chk('Internet via WiFi', False, 'WiFi not configured', warn=True)
    elif wifi.get('connecting'):
        chk('WiFi connected', True, f'Connecting to {ssid}…', warn=True)
        chk('Internet via WiFi', False, 'WiFi connecting…', warn=True)
    else:
        chk('WiFi connected', bool(ssid), ssid or 'not connected')
        # Internet reachability via WiFi (matters for the install, not ethernet)
        if ssid:
            ping = run(['ping', '-I', 'wlan0', '-c', '1', '-W', '3', '8.8.8.8'],
                       timeout=8)
            ok_ping = '1 received' in ping
            chk('Internet via WiFi', ok_ping,
                'OK' if ok_ping else 'no route via wlan0',
                warn=not ok_ping)
        else:
            chk('Internet via WiFi', False, 'WiFi not connected', warn=True)

    # DS18B20 sensors — 2 required
    sensors = get_ds18b20()
    chk('DS18B20 sensors', len(sensors) >= 2,
        f'{len(sensors)} found (2 required)', warn=len(sensors) == 1)

    # USB serial devices — OTGW adapter and P1 dongle are distinct device types
    # Pre-fetch readings to cross-reference data flow for the OTGW device check.
    readings = get_recent_readings()
    otgw_dev = find_otgw_device()
    p1_dev   = find_p1_dongle()
    if otgw_dev:
        chk('OTGW serial device', True, otgw_dev)
    else:
        otgw_ts = readings.get('otgw')
        otgw_age = (time.time() - otgw_ts) if otgw_ts else None
        if otgw_age is not None and otgw_age < READING_THRESHOLDS['otgw'][1]:
            # Device path not identifiable by VID, but data is flowing
            chk('OTGW serial device', True,
                'data flowing (device path not identified by VID)', warn=False)
        else:
            chk('OTGW serial device', False, 'not found', warn=True)
    if p1_dev:
        p1_type = classify_serial_device(p1_dev)
        if p1_type == 'otgw':
            chk('P1 dongle', False,
                f'found {p1_dev} but it looks like an OTGW adapter', warn=True)
        else:
            chk('P1 dongle', True, p1_dev, warn=False)
    else:
        # Check if gas data is flowing even without device detection
        gas_ts = readings.get('gas')
        gas_age = (time.time() - gas_ts) if gas_ts else None
        if gas_age is not None and gas_age < READING_THRESHOLDS['gas'][1]:
            # Device path not identifiable by VID, but data is flowing
            chk('P1 dongle', True,
                'data flowing (device path not identified by VID)', warn=False)
        else:
            chk('P1 dongle', False, 'not found', warn=True)

    # Recent data checks (readings already fetched above)
    for key, label, (ok_s, warn_s) in [
        ('otgw',        'OTGW data',        READING_THRESHOLDS['otgw']),
        ('gas',         'Gas (P1) data',    READING_THRESHOLDS['gas']),
        ('temperature', 'Temperature data', READING_THRESHOLDS['temperature']),
    ]:
        ts = readings[key]
        if ts is None:
            chk(f'Recent {label}', False, 'no data', warn=True)
        else:
            age = time.time() - ts
            ok = age < ok_s
            chk(f'Recent {label}', ok,
                f'{format_age(ts)} ago', warn=not ok and age < warn_s)

    # Log partition
    chk('Log partition mounted',
        bool(run(['findmnt', '-n', str(LOG_DIR)])),
        'FAT32')

    # Disk space
    out = run(['df', '-k', str(LOG_DIR)])
    lines = out.splitlines()
    disk_ok = True
    disk_detail = '?'
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 4:
            avail_mb = int(parts[3]) // 1024
            pct      = parts[4].rstrip('%') if len(parts) >= 5 else '0'
            disk_detail = f'{avail_mb} MB free ({pct}% used)'
            disk_ok = avail_mb > 50 and int(pct) < 90
    chk('Disk space', disk_ok, disk_detail, warn=not disk_ok)

    # Image ID and Pi ID
    image_id = get_image_id()
    chk('Image ID set', image_id != 'unknown', image_id)
    pi_id = get_device_id()
    chk('Pi ID set', pi_id != 'unknown', pi_id)

    # wifi.conf
    chk('wifi.conf present', WIFI_CONF.exists())

    # VPS sync keys
    keys = get_sync_targets()
    chk('VPS sync key', len(keys) > 0,
        ', '.join(keys) if keys else 'none configured', warn=len(keys) == 0)

    # VPS log sync — verify rsync actually succeeded recently
    sync_ok, sync_detail = _check_log_sync_recent()
    chk('VPS log sync', sync_ok, sync_detail, warn=not sync_ok)

    return results


def _check_log_sync_recent(max_age_s: int = 300) -> tuple[bool, str]:
    """Return (ok, detail) — True if log-sync completed successfully recently.

    Queries systemd for the last InactiveEnterTimestamp and Result of the
    log-sync oneshot service.  A successful run within max_age_s counts as ok.
    A run that exited non-zero, or no run within the window, is a warning.
    """
    props = run([
        'systemctl', 'show', 'log-sync.service',
        '--property=Result,InactiveEnterTimestamp',
    ])
    result    = ''
    ts_str    = ''
    for line in props.splitlines():
        if line.startswith('Result='):
            result = line.split('=', 1)[1].strip()
        elif line.startswith('InactiveEnterTimestamp='):
            ts_str = line.split('=', 1)[1].strip()

    if not ts_str or ts_str == 'n/a':
        return False, 'no sync run recorded yet'

    # Parse "Day YYYY-MM-DD HH:MM:SS TZ" from systemd
    import datetime as _dt
    try:
        # systemd format: "Sat 2026-04-04 12:34:56 CEST"
        parts = ts_str.split()
        ts = _dt.datetime.strptime(f'{parts[1]} {parts[2]}', '%Y-%m-%d %H:%M:%S')
        # Treat as local time for age calculation (monotonic not available here)
        age_s = int((_dt.datetime.now() - ts).total_seconds())
    except (ValueError, IndexError):
        return False, f'could not parse timestamp: {ts_str}'

    if result != 'success':
        return False, f'last sync failed (result={result}, {age_s}s ago)'

    if age_s > max_age_s:
        return False, f'last success {age_s}s ago (>{max_age_s}s threshold)'

    age_m = age_s // 60
    detail = f'{age_s}s ago' if age_m == 0 else f'{age_m}m ago'
    return True, f'OK — {detail}'


def write_signoff(checks: list[dict]) -> Path:
    """Write timestamped sign-off JSON to the log directory."""
    image_id = get_image_id()
    ts    = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    total  = len(checks)
    passed = sum(1 for c in checks if c['status'] == 'pass')
    warned = sum(1 for c in checks if c['status'] == 'warn')
    failed = sum(1 for c in checks if c['status'] == 'fail')
    overall = 'fail' if failed else ('warn' if warned else 'pass')
    record = {
        'timestamp': ts,
        'image_id':  image_id,
        'total':     total,
        'passed':    passed,
        'warned':    warned,
        'failed':    failed,
        'overall':   overall,
        'checks':    checks,
    }
    tag      = ts.replace(':', '').replace('-', '')
    filename = f'signoff_{image_id}_{tag}.json'
    out_dir  = LOG_DIR / image_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    out_path.write_text(json.dumps(record, indent=2) + '\n')
    log.info('Sign-off written: %s (%d/%d pass)', out_path, passed, total)
    return out_path


def save_wifi(ssid: str, password: str) -> tuple[bool, str]:
    """Save credentials to wifi.conf and connect wlan0 to home WiFi."""
    try:
        WIFI_CONF.write_text(f'SSID="{ssid}"\nPASSWORD="{password}"\n')
        WIFI_CONF.chmod(0o600)
    except OSError as exc:
        log.error('Failed to write wifi.conf: %s', exc)
        return False, str(exc)

    r = subprocess.run(['nmcli', 'con', 'show', ssid], capture_output=True)
    if r.returncode == 0:
        subprocess.run(['nmcli', 'con', 'modify', ssid,
                        'wifi-sec.key-mgmt', 'wpa-psk',
                        'wifi-sec.psk', password], capture_output=True)
    else:
        subprocess.run(['nmcli', 'con', 'add',
                        'type', 'wifi', 'ifname', 'wlan0',
                        'con-name', ssid, 'ssid', ssid,
                        'wifi-sec.key-mgmt', 'wpa-psk',
                        'wifi-sec.psk', password], capture_output=True)

    dual_mode = Path(f'/sys/class/net/{VIRTUAL_IFACE}').exists()

    if dual_mode:
        subprocess.run(['nmcli', 'con', 'up', ssid], capture_output=True)
    else:
        CONFIG_ACTIVE.unlink(missing_ok=True)
        subprocess.run(['nmcli', 'con', 'down', 'otgw-config-ap'],
                       capture_output=True)
        subprocess.run(['nmcli', 'con', 'up', ssid], capture_output=True)

    subprocess.run(['systemctl', 'restart', 'wifi-switch'], capture_output=True)

    time.sleep(2)
    new_ip = ''
    out = run(['nmcli', '-t', '-f', 'IP4.ADDRESS', 'dev', 'show', 'wlan0'])
    for line in out.splitlines():
        if line.startswith('IP4.ADDRESS'):
            new_ip = line.split(':')[-1].split('/')[0]
            break

    return True, new_ip


def activate_case_a() -> None:
    """Write the Case A flag file and restart wifi-switch."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        NO_HOME_WIFI_FILE.write_text('')
        NO_HOME_WIFI_FILE.chmod(0o644)
    except OSError as exc:
        log.error('Failed to write Case A flag: %s', exc)
    subprocess.run(['systemctl', 'restart', 'wifi-switch'], capture_output=True)


def deactivate_case_a() -> None:
    """Remove the Case A flag file and restart wifi-switch."""
    try:
        NO_HOME_WIFI_FILE.unlink(missing_ok=True)
    except OSError as exc:
        log.error('Failed to remove Case A flag: %s', exc)
    subprocess.run(['systemctl', 'restart', 'wifi-switch'], capture_output=True)


def forget_wifi() -> None:
    """Clear saved WiFi credentials and disconnect wlan0 from home network."""
    ssid = ''
    try:
        conf = load_wifi_conf()
        ssid = conf.get('ssid', '')
    except Exception:
        pass
    # Remove nmcli connection profile for this SSID
    if ssid:
        subprocess.run(['nmcli', 'con', 'delete', ssid],
                       capture_output=True)
    # Clear wifi.conf
    try:
        WIFI_CONF.write_text('')
    except OSError:
        pass


# ── HTML helpers ──────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; background: #f0f4f0;
       color: #1a2e1a; max-width: 520px; margin: 0 auto; }
header { background: #2d5a2d; color: white; padding: 12px 16px;
         display: flex; align-items: center; gap: 12px; }
header h1 { font-size: 1.2rem; flex: 1; }
header .badge { background: #1a3a1a; padding: 2px 8px; border-radius: 12px;
                font-size: 0.75rem; }
header .badge.test { background: #7a5a00; }
nav { background: #3d7a3d; display: flex; gap: 0; flex-wrap: wrap; }
nav a { color: #c8e6c8; padding: 10px 10px; text-decoration: none;
        font-size: 0.8rem; text-align: center; white-space: nowrap; }
nav a:hover, nav a.active { background: #2d5a2d; color: white; }
.card { background: white; margin: 12px; border-radius: 8px;
        box-shadow: 0 1px 4px rgba(0,0,0,.1); overflow: hidden; }
.card-title { background: #e8f5e8; padding: 10px 14px;
              font-weight: 600; font-size: 0.9rem; color: #2d5a2d;
              border-bottom: 1px solid #c8e6c8; }
.card-title a { color: #2d5a2d; text-decoration: none; }
.card-title a:hover { text-decoration: underline; }
.card-body { padding: 12px 14px; }
.row { display: flex; justify-content: space-between; align-items: center;
       padding: 6px 0; border-bottom: 1px solid #f0f0f0; }
.row:last-child { border-bottom: none; }
.label { color: #555; font-size: 0.85rem; }
.value { font-size: 0.85rem; font-weight: 500; }
.badge-active { color: #2d5a2d; }
.badge-inactive { color: #c0392b; }
.badge-unknown { color: #888; }
.badge-ap { background: #3d7a3d; color: white; padding: 2px 8px;
            border-radius: 12px; font-size: 0.75rem; }
.age-ok   { color: #2d5a2d; font-weight: 600; }
.age-warn { color: #e67e22; font-weight: 600; }
.age-old  { color: #c0392b; font-weight: 600; }
.age-none { color: #888; }
form { padding: 12px 14px; }
label { display: block; margin-bottom: 4px; font-size: 0.85rem; color: #555; }
input[type=text], input[type=password], select, textarea {
  width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 6px;
  font-size: 1rem; margin-bottom: 12px; }
textarea { font-family: monospace; font-size: 0.8rem; resize: vertical; }
.show-pw { font-size: 0.8rem; color: #3d7a3d; cursor: pointer; margin-top: -8px;
            margin-bottom: 10px; display: block; }
button, .btn {
  display: block; width: 100%; padding: 12px; border: none; border-radius: 6px;
  font-size: 1rem; font-weight: 600; cursor: pointer; margin-bottom: 8px;
  text-decoration: none; text-align: center; }
.btn-primary { background: #2d5a2d; color: white; }
.btn-primary:hover { background: #1a3a1a; }
.btn-secondary { background: #e8f5e8; color: #2d5a2d; border: 1px solid #c8e6c8; }
.btn-danger { background: #c0392b; color: white; }
.btn-danger:hover { background: #962d22; }
.btn-warn { background: #e67e22; color: white; }
.btn-sm { display: inline-block; width: auto; padding: 6px 12px;
          font-size: 0.85rem; margin: 2px; }
.check-pass { color: #2d5a2d; font-weight: 600; }
.check-fail { color: #c0392b; font-weight: 600; }
.check-warn { color: #e67e22; font-weight: 600; }
.temp { font-size: 1.1rem; font-weight: 600; color: #2d5a2d; }
.banner { margin: 12px; padding: 10px 14px; border-radius: 0 6px 6px 0;
           font-size: 0.85rem; line-height: 1.5; }
.banner-ap-dual { background: #e8f5e8; border-left: 4px solid #2d5a2d; }
.banner-ap-single { background: #fff3cd; border-left: 4px solid #f0a000; }
.banner-row { display: flex; justify-content: space-between; align-items: center; }
.msg-ok  { background: #e8f5e8; border-left: 4px solid #2d5a2d;
            margin: 12px; padding: 10px 14px; border-radius: 0 6px 6px 0; }
.msg-err { background: #fdecea; border-left: 4px solid #c0392b;
            margin: 12px; padding: 10px 14px; border-radius: 0 6px 6px 0; }
.mqtt-table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
.mqtt-table th { background: #e8f5e8; color: #2d5a2d; padding: 6px 8px;
                  text-align: left; font-size: 0.8rem; }
.mqtt-table td { padding: 5px 8px; border-bottom: 1px solid #f0f0f0;
                  word-break: break-all; vertical-align: top; }
.mqtt-table tr:last-child td { border-bottom: none; }
.status-dot { display: inline-block; width: 8px; height: 8px;
              border-radius: 50%; margin-right: 4px; }
.dot-green { background: #2d5a2d; }
.dot-red   { background: #c0392b; }
.lang-toggle { font-size: 0.75rem; color: #c8e6c8; text-decoration: none;
               padding: 2px 6px; border: 1px solid #c8e6c8; border-radius: 4px; }
.lang-toggle:hover { background: rgba(255,255,255,0.2); }
pre { background: #f8f8f8; padding: 10px; border-radius: 4px; font-size: 0.8rem;
      overflow-x: auto; white-space: pre-wrap; word-break: break-all; }
.radio-group { margin-bottom: 12px; }
.radio-group label { display: flex; align-items: center; gap: 8px;
                     padding: 8px; cursor: pointer; font-size: 0.95rem;
                     color: #1a2e1a; }
.radio-group input[type=radio] { width: auto; margin: 0; }
/* Make key values easy to tap-select and copy on mobile */
.copyable { user-select: all; -webkit-user-select: all; cursor: copy;
            font-family: monospace; font-size: 0.85rem; }
footer { padding: 20px; text-align: center; font-size: 0.75rem; color: #888; }
"""

JS_REFRESH = """
var _ageTs={};  // {key: unix_timestamp_or_null}
var _ageThr={}; // {key: [ok_s, warn_s]}

function fmtAge(ts){
  if(ts===null||ts===undefined) return '\u2014';
  var age=Math.max(0,Math.round(Date.now()/1000-ts));
  if(age<60) return age+' s';
  var h=Math.floor(age/3600),rem=age%3600,m=Math.floor(rem/60),s=rem%60;
  if(age<86400) return (''+h).padStart(2,'0')+':'+(''+m).padStart(2,'0')+':'+(''+s).padStart(2,'0');
  var d=Math.floor(age/86400),h2=Math.floor((age%86400)/3600),r2=(age%86400)%3600,m2=Math.floor(r2/60),s2=r2%60;
  return 'D'+d+' '+(''+h2).padStart(2,'0')+':'+(''+m2).padStart(2,'0')+':'+(''+s2).padStart(2,'0');
}
function ageCss(ts,ok_s,warn_s){
  if(ts===null||ts===undefined) return 'age-none';
  var age=Date.now()/1000-ts;
  if(age<ok_s) return 'age-ok';
  if(age<warn_s) return 'age-warn';
  return 'age-old';
}
function tickAges(){
  ['otgw','gas','temperature'].forEach(function(k){
    var el=document.getElementById('age-'+k);
    if(!el) return;
    var ts=_ageTs[k],thr=_ageThr[k]||[300,900];
    el.textContent=fmtAge(ts);
    el.className=ageCss(ts,thr[0],thr[1]);
  });
}
function refreshStatus() {
  fetch('/status.json').then(r=>r.json()).then(d=>{
    document.querySelectorAll('[data-svc]').forEach(el=>{
      var s=d.services[el.dataset.svc]||'unknown';
      el.textContent=s;
      el.className='value badge-'+((s==='active')?'active':(s==='inactive'?'inactive':'unknown'));
    });
    if(d.wifi.ssid !== undefined && document.getElementById('wifi-ssid'))
      document.getElementById('wifi-ssid').textContent=d.wifi.ssid||'—';
    if(d.wifi.ip !== undefined && document.getElementById('wifi-ip'))
      document.getElementById('wifi-ip').textContent=d.wifi.ip||'—';
    if(d.wifi.wifi_state !== undefined && document.getElementById('wifi-state'))
      document.getElementById('wifi-state').textContent=
        ({'PAUSED':'Connected','CLIENT':'Connected','WAITING':'not configured'}[d.wifi.wifi_state]
         ||d.wifi.wifi_state)||'—';
    if(d.readings){
      ['otgw','gas','temperature'].forEach(function(k){
        if(d.readings[k]!==undefined){
          _ageTs[k]=d.readings[k].ts;
          _ageThr[k]=d.readings[k].thr;
        }
      });
      tickAges();
    }
  }).catch(()=>{});
}
function refreshPortalTimer(){
  var el=document.getElementById('portal-timer');
  if(!el) return;
  fetch('/timer.json').then(function(r){return r.json();}).then(function(d){
    var s=Math.max(0,d.remaining_s);
    var m=Math.floor(s/60),sec=s%60;
    el.textContent=m+':'+String(sec).padStart(2,'0');
    el.style.color=s<60?'#c00':'inherit';
  }).catch(function(){});
}
setInterval(refreshStatus, 2000);
setInterval(tickAges, 1000);
setInterval(refreshPortalTimer, 1000);
refreshStatus();
refreshPortalTimer();
// Sync Pi clock from browser on first load — no-op if NTP is already synced.
(function(){fetch('/api/set-time',{method:'POST',
  headers:{'Content-Type':'application/x-www-form-urlencoded'},
  body:'ts='+Math.round(Date.now()/1000)}).catch(function(){});})();
"""

JS_LIVE_MESSAGES = """
function refreshMessages() {
  fetch('/messages.json').then(r=>r.json()).then(msgs=>{
    var tbody = document.getElementById('live-mqtt-tbody');
    if(!tbody) return;
    if(!msgs || msgs.length === 0){
      tbody.innerHTML = '<tr><td colspan="3" style="color:#888;padding:8px">No messages yet</td></tr>';
      return;
    }
    var rows = msgs.slice(0,10).map(function(m){
      var age = m.age < 60 ? m.age.toFixed(0)+'s' : (m.age/60).toFixed(1)+'m';
      return '<tr><td style="word-break:break-all;max-width:200px">'+escHtml(m.topic)+'</td>'
        +'<td style="word-break:break-all;max-width:160px">'+escHtml(m.payload)+'</td>'
        +'<td style="white-space:nowrap">'+age+'</td></tr>';
    });
    tbody.innerHTML = rows.join('');
  }).catch(()=>{});
}
function escHtml(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
setInterval(refreshMessages, 1000);
refreshMessages();
"""


def page(title: str, body: str, lang: str, active_nav: str = '',
         extra_js: str = '') -> str:
    mode      = get_mode()
    image_id  = get_image_id()
    device_id = get_device_id()
    mode_label = tr('mode_prod' if mode == 'production' else 'mode_test', lang)
    badge_cls  = '' if mode == 'production' else ' test'

    lang_other = 'nl' if lang == 'en' else 'en'
    lang_label = tr('lang_toggle', lang)

    nav_items = [
        ('/wizard', 'nav_wizard'),
        ('/', 'nav_home'),
        ('/wifi', 'nav_wifi'),
        ('/mode', 'nav_mode'),
        ('/checks', 'nav_checks'),
        ('/p1', 'nav_p1'),
        ('/sync', 'nav_sync'),
        ('/live', 'nav_live'),
        ('/simulators', 'nav_simulators'),
    ]
    if not is_log_partition_fat32():
        nav_items.append(('/log-partition', 'nav_log_part'))
    nav_links = ''.join(
        f'<a href="{href}"{" class=active" if active_nav == name else ""}>'
        f'{tr(name, lang)}</a>'
        for href, name in nav_items
    )

    ap_banner = ''
    if CONFIG_ACTIVE.exists():
        wifi = get_wifi_info()
        exit_btn = (
            f'<form method="post" action="/ap/exit" style="display:inline">'
            f'<button type="submit" style="background:none;border:none;'
            f'color:inherit;cursor:pointer;font-size:inherit;'
            f'font-weight:600;padding:0;text-decoration:underline;">'
            f'{tr("ap_exit", lang)}</button></form>'
        )
        ssh_state = get_wifi_ssh_state()
        ssh_btn_label = tr(
            'wifi_ssh_off' if ssh_state == 'enabled' else 'wifi_ssh_on', lang)
        _btn = (
            'background:none;border:none;color:inherit;cursor:pointer;'
            'font-size:inherit;padding:0;text-decoration:underline;'
        )
        timer_row = (
            f'<div style="margin-top:6px;font-size:0.85rem;color:#555">'
            f'{tr("ap_timer_label", lang)}: '
            f'<strong id="portal-timer">…</strong>'
            f' &nbsp;·&nbsp; 🧲 {tr("ap_timer_reset", lang)}'
            f' &nbsp;·&nbsp; '
            f'<form method="post" action="/wifi-ssh/toggle"'
            f' style="display:inline">'
            f'<button type="submit" style="{_btn}">'
            f'{tr("wifi_ssh_title", lang)}: {ssh_btn_label}'
            f'</button></form>'
            f'</div>'
        )
        if wifi.get('dual_mode'):
            home_ip = wifi.get('ip', '?')
            ap_ip   = wifi.get('ap_ip', '10.42.0.1')
            ap_banner = (
                f'<div class="banner banner-ap-dual">'
                f'<div class="banner-row">'
                f'<span>🔗 {tr("ap_dual", lang)}</span>{exit_btn}</div>'
                f'<div style="margin-top:4px;color:#555">'
                f'{tr("wifi_home", lang)}: <strong>{home_ip}</strong> &nbsp;·&nbsp; '
                f'{tr("wifi_ap", lang)}: <strong>{ap_ip}</strong></div>'
                f'{timer_row}'
                f'</div>'
            )
        else:
            ap_banner = (
                f'<div class="banner banner-ap-single">'
                f'<div class="banner-row">'
                f'<span>📶 {tr("ap_single", lang)}</span>{exit_btn}</div>'
                f'{timer_row}'
                f'</div>'
            )

    combined_js = JS_REFRESH
    if extra_js:
        combined_js += '\n' + extra_js

    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — {tr('title', lang)}</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>{tr('title', lang)}</h1>
  <form method="post" action="/lang" style="display:inline;margin:0">
    <input type="hidden" name="lang" value="{lang_other}">
    <button type="submit" class="lang-toggle" style="background:none;border:1px solid #c8e6c8;cursor:pointer">{lang_label}</button>
  </form>
  <span class="badge{badge_cls}">{mode_label}</span>
  <span class="badge copyable" title="Image ID — identifies the firmware build (baked in at image creation)" style="background:#1a3a1a">{image_id}</span>
  <span class="badge copyable" title="Device ID — identifies this physical board (derived from MAC address at first boot)" style="background:#1a2a3a">{device_id}</span>
</header>
<nav>{nav_links}</nav>
{ap_banner}
{body}
<footer>OTGW Logger · Image: <span class="copyable" style="color:inherit">{image_id}</span> · Device: <span class="copyable" style="color:inherit">{device_id}</span> · {tr('version', lang)}: <span class="copyable" style="color:inherit">{get_version()}</span></footer>
<script>{combined_js}</script>
</body>
</html>"""


# ── page builders ─────────────────────────────────────────────────────────────

def build_dashboard(lang: str) -> str:
    wifi     = _checks_cache.get_wifi()
    sensors  = _checks_cache.get_sensors()
    readings = _checks_cache.get_readings()

    # WiFi card — show dual-mode info when applicable
    wifi_ssid  = html.escape(wifi.get('ssid', '—'))
    wifi_ip    = html.escape(wifi.get('ip', '—'))
    wifi_sig   = html.escape(wifi.get('signal', '—') or '—')
    # Show '—' when signal reads '0 %' (NM not yet populated after connect)
    if wifi_sig == '0 %':
        wifi_sig = '—'
    wifi_state = html.escape(wifi.get('wifi_state', '—'))

    extra_rows = ''
    if wifi.get('dual_mode') and wifi.get('ap_ip'):
        extra_rows += (
            f'<div class="row">'
            f'<span class="label">{tr("wifi_ap", lang)} IP</span>'
            f'<span class="value copyable">{html.escape(wifi["ap_ip"])}</span></div>'
        )

    # Services card
    cached_svcs = _checks_cache.get_services()
    svc_rows = ''
    for svc in SERVICES:
        status = cached_svcs.get(svc, 'unknown')
        cls = ('active' if status == 'active'
               else 'inactive' if status == 'inactive' else 'unknown')
        svc_rows += (
            f'<div class="row"><span class="label">{svc}</span>'
            f'<span class="value badge-{cls}" data-svc="{svc}">'
            f'{status}</span></div>'
        )

    # Recent readings card
    reading_rows = ''
    for key, label_key, (ok_s, warn_s) in [
        ('otgw',        'last_otgw',  READING_THRESHOLDS['otgw']),
        ('gas',         'last_gas',   READING_THRESHOLDS['gas']),
        ('temperature', 'last_temp',  READING_THRESHOLDS['temperature']),
    ]:
        ts  = readings[key]
        age = format_age(ts)
        css = age_css(ts, ok_s, warn_s)
        reading_rows += (
            f'<div class="row">'
            f'<span class="label">{tr(label_key, lang)}</span>'
            f'<span id="age-{key}" class="{css}">{age}</span>'
            f'</div>'
        )

    # DS18B20 card
    if sensors:
        sensor_rows = ''.join(
            f'<div class="row"><span class="label">{html.escape(s["id"])}</span>'
            f'<span class="temp">{s["temp"]:.1f} °C</span></div>'
            for s in sensors
        )
    else:
        sensor_rows = (
            f'<div class="row"><span class="label">'
            f'{tr("no_sensors", lang)}</span></div>'
        )

    log_usage = _checks_cache.get_log_usage()
    sync_tgts = get_sync_targets()
    sync_str  = ', '.join(sync_tgts) if sync_tgts else tr('none', lang)

    # WiFi SSH toggle card
    ssh_state = get_wifi_ssh_state()
    ssh_label = tr('wifi_ssh_on' if ssh_state == 'enabled' else 'wifi_ssh_off', lang)
    ssh_cls   = 'btn-warn' if ssh_state == 'enabled' else 'btn-secondary'
    ssh_toggle_label = (
        tr('wifi_ssh_off', lang) if ssh_state == 'enabled'
        else tr('wifi_ssh_on', lang)
    )

    # Live MQTT mini card
    msgs = _mqtt_cache.get_messages()
    conn_dot = (
        '<span class="status-dot dot-green"></span>'
        if _mqtt_cache.connected
        else '<span class="status-dot dot-red"></span>'
    )
    if msgs:
        mqtt_rows_html = ''.join(
            f'<tr>'
            f'<td style="word-break:break-all;max-width:200px">'
            f'{html.escape(m["topic"])}</td>'
            f'<td style="word-break:break-all;max-width:120px">'
            f'{html.escape(m["payload"])}</td>'
            f'<td style="white-space:nowrap">'
            f'{"%.0f"%m["age"]}s</td>'
            f'</tr>'
            for m in msgs[:10]
        )
    else:
        mqtt_rows_html = (
            f'<tr><td colspan="3" style="color:#888;padding:8px">'
            f'{tr("live_no_msgs", lang)}</td></tr>'
        )

    body = f"""
<div class="card">
  <div class="card-body" style="text-align:center">
    <a href="/wizard" class="btn btn-primary" style="margin-bottom:0.5rem;display:block">
      {tr('wizard_title', lang)} →
    </a>
    <p style="font-size:0.82rem;color:#777;margin:0">
      {tr('wizard_subtitle', lang)}
    </p>
  </div>
</div>
<div class="card">
  <div class="card-title">{tr('wifi_status', lang)}</div>
  <div class="card-body">
    <div class="row"><span class="label">{tr('wifi_ssid', lang)}</span>
      <span id="wifi-ssid" class="value copyable">{wifi_ssid}</span></div>
    <div class="row"><span class="label">{tr('wifi_ip', lang)}</span>
      <span id="wifi-ip" class="value copyable">{wifi_ip}</span></div>
    <div class="row"><span class="label">{tr('wifi_rssi', lang)}</span>
      <span class="value">{wifi_sig}</span></div>
    <div class="row"><span class="label">State</span>
      <span id="wifi-state" class="value">{ {'PAUSED': 'Connected', 'CLIENT': 'Connected', 'WAITING': 'not configured'}.get(wifi_state, wifi_state) or '—'}</span></div>
    {extra_rows}
  </div>
</div>
<div class="card">
  <div class="card-title">{tr('recent_data', lang)}</div>
  <div class="card-body">{reading_rows}</div>
</div>
<div class="card">
  <div class="card-title"><a href="/live">{tr('live_mqtt', lang)}</a>
    &nbsp;{conn_dot}</div>
  <div class="card-body" id="live-mqtt-card">
    <table class="mqtt-table">
      <thead><tr>
        <th>{tr('live_topic', lang)}</th>
        <th>{tr('live_payload', lang)}</th>
        <th>{tr('live_age', lang)}</th>
      </tr></thead>
      <tbody id="live-mqtt-tbody">{mqtt_rows_html}</tbody>
    </table>
  </div>
</div>
<div class="card">
  <div class="card-title">{tr('services', lang)}</div>
  <div class="card-body">{svc_rows}</div>
</div>
<div class="card">
  <div class="card-title">{tr('ds18b20', lang)}</div>
  <div class="card-body">{sensor_rows}</div>
</div>
<div class="card">
  <div class="card-title">{tr('log_space', lang)}</div>
  <div class="card-body">
    <div class="row"><span class="label">{tr('log_space', lang)}</span>
      <span class="value">{html.escape(log_usage)}</span></div>
    <div class="row"><span class="label">{tr('sync_targets', lang)}</span>
      <span class="value">{html.escape(sync_str)}</span></div>
    <div class="row"><span class="label">{tr('uptime', lang)}</span>
      <span class="value">{html.escape(get_uptime())}</span></div>
  </div>
</div>
<div class="card">
  <div class="card-title">{tr('wifi_ssh_title', lang)}</div>
  <div class="card-body">
    <div class="row"><span class="label">{tr('wifi_ssh_title', lang)}</span>
      <span class="value {ssh_cls}">{ssh_label}</span></div>
    <form method="post" action="/wifi-ssh/toggle" style="padding:8px 0 0">
      <button type="submit" class="btn {ssh_cls} btn-sm">
        {tr('wifi_ssh_toggle', lang)}</button>
    </form>
  </div>
</div>
<div class="card">
  <div class="card-body">
    <form method="post" action="/reboot"
          onsubmit="return confirm('{tr('confirm_reboot', lang)}')">
      <button type="submit" class="btn btn-warn">{tr('reboot', lang)}</button>
    </form>
    <form method="post" action="/shutdown"
          onsubmit="return confirm('{tr('confirm_shutdown', lang)}')">
      <button type="submit" class="btn btn-danger">{tr('shutdown', lang)}</button>
    </form>
  </div>
</div>"""
    return page(tr('nav_home', lang), body, lang, 'nav_home',
                extra_js=JS_LIVE_MESSAGES)


def _save_and_configure(ssid: str, password: str) -> None:
    """Background thread: save WiFi then auto-configure P1 dongle if present."""
    save_wifi(ssid, password)
    script = Path('/opt/otgw/scripts/auto_p1_config.py')
    if script.exists():
        import subprocess as _sp
        _sp.run([sys.executable, str(script)], timeout=30, capture_output=True)


def build_wifi_connecting(lang: str, ssid: str) -> str:
    body = f"""
<meta http-equiv="refresh" content="8;url=/">
<div class="card">
  <div class="card-title">{tr('wifi_connecting_title', lang)}</div>
  <div class="card-body">
    <div class="row">
      <span class="label">SSID</span>
      <span class="value">{html.escape(ssid)}</span>
    </div>
    <div style="padding:10px 0;font-size:0.85rem;color:#555">
      {tr('wifi_channel_note', lang)}
    </div>
    <div style="font-size:0.85rem;color:#888">{tr('wifi_reload', lang)}</div>
  </div>
</div>"""
    return page(tr('wifi_connecting_title', lang), body, lang, 'nav_wifi')


def _build_case_a_section(lang: str, msg: str = '', err: str = '') -> str:
    """Return the Case A (no home WiFi) view for build_wifi()."""
    device_id = DEVICE_ID_FILE.read_text().strip() if DEVICE_ID_FILE.exists() else 'unknown'
    ssid = f'otgw-{device_id}'
    password = _derive_ap_password()
    is_active = NO_HOME_WIFI_FILE.exists()

    notice = ''
    if msg:
        notice = f'<div class="msg-ok">{html.escape(msg)}</div>'
    elif err:
        notice = f'<div class="msg-err">{html.escape(err)}</div>'

    active_banner = ''
    if is_active:
        active_banner = (
            f'<div class="msg-ok" style="margin-bottom:0.5rem">'
            f'{tr("wifi_case_a_active", lang)}</div>'
        )

    copy_label = tr('wifi_case_a_copy', lang)
    copied_label = tr('wifi_case_a_copied', lang)

    def _copy_row(label_key: str, value: str, field_id: str) -> str:
        return f"""
  <div class="row" style="align-items:center">
    <span class="label">{tr(label_key, lang)}</span>
    <span class="value" style="display:flex;align-items:center;gap:8px">
      <strong id="{field_id}" class="copyable"
              style="font-family:monospace;font-size:1rem;letter-spacing:0.04em">
        {html.escape(value)}
      </strong>
      <button type="button" class="btn btn-secondary"
              style="padding:2px 10px;font-size:0.8rem"
              onclick="navigator.clipboard.writeText('{html.escape(value)}').then(
                function(){{var b=this;b.textContent='{copied_label}';
                  setTimeout(function(){{b.textContent='{copy_label}';}},1500);}}.bind(this))">
        {copy_label}
      </button>
    </span>
  </div>"""

    deactivate_btn = ''
    if is_active:
        deactivate_btn = f"""
<div class="card">
  <div class="card-body">
    <form method="post" action="/wifi">
      <input type="hidden" name="wifi_mode" value="home_reset">
      <button type="submit" class="btn btn-secondary">
        {tr('wifi_case_a_deactivate', lang)}
      </button>
    </form>
  </div>
</div>"""

    activate_btn = ''
    if not is_active:
        activate_btn = f"""
<div class="card">
  <div class="card-body">
    <form method="post" action="/wifi">
      <input type="hidden" name="wifi_mode" value="case_a">
      <input type="hidden" name="case_a_ssid" value="{html.escape(ssid)}">
      <button type="submit" class="btn btn-primary">
        {tr('wifi_case_a_activate', lang)}
      </button>
    </form>
  </div>
</div>"""

    send_p1_btn = f"""
<div class="card">
  <div class="card-body" style="font-size:0.88rem;color:#555">
    <p style="margin-top:0">{tr('wifi_case_a_send_p1', lang)}</p>
    <form method="post" action="/p1/send-case-a-wifi">
      <button type="submit" class="btn btn-secondary">
        {tr('wifi_case_a_send_p1', lang)} →
      </button>
    </form>
  </div>
</div>"""

    recovery_note = (
        f'<div class="card"><div class="card-body" '
        f'style="font-size:0.82rem;color:#666">'
        f'{tr("wifi_case_a_recovery", lang)}'
        f'</div></div>'
    )

    return f"""{notice}
<div class="card">
  <div class="card-title">{tr('wifi_case_a_title', lang)}</div>
  <div class="card-body">
    {active_banner}
    <p style="font-size:0.88rem;color:#555;margin-top:0">
      {tr('wifi_case_a_desc', lang)}
    </p>
    {_copy_row('wifi_case_a_ssid', ssid, 'case-a-ssid')}
    {_copy_row('wifi_case_a_pw', password, 'case-a-pw')}
  </div>
</div>
{activate_btn}
{deactivate_btn}
{send_p1_btn}
{recovery_note}"""


def build_wifi(lang: str, msg: str = '', err: str = '',
               home_ip: str = '') -> str:
    case_a = NO_HOME_WIFI_FILE.exists()

    # If Case A is active, show only the Case A view
    if case_a:
        body = _build_case_a_section(lang, msg=msg, err=err)
        return page(tr('nav_wifi', lang), body, lang, 'nav_wifi')

    saved_ssid = load_wifi_conf().get('ssid', '')
    # Also use the currently connected SSID if available
    wifi_info = _checks_cache.get_wifi()
    current_ssid = wifi_info.get('ssid', '') or saved_ssid
    # Use cached results for instant page load; JS will refresh via /scan.json
    networks = scan_wifi(rescan=False)
    incompatible_label = (
        '5 GHz – not compatible' if lang == 'en' else '5 GHz – niet compatibel'
    )

    def _net_option(n: dict) -> str:
        is_5ghz = n.get('band') == '5'
        label = html.escape(n['ssid'])
        if is_5ghz:
            label += f' ({incompatible_label})'
        else:
            label += f' ({n["signal"]}%)'
        selected = ' selected' if n['ssid'] == current_ssid and not is_5ghz else ''
        disabled = ' disabled' if is_5ghz else ''
        return (
            f'<option value="{html.escape(n["ssid"])}"'
            f'{selected}{disabled}>{label}</option>'
        )

    options = ''.join(_net_option(n) for n in networks)
    notice = ''
    if msg:
        detail = ''
        if home_ip:
            detail = (
                f'<br><small style="color:#555">'
                f'{tr("wifi_ip", lang)}: <strong class="copyable">{html.escape(home_ip)}</strong>'
                f' &mdash; verify connection, then exit config mode.</small>'
            )
        notice = f'<div class="msg-ok">{html.escape(msg)}{detail}</div>'
    elif err:
        notice = f'<div class="msg-err">{html.escape(err)}</div>'

    scan_label    = 'Scanning…'   if lang == 'en' else 'Scannen…'
    updated_label = 'Updated'     if lang == 'en' else 'Bijgewerkt'
    select_label  = '— select —'  if lang == 'en' else '— kies —'
    confirm_msg   = tr('wifi_confirm_forget', lang).replace("'", "\\'")
    forget_card   = (
        f'<div class="card"><div class="card-body">'
        f'<form method="post" action="/wifi/forget"'
        f' onsubmit="return confirm(\'{confirm_msg}\')">'
        f'<button type="submit" class="btn btn-secondary">'
        f'{tr("wifi_forget", lang)}</button></form></div></div>'
        if saved_ssid else ''
    )

    body = f"""
{notice}
<div class="card">
  <div class="card-title">{tr('wifi_config', lang)}</div>
  <form method="post" action="/wifi">
    <div style="padding:12px 14px 0">
      <label>
        {tr('wifi_select', lang)}
        <small id="scan-status" style="color:#888;font-weight:normal;margin-left:6px">
          {scan_label if not networks else ''}
        </small>
      </label>
      <select name="ssid_select" id="ssid_select"
              onchange="document.getElementById('ssid').value=this.value">
        <option value="">{select_label if networks else scan_label}</option>
        {options}
      </select>
      <small style="color:#888;display:block;margin-top:4px">
        {tr('wifi_24ghz_note', lang)}
      </small>
      <label>{tr('wifi_manual', lang)}</label>
      <input type="text" name="ssid" id="ssid" placeholder="SSID"
             value="{html.escape(current_ssid)}"
             autocomplete="off" autocapitalize="off" spellcheck="false">
      <label>{tr('wifi_password', lang)}</label>
      <input type="password" name="password" id="pw" placeholder="••••••••">
      <span class="show-pw"
        onclick="var p=document.getElementById('pw');
                 p.type=p.type==='password'?'text':'password';
                 this.textContent=p.type==='password'?
                   '{'show' if lang=='en' else 'toon'}':'{'hide' if lang=='en' else 'verberg'}'">
        {'show' if lang == 'en' else 'toon'}
      </span>
      <button type="submit" class="btn btn-primary">{tr('wifi_connect', lang)}</button>
    </div>
  </form>
</div>
{forget_card}
<script>
// Refresh scan results in background — keeps page load instant
(function() {{
  var sel = document.getElementById('ssid_select');
  var status = document.getElementById('scan-status');
  fetch('/scan.json').then(function(r) {{ return r.json(); }}).then(function(nets) {{
    var cur = sel.value || document.getElementById('ssid').value;
    sel.innerHTML = '<option value="">{select_label}</option>';
    nets.forEach(function(n) {{
      var o = document.createElement('option');
      var is5ghz = n.band === '5';
      if (is5ghz) {{
        o.value = n.ssid;
        o.textContent = n.ssid + ' ({incompatible_label})';
        o.disabled = true;
      }} else {{
        o.value = n.ssid; o.textContent = n.ssid + ' (' + n.signal + '%)';
      }}
      if (n.ssid === cur) o.selected = true;
      sel.appendChild(o);
    }});
    if (cur) sel.value = cur;
    if (status) status.textContent = '{updated_label} ✓';
  }}).catch(function() {{
    if (status) status.textContent = '';
  }});
}})();
</script>
<div class="card" style="margin-top:1rem">
  <div class="card-body" style="font-size:0.88rem;color:#555">
    <strong>{tr('wifi_mode_case_a', lang)}</strong><br>
    <a href="/wifi/case-a">{tr('wifi_case_a_activate', lang)} →</a>
  </div>
</div>"""
    return page(tr('nav_wifi', lang), body, lang, 'nav_wifi')


def build_mode(lang: str) -> str:
    mode  = get_mode()
    other = 'test' if mode == 'production' else 'production'
    other_label = tr('mode_test' if other == 'test' else 'mode_prod', lang)
    desc  = tr('mode_desc_prod' if mode == 'production' else 'mode_desc_test', lang)

    body = f"""
<div class="card">
  <div class="card-title">{tr('mode_title', lang)}</div>
  <div class="card-body">
    <div class="row"><span class="label">{tr('mode', lang)}</span>
      <span class="value">{tr('mode_prod' if mode=='production' else 'mode_test', lang)}</span>
    </div>
    <div style="padding:8px 0;font-size:0.85rem;color:#555">{desc}</div>
  </div>
  <form method="post" action="/mode" style="padding:0 14px 14px">
    <input type="hidden" name="mode" value="{other}">
    <button type="submit" class="btn btn-secondary">
      {tr('mode_switch', lang)} {other_label}
    </button>
  </form>
</div>"""
    return page(tr('nav_mode', lang), body, lang, 'nav_mode')


# ── setup wizard ─────────────────────────────────────────────────────────────

# Each step definition:
#   id       — unique identifier
#   title_key — STRINGS key for the step title
#   checks   — list of run_checks() name strings that determine step status
#   action   — portal URL to send the user to (None = auto-check only)
#   required — if True, Finish is blocked when this step is not 'pass'/'warn'
#   optional — shown as optional in the UI
_WIZARD_STEPS: list[dict] = [
    {
        'id': 'wifi',
        'title_key': 'wizard_step_wifi',
        'checks': ['WiFi connected'],
        'action': '/wifi',
        'required': True,
        'optional': False,
    },
    {
        'id': 'internet',
        'title_key': 'wizard_step_internet',
        'checks': ['Internet via WiFi'],
        'action': None,
        'required': False,
        'optional': False,
    },
    {
        'id': 'p1',
        'title_key': 'wizard_step_p1',
        'checks': ['Recent Gas (P1) data'],
        'action': '/p1',
        'required': True,
        'optional': False,
    },
    {
        'id': 'otgw',
        'title_key': 'wizard_step_otgw',
        'checks': ['Recent OTGW data'],
        'action': '/checks',
        'required': True,
        'optional': False,
    },
    {
        'id': 'sensors',
        'title_key': 'wizard_step_sensors',
        'checks': ['DS18B20 sensors', 'Recent Temperature data'],
        'action': '/checks',
        'required': True,
        'optional': False,
    },
    {
        'id': 'sync',
        'title_key': 'wizard_step_sync',
        'checks': ['VPS sync key'],
        'action': '/sync',
        'required': False,
        'optional': True,
    },
    {
        'id': 'services',
        'title_key': 'wizard_step_services',
        'checks': [
            'Service: mosquitto', 'Service: mqtt-logger',
            'Service: otgw-listener', 'Service: ds18b20-listener',
        ],
        'action': '/checks',
        'required': True,
        'optional': False,
    },
]


def _wizard_step_status(
    step: dict, by_name: dict[str, dict]
) -> tuple[str, str]:
    """Return (status, detail) for a wizard step given indexed check results.

    Status is one of: 'pass', 'warn', 'fail', 'pending'
    """
    if not step['checks']:
        return 'pending', ''
    statuses = []
    details  = []
    for name in step['checks']:
        c = by_name.get(name)
        if c is None:
            return 'pending', ''
        statuses.append(c['status'])
        if c.get('detail'):
            details.append(c['detail'])
    if any(s == 'fail' for s in statuses):
        return 'fail', '; '.join(details)
    if any(s == 'warn' for s in statuses):
        return 'warn', '; '.join(details)
    return 'pass', '; '.join(details)


def build_wizard(lang: str, banner: str = '') -> str:
    """Setup wizard page — step-by-step installation overview."""
    checks, _ts  = _checks_cache.get()
    by_name = {c['name']: c for c in checks}

    steps_html = ''
    first_incomplete = None   # first step that is not pass/warn
    required_done    = True   # track whether all required steps are satisfied

    for i, step in enumerate(_WIZARD_STEPS, start=1):
        status, detail = _wizard_step_status(step, by_name)

        if step['required'] and status == 'fail':
            required_done = False

        if first_incomplete is None and status not in ('pass', 'warn'):
            first_incomplete = step['id']

        # Visual indicator and row style
        if status == 'pass':
            icon      = '✓'
            row_style = 'border-left:4px solid #2e7d32;'
            icon_col  = 'color:#2e7d32;font-weight:700'
        elif status == 'warn':
            icon      = '!'
            row_style = 'border-left:4px solid #e65100;'
            icon_col  = 'color:#e65100;font-weight:700'
        elif status == 'fail':
            icon      = '✗'
            row_style = 'border-left:4px solid #b71c1c;'
            icon_col  = 'color:#b71c1c;font-weight:700'
        else:
            icon      = '○'
            row_style = 'border-left:4px solid #9e9e9e;'
            icon_col  = 'color:#9e9e9e'

        # Highlight current step
        if step['id'] == first_incomplete:
            row_style += 'background:#fffde7;'

        opt_badge = (
            f' <span style="font-size:0.75rem;color:#888">'
            f'({tr("wizard_optional", lang)})</span>'
            if step['optional'] else ''
        )

        detail_html = (
            f'<div style="font-size:0.82rem;color:#555;margin-top:2px">'
            f'{html.escape(detail)}</div>'
            if detail else ''
        )

        if step['action']:
            btn_label = (
                tr('wizard_redo', lang) if status in ('pass', 'warn')
                else tr('wizard_go', lang)
            )
            btn_cls = 'btn-secondary' if status in ('pass', 'warn') else 'btn'
            action_html = (
                f'<a href="{step["action"]}" class="btn {btn_cls} btn-sm"'
                f' style="white-space:nowrap">{btn_label}</a>'
            )
        else:
            action_html = ''

        steps_html += f"""
<div style="display:flex;align-items:flex-start;gap:0.75rem;padding:0.65rem 0.75rem;
            margin-bottom:0.4rem;border-radius:6px;{row_style}">
  <span style="font-size:1.25rem;min-width:1.5rem;text-align:center;{icon_col}">{icon}</span>
  <div style="flex:1;min-width:0">
    <div style="font-weight:600">{i}. {tr(step["title_key"], lang)}{opt_badge}</div>
    {detail_html}
  </div>
  {action_html}
</div>"""

    # Progress summary
    done_count = sum(
        1 for s in _WIZARD_STEPS
        if _wizard_step_status(s, by_name)[0] in ('pass', 'warn')
    )
    progress = tr('wizard_progress', lang).format(
        done=done_count, total=len(_WIZARD_STEPS)
    )

    # Finish button
    finish_hint    = (
        tr('wizard_finish_hint', lang) if required_done
        else tr('wizard_finish_blocked', lang)
    )
    finish_disabled = '' if required_done else 'disabled'
    finish_style    = (
        'background:#2e7d32;color:#fff;'
        if required_done
        else 'opacity:0.45;cursor:not-allowed;'
    )
    finish_title = f'title="{finish_hint}"'

    # Quick controls card
    ssh_state = get_wifi_ssh_state()
    ssh_label = tr('wifi_ssh_on' if ssh_state == 'enabled' else 'wifi_ssh_off', lang)
    ssh_badge_style = (
        'color:#e65100;font-weight:700' if ssh_state == 'enabled'
        else 'color:#555'
    )
    ssh_btn = (
        tr('wifi_ssh_off', lang) if ssh_state == 'enabled'
        else tr('wifi_ssh_on', lang)
    )
    ap_pw = get_config_ap_pw()
    ap_pw_label = tr('config_ap_pw_on' if ap_pw else 'config_ap_pw_off', lang)
    ap_pw_badge_style = 'color:#e65100;font-weight:700' if ap_pw else 'color:#555'
    ap_pw_btn = (
        tr('config_ap_pw_disable', lang) if ap_pw
        else tr('config_ap_pw_enable', lang)
    )
    _dev_id = (DEVICE_ID_FILE.read_text().strip()
               if DEVICE_ID_FILE.exists() else 'unknown')
    _ap_ssid = f'otgw-{_dev_id}'
    _ap_pw_val = _derive_ap_password()
    ap_pw_extra = (
        f'<div style="font-size:0.82rem;color:#555;margin-top:0.25rem">'
        f'SSID: <code>{html.escape(_ap_ssid)}</code>&nbsp;&nbsp;'
        f'Password: <code>{html.escape(_ap_pw_val)}</code></div>'
    )

    controls_html = f"""
<div class="card" style="margin-top:1rem">
  <div class="card-title">{tr('wizard_controls_title', lang)}</div>
  <div class="card-body">
    <div class="row" style="align-items:center">
      <span class="label">{tr('wifi_ssh_title', lang)}</span>
      <span class="value" style="{ssh_badge_style}">{ssh_label}</span>
      <form method="post" action="/wifi-ssh/toggle" style="margin:0">
        <button type="submit" class="btn btn-secondary btn-sm">{ssh_btn}</button>
      </form>
    </div>
    <div class="row" style="align-items:center;margin-top:0.5rem">
      <span class="label">{tr('config_ap_pw_label', lang)}</span>
      <span class="value" style="{ap_pw_badge_style}">{ap_pw_label}</span>
      <form method="post" action="/config-ap-pw/toggle" style="margin:0">
        <button type="submit" class="btn btn-secondary btn-sm">{ap_pw_btn}</button>
      </form>
    </div>
    {ap_pw_extra}
  </div>
</div>"""

    body = f"""
{banner}
<div class="card">
  <div class="card-title">{tr('wizard_title', lang)}</div>
  <div class="card-body">
    <p style="color:#555;margin:0 0 0.75rem">{tr('wizard_subtitle', lang)}</p>
    <div style="font-size:0.9rem;font-weight:600;margin-bottom:0.75rem">{progress}</div>
    {steps_html}
  </div>
</div>
{controls_html}
<div style="margin-top:1rem;text-align:center">
  <p style="font-size:0.85rem;color:#555;margin-bottom:0.5rem">{finish_hint}</p>
  <form method="post" action="/stop-ap" style="display:inline">
    <button type="submit" {finish_disabled} {finish_title}
      style="padding:0.65rem 1.5rem;font-size:1rem;{finish_style}">
      {tr('checks_finish_btn', lang)}
    </button>
  </form>
</div>"""
    return page(tr('wizard_title', lang), body, lang, 'nav_wizard')


def _checks_rows(checks: list[dict], lang: str) -> str:
    rows = ''
    for c in checks:
        cls    = f'check-{c["status"]}'
        label  = tr(f'checks_{c["status"]}', lang)
        detail = (f' <span style="color:#888;font-size:0.8rem">'
                  f'— {html.escape(c["detail"])}</span>' if c['detail'] else '')
        rows += (
            f'<div class="row">'
            f'<span class="label">{html.escape(c["name"])}</span>'
            f'<span class="{cls}">{label}{detail}</span>'
            f'</div>'
        )
    return rows


def build_checks(lang: str, banner: str = '',
                 live_checks: list[dict] | None = None) -> str:
    if live_checks is not None:
        checks = live_checks
        last_run_html = (
            '<div style="font-size:0.8rem;color:#2e7d32;margin-bottom:0.5rem">'
            + ('✓ Zojuist uitgevoerd' if lang == 'nl' else '✓ Just ran')
            + '</div>'
        )
    else:
        checks, last_ts = _checks_cache.get()
        if last_ts:
            age = int(time.time() - last_ts)
            age_str = (f'{age}s geleden' if lang == 'nl' else f'{age}s ago') if age < 60 else (
                f'{age // 60}m geleden' if lang == 'nl' else f'{age // 60}m ago'
            )
            last_run_html = (
                f'<div style="font-size:0.8rem;color:#888;margin-bottom:0.5rem">'
                f'{"Laatste run: " if lang == "nl" else "Last run: "}{age_str}</div>'
            )
        else:
            last_run_html = ''
    rows    = _checks_rows(checks, lang)
    total   = len(checks)
    passed  = sum(1 for c in checks if c['status'] == 'pass')
    any_fail = any(c['status'] == 'fail' for c in checks)

    # Data flow section
    readings = _checks_cache.get_readings()
    df_rows = ''
    for key, label_key, (ok_s, warn_s) in [
        ('otgw',        'last_otgw',  READING_THRESHOLDS['otgw']),
        ('gas',         'last_gas',   READING_THRESHOLDS['gas']),
        ('temperature', 'last_temp',  READING_THRESHOLDS['temperature']),
    ]:
        ts  = readings[key]
        age = format_age(ts)
        css = age_css(ts, ok_s, warn_s)
        df_rows += (
            f'<div class="row">'
            f'<span class="label">{tr(label_key, lang)}</span>'
            f'<span class="{css}">{age}</span>'
            f'</div>'
        )

    # "Finish & Stop Portal" button — disabled if any check fails
    finish_disabled = 'disabled' if any_fail else ''
    finish_title    = (
        f'title="{tr("checks_finish_disabled", lang)}"'
        if any_fail else ''
    )
    finish_style = (
        'opacity:0.45;cursor:not-allowed;'
        if any_fail
        else 'background:#2e7d32;color:#fff;'
    )

    body = f"""
{banner}
{last_run_html}
<div class="card">
  <div class="card-title">{tr('checks_title', lang)} ({passed}/{total})</div>
  <div class="card-body">{rows}</div>
</div>
<div class="card">
  <div class="card-title">{tr('checks_data_flow', lang)}</div>
  <div class="card-body">{df_rows}</div>
</div>
<div style="margin-top:1rem;text-align:center;display:flex;gap:0.75rem;justify-content:center;flex-wrap:wrap">
  <form method="post" action="/checks/run">
    <button type="submit" style="padding:0.6rem 1.4rem;font-size:1rem">
      {'Test opnieuw' if lang == 'nl' else 'Test again'}
    </button>
  </form>
  <form method="post" action="/checks/signoff">
    <button type="submit"
      {'disabled title="' + html.escape(tr('checks_signoff_blocked', lang)) + '"' if any_fail else ''}
      style="padding:0.6rem 1.4rem;font-size:1rem{';opacity:0.45;cursor:not-allowed' if any_fail else ''}">
      {tr('checks_signoff_btn', lang)}
    </button>
  </form>
  <form method="post" action="/stop-ap">
    <button type="submit" {finish_disabled} {finish_title}
      style="padding:0.6rem 1.4rem;font-size:1rem;{finish_style}">
      {tr('checks_finish_btn', lang)}
    </button>
  </form>
</div>"""
    return page(tr('checks_title', lang), body, lang, 'nav_checks')


def build_p1(lang: str, msg: str = '', err: str = '',
             output: str = '') -> str:
    """P1 dongle page — flash first, read-only config below."""
    image_id = get_image_id()
    wifi_conf = load_wifi_conf()
    broker       = wifi_conf.get('P1_BROKER', wifi_conf.get('p1_broker',
                                socket.gethostname() + '.local'))
    topic_prefix = wifi_conf.get(
        'P1_TOPIC_PREFIX',
        wifi_conf.get('p1_topic_prefix', f'home/{image_id}'),
    )
    if not topic_prefix:
        topic_prefix = f'home/{image_id}'

    dongle = find_p1_dongle()
    otgw_dev = find_otgw_device()
    if dongle:
        dongle_type = classify_serial_device(dongle)
        dongle_label = {'p1_dongle': 'P1 dongle', 'otgw': 'OTGW adapter (not a P1 dongle!)',
                        'unknown': 'USB device (type unknown)'}.get(dongle_type, dongle_type)
        dongle_row = (
            f'<div class="row"><span class="label">{tr("p1_dongle_found", lang)}</span>'
            f'<span class="value">{html.escape(dongle)}</span></div>'
            f'<div class="row"><span class="label">Device type</span>'
            f'<span class="value">{html.escape(dongle_label)}</span></div>'
        )
        if otgw_dev and otgw_dev != dongle:
            dongle_row += (
                f'<div class="row"><span class="label">Also found</span>'
                f'<span class="value">{html.escape(otgw_dev)} (OTGW adapter)</span></div>'
            )
    else:
        all_devs = sorted(_glob.glob('/dev/ttyACM*') + _glob.glob('/dev/ttyUSB*'))
        if all_devs:
            types = {d: classify_serial_device(d) for d in all_devs}
            otgw_devs = [d for d, t in types.items() if t == 'otgw']
            if otgw_devs:
                dongle_row = (
                    f'<div class="msg-err" style="margin-bottom:8px">'
                    f'{tr("p1_dongle_none", lang)} — found OTGW adapter: '
                    f'{html.escape(otgw_devs[0])}</div>'
                )
            else:
                dongle_row = (
                    f'<div class="msg-err" style="margin-bottom:8px">'
                    f'{tr("p1_dongle_none", lang)}</div>'
                )
        else:
            dongle_row = (
                f'<div class="msg-err" style="margin-bottom:8px">'
                f'{tr("p1_dongle_none", lang)}</div>'
            )

    notice = ''
    if msg:
        notice = f'<div class="msg-ok">{html.escape(msg)}</div>'
    elif err:
        notice = f'<div class="msg-err">{html.escape(err)}</div>'

    # Add WiFi config form if any USB device is detected
    config_form = ''
    all_devs = sorted(_glob.glob('/dev/ttyACM*') + _glob.glob('/dev/ttyUSB*'))
    if all_devs:
        # Get current WiFi SSID - try multiple sources
        current_ssid = (wifi_conf.get('ssid', '') or 
                       wifi_conf.get('SSID', '') or 
                       wifi_conf.get('wifi_ssid', '') or
                       '')
        
        # Also try to get SSID from NetworkManager (if wifi.conf doesn't have it)
        if not current_ssid:
            try:
                import subprocess
                result = subprocess.run(
                    ['nmcli', '-t', '-f', 'ACTIVE,SSID', 'connection', 'show', '--active'],
                    capture_output=True, text=True, timeout=2
                )
                if result.returncode == 0 and result.stdout:
                    for line in result.stdout.strip().split('\n'):
                        if ':yes:' in line:
                            # Format: "connection_name:yes:ssid"
                            parts = line.split(':')
                            if len(parts) >= 3:
                                current_ssid = parts[2]
                                break
            except Exception:
                pass
        
        # Try to get WiFi password for partial display
        current_password = ''
        password_hint = ''
        if current_ssid:
            try:
                import subprocess
                # Try to get password from NetworkManager
                result = subprocess.run(
                    ['nmcli', '-s', '-f', '802-11-wireless-security.psk', 'connection', 'show', current_ssid],
                    capture_output=True, text=True, timeout=2
                )
                if result.returncode == 0 and result.stdout:
                    # Extract password from output
                    for line in result.stdout.strip().split('\n'):
                        if '802-11-wireless-security.psk:' in line:
                            password_part = line.split('802-11-wireless-security.psk:', 1)
                            if len(password_part) > 1:
                                current_password = password_part[1].strip()
                                # Create partial display: first 2 + last 2 chars
                                if len(current_password) > 4:
                                    password_hint = f"{current_password[:2]}••••{current_password[-2:]}"
                                elif len(current_password) > 0:
                                    password_hint = f"{current_password[:1]}••{current_password[-1:]}"
                                break
            except Exception:
                pass
        
        current_broker = broker
        current_topic = f'{topic_prefix}/energy/gas' if topic_prefix else 'home/{image_id}/energy/gas'
        
        # Add helpful note if no SSID found
        ssid_note = ''
        if not current_ssid:
            ssid_note = (f'<div style="font-size:0.8rem;color:#888;margin-bottom:0.5rem;">'
                        f'{tr("p1_wifi_ssid_note", lang)}</div>')
        
        config_form = f'''
<div class="card">
  <div class="card-title">{tr('p1_send_config_title', lang)}</div>
  <div class="card-body">
    <form method="post" action="/p1/send-config">
      {ssid_note}
      <label for="ssid">{tr('p1_wifi_ssid', lang)}</label>
      <input type="text" id="ssid" name="ssid" value="{html.escape(current_ssid)}" required>

      <label for="password" style="margin-top:0.5rem;">{tr('p1_wifi_password', lang)}</label>
      <div style="font-size:0.8rem;color:#666;margin-bottom:0.3rem;">
        {password_hint if password_hint else tr('p1_wifi_password_placeholder', lang)}
      </div>
      <input type="password" id="password" name="password" value="" placeholder="{tr('p1_wifi_password_placeholder', lang)}">

      <label for="broker" style="margin-top:0.5rem;">{tr('p1_mqtt_broker', lang)}</label>
      <input type="text" id="broker" name="broker" value="{html.escape(current_broker)}">

      <label for="port" style="margin-top:0.5rem;">{tr('p1_mqtt_port', lang)}</label>
      <input type="number" id="port" name="port" value="1883" min="1" max="65535">

      <label for="topic" style="margin-top:0.5rem;">{tr('p1_mqtt_topic', lang)}</label>
      <input type="text" id="topic" name="topic" value="{html.escape(current_topic)}">

      <button type="submit" class="btn btn-primary" style="margin-top:0.75rem;">
        {tr('p1_send_config', lang)}
      </button>
    </form>
  </div>
</div>'''
    
    # Add flashing warning and instructions card
    flash_warning = f'''
<div class="card">
  <div class="card-title">{tr('p1_flash_warning_title', lang)}</div>
  <div class="card-body">
    <div class="msg-warn" style="margin-bottom:1rem;">
      {tr('p1_flash_warning_slow', lang)}
    </div>
    <p style="margin-bottom:1rem;">{tr('p1_flash_recommend_pc', lang)}</p>
    
    <div class="card" style="margin-bottom:1rem;">
      <div class="card-title">{tr('p1_flash_manual_title', lang)}</div>
      <div class="card-body">
        <ol style="padding-left:1.2rem;">
          <li>{tr('p1_flash_step1', lang)} <a href="/firmware/p1_reader.bin" class="copyable" style="color:#2d5a2d;text-decoration:underline;">{tr('p1_flash_download', lang)}</a></li>
          <li>{tr('p1_flash_step2', lang)}</li>
          <li>{tr('p1_flash_step3', lang)}</li>
          <li>{tr('p1_flash_step4', lang)}</li>
        </ol>
      </div>
    </div>
    
    <a href="/p1/flash" class="btn btn-secondary"
       style="display:block;padding:0.75rem 1rem;font-size:1rem;">
      {tr('p1_flash_btn', lang)} (experimental)
    </a>
  </div>
</div>'''

    body = f"""
{notice}
{config_form}
{flash_warning}
<div class="card">
  <div class="card-body" style="text-align:center;padding:1.2rem 1rem">
    {dongle_row}
  </div>
</div>
<div class="card">
  <div class="card-title">{tr('p1_title', lang)}</div>
  <div class="card-body">
    <div class="row"><span class="label">{tr('p1_broker', lang)}</span>
      <span class="value">{html.escape(broker)}</span></div>
    <div class="row"><span class="label">{tr('p1_topic_prefix', lang)}</span>
      <span class="value">{html.escape(topic_prefix)}</span></div>
  </div>
</div>"""
    return page(tr('p1_title', lang), body, lang, 'nav_p1')


def build_p1_flash(lang: str, msg: str = '', err: str = '',
                   output: str = '') -> str:
    """P1 firmware flash page."""
    fw = otgw_api.find_p1_firmware()
    dongle = otgw_api.find_p1_dongle()
    esptool = otgw_api.find_esptool()

    fw_line = (
        f'<div class="msg-ok">{tr("p1_dongle_found", lang)}: '
        f'<strong>{html.escape(str(fw))}</strong></div>'
        if fw else
        f'<div class="msg-err">{tr("p1_flash_no_fw", lang)}</div>'
    )
    dongle_line = (
        f'<div class="msg-ok">{tr("p1_dongle_found", lang)}: '
        f'<strong>{html.escape(dongle)}</strong></div>'
        if dongle else
        f'<div class="msg-err">{tr("p1_dongle_none", lang)}</div>'
    )
    esptool_line = (
        f'<div class="msg-ok">esptool: <strong>{html.escape(" ".join(esptool))}</strong></div>'
        if esptool else
        f'<div class="msg-err">{tr("p1_flash_no_esptool", lang)}</div>'
    )

    notice = f'<div class="msg-ok">{html.escape(msg)}</div>' if msg else ''
    error  = f'<div class="msg-err">{html.escape(err)}</div>' if err else ''
    output_section = ''
    if output:
        output_section = (
            f'<div class="card">'
            f'<div class="card-title">{tr("p1_flash_output", lang)}</div>'
            f'<div class="card-body"><pre>{html.escape(output)}</pre></div>'
            f'</div>'
        )

    can_flash = fw and dongle and esptool
    flash_btn = (
        f'<form method="post" action="/p1/flash">'
        f'<button type="submit" class="btn btn-danger"'
        f' onclick="this.disabled=true;this.textContent='
        f"'{tr('p1_flash_running', lang)}';"
        f'this.form.submit()">'
        f'{tr("p1_flash_btn", lang)}</button></form>'
        if can_flash else
        f'<p style="color:#888;font-size:0.85rem">Fix the issues above before flashing.</p>'
    )

    body = f"""
{notice}{error}
<div class="card">
  <div class="card-title">{tr('p1_flash_title', lang)}</div>
  <div class="card-body">
    {fw_line}
    {dongle_line}
    {esptool_line}
    <div style="margin-top:12px">{flash_btn}</div>
  </div>
</div>
{output_section}"""
    return page(tr('p1_flash_title', lang), body, lang, 'nav_p1')


def build_sync(lang: str, msg: str = '', err: str = '',
               output: str = '', p1_msg: str = '', p1_err: str = '') -> str:
    """Log sync targets management page."""
    targets = get_sync_targets_full()
    pub_keys = get_sync_pub_key_paths()

    notice = ''
    if msg:
        notice = f'<div class="msg-ok">{html.escape(msg)}</div>'
    elif err:
        notice = f'<div class="msg-err">{html.escape(err)}</div>'

    output_section = ''
    if output:
        out_ok = all('OK' in ln or ln.startswith('[') for ln in output.splitlines() if ln.strip())
        out_style = 'background:#d4edda;color:#155724' if out_ok else 'background:#f8d7da;color:#721c24'
        output_section = (
            f'<div class="card" style="{out_style};border-radius:6px">'
            f'<div class="card-title">{tr("sync_test_result", lang)}</div>'
            f'<div class="card-body"><pre style="margin:0">{html.escape(output)}</pre></div>'
            f'</div>'
        )

    # Existing targets table
    if targets:
        target_rows = ''
        for name, cfg in targets.items():
            host     = html.escape(cfg.get('sync_remote_host', '?'))
            user     = html.escape(cfg.get('sync_remote_user', 'logbackup'))
            path     = html.escape(cfg.get('sync_remote_path', '?'))
            prebaked = is_prebaked_sync_target(name)
            badge    = (
                f' <span style="background:#d4edda;color:#155724;'
                f'font-size:0.75rem;padding:1px 6px;border-radius:10px;'
                f'vertical-align:middle">{tr("sync_prebaked_badge", lang)}</span>'
                if prebaked else ''
            )
            target_rows += f"""
<div class="row" style="flex-wrap:wrap;gap:4px">
  <span class="label" style="flex:1;min-width:120px">
    <strong>{html.escape(name)}</strong>{badge}<br>
    <small style="color:#888">{user}@{host}:{path}</small>
  </span>
  <span style="display:flex;gap:4px;flex-wrap:wrap">
    <form method="post" action="/sync/test" style="display:inline">
      <input type="hidden" name="name" value="{html.escape(name)}">
      <button type="submit" class="btn btn-secondary btn-sm">{tr('sync_test', lang)}</button>
    </form>
    <form method="post" action="/sync/delete"
          onsubmit="return confirm('Delete {html.escape(name)}?')" style="display:inline">
      <input type="hidden" name="name" value="{html.escape(name)}">
      <button type="submit" class="btn btn-danger btn-sm">{tr('sync_delete', lang)}</button>
    </form>
  </span>
</div>"""
        test_all_btn = (
            f'<form method="post" action="/sync/test-all">'
            f'<button type="submit" class="btn btn-secondary btn-sm">'
            f'{tr("sync_test_all", lang)}</button></form>'
        )
        flush_now_btn = (
            f'<form method="post" action="/sync/flush-now">'
            f'<button type="submit" class="btn btn-primary btn-sm">'
            f'{tr("sync_flush_now", lang)}</button></form>'
        )
        targets_html = f'''
<div class="card">
  <div class="card-title">{tr('sync_title', lang)}</div>
  <div class="card-body">{target_rows}
    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">{test_all_btn}{flush_now_btn}</div>
  </div>
</div>'''
    else:
        targets_html = (
            f'<div class="card"><div class="card-body">'
            f'<span class="label">{tr("none", lang)}</span>'
            f'</div></div>'
        )

    # SSH public key display
    any_prebaked = any(is_prebaked_sync_target(n) for n in targets)
    if pub_keys:
        key_content = html.escape(pub_keys[0].read_text().strip())
        key_name    = html.escape(pub_keys[0].name)
        prebaked_note = (
            f'<p style="color:#155724;background:#d4edda;padding:6px 8px;'
            f'border-radius:4px;font-size:0.85rem;margin-bottom:8px">'
            f'{tr("sync_prebaked_key", lang)}</p>'
            if any_prebaked else ''
        )
        key_section = f"""
<div class="card">
  <div class="card-title">{tr('sync_public_key', lang)} ({key_name})</div>
  <div class="card-body">
    {prebaked_note}
    <pre style="font-size:0.7rem;word-break:break-all">{key_content}</pre>
  </div>
</div>"""
    else:
        key_section = f"""
<div class="card">
  <div class="card-title">{tr('sync_public_key', lang)}</div>
  <div class="card-body">
    <p style="color:#888;font-size:0.85rem;padding:4px 0">{tr('sync_no_key', lang)}</p>
    <form method="post" action="/sync/keygen">
      <button type="submit" class="btn btn-secondary">{tr('sync_gen_key', lang)}</button>
    </form>
  </div>
</div>"""

    # Add target form
    add_form = f"""
<div class="card">
  <div class="card-title">{tr('sync_add_target', lang)}</div>
  <form method="post" action="/sync/add">
    <div style="padding:12px 14px 0">
      <label>{tr('sync_name', lang)}</label>
      <input type="text" name="name" placeholder="vps1" autocomplete="off">
      <label>{tr('sync_host', lang)}</label>
      <input type="text" name="host" placeholder="vps1.example.com" autocomplete="off">
      <label>{tr('sync_user', lang)}</label>
      <input type="text" name="user" value="logbackup" autocomplete="off">
      <label>{tr('sync_path', lang)}</label>
      <input type="text" name="path" value="otgw-logs" autocomplete="off">
      <button type="submit" class="btn btn-primary">{tr('sync_add_target', lang)}</button>
    </div>
  </form>
</div>"""

        # P1 dongle USB sync card
    p1_notice = ''
    if p1_msg:
        p1_notice = f'<div class="msg-ok">{html.escape(p1_msg)}</div>'
    elif p1_err:
        p1_notice = f'<div class="msg-err">{html.escape(p1_err)}</div>'

    busy_attr = ' disabled' if _p1_sync_busy else ''
    p1_sync_card = f'''
<div class="card">
  <div class="card-title">{tr('p1_sync_title', lang)}</div>
  <div class="card-body">
    {p1_notice}
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
      <form method="post" action="/sync/p1-usb-sync">
        <input type="hidden" name="full" value="0">
        <button type="submit" class="btn btn-primary btn-sm"{busy_attr}>
          {tr('p1_sync_btn', lang)}</button>
      </form>
      <form method="post" action="/sync/p1-usb-sync">
        <input type="hidden" name="full" value="1">
        <button type="submit" class="btn btn-secondary btn-sm"{busy_attr}>
          {tr('p1_sync_full_btn', lang)}</button>
      </form>
      <form method="post" action="/sync/p1-usb-clear">
        <button type="submit" class="btn btn-warning btn-sm"{busy_attr}>
          {tr('p1_sync_clear_btn', lang)}</button>
      </form>
    </div>
    <p style="font-size:0.8rem;color:#666;margin-top:6px">
      Use <em>{tr('p1_sync_clear_btn', lang)}</em> only after a confirmed sync.
      This prevents the dongle from re-sending records via MQTT.
    </p>
  </div>
</div>'''

    body = f"""
{notice}
{output_section}
{targets_html}
{key_section}
{add_form}
{p1_sync_card}"""
    return page(tr('sync_title', lang), body, lang, 'nav_sync')


def build_live(lang: str) -> str:
    """Full-page live MQTT data viewer."""
    conn_status = tr('live_connected' if _mqtt_cache.connected else 'live_disconnected',
                     lang)
    conn_dot = (
        '<span class="status-dot dot-green"></span>'
        if _mqtt_cache.connected
        else '<span class="status-dot dot-red"></span>'
    )

    js_live_full = """
function refreshLive() {
  fetch('/messages.json').then(r=>r.json()).then(msgs=>{
    var tbody = document.getElementById('live-full-tbody');
    if(!tbody) return;
    var connEl = document.getElementById('live-conn-status');
    if(connEl) connEl.textContent = msgs.length > 0 ? 'Connected' : connEl.textContent;
    if(!msgs || msgs.length === 0){
      tbody.innerHTML = '<tr><td colspan="3" style="color:#888;padding:8px">No messages yet</td></tr>';
      return;
    }
    var rows = msgs.map(function(m){
      var ts = new Date(m.ts * 1000).toLocaleTimeString();
      return '<tr><td style="white-space:nowrap;color:#888">'+ts+'</td>'
        +'<td style="word-break:break-all">'+escHtml(m.topic)+'</td>'
        +'<td style="word-break:break-all">'+escHtml(m.payload)+'</td>'
        +'</tr>';
    });
    tbody.innerHTML = rows.join('');
  }).catch(()=>{});
}
function escHtml(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
setInterval(refreshLive, 3000);
refreshLive();
"""

    body = f"""
<div class="card">
  <div class="card-title">
    {conn_dot}
    <span id="live-conn-status">{html.escape(conn_status)}</span>
  </div>
  <div class="card-body" style="padding:0;overflow-x:auto">
    <table class="mqtt-table" style="min-width:320px">
      <thead><tr>
        <th style="white-space:nowrap">Time</th>
        <th>{tr('live_topic', lang)}</th>
        <th>{tr('live_payload', lang)}</th>
      </tr></thead>
      <tbody id="live-full-tbody">
        <tr><td colspan="3" style="color:#888;padding:8px">{tr('live_no_msgs', lang)}</td></tr>
      </tbody>
    </table>
  </div>
</div>"""
    return page(tr('live_title', lang), body, lang, 'nav_live',
                extra_js=js_live_full)


def build_simulators(lang: str, msg: str = '', err: str = '',
                     fresh: dict[str, str] | None = None) -> str:
    """Simulator management page.

    fresh: optional dict of {service: status} that overrides the cache for
    just-toggled services, so the page reflects the new state immediately.
    """
    notice = ''
    if msg:
        notice = f'<div class="msg-ok">{html.escape(msg)}</div>'
    elif err:
        notice = f'<div class="msg-err">{html.escape(err)}</div>'

    cached_svcs = _checks_cache.get_services()
    if fresh:
        cached_svcs = {**cached_svcs, **fresh}
    rows = ''
    for svc in SIMULATOR_SERVICES:
        # Skip services whose unit file is not installed — toggling them would
        # produce a confusing "unit not found" error (see FSD §G.31).
        unit_file = Path(f'/etc/systemd/system/{svc}.service')
        if not unit_file.exists():
            rows += f"""
<div class="row" style="flex-wrap:wrap;gap:4px;opacity:0.45">
  <span class="label" style="flex:1">{html.escape(svc)}</span>
  <span class="value badge-inactive" style="margin-right:8px">not installed</span>
  <button class="btn btn-secondary btn-sm" disabled>Toggle</button>
</div>"""
            continue
        status = cached_svcs.get(svc, 'unknown')
        active = status == 'active'
        cls    = 'badge-active' if active else 'badge-inactive'
        action = 'stop' if active else 'start'
        action_label = tr('sim_stopped' if active else 'sim_started', lang)
        rows += f"""
<div class="row" style="flex-wrap:wrap;gap:4px">
  <span class="label" style="flex:1">{html.escape(svc)}</span>
  <span class="value {cls}" style="margin-right:8px">{status}</span>
  <form method="post" action="/simulators/toggle" style="display:inline;margin:0">
    <input type="hidden" name="service" value="{html.escape(svc)}">
    <input type="hidden" name="action" value="{action}">
    <button type="submit" class="btn btn-secondary btn-sm">{tr('sim_toggle', lang)}</button>
  </form>
</div>"""

    body = f"""
{notice}
<div class="card">
  <div class="card-title">{tr('sim_title', lang)}</div>
  <div class="card-body">{rows}</div>
</div>"""
    return page(tr('sim_title', lang), body, lang, 'nav_simulators')


def build_factory_reset(lang: str, msg: str = '', err: str = '') -> str:
    """Factory reset page."""
    notice = ''
    if msg:
        notice = f'<div class="msg-ok">{html.escape(msg)}</div>'
    elif err:
        notice = f'<div class="msg-err">{html.escape(err)}</div>'

    body = f"""
{notice}
<div class="card">
  <div class="card-title" style="color:#c0392b">{tr('reset_title', lang)}</div>
  <div class="card-body">
    <p style="font-size:0.9rem;padding:8px 0;color:#c0392b">
      {tr('reset_warning', lang)}
    </p>
  </div>
  <form method="post" action="/factory-reset"
        onsubmit="return confirm('{tr('reset_btn', lang)}?')">
    <div style="padding:0 14px 14px">
      <div class="radio-group">
        <label>
          <input type="radio" name="logs" value="keep" checked>
          {tr('reset_keep_logs', lang)}
        </label>
        <label>
          <input type="radio" name="logs" value="delete">
          {tr('reset_delete_logs', lang)}
        </label>
      </div>
      <button type="submit" class="btn btn-danger">{tr('reset_btn', lang)}</button>
    </div>
  </form>
</div>"""
    return page(tr('reset_title', lang), body, lang)


def build_maintenance(lang: str, msg: str = '', err: str = '',
                      output: str = '') -> str:
    """System maintenance page."""
    notice = ''
    if msg:
        notice = f'<div class="msg-ok">{html.escape(msg)}</div>'
    elif err:
        notice = f'<div class="msg-err">{html.escape(err)}</div>'

    missing = check_missing_packages()
    if missing:
        pkg_status = (
            tr('maint_pkg_missing', lang).format(pkgs=', '.join(missing))
        )
        pkg_cls = 'msg-err'
        install_btn = (
            f'<form method="post" action="/maintenance/install-packages"'
            f' style="margin-top:10px">'
            f'<button type="submit" class="btn btn-primary">'
            f'{tr("pkg_install_btn", lang)}</button></form>'
        )
    else:
        pkg_status = tr('maint_pkg_ok', lang)
        pkg_cls = 'msg-ok'
        install_btn = ''

    output_section = ''
    if output:
        output_section = (
            f'<div class="card">'
            f'<div class="card-title">{tr("maint_output", lang)}</div>'
            f'<div class="card-body"><pre>{html.escape(output)}</pre></div>'
            f'</div>'
        )

    body = f"""
{notice}
<div class="card">
  <div class="card-title">{tr('maint_pkgs', lang)}</div>
  <div class="card-body">
    <div class="{pkg_cls}" style="margin:0">{html.escape(pkg_status)}</div>
    {install_btn}
  </div>
</div>
<div class="card">
  <div class="card-title">{tr('maint_title', lang)}</div>
  <div class="card-body">
    <form method="post" action="/maintenance/run">
      <button type="submit" class="btn btn-primary">{tr('maint_run_btn', lang)}</button>
    </form>
  </div>
</div>
{output_section}"""
    return page(tr('maint_title', lang), body, lang)


def build_log_partition(lang: str, msg: str = '', err: str = '',
                        output: str = '') -> str:
    lp = get_log_partition_info()
    if lp['mounted'] and lp['fstype'] == 'vfat':
        status = tr('logpart_status_ok', lang).format(dev=lp['device'])
        status_cls = 'msg-ok'
    elif lp['mounted']:
        status = tr('logpart_status_other', lang).format(fs=lp['fstype'])
        status_cls = 'msg-warn'
    else:
        status = tr('logpart_status_none', lang)
        status_cls = 'msg-warn'

    notice = f'<div class="msg-ok">{html.escape(msg)}</div>' if msg else ''
    error  = f'<div class="msg-err">{html.escape(err)}</div>' if err else ''
    output_section = ''
    if output:
        output_section = (
            f'<div class="card">'
            f'<div class="card-title">{tr("logpart_output", lang)}</div>'
            f'<div class="card-body"><pre>{html.escape(output)}</pre></div>'
            f'</div>'
        )

    setup_form = ''
    if not (lp['mounted'] and lp['fstype'] == 'vfat'):
        setup_form = f"""
<div class="card">
  <div class="card-title">{tr('logpart_title', lang)}</div>
  <div class="card-body">
    <p style="margin:0 0 12px">{tr('logpart_intro', lang)}</p>
    <p style="margin:0 0 12px;color:#c44">{tr('logpart_warning', lang)}</p>
    <form method="post" action="/log-partition/setup">
      <button type="submit" class="btn btn-danger"
              onclick="this.disabled=true;this.textContent='{tr('logpart_running', lang)}';this.form.submit()">
        {tr('logpart_btn', lang)}</button>
    </form>
  </div>
</div>"""

    body = f"""
{notice}{error}
<div class="card">
  <div class="card-title">{tr('logpart_title', lang)}</div>
  <div class="card-body">
    <div class="{status_cls}" style="margin:0">{html.escape(status)}</div>
  </div>
</div>
{setup_form}
{output_section}"""
    return page(tr('logpart_title', lang), body, lang, active_nav='nav_log_part')


# ── HTTP handler ──────────────────────────────────────────────────────────────

class PortalHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt: str, *args) -> None:
        log.debug(fmt, *args)

    def _lang(self) -> str:
        cookies = _parse_cookie(self.headers.get('Cookie', ''))
        cookie_lang = cookies.get('lang', '')
        return detect_lang(
            self.headers.get('Accept-Language', ''), cookie_lang
        )

    def _send(self, code: int, body: str,
              ctype: str = 'text/html; charset=utf-8',
              extra_headers: list[tuple[str, str]] | None = None) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str,
                  extra_headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(302)
        self.send_header('Location', location)
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()

    def _touch_activity(self) -> None:
        """Signal activity to the AP monitor (for inactivity timeout)."""
        try:
            ACTIVITY_FILE.touch()
        except OSError:
            pass

    def _read_body(self) -> dict[str, str] | None:
        """Read and parse POST body. Returns None if body exceeds MAX_POST_BODY."""
        length = int(self.headers.get('Content-Length', 0))
        if length > MAX_POST_BODY:
            # Drain and reject to keep the connection clean
            self.rfile.read(MAX_POST_BODY)
            self._send(413, '<html><body>Request too large</body></html>')
            return None
        raw    = self.rfile.read(length).decode()
        params = parse_qs(raw, keep_blank_values=True)
        return {k: v[0] for k, v in params.items()}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/')
        lang   = self._lang()

        if path == '/timer.json':
            # Do NOT touch activity — this endpoint measures the inactivity timer
            try:
                last_act = ACTIVITY_FILE.stat().st_mtime
            except OSError:
                last_act = time.time()  # file absent → AP just started, full time
            remaining = max(0, int(AP_INACTIVITY_S - (time.time() - last_act)))
            self._send(200, json.dumps(
                {'remaining_s': remaining}),
                'application/json')
            return

        if path not in _NO_ACTIVITY_PATHS:
            self._touch_activity()

        if path in ('', '/'):
            self._send(200, build_dashboard(lang))
        elif path in ('/wifi', '/wifi/case-a'):
            self._send(200, build_wifi(lang))
        elif path == '/wizard':
            self._send(200, build_wizard(lang))
        elif path == '/mode':
            self._send(200, build_mode(lang))
        elif path == '/checks':
            self._send(200, build_checks(lang))
        elif path == '/p1':
            self._send(200, build_p1(lang))
        elif path == '/p1/flash':
            self._send(200, build_p1_flash(lang))
        elif path == '/sync':
            self._send(200, build_sync(lang))
        elif path == '/live':
            self._send(200, build_live(lang))
        elif path == '/simulators':
            self._send(200, build_simulators(lang))
        elif path == '/factory-reset':
            self._send(200, build_factory_reset(lang))
        elif path == '/maintenance':
            self._send(200, build_maintenance(lang))
        elif path == '/log-partition':
            self._send(200, build_log_partition(lang))
        elif path == '/status.json':
            readings = get_recent_readings()
            readings_json = {}
            for key, (ok_s, warn_s) in READING_THRESHOLDS.items():
                readings_json[key] = {
                    'ts':  readings[key],   # raw Unix timestamp; JS computes age
                    'thr': [ok_s, warn_s],  # thresholds for colour coding
                }
            data = json.dumps({
                'services':           _checks_cache.get_services(),
                'wifi':               _checks_cache.get_wifi(),
                'readings':           readings_json,
                'mqtt_connected':     _mqtt_cache.connected,
                'mqtt_message_count': _mqtt_cache.message_count,
            })
            self._send(200, data, 'application/json')
        elif path == '/messages.json':
            self._send(200, json.dumps(_mqtt_cache.get_messages()),
                       'application/json')
        elif path == '/scan.json':
            self._send(200, json.dumps(scan_wifi(rescan=True)), 'application/json')
        elif path == '/firmware/p1_reader.bin':
            # Serve P1 reader firmware binary
            firmware_path = Path('/opt/otgw/firmware/p1_reader.bin')
            if firmware_path.exists():
                try:
                    # Get file size for Content-Length header
                    file_size = firmware_path.stat().st_size
                    
                    # Send headers first
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Disposition', 'attachment; filename="p1_reader.bin"')
                    self.send_header('Content-Length', str(file_size))
                    self.end_headers()
                    
                    # Stream file in chunks to avoid memory issues
                    chunk_size = 8192  # 8KB chunks
                    with open(firmware_path, 'rb') as f:
                        while True:
                            chunk = f.read(chunk_size)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                    return
                except Exception as e:
                    self._send(500, f'Error reading firmware: {str(e)}'.encode(), 'text/plain')
                    return
            else:
                self._send(404, b'Firmware not found', 'text/plain')
                return

        elif path in _CAPTIVE_PROBE_PATHS:
            # Redirect to portal root so CNA mini-browser shows full portal directly.
            # iOS still detects captive portal (DNS is hijacked regardless of content).
            # See FSD §G.33.
            self._redirect('/')
        else:
            # Captive portal catch-all — redirect to portal root
            self._redirect('/')

    def do_POST(self) -> None:
        self._touch_activity()
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/')
        lang   = self._lang()
        body   = self._read_body()
        if body is None:
            return  # 413 already sent by _read_body

        if path == '/wifi/forget':
            forget_wifi()
            self._send(200, build_wifi(lang, msg=tr('wifi_forgotten', lang)))

        elif path == '/wifi':
            wifi_mode = body.get('wifi_mode', 'home').strip()
            if wifi_mode == 'case_a':
                activate_case_a()
                device_id = (DEVICE_ID_FILE.read_text().strip()
                             if DEVICE_ID_FILE.exists() else 'unknown')
                msg = tr('wifi_case_a_active', lang)
                self._send(200, build_wifi(lang, msg=msg))
                return
            if wifi_mode == 'home_reset':
                deactivate_case_a()
                self._send(200, build_wifi(lang,
                    msg=tr('wifi_saved', lang)))
                return
            ssid     = (body.get('ssid') or body.get('ssid_select', '')).strip()
            password = body.get('password', '').strip()
            if not ssid:
                self._send(200, build_wifi(lang, err='SSID is required'))
                return
            self._send(200, build_wifi_connecting(lang, ssid))
            threading.Thread(
                target=_save_and_configure,
                args=(ssid, password),
                daemon=True,
            ).start()

        elif path == '/checks/run':
            fresh = run_checks()
            self._send(200, build_checks(lang, live_checks=fresh))

        elif path == '/checks/signoff':
            checks   = run_checks()
            any_fail = any(c['status'] == 'fail' for c in checks)
            rows     = _checks_rows(checks, lang)
            total    = len(checks)
            passed   = sum(1 for c in checks if c['status'] == 'pass')

            if any_fail:
                msg    = tr('checks_signoff_blocked', lang)
                banner = (f'<div style="background:#f8d7da;color:#721c24;'
                          f'padding:0.8rem;border-radius:6px;margin-bottom:1rem">'
                          f'{html.escape(msg)}</div>')
                admin_card = ''
            else:
                try:
                    write_signoff(checks)
                    ts  = datetime.now().strftime('%H:%M:%S')
                    msg = tr('checks_signoff_ok', lang).format(ts=ts)
                    banner = (f'<div style="background:#d4edda;color:#155724;'
                              f'padding:0.8rem;border-radius:6px;margin-bottom:1rem">'
                              f'{html.escape(msg)}</div>')
                except OSError as exc:
                    msg    = tr('checks_signoff_err', lang).format(err=str(exc))
                    banner = (f'<div style="background:#f8d7da;color:#721c24;'
                              f'padding:0.8rem;border-radius:6px;margin-bottom:1rem">'
                              f'{html.escape(msg)}</div>')
                    admin_card = ''
                else:
                    admin_card = ''

            pg_body = f"""
{banner}
<div class="card">
  <div class="card-title">{tr('checks_title', lang)} ({passed}/{total})</div>
  <div class="card-body">{rows}</div>
</div>
{admin_card}"""
            self._send(200, page(tr('checks_title', lang), pg_body, lang,
                                 'nav_checks'))

        elif path == '/mode':
            new_mode = body.get('mode', 'production')
            if new_mode in ('production', 'test'):
                MODE_FILE.write_text(new_mode)
                subprocess.run(['systemctl', 'restart', 'mqtt-logger'],
                               capture_output=True)
            self._redirect('/mode')

        elif path == '/p1':
            broker       = body.get('p1_broker', '').strip()
            topic_prefix = body.get('p1_topic_prefix', '').strip()
            if not broker:
                self._send(200, build_p1(lang, err='Broker is required'))
                return
            try:
                save_wifi_conf_fields({
                    'P1_BROKER':       broker,
                    'P1_TOPIC_PREFIX': topic_prefix,
                })
            except OSError as exc:
                self._send(200, build_p1(lang, err=str(exc)))
                return

            # Run auto_p1_config.py if dongle present
            auto_output = ''
            dongle = find_p1_dongle()
            if dongle:
                script = Path('/opt/otgw/scripts/auto_p1_config.py')
                if script.exists():
                    try:
                        r = subprocess.run(
                            [sys.executable, str(script)],
                            capture_output=True, text=True, timeout=30,
                        )
                        auto_output = (r.stdout + r.stderr).strip()
                    except Exception as exc:
                        auto_output = str(exc)

            self._send(200, build_p1(lang, msg=tr('p1_saved', lang),
                                     output=auto_output))

        elif path == '/p1/flash':
            ok, output = otgw_api.flash_p1_dongle_collect()
            if ok:
                self._send(200, build_p1_flash(
                    lang, msg=tr('p1_flash_ok', lang), output=output))
            else:
                self._send(200, build_p1_flash(
                    lang, err=tr('p1_flash_err', lang), output=output))

        elif path == '/p1/send-config':
            status, message = handle_send_p1_config(body)
            if status == 'success':
                self._send(200, build_p1(lang, msg=message))
            else:
                self._send(200, build_p1(lang, err=message))

        elif path == '/p1/send-case-a-wifi':
            # Send Pi AP credentials (Case A) to the P1 dongle via USB
            device_id = (DEVICE_ID_FILE.read_text().strip()
                         if DEVICE_ID_FILE.exists() else 'unknown')
            ssid = f'otgw-{device_id}'
            password = _derive_ap_password()
            p1_device = find_p1_dongle()
            if not p1_device:
                self._send(200, build_wifi(
                    lang, err='P1 dongle not found on USB'))
                return
            config = {'wifi_ssid': ssid, 'wifi_password': password}
            ok, msg = send_p1_config(p1_device, config)
            if ok:
                self._send(200, build_wifi(
                    lang, msg=tr('p1_config_sent', lang)))
            else:
                self._send(200, build_wifi(
                    lang, err=tr('p1_config_error', lang).format(error=msg)))

        elif path == '/sync/keygen':
            # Find existing key name or use 'default'
            existing = list(CONFIG_DIR.glob('sync_key_*'))
            # Exclude .pub files
            privates = [k for k in existing if not k.name.endswith('.pub')]
            if privates:
                key_path = privates[0]
            else:
                key_path = CONFIG_DIR / 'sync_key_default'
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                r = subprocess.run(
                    ['ssh-keygen', '-t', 'ed25519', '-f', str(key_path),
                     '-N', ''],
                    capture_output=True, text=True,
                )
                if r.returncode == 0:
                    self._send(200, build_sync(lang,
                                               msg=tr('sync_key_gen_ok', lang)))
                else:
                    err = (r.stderr or r.stdout).strip()
                    self._send(200, build_sync(
                        lang,
                        err=tr('sync_key_gen_err', lang).format(err=err),
                    ))
            except Exception as exc:
                self._send(200, build_sync(
                    lang,
                    err=tr('sync_key_gen_err', lang).format(err=str(exc)),
                ))

        elif path == '/sync/add':
            name = re.sub(r'[^a-z0-9_-]', '-',
                          body.get('name', '').strip().lower()).strip('-')
            host = body.get('host', '').strip()
            user = body.get('user', 'logbackup').strip()
            path_val = body.get('path', 'otgw-logs').strip()
            if not name or not host:
                self._send(200, build_sync(lang, err='Name and host are required'))
                return
            try:
                SYNC_D_DIR.mkdir(parents=True, exist_ok=True)
                ssh_key = CONFIG_DIR / f'sync_key_{name}'
                conf_file = SYNC_D_DIR / f'{name}.conf'
                content = (
                    f'SYNC_REMOTE_HOST="{host}"\n'
                    f'SYNC_REMOTE_USER="{user}"\n'
                    f'SYNC_REMOTE_PATH="{path_val}"\n'
                    f'SYNC_SSH_KEY="{ssh_key}"\n'
                )
                conf_file.write_text(content)
                conf_file.chmod(0o600)
                self._send(200, build_sync(lang, msg=tr('sync_saved', lang)))
            except Exception as exc:
                self._send(200, build_sync(lang, err=str(exc)))

        elif path == '/sync/delete':
            name = body.get('name', '').strip()
            if name:
                conf_file = SYNC_D_DIR / f'{name}.conf'
                try:
                    conf_file.unlink(missing_ok=True)
                    self._send(200, build_sync(lang,
                                               msg=tr('sync_deleted', lang)))
                except Exception as exc:
                    self._send(200, build_sync(lang, err=str(exc)))
            else:
                self._redirect('/sync')

        elif path == '/sync/test':
            name = body.get('name', '').strip()
            output = ''
            if name:
                try:
                    r = subprocess.run(
                        ['/opt/otgw/scripts/log_sync.sh', '--test',
                         '--target', name],
                        capture_output=True, text=True, timeout=30,
                    )
                    output = (r.stdout + r.stderr).strip()
                except Exception as exc:
                    output = str(exc)
            self._send(200, build_sync(lang, output=output))

        elif path == '/sync/test-all':
            targets = get_sync_targets_full()
            lines: list[str] = []
            for name in targets:
                lines.append(f'=== {name} ===')
                try:
                    r = subprocess.run(
                        ['/opt/otgw/scripts/log_sync.sh', '--test',
                         '--target', name],
                        capture_output=True, text=True, timeout=30,
                    )
                    lines.append((r.stdout + r.stderr).strip())
                except Exception as exc:
                    lines.append(str(exc))
            output = '\n'.join(lines) if lines else '(no targets configured)'
            self._send(200, build_sync(lang, output=output))

        elif path == '/sync/flush-now':
            global _portal_last_flush_time
            now = time.monotonic()
            if now - _portal_last_flush_time < FLUSH_RATE_LIMIT_S:
                self._send(200, build_sync(
                    lang, err=tr('sync_flush_rate_limited', lang),
                ))
            else:
                _portal_last_flush_time = now

                def _do_flush() -> None:
                    subprocess.run(
                        ['systemctl', 'start', 'log-flush.service'],
                        capture_output=True, timeout=30,
                    )
                    subprocess.run(
                        ['systemctl', 'start', 'log-sync.service'],
                        capture_output=True, timeout=60,
                    )

                threading.Thread(target=_do_flush, daemon=True,
                                 name='portal-flush').start()
                self._send(200, build_sync(
                    lang, msg=tr('sync_flush_started', lang),
                ))

        elif path in ('/sync/p1-usb-sync', '/sync/p1-usb-clear'):
            global _p1_sync_busy
            if _p1_sync_busy:
                self._send(200, build_sync(
                    lang, p1_err=tr('p1_sync_busy', lang),
                ))
                return

            port = find_p1_dongle()
            if not port:
                self._send(200, build_sync(
                    lang, p1_err=tr('p1_sync_no_port', lang),
                ))
                return

            image_id = get_image_id()
            mode     = get_mode()
            eff_dir  = str(LOG_DIR / image_id)

            if path == '/sync/p1-usb-clear':
                ok, msg = p1_usb_sync.send_clear(port)
                if ok:
                    self._send(200, build_sync(
                        lang,
                        p1_msg=tr('p1_sync_cleared', lang).format(msg=msg),
                    ))
                else:
                    self._send(200, build_sync(
                        lang,
                        p1_err=tr('p1_sync_clear_err', lang).format(msg=msg),
                    ))
                return

            # /sync/p1-usb-sync — run in background thread, respond immediately
            # with "busy" state; result posted back on next page load via redirect.
            # We run synchronously here (portal request handling is threaded per
            # connection already) — keep it simple.
            full = body.get('full', '0') == '1'
            _p1_sync_busy = True
            try:
                result = p1_usb_sync.run_sync(
                    port=port,
                    effective_dir=eff_dir,
                    image_id=image_id,
                    mode=mode,
                    full=full,
                )
            finally:
                _p1_sync_busy = False

            if result.success:
                msg_str = tr('p1_sync_done', lang).format(
                    merged=result.merged,
                    skipped=result.skipped,
                    malformed=result.malformed,
                )
                self._send(200, build_sync(lang, p1_msg=msg_str))
            else:
                err_str = tr('p1_sync_error', lang).format(error=result.error)
                self._send(200, build_sync(lang, p1_err=err_str))

        elif path == '/simulators/toggle':
            svc    = body.get('service', '').strip()
            action = body.get('action', 'start').strip()
            if svc in SIMULATOR_SERVICES:
                if action == 'start':
                    subprocess.run(['systemctl', 'unmask', svc],
                                   capture_output=True)
                    r = subprocess.run(['systemctl', 'start', svc],
                                       capture_output=True)
                    if r.returncode != 0:
                        err_txt = r.stderr.decode(errors='replace').strip() \
                                  or f'{svc}: start failed'
                        self._send(200, build_simulators(lang, err=err_txt))
                    else:
                        fresh = {svc: get_service_status(svc)}
                        msg = f'{svc} {tr("sim_started", lang)}'
                        self._send(200, build_simulators(lang, msg=msg,
                                                         fresh=fresh))
                else:
                    r = subprocess.run(['systemctl', 'stop', svc],
                                       capture_output=True)
                    if r.returncode != 0:
                        err_txt = r.stderr.decode(errors='replace').strip() \
                                  or f'{svc}: stop failed'
                        self._send(200, build_simulators(lang, err=err_txt))
                    else:
                        fresh = {svc: get_service_status(svc)}
                        msg = f'{svc} {tr("sim_stopped", lang)}'
                        self._send(200, build_simulators(lang, msg=msg,
                                                         fresh=fresh))
            else:
                self._redirect('/simulators')

        elif path == '/wifi-ssh/toggle':
            toggle_wifi_ssh()
            referer = self.headers.get('Referer', '/')
            safe_ref = urlparse(referer).path or '/'
            self._redirect(safe_ref)

        elif path == '/config-ap-pw/toggle':
            set_config_ap_pw(not get_config_ap_pw())
            referer = self.headers.get('Referer', '/wizard')
            safe_ref = urlparse(referer).path or '/wizard'
            self._redirect(safe_ref)

        elif path == '/lang':
            new_lang = body.get('lang', 'en')
            if new_lang not in ('nl', 'en'):
                new_lang = 'en'
            # Save to file for persistence across sessions
            try:
                LANG_FILE.write_text(new_lang + '\n')
            except OSError:
                pass
            cookie = (
                f'lang={new_lang}; Path=/; Max-Age=31536000; SameSite=Lax'
            )
            referer = self.headers.get('Referer', '/')
            parsed_ref = urlparse(referer)
            safe_ref = parsed_ref.path or '/'
            self._redirect(safe_ref, extra_headers=[('Set-Cookie', cookie)])

        elif path == '/factory-reset':
            logs_choice = body.get('logs', 'keep')

            # Stop services (except mosquitto/SSH)
            reset_services = [
                'otgw-listener', 'ds18b20-listener', 'mqtt-logger',
                'log-sync.timer', 'heartbeat.timer',
            ]
            subprocess.run(
                ['systemctl', 'disable', '--now'] + reset_services,
                capture_output=True,
            )
            for sim in SIMULATOR_SERVICES:
                subprocess.run(['systemctl', 'stop', sim], capture_output=True)

            # Remove config files
            WIFI_CONF.unlink(missing_ok=True)
            if SYNC_D_DIR.exists():
                for conf in SYNC_D_DIR.glob('*.conf'):
                    conf.unlink()
            # Reset mode
            try:
                MODE_FILE.write_text('production\n')
            except OSError:
                pass

            # Handle logs
            log_dir = LOG_DIR
            if logs_choice == 'delete':
                subprocess.run(
                    ['sh', '-c', f'rm -rf {log_dir}/* {log_dir}/.*'],
                    capture_output=True,
                )

            msg = tr('reset_done', lang)
            self._send(200, page(
                tr('reset_title', lang),
                f'<div class="msg-ok">{html.escape(msg)}</div>',
                lang,
            ))
            threading.Timer(2.0, lambda: os.system('systemctl reboot')).start()

        elif path == '/maintenance/run':
            provision = Path('/opt/otgw/scripts/provision.sh')
            output = ''
            if provision.exists():
                try:
                    r = subprocess.run(
                        [str(provision)],
                        capture_output=True, text=True, timeout=120,
                    )
                    output = (r.stdout + r.stderr).strip()
                except Exception as exc:
                    output = str(exc)
            else:
                output = f'provision.sh not found at {provision}'
            self._send(200, build_maintenance(lang, output=output))

        elif path == '/maintenance/install-packages':
            ok, output = otgw_api.install_packages()
            if ok:
                self._send(200, build_maintenance(
                    lang, msg=tr('pkg_install_ok', lang), output=output))
            else:
                self._send(200, build_maintenance(
                    lang, err=tr('pkg_install_err', lang), output=output))

        elif path == '/log-partition/setup':
            lp = get_log_partition_info()
            if lp['mounted'] and lp['fstype'] == 'vfat':
                self._send(200, build_log_partition(
                    lang, msg=tr('logpart_already', lang).format(dev=lp['device'])))
                return
            output_lines: list[str] = []
            ok = True
            disk = '/dev/mmcblk0'
            log_part = '/dev/mmcblk0p3'
            log_dir = '/var/log/otgw'

            def _run_step(cmd: list[str], label: str) -> bool:
                output_lines.append(f'$ {" ".join(cmd)}')
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                    if r.stdout.strip():
                        output_lines.append(r.stdout.strip())
                    if r.returncode != 0:
                        output_lines.append(f'ERROR: {r.stderr.strip()}')
                        return False
                    output_lines.append('OK')
                    return True
                except Exception as exc:
                    output_lines.append(f'ERROR: {exc}')
                    return False

            # Check tools
            for tool in ('parted', 'mkfs.vfat', 'resize2fs', 'partprobe'):
                r = subprocess.run(['which', tool], capture_output=True)
                if r.returncode != 0:
                    err_msg = tr('logpart_failed', lang).format(v=f'{tool} not found')
                    self._send(200, build_log_partition(lang, err=err_msg))
                    return

            # Check disk size
            try:
                r = subprocess.run(
                    ['lsblk', '-b', '-dn', '-o', 'SIZE', disk],
                    capture_output=True, text=True, timeout=10)
                total_bytes = int(r.stdout.strip())
            except Exception as exc:
                self._send(200, build_log_partition(
                    lang, err=tr('logpart_failed', lang).format(v=str(exc))))
                return

            log_size = 1 * 1024 * 1024 * 1024  # 1 GB
            if total_bytes <= 2 * log_size:
                self._send(200, build_log_partition(
                    lang, err=tr('logpart_failed', lang).format(
                        v=f'Disk too small ({total_bytes // 1024 // 1024} MB)')))
                return

            new_p2_end = total_bytes - log_size

            # Stop mqtt-logger during partition ops
            subprocess.run(['systemctl', 'stop', 'mqtt-logger'], capture_output=True)

            # Backup existing logs
            import tempfile
            tmp_copy = None
            if Path(log_dir).is_dir():
                tmp_copy = tempfile.mkdtemp(prefix='otgw_logs_backup_')
                subprocess.run(['cp', '-a', log_dir + '/.', tmp_copy + '/'],
                               capture_output=True)

            steps = [
                (['parted', '-s', disk, 'resizepart', '2', f'{new_p2_end}B'],
                 'Resize root partition'),
                (['resize2fs', f'{disk}p2'], 'Resize ext4 filesystem'),
                (['partprobe', disk], 'Re-read partition table'),
                (['parted', '-s', disk, 'mkpart', 'primary', 'fat32',
                  f'{new_p2_end}B', '100%'], 'Create log partition'),
                (['mkfs.vfat', '-F', '32', '-n', 'OTGW-LOGS', log_part],
                 'Format as FAT32'),
            ]
            for cmd, label in steps:
                output_lines.append(f'\n--- {label} ---')
                if not _run_step(cmd, label):
                    ok = False
                    break

            if ok:
                # Add fstab entry
                fstab_line = (
                    f'{log_part} {log_dir} vfat '
                    f'defaults,noatime,uid=1000,gid=1000,umask=022 0 2\n'
                )
                try:
                    with open('/etc/fstab') as f:
                        fstab = f.read()
                    if log_part not in fstab:
                        output_lines.append('\n--- Update /etc/fstab ---')
                        r = subprocess.run(
                            ['tee', '-a', '/etc/fstab'],
                            input=f'\n# OTGW log partition — FAT32\n{fstab_line}',
                            capture_output=True, text=True, timeout=10)
                        ok = (r.returncode == 0)
                        output_lines.append('OK' if ok else f'ERROR: {r.stderr}')
                except Exception as exc:
                    ok = False
                    output_lines.append(f'fstab error: {exc}')

            if ok:
                output_lines.append('\n--- Mount log partition ---')
                subprocess.run(['mkdir', '-p', log_dir], capture_output=True)
                r = subprocess.run(['mount', log_dir], capture_output=True, text=True)
                if r.returncode != 0:
                    ok = False
                    output_lines.append(f'ERROR: {r.stderr.strip()}')
                else:
                    output_lines.append('OK')

            if ok and tmp_copy:
                output_lines.append('\n--- Restore log files ---')
                subprocess.run(['cp', '-a', tmp_copy + '/.', log_dir + '/'],
                               capture_output=True)
                output_lines.append('OK')

            if tmp_copy:
                subprocess.run(['rm', '-rf', tmp_copy], capture_output=True)

            subprocess.run(['systemctl', 'start', 'mqtt-logger'], capture_output=True)

            full_output = '\n'.join(output_lines)
            if ok:
                self._send(200, build_log_partition(
                    lang, msg=tr('logpart_done', lang), output=full_output))
            else:
                self._send(200, build_log_partition(
                    lang, err=tr('logpart_failed', lang).format(v='see output below'),
                    output=full_output))

        elif path in ('/ap/exit', '/stop-ap'):
            # Request config_sensor to stop the AP via flag file.
            # config_sensor's _software_toggle_monitor thread picks this up
            # within 2 s and calls exit_config_ap() which handles firewall
            # cleanup and interface teardown properly.
            try:
                AP_TOGGLE_FLAG.write_text('stop\n')
            except OSError:
                pass
            if path == '/stop-ap':
                body_html = (
                    f'<div class="card">'
                    f'<div class="card-title">'
                    f'{tr("stop_ap_title", lang)}</div>'
                    f'<div class="card-body">'
                    f'{tr("stop_ap_body", lang)}</div>'
                    f'</div>'
                )
                self._send(200, page(
                    tr('stop_ap_title', lang), body_html, lang,
                ))
            else:
                self._redirect('/')

        elif path == '/reboot':
            self._send(200, page(
                'Rebooting…',
                '<div class="card"><div class="card-body">Rebooting…</div></div>',
                lang,
            ))
            threading.Timer(1.0, lambda: os.system('systemctl reboot')).start()

        elif path == '/shutdown':
            self._send(200, page(
                'Shutting down…',
                '<div class="card"><div class="card-body">Shutting down…</div></div>',
                lang,
            ))
            threading.Timer(1.0, lambda: os.system('systemctl poweroff')).start()

        elif path == '/api/set-time':
            # Set system clock from browser timestamp — called automatically by JS
            # on every page load.  No-op if NTP is already synchronised.
            ts_str = body.get('ts', '')
            try:
                ts = float(ts_str)
            except (ValueError, TypeError):
                self._send(400, json.dumps({'status': 'invalid'}),
                           'application/json')
                return
            if ts < 1_700_000_000:  # sanity: before Nov 2023
                self._send(400, json.dumps({'status': 'invalid'}),
                           'application/json')
                return
            ntp_r = subprocess.run(
                ['timedatectl', 'show', '--property=NTPSynchronized', '--value'],
                capture_output=True, text=True, timeout=5,
            )
            if ntp_r.stdout.strip().lower() == 'yes':
                self._send(200, json.dumps({'status': 'ntp_ok'}),
                           'application/json')
                return
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            date_str = dt.strftime('%Y-%m-%d %H:%M:%S')
            r = subprocess.run(
                ['date', '-u', '-s', date_str],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                subprocess.run(['fake-hwclock', 'save'],
                               capture_output=True, timeout=5)
                try:
                    Path('/run/otgw-time-source').write_text('browser\n')
                except OSError:
                    pass
                log.info('System clock set from browser: %s UTC', date_str)
                self._send(200, json.dumps({'status': 'ok', 'time': date_str}),
                           'application/json')
            else:
                log.warning('date -s failed: %s', r.stderr.strip())
                self._send(500, json.dumps({'status': 'error'}),
                           'application/json')

        else:
            self._redirect('/')


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info('config-portal starting on port %d', PORT)
    _mqtt_cache.start()
    _checks_cache.start()   # first collect runs in background; server opens immediately
    log.info('HTTP server starting on port %d', PORT)
    class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedHTTPServer(('', PORT), PortalHandler)
    server.serve_forever()


if __name__ == '__main__':
    main()
