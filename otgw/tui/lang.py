"""
Localisation module for the OTGW Logger Setup TUI.

Usage:
    from tui.lang import t, set_lang

    print(t('main_title'))          # prints in current language
    set_lang('en')                   # switch to English

Language is read from /etc/otgw/language ('nl' or 'en').
Default: 'nl' (Dutch).

Keys follow the pattern  section_description  (all lower-case, underscores).
Add new keys here; never use bare string literals in otgw_setup.py.
"""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

_LANG_FILE = Path('/etc/otgw/language')
_LANG: str = 'en'   # module-level default; overridden by init() or set_lang()


def init() -> None:
    """Read language from /etc/otgw/language and set the module lang."""
    global _LANG
    try:
        lang = _LANG_FILE.read_text().strip().lower()
        if lang in ('nl', 'en'):
            _LANG = lang
    except OSError:
        pass  # file absent → keep default


def set_lang(lang: str) -> None:
    """Set the active language ('nl' or 'en')."""
    global _LANG
    if lang in ('nl', 'en'):
        _LANG = lang


def get_lang() -> str:
    """Return the active language code."""
    return _LANG


def t(key: str) -> str:
    """Return the translated string for *key* in the active language.

    Falls back to English if the key has no Dutch translation.
    Raises KeyError for unknown keys (programming error).
    """
    entry = STRINGS[key]
    return entry.get(_LANG) or entry['en']


# ---------------------------------------------------------------------------
# String table
#
# Each entry:  key → {'nl': '...', 'en': '...'}
# Keys absent from 'nl' fall back to 'en' at runtime.
# ---------------------------------------------------------------------------

STRINGS: dict[str, dict[str, str]] = {

    # -----------------------------------------------------------------------
    # Generic / shared
    # -----------------------------------------------------------------------
    'back':
        {'nl': 'Terug', 'en': 'Back'},
    'exit':
        {'nl': 'Afsluiten', 'en': 'Exit'},
    'press_enter':
        {'nl': 'Druk op Enter om door te gaan...', 'en': 'Press Enter to continue...'},
    'select_option':
        {'nl': 'Kies een optie', 'en': 'Select option'},
    'choice_prompt':
        {'nl': 'Keuze', 'en': 'Choice'},
    'confirm_yes_no':
        {'nl': '[Y/n]', 'en': '[Y/n]'},
    'confirm_no_yes':
        {'nl': '[y/N]', 'en': '[y/N]'},
    'invalid_number':
        {'nl': 'Voer een geldig getal in', 'en': 'Please enter a valid number'},
    'number_range':
        {'nl': 'Voer een getal in tussen 1 en {n}',
         'en': 'Please enter a number between 1 and {n}'},
    'enter_to_cancel':
        {'nl': 'Enter om te annuleren', 'en': 'Enter to cancel'},

    # -----------------------------------------------------------------------
    # Dry-run mode
    # -----------------------------------------------------------------------
    'dry_run_banner':
        {'nl': '  [TESTMODUS - Er worden geen wijzigingen aangebracht]\n',
         'en': '  [DRY-RUN MODE - No changes will be made]\n'},
    'dry_run_prefix':
        {'nl': '[TESTMODUS] Zou uitvoeren: ', 'en': '[DRY-RUN] Would run: '},

    # -----------------------------------------------------------------------
    # Main menu
    # -----------------------------------------------------------------------
    'main_title':
        {'nl': 'OTGW Logger — Installatie', 'en': 'OTGW Logger Setup'},
    'goodbye':
        {'nl': '\nTot ziens!\n', 'en': '\nGoodbye!\n'},

    # Network summary labels
    'net_ethernet':
        {'nl': '  Ethernet: {v}', 'en': '  Ethernet: {v}'},
    'net_wifi_no_adapter':
        {'nl': '  WiFi:     geen adapter', 'en': '  WiFi:     no adapter'},
    'net_wifi_connected':
        {'nl': '  WiFi:     {v}', 'en': '  WiFi:     {v}'},
    'net_wifi_not_connected':
        {'nl': '  WiFi:     niet verbonden', 'en': '  WiFi:     not connected'},
    'net_internet_ok':
        {'nl': '  Internet: OK', 'en': '  Internet: OK'},
    'net_internet_fail':
        {'nl': '  Internet: geen verbinding', 'en': '  Internet: no connection'},
    'net_wifi_switch':
        {'nl': '  WiFi-modus: {v}', 'en': '  WiFi mode: {v}'},

    # Service summary
    'svc_all_running':
        {'nl': '  Diensten: alle actief ({n}/{total})',
         'en': '  Services: all running ({n}/{total})'},
    'svc_partial':
        {'nl': '  Diensten: {n}/{total} actief',
         'en': '  Services: {n}/{total} running'},

    # Package summary
    'pkg_missing':
        {'nl': '  Pakketten: {n} ontbreekt', 'en': '  Packages: {n} missing'},
    'pkg_all_installed':
        {'nl': '  Pakketten: alle geïnstalleerd', 'en': '  Packages: all installed'},

    # Mode / WiFi SSH status badges
    'mode_test_badge':
        {'nl': 'MODUS: TEST', 'en': 'MODE: TEST'},
    'mode_prod_badge':
        {'nl': 'MODUS: PROD', 'en': 'MODE: PROD'},
    'wifi_ssh_on_badge':
        {'nl': 'WiFi SSH: AAN', 'en': 'WiFi SSH: ON'},
    'wifi_ssh_off_badge':
        {'nl': 'WiFi SSH: uit', 'en': 'WiFi SSH: off'},

    # Main menu options
    'menu_wifi':
        {'nl': 'WiFi instellen', 'en': 'Configure WiFi'},
    'menu_p1':
        {'nl': 'P1-dongle instellen', 'en': 'Configure P1 Dongle'},
    'menu_sync':
        {'nl': 'Logboeksynchronisatie instellen', 'en': 'Configure Log Sync'},
    'menu_p1_usb_sync':
        {'nl': 'P1-dongle USB-synchronisatie', 'en': 'P1 Dongle USB Sync'},
    'p1_usb_sync_title':
        {'nl': 'P1-dongle USB-synchronisatie', 'en': 'P1 Dongle USB Sync'},
    'p1_usb_sync_intro':
        {'nl': 'Gebufferde gasmetingen ophalen van de P1-dongle via USB.',
         'en': 'Retrieve buffered gas readings from the P1 dongle via USB.'},
    'p1_usb_sync_status':
        {'nl': 'Dongle-buffer: {count} records in wachtrij ({bytes} bytes)',
         'en': 'Dongle buffer: {count} records pending ({bytes} bytes)'},
    'p1_usb_sync_status_err':
        {'nl': 'Kan status van dongle-buffer niet ophalen.',
         'en': 'Could not query dongle buffer status.'},
    'p1_usb_sync_no_port':
        {'nl': 'P1-dongle niet gevonden. Sluit de dongle aan en probeer opnieuw.',
         'en': 'P1 dongle not found. Connect dongle via USB and retry.'},
    'p1_usb_sync_running':
        {'nl': 'Ophalen... {n} records ontvangen.',
         'en': 'Retrieving... {n} records received.'},
    'p1_usb_sync_done':
        {'nl': 'Klaar: {merged} nieuw, {skipped} duplicaat, {malformed} ongeldig.',
         'en': 'Done: {merged} new, {skipped} duplicate, {malformed} malformed.'},
    'p1_usb_sync_error':
        {'nl': 'Mislukt: {error}', 'en': 'Failed: {error}'},
    'p1_usb_sync_clear_prompt':
        {'nl': 'Dongle markeren als ontvangen? (voorkomt MQTT-hertransmissie)',
         'en': 'Mark dongle as received? (prevents MQTT re-delivery)'},
    'p1_usb_sync_cleared':
        {'nl': 'Dongle gewist: {msg}', 'en': 'Dongle cleared: {msg}'},
    'p1_usb_sync_clear_err':
        {'nl': 'Wissen mislukt: {msg}', 'en': 'Clear failed: {msg}'},
    'menu_verify':
        {'nl': 'Alle systemen controleren', 'en': 'Verify All Systems'},
    'menu_data_flow':
        {'nl': 'Datastromen controleren (MQTT)', 'en': 'Check Data Flow (MQTT)'},
    'menu_live':
        {'nl': 'Live data bekijken', 'en': 'View Live Data'},
    'menu_maintenance':
        {'nl': 'Systeemonderhoud', 'en': 'System Maintenance'},
    'menu_mode':
        {'nl': 'Modus instellen (test / productie)', 'en': 'Set Mode (test / production)'},
    'menu_wifi_ssh':
        {'nl': 'WiFi SSH in-/uitschakelen', 'en': 'Toggle WiFi SSH'},
    'menu_config_ap_pw':
        {'nl': 'AP-wachtwoord in-/uitschakelen',
         'en': 'Toggle Config AP Password'},
    'menu_simulators':
        {'nl': 'Simulatoren (testen zonder hardware)', 'en': 'Simulators (test without hardware)'},
    'menu_install_pkgs':
        {'nl': 'Ontbrekende pakketten installeren ({n})',
         'en': 'Install Missing Packages ({n})'},

    # -----------------------------------------------------------------------
    # Mode selection
    # -----------------------------------------------------------------------
    'mode_title':
        {'nl': 'Modus instellen', 'en': 'Set Mode'},
    'mode_current':
        {'nl': 'Huidige modus: {v}', 'en': 'Current mode: {v}'},
    'mode_test_desc1':
        {'nl': '  test        — MQTT-prefix: test/  VPS-pad: otgw-logs/test/',
         'en': '  test        — MQTT prefix: test/  VPS path: otgw-logs/test/'},
    'mode_test_desc2':
        {'nl': '                WiFi SSH wordt automatisch ingeschakeld',
         'en': '                WiFi SSH is enabled automatically'},
    'mode_prod_desc1':
        {'nl': '  productie   — MQTT-prefix: home/  VPS-pad: otgw-logs/',
         'en': '  production  — MQTT prefix: home/  VPS path: otgw-logs/'},
    'mode_prod_desc2':
        {'nl': '                WiFi SSH wordt automatisch uitgeschakeld',
         'en': '                WiFi SSH is disabled automatically'},
    'mode_switch_test':
        {'nl': 'Naar TEST-modus schakelen', 'en': 'Switch to TEST mode'},
    'mode_switch_prod':
        {'nl': 'Naar PRODUCTIE-modus schakelen', 'en': 'Switch to PRODUCTION mode'},
    'mode_already':
        {'nl': '\nAl in {v}-modus.', 'en': '\nAlready in {v} mode.'},
    'mode_confirm_switch':
        {'nl': '\nNaar {v}-modus schakelen?', 'en': '\nSwitch to {v} mode?'},
    'mode_set':
        {'nl': '\nModus ingesteld op {v}.', 'en': '\nMode set to {v}.'},
    'mode_wifi_ssh_auto':
        {'nl': 'WiFi SSH automatisch {v}.', 'en': 'WiFi SSH {v} automatically.'},
    'mode_restart_hint':
        {'nl': 'Herstart de diensten om de nieuwe MQTT-prefix te activeren:',
         'en': 'Restart listener services to apply the new MQTT prefix:'},
    'mode_restart_cmd':
        {'nl': '  sudo systemctl restart otgw-listener ds18b20-listener heartbeat.timer',
         'en': '  sudo systemctl restart otgw-listener ds18b20-listener heartbeat.timer'},

    # -----------------------------------------------------------------------
    # WiFi SSH toggle
    # -----------------------------------------------------------------------
    'wifi_ssh_title':
        {'nl': 'WiFi SSH in-/uitschakelen', 'en': 'Toggle WiFi SSH'},
    'wifi_ssh_state':
        {'nl': 'Huidige toestand: WiFi SSH {v}', 'en': 'Current state: WiFi SSH {v}'},
    'wifi_ssh_mode':
        {'nl': 'Huidige modus:    {v}', 'en': 'Current mode:  {v}'},
    'wifi_ssh_warning':
        {'nl': 'WAARSCHUWING: WiFi SSH inschakelen in PRODUCTIE-modus is een beveiligingsrisico.\n'
               'Iedereen op hetzelfde WiFi-netwerk kan via SSH bij deze Pi.\n'
               'Schakel dit alleen tijdelijk in voor foutopsporing, en daarna weer uit.',
         'en': 'WARNING: Enabling WiFi SSH in PRODUCTION mode is a security risk.\n'
               'Anyone on the same home WiFi network can reach this Pi over SSH.\n'
               'Only enable this temporarily for debugging, then disable again.'},
    'wifi_ssh_enable':
        {'nl': 'WiFi SSH inschakelen', 'en': 'Enable WiFi SSH'},
    'wifi_ssh_disable':
        {'nl': 'WiFi SSH uitschakelen', 'en': 'Disable WiFi SSH'},
    'wifi_ssh_confirm':
        {'nl': '{action}?', 'en': '{action}?'},
    'wifi_ssh_done':
        {'nl': '\nWiFi SSH {v}.', 'en': '\nWiFi SSH {v}.'},
    'wifi_ssh_enabled_hint1':
        {'nl': 'U kunt nu via WiFi inloggen op deze Pi.', 'en': 'You can now SSH to this Pi over WiFi.'},
    'wifi_ssh_enabled_hint2':
        {'nl': 'Vergeet niet om dit vóór de uitrol in het veld weer uit te schakelen.',
         'en': 'Remember to disable again before field deployment.'},

    # -----------------------------------------------------------------------
    # Config AP password toggle
    # -----------------------------------------------------------------------
    'config_ap_pw_title':
        {'nl': 'AP-wachtwoord in-/uitschakelen',
         'en': 'Toggle Config AP Password'},
    'config_ap_pw_state_on':
        {'nl': '  AP-wachtwoord is INGESCHAKELD (WPA2-beveiligd).',
         'en': '  Config AP password is ENABLED (WPA2-protected).'},
    'config_ap_pw_state_off':
        {'nl': '  AP-wachtwoord is UITGESCHAKELD (open AP, geen wachtwoord).',
         'en': '  Config AP password is DISABLED (open AP, no password).'},
    'config_ap_pw_enable_hint':
        {'nl': '  Schakel het AP-wachtwoord in zodat de P1-dongle verbonden\n'
               '  blijft tijdens de magneet-herstelperiode.',
         'en': '  Enable the AP password so the P1 dongle stays connected\n'
               '  during the hall-magnet recovery window.'},
    'config_ap_pw_enable_prompt':
        {'nl': 'AP-wachtwoord inschakelen (WPA2)?',
         'en': 'Enable AP password (WPA2)?'},
    'config_ap_pw_disable_prompt':
        {'nl': 'AP-wachtwoord uitschakelen (open AP)?',
         'en': 'Disable AP password (open AP)?'},
    'config_ap_pw_enabled':
        {'nl': '  AP-wachtwoord ingeschakeld. Voer onderstaande gegevens\n'
               '  in op de P1-dongle:',
         'en': '  AP password enabled. Enter the credentials below\n'
               '  into the P1 dongle:'},
    'config_ap_pw_disabled':
        {'nl': '  AP-wachtwoord uitgeschakeld. Config AP is nu open.',
         'en': '  AP password disabled. Config AP is now open.'},
    'config_ap_pw_enter_in_dongle':
        {'nl': '  Gebruik de portal of TUI om deze gegevens naar de P1-dongle te sturen.',
         'en': '  Use the portal or TUI to send these credentials to the P1 dongle.'},

    # State words (enabled / disabled)
    'state_enabled':
        {'nl': 'ingeschakeld', 'en': 'enabled'},
    'state_disabled':
        {'nl': 'uitgeschakeld', 'en': 'disabled'},
    'state_enabled_upper':
        {'nl': 'INGESCHAKELD', 'en': 'ENABLED'},
    'state_disabled_upper':
        {'nl': 'UITGESCHAKELD', 'en': 'DISABLED'},

    # -----------------------------------------------------------------------
    # Package management
    # -----------------------------------------------------------------------
    'pkg_title':
        {'nl': 'Ontbrekende pakketten installeren', 'en': 'Install Missing Packages'},
    'pkg_all_ok':
        {'nl': 'Alle benodigde pakketten zijn geïnstalleerd.',
         'en': 'All required packages are installed.'},
    'pkg_missing_list':
        {'nl': 'Ontbrekende pakketten:', 'en': 'Missing packages:'},
    'pkg_install_confirm':
        {'nl': 'Deze pakketten nu installeren?', 'en': 'Install these packages now?'},
    'pkg_updating':
        {'nl': '\nPakkettenlijst bijwerken...', 'en': '\nUpdating package lists...'},
    'pkg_update_failed':
        {'nl': '\napt-get update mislukt (geen internet?)',
         'en': '\napt-get update failed (no internet?)'},
    'pkg_installing':
        {'nl': '\n{n} pakket(ten) installeren...', 'en': '\nInstalling {n} packages...'},
    'pkg_install_ok':
        {'nl': '\nAlle pakketten succesvol geïnstalleerd.',
         'en': '\nAll packages installed successfully.'},
    'pkg_install_fail':
        {'nl': '\nEnkele pakketten konden niet worden geïnstalleerd.',
         'en': '\nSome packages failed to install.'},

    # -----------------------------------------------------------------------
    # System maintenance
    # -----------------------------------------------------------------------
    'maint_title':
        {'nl': 'Systeemonderhoud', 'en': 'System Maintenance'},
    'maint_desc1':
        {'nl': 'Dit voert provision.sh uit om te controleren of alle pakketten, diensten en',
         'en': 'This runs provision.sh to ensure all packages, services, and'},
    'maint_desc2':
        {'nl': 'instellingen correct zijn geïnstalleerd. Veilig om opnieuw uit te voeren —',
         'en': 'configuration are correctly installed. It is safe to re-run'},
    'maint_desc3':
        {'nl': 'alle stappen zijn idempotent (meerdere keren uitvoeren geeft hetzelfde resultaat).',
         'en': 'at any time — running it multiple times has the same effect as running it once.'},
    'maint_desc4':
        {'nl': 'Systeempakketten worden NIET geupgrade. Bouw een nieuw image en',
         'en': 'It does NOT upgrade system packages. To upgrade, build and'},
    'maint_desc5':
        {'nl': 'test dit op een Pi, en zet het daarna uit.',
         'en': 'flash a new image on a test Pi first, then deploy.'},
    'maint_not_found':
        {'nl': 'Fout: {v} niet gevonden.', 'en': 'Error: {v} not found.'},
    'maint_not_installed':
        {'nl': 'Is de software geïnstalleerd in /opt/otgw/?',
         'en': 'Is the software installed in /opt/otgw/?'},
    'maint_check_only':
        {'nl': 'Alleen controleren (testmodus — toon wat ontbreekt)',
         'en': 'Check only (dry run — show what is missing)'},
    'maint_check_fix':
        {'nl': 'Controleren en herstellen (voer ontbrekende stappen uit)',
         'en': 'Check and fix (apply any missing steps)'},
    'maint_reboot':
        {'nl': 'Herstart Pi', 'en': 'Reboot Pi'},
    'maint_shutdown':
        {'nl': 'Pi uitschakelen', 'en': 'Shut down Pi'},
    'maint_ok':
        {'nl': 'Provision-script succesvol uitgevoerd.',
         'en': 'Provision script completed successfully.'},
    'maint_fail':
        {'nl': 'Provision-script afgesloten met fouten (zie hierboven).',
         'en': 'Provision script finished with errors (see above).'},
    'maint_sudo_missing':
        {'nl': 'sudo niet gevonden — draait u al als root?',
         'en': 'sudo not found — are you already running as root?'},

    # -----------------------------------------------------------------------
    # WiFi configuration
    # -----------------------------------------------------------------------
    'wifi_title':
        {'nl': 'WiFi instellen', 'en': 'WiFi Configuration'},
    'wifi_status_header':
        {'nl': 'Huidige WiFi-status:', 'en': 'Current WiFi status:'},
    'wifi_no_iface':
        {'nl': '  Interface: geen gevonden', 'en': '  Interface: none found'},
    'wifi_pi1_hint':
        {'nl': '  (Pi 1 heeft een USB WiFi-adapter nodig)\n',
         'en': '  (Pi 1 needs a USB WiFi adapter)\n'},
    'wifi_iface':
        {'nl': '  Interface: {v}', 'en': '  Interface: {v}'},
    'wifi_connected':
        {'nl': '  Verbonden: {v}', 'en': '  Connected: {v}'},
    'wifi_ip':
        {'nl': '  IP-adres: {v}', 'en': '  IP address: {v}'},
    'wifi_signal':
        {'nl': '  Signaal: {v}%', 'en': '  Signal: {v}%'},
    'wifi_not_connected':
        {'nl': '  Verbonden: nee', 'en': '  Connected: no'},
    'wifi_forget_title':
        {'nl': 'WiFi vergeten', 'en': 'Forget WiFi'},
    'wifi_forget_confirm':
        {'nl': 'WiFi-gegevens wissen en verbinding verbreken?', 'en': 'Clear saved WiFi credentials and disconnect?'},
    'wifi_forgotten':
        {'nl': 'WiFi-gegevens gewist.', 'en': 'WiFi credentials cleared.'},
    'wifi_configure_intro':
        {'nl': 'WiFi-gegevens instellen voor de P1-dongle en deze Pi.\n',
         'en': 'Configure WiFi credentials for the P1 dongle and this Pi.\n'},
    'wifi_ssid_prompt':
        {'nl': 'WiFi SSID', 'en': 'WiFi SSID'},
    'wifi_password_prompt':
        {'nl': 'WiFi-wachtwoord', 'en': 'WiFi Password'},
    'wifi_ssid_required':
        {'nl': 'SSID is verplicht', 'en': 'SSID is required'},
    'wifi_config_header':
        {'nl': '\nWiFi-instellingen:', 'en': '\nWiFi Configuration:'},
    'wifi_config_ssid':
        {'nl': '  SSID: {v}', 'en': '  SSID: {v}'},
    'wifi_config_pass':
        {'nl': '  Wachtwoord: {v}', 'en': '  Password: {v}'},
    'wifi_save_confirm':
        {'nl': '\nDeze instellingen opslaan?', 'en': '\nSave this configuration?'},
    'wifi_saved':
        {'nl': '\nWiFi-instellingen opgeslagen.', 'en': '\nWiFi configuration saved.'},
    'wifi_connect_now':
        {'nl': 'Nu verbinden met WiFi?', 'en': 'Connect this Pi to WiFi now?'},
    'wifi_connect_fail':
        {'nl': '\n  Mislukt: {v}', 'en': '\n  Failed: {v}'},
    'wifi_testing':
        {'nl': "\nVerbinding testen met '{v}'...", 'en': "\nTesting connection to '{v}'..."},
    'wifi_internet_ok':
        {'nl': '  Internet: bereikbaar', 'en': '  Internet: reachable'},
    'wifi_internet_fail':
        {'nl': '  Internet: niet bereikbaar (WiFi verbonden maar geen internet)',
         'en': '  Internet: not reachable (WiFi connected but no internet)'},
    'wifi_dry_run_connect':
        {'nl': "[TESTMODUS] Zou Pi verbinden met WiFi '{v}'",
         'en': "[DRY-RUN] Would connect Pi to WiFi '{v}'"},

    # -----------------------------------------------------------------------
    # P1 dongle
    # -----------------------------------------------------------------------
    'p1_title':
        {'nl': 'P1-dongle instellen', 'en': 'P1 Dongle Configuration'},
    'p1_intro1':
        {'nl': 'WiFi-gegevens worden via USB naar de P1-dongle gestuurd.\n',
         'en': 'This will send WiFi credentials to the P1 dongle via USB serial.\n'},
    'p1_intro2':
        {'nl': 'Zorg dat de P1-dongle via USB is aangesloten.\n',
         'en': 'Make sure the P1 dongle is connected via USB.\n'},
    'p1_not_found':
        {'nl': 'Geen P1-dongle gevonden.', 'en': 'No P1 dongle detected.'},
    'p1_searching':
        {'nl': '\nZoeken naar /dev/ttyACM* of /dev/ttyUSB* apparaten...',
         'en': '\nLooking for /dev/ttyACM* or /dev/ttyUSB* devices...'},
    'p1_manual_path':
        {'nl': '\nApparaatpad handmatig invoeren (of Enter om te annuleren)',
         'en': '\nEnter device path manually (or press Enter to cancel)'},
    'p1_found':
        {'nl': 'Apparaat gevonden: {v}', 'en': 'Found device: {v}'},
    'p1_use_device':
        {'nl': 'Dit apparaat gebruiken?', 'en': 'Use this device?'},
    'p1_enter_path':
        {'nl': 'Apparaatpad invoeren', 'en': 'Enter device path'},
    'p1_no_wifi':
        {'nl': '\nGeen WiFi-instellingen gevonden. Stel eerst WiFi in.',
         'en': '\nNo WiFi configuration found. Please configure WiFi first.'},
    'p1_broker_prompt':
        {'nl': 'MQTT-broker hostnaam', 'en': 'MQTT Broker hostname'},
    'p1_config_header':
        {'nl': '\nTe versturen instellingen:', 'en': '\nConfiguration to send:'},
    'p1_send_confirm':
        {'nl': '\nDeze instellingen naar de P1-dongle sturen?',
         'en': '\nSend this configuration to the P1 dongle?'},
    'p1_opening':
        {'nl': '\n{v} openen...', 'en': '\nOpening {v}...'},
    'p1_sending':
        {'nl': 'Versturen: {v}...', 'en': 'Sending: {v}...'},
    'p1_waiting':
        {'nl': 'Wachten op antwoord...', 'en': 'Waiting for response...'},
    'p1_success':
        {'nl': '\nGelukt! Antwoord: {v}', 'en': '\nSuccess! Response: {v}'},
    'p1_error_response':
        {'nl': '\nFout van apparaat: {v}', 'en': '\nError from device: {v}'},
    'p1_unexpected':
        {'nl': '\nOnverwacht antwoord: {v}', 'en': '\nUnexpected response: {v}'},
    'p1_serial_error':
        {'nl': '\nSeriële fout: {v}', 'en': '\nSerial error: {v}'},
    'p1_pyserial_missing':
        {'nl': 'Fout: pyserial niet geïnstalleerd. Voer uit: pip install pyserial',
         'en': 'Error: pyserial not installed. Run: pip install pyserial'},

    # -----------------------------------------------------------------------
    # Log sync targets
    # -----------------------------------------------------------------------
    'sync_title':
        {'nl': 'Logboeksynchronisatie-doelen', 'en': 'Log Sync Targets'},
    'sync_configured':
        {'nl': 'Geconfigureerde doelen:\n', 'en': 'Configured targets:\n'},
    'sync_none':
        {'nl': 'Geen synchronisatiedoelen geconfigureerd.\n',
         'en': 'No sync targets configured.\n'},
    'sync_prebaked_hint':
        {'nl': 'Voorgeregistreerde sleutel gedetecteerd — gebruik [T] om de verbinding te testen.',
         'en': 'Pre-registered key detected — use [T] to verify the connection.'},
    'sync_add':
        {'nl': '[A] Doel toevoegen', 'en': '[A] Add target'},
    'sync_remove':
        {'nl': '[R] Doel verwijderen', 'en': '[R] Remove target'},
    'sync_test_one':
        {'nl': '[T] Één doel testen', 'en': '[T] Test one target'},
    'sync_test_all':
        {'nl': '[X] Alle doelen testen', 'en': '[X] Test all targets'},
    'sync_flush':
        {'nl': '[F] Nu doorspoelen & synchroniseren', 'en': '[F] Flush & Sync Now'},
    'sync_flush_started':
        {'nl': 'Doorspoel- en synchronisatieservices gestart.', 'en': 'Flush and sync services started.'},
    'sync_flush_failed':
        {'nl': 'Kon service niet starten: {err}', 'en': 'Could not start service: {err}'},
    'sync_back':
        {'nl': '[B] Terug', 'en': '[B] Back'},

    # Add sync target
    'sync_add_title':
        {'nl': 'Synchronisatiedoel toevoegen', 'en': 'Add Sync Target'},
    'sync_name_prompt':
        {'nl': 'Doelnaam (bijv. primair, hetzner)', 'en': 'Target name (e.g. primary, hetzner)'},
    'sync_invalid_name':
        {'nl': 'Ongeldige naam.', 'en': 'Invalid name.'},
    'sync_host_prompt':
        {'nl': 'Remote hostnaam', 'en': 'Remote hostname'},
    'sync_user_prompt':
        {'nl': 'Remote gebruikersnaam', 'en': 'Remote username'},
    'sync_path_prompt':
        {'nl': 'Remote pad', 'en': 'Remote path'},
    'sync_mode_test_path':
        {'nl': '  (modus=TEST: standaardpad is {v})', 'en': '  (mode=TEST: default path is {v})'},
    'sync_summary_target':
        {'nl': '\nDoel:    {v}', 'en': '\nTarget:  {v}'},
    'sync_summary_host':
        {'nl': '  Host:  {v}', 'en': '  Host:  {v}'},
    'sync_summary_user':
        {'nl': '  Gebruiker: {v}', 'en': '  User:  {v}'},
    'sync_summary_path':
        {'nl': '  Pad:   ~/{v}', 'en': '  Path:  ~/{v}'},
    'sync_summary_key':
        {'nl': '  Sleutel: {v}', 'en': '  Key:   {v}'},
    'sync_save_confirm':
        {'nl': '\nDit doel opslaan?', 'en': '\nSave this target?'},
    'sync_saved':
        {'nl': "\nDoel '{v}' opgeslagen.", 'en': "\nTarget '{v}' saved."},
    'sync_gen_key':
        {'nl': '\nSSH-sleutel aanmaken?', 'en': '\nGenerate SSH key now?'},
    'sync_key_prebaked':
        {'nl': '\nSleutel is vooraf geregistreerd bij image-build — geen VPS-registratie nodig.',
         'en': '\nKey was pre-registered at image build time — no VPS registration needed.'},
    'sync_test_now':
        {'nl': '\nVerbinding nu testen?', 'en': '\nTest connection now?'},

    # Remove sync target
    'sync_remove_title':
        {'nl': 'Synchronisatiedoel verwijderen', 'en': 'Remove Sync Target'},
    'sync_remove_prompt':
        {'nl': '\nNummer van te verwijderen doel (Enter om te annuleren)',
         'en': '\nTarget number to remove (Enter to cancel)'},
    'sync_remove_confirm':
        {'nl': "\nDoel '{v}' verwijderen? Hierdoor wordt het configuratiebestand gewist.",
         'en': "\nRemove target '{v}'? Deletes its config file."},
    'sync_removed':
        {'nl': "Doel '{v}' verwijderd.", 'en': "Target '{v}' removed."},
    'sync_key_kept':
        {'nl': 'SSH-sleutel behouden — verwijder handmatig indien niet meer nodig:',
         'en': 'SSH key kept — remove manually if no longer needed:'},
    'sync_not_found':
        {'nl': "Doel '{v}' niet gevonden.", 'en': "Target '{v}' not found."},

    # Test sync target
    'sync_test_title':
        {'nl': 'Synchronisatiedoel testen', 'en': 'Test Sync Target'},
    'sync_test_prompt':
        {'nl': '\nNummer van te testen doel (Enter om te annuleren)',
         'en': '\nTarget number to test (Enter to cancel)'},
    'sync_testing_all':
        {'nl': '\nAlle synchronisatiedoelen testen...\n',
         'en': '\nTesting all sync targets...\n'},
    'sync_all_ok':
        {'nl': 'Alle doelen OK.', 'en': 'All targets OK.'},
    'sync_some_failed':
        {'nl': 'Een of meer doelen zijn mislukt.', 'en': 'One or more targets failed.'},
    'sync_testing_one':
        {'nl': "\nDoel '{v}' testen...\n", 'en': "\nTesting target '{v}'...\n"},
    'sync_test_ok':
        {'nl': "[{v}] Verbindingstest geslaagd!", 'en': "[{v}] Connection test successful!"},
    'sync_test_fail':
        {'nl': "[{v}] Verbindingstest mislukt.", 'en': "[{v}] Connection test failed."},

    # SSH key
    'ssh_key_generated':
        {'nl': '\nSSH-sleutel aangemaakt: {v}', 'en': '\nSSH key generated: {v}'},
    'ssh_public_key':
        {'nl': '\nPublieke sleutel:', 'en': '\nPublic key:'},
    'ssh_key_fail':
        {'nl': '\nSSH-sleutel aanmaken mislukt: {v}',
         'en': '\nFailed to generate SSH key: {v}'},
    'ssh_install_line':
        {'nl': '\nVoeg deze regel toe aan {user}@{host}:~/.ssh/authorized_keys:',
         'en': '\nAdd this line to {user}@{host}:~/.ssh/authorized_keys:'},
    'ssh_scoped_hint':
        {'nl': '\nDeze sleutel is beperkt tot alleen deze Pi ({v}).'
               ' De logboeken van andere Pis zijn veilig, ook als deze Pi gecompromitteerd is.',
         'en': '\nThis key is scoped to this Pi only ({v}).'
               ' Other Pis\' logs are safe even if this Pi is compromised.'},
    'ssh_install_confirm':
        {'nl': '\nSleutel nu installeren op VPS via SSH'
               ' (vereist wachtwoord of bestaande SSH-toegang tot {user}@{host})?',
         'en': '\nInstall key on VPS now via SSH'
               ' (requires password or existing SSH access to {user}@{host})?'},
    'ssh_connecting':
        {'nl': '\nVerbinden met {v}...', 'en': '\nConnecting to {v}...'},
    'ssh_password_prompt':
        {'nl': 'Mogelijk wordt om een wachtwoord gevraagd.\n',
         'en': 'You may be prompted for a password.\n'},
    'ssh_cmd_fail':
        {'nl': '\nSSH-opdracht mislukt. Voeg de sleutel handmatig toe'
               ' via de bovenstaande regel, en maak de logmap aan:\n  mkdir -p {v}',
         'en': '\nSSH command failed. Add the key manually'
               ' using the line above, and create the log directory:\n  mkdir -p {v}'},
    'ssh_not_found':
        {'nl': '\nssh niet gevonden. Voeg de sleutel handmatig toe via de bovenstaande regel.',
         'en': '\nssh not found. Add the key manually using the line above.'},

    # -----------------------------------------------------------------------
    # Simulators
    # -----------------------------------------------------------------------
    'sim_title':
        {'nl': 'Simulatoren', 'en': 'Simulators'},
    'sim_prod_warning':
        {'nl': 'WAARSCHUWING: Simulatoren zijn alleen voor testen.\n'
               'U bent in PRODUCTIE-modus — nep-data gaat naar uw productie-MQTT-stroom.',
         'en': 'WARNING: Simulators are only for testing.\n'
               'You are in PRODUCTION mode — enabling simulators will\n'
               'publish fake data into your production MQTT stream.'},
    'sim_continue_anyway':
        {'nl': 'Toch doorgaan?', 'en': 'Continue anyway?'},
    'sim_mode':
        {'nl': 'Modus: {v}\n', 'en': 'Mode: {v}\n'},
    'sim_state_simulated':
        {'nl': 'GESIMULEERD', 'en': 'SIMULATED'},
    'sim_state_real':
        {'nl': 'echte hardware', 'en': 'real hardware'},
    'sim_state_off':
        {'nl': 'uit', 'en': 'off'},
    'sim_enable_all':
        {'nl': 'Alle simulatoren inschakelen', 'en': 'Enable all simulators'},
    'sim_disable_all':
        {'nl': 'Alle simulatoren uitschakelen', 'en': 'Disable all simulators'},
    'sim_toggle_one':
        {'nl': 'Individuele simulator wisselen', 'en': 'Toggle individual simulator'},
    'sim_all_started':
        {'nl': '\nAlle simulatoren gestart.', 'en': '\nAll simulators started.'},
    'sim_all_stopped':
        {'nl': '\nAlle simulatoren gestopt.', 'en': '\nAll simulators stopped.'},
    'sim_toggle_prompt':
        {'nl': '\nNummer om te wisselen (Enter om te annuleren)',
         'en': '\nNumber to toggle (Enter to cancel)'},

    # -----------------------------------------------------------------------
    # System verification
    # -----------------------------------------------------------------------
    'verify_title':
        {'nl': 'Systeemcontrole', 'en': 'System Verification'},
    'verify_checking':
        {'nl': '{v} controleren...', 'en': 'Checking {v}...'},
    'verify_summary':
        {'nl': 'Resultaten: {ok}/{total} controles geslaagd',
         'en': 'Results: {ok}/{total} checks passed'},

    # Check names
    'check_image_id':
        {'nl': 'Image-ID', 'en': 'Image ID'},
    'check_wifi':
        {'nl': 'WiFi-verbinding', 'en': 'WiFi Connection'},
    'check_internet':
        {'nl': 'Internet', 'en': 'Internet'},
    'check_mqtt':
        {'nl': 'MQTT-broker', 'en': 'MQTT Broker'},
    'check_otgw':
        {'nl': 'OTGW (luisteraar)', 'en': 'OTGW (listener)'},
    'check_ds18b20':
        {'nl': 'DS18B20 (sensor)', 'en': 'DS18B20 (sensor)'},
    'check_p1':
        {'nl': 'P1 (gas)', 'en': 'P1 (gas)'},
    'check_logger':
        {'nl': 'MQTT-logger', 'en': 'MQTT Logger'},
    'check_sync_timer':
        {'nl': 'Synchronisatietimer', 'en': 'Log Sync Timer'},
    'check_log_dir':
        {'nl': 'Logmap', 'en': 'Log Directory'},
    'check_log_part':
        {'nl': 'Logpartitie (FAT32)', 'en': 'Log Partition (FAT32)'},
    'check_log_disk':
        {'nl': 'Logpartitie-schijfruimte', 'en': 'Log Disk Space'},
    'check_sync_conf':
        {'nl': 'Synchronisatie geconfigureerd', 'en': 'Sync Configured'},
    'check_shutdown_btn':
        {'nl': 'Uitknop-service', 'en': 'Shutdown Button Service'},

    # Check result messages
    'check_running':
        {'nl': 'Actief', 'en': 'Running'},
    'check_not_running':
        {'nl': 'Niet actief', 'en': 'Not running'},
    'check_connected':
        {'nl': 'Verbonden', 'en': 'Connected'},
    'check_no_connection':
        {'nl': 'Geen verbinding', 'en': 'No connection'},
    'check_active':
        {'nl': 'Actief', 'en': 'Active'},
    'check_not_active':
        {'nl': 'Niet actief', 'en': 'Not active'},
    'check_no_wifi_iface':
        {'nl': 'Geen WiFi-interface', 'en': 'No WiFi interface'},
    'check_image_id_missing':
        {'nl': 'Niet ingesteld — voer pi_id.sh of provision.sh uit',
         'en': 'Not set — run pi_id.sh or provision.sh'},
    'check_log_dir_missing':
        {'nl': '{v} bestaat niet', 'en': '{v} does not exist'},
    'check_log_disk_missing':
        {'nl': 'Logmap ontbreekt', 'en': 'Log directory missing'},
    'check_log_disk_low':
        {'nl': ' — LAAG', 'en': ' — LOW'},
    'check_no_sync':
        {'nl': 'Geen synchronisatiedoelen geconfigureerd',
         'en': 'No sync targets configured'},
    'check_legacy_sync':
        {'nl': 'Verouderd sync.conf aanwezig', 'en': 'Legacy sync.conf present'},
    'check_simulated':
        {'nl': 'GESIMULEERD', 'en': 'SIMULATED'},
    'check_sim_conflict':
        {'nl': 'GESIMULEERD + echt beide aan (conflict?)',
         'en': 'SIMULATED + real both on (conflict?)'},
    'check_real_hw':
        {'nl': 'Actief (echte hardware)', 'en': 'Running (real hardware)'},
    'check_real_hw_retrying':
        {'nl': 'Actief maar geen apparaat gevonden (wordt opnieuw geprobeerd)',
         'en': 'Active but no device found (retrying)'},
    'check_no_p1':
        {'nl': 'Geen P1-dongle luisteraar geconfigureerd',
         'en': 'No P1 dongle listener configured'},
    'check_p1_no_data':
        {'nl': 'P1-simulator actief maar geen MQTT-data',
         'en': 'P1 listener active but no MQTT data'},
    'check_error':
        {'nl': '✗ Fout: {v}', 'en': '✗ Error: {v}'},

    # -----------------------------------------------------------------------
    # Data flow check (MQTT subscribe + checklist)
    # -----------------------------------------------------------------------
    'data_flow_title':
        {'nl': 'Datastromen controleren', 'en': 'Check Data Flow'},
    'data_flow_mode':
        {'nl': 'Modus: {v}', 'en': 'Mode: {v}'},
    'data_flow_note':
        {'nl': 'Bronnen die de afgelopen 65 seconden actief waren, worden direct getoond.\n'
               'Ontbrekende bronnen worden nog maximaal 65 seconden afgewacht.',
         'en': 'Sources active in the last 65 s are shown instantly from the background\n'
               'listener. Missing sources are waited for up to 65 s.'},
    'data_flow_waiting':
        {'nl': 'Wachten op MQTT-data... {s}/{max}s', 'en': 'Waiting for MQTT data... {s}/{max}s'},
    'data_flow_source_gas':
        {'nl': 'P1 gas (energie/gas)', 'en': 'P1 gas (energy/gas)'},
    'data_flow_source_otgw':
        {'nl': 'OTGW verwarming (energie/otgw)', 'en': 'OTGW heating (energy/otgw)'},
    'data_flow_source_temp':
        {'nl': 'DS18B20 temperatuur (sensoren/temperatuur)',
         'en': 'DS18B20 temperature (sensors/temperature)'},
    'data_flow_all_ok':
        {'nl': 'Alle datastromen actief.', 'en': 'All data streams active.'},
    'data_flow_partial':
        {'nl': 'Sommige bronnen actief — zie bovenstaande lijst.',
         'en': 'Some sources active — see checklist above.'},
    'data_flow_none':
        {'nl': 'Geen MQTT-data ontvangen. Zijn de diensten actief?',
         'en': 'No MQTT data received. Are the services running?'},
    'data_flow_mqtt_err':
        {'nl': 'Kan geen verbinding maken met MQTT-broker: {v}',
         'en': 'Cannot connect to MQTT broker: {v}'},

    # -----------------------------------------------------------------------
    # Live data view
    # -----------------------------------------------------------------------
    'live_title':
        {'nl': 'Live data bekijken', 'en': 'Live Data View'},
    'live_stop_hint':
        {'nl': 'Druk op een toets om te stoppen.\n', 'en': 'Press any key to stop.\n'},
    'live_history_header':
        {'nl': '─── Geschiedenis (laatste 65 seconden) ───',
         'en': '─── History (last 65 seconds) ───'},
    'live_no_history':
        {'nl': 'Geen recente berichten in de cache.',
         'en': 'No recent messages in the cache.'},
    'live_live_header':
        {'nl': '─── Live ─── (nieuwe berichten verschijnen hieronder)',
         'en': '─── Live ─── (new messages appear below)'},
    'live_stopped':
        {'nl': '\n\nGestopt.', 'en': '\n\nStopped.'},
    'live_error':
        {'nl': '\nFout: {v}', 'en': '\nError: {v}'},

    # -----------------------------------------------------------------------
    # Language toggle (Step 4)
    # -----------------------------------------------------------------------
    'menu_language':
        {'nl': 'Taal: EN | [NL]', 'en': 'Language: [EN] | NL'},
    'lang_title':
        {'nl': 'Taal instellen', 'en': 'Set Language'},
    'lang_current':
        {'nl': 'Huidige taal: {v}', 'en': 'Current language: {v}'},
    'lang_nl':
        {'nl': 'Nederlands', 'en': 'Dutch (Nederlands)'},
    'lang_en':
        {'nl': 'Engels', 'en': 'English'},
    'lang_set':
        {'nl': '\nTaal ingesteld op {v}.', 'en': '\nLanguage set to {v}.'},

    # -----------------------------------------------------------------------
    # Factory reset
    # -----------------------------------------------------------------------
    'menu_factory_reset':
        {'nl': 'Fabrieksinstellingen herstellen', 'en': 'Factory Reset'},
    'reset_title':
        {'nl': 'Fabrieksinstellingen herstellen', 'en': 'Factory Reset'},
    'reset_warning':
        {'nl': (
            'Dit verwijdert de WiFi-config, sync-doelen, stopt alle diensten\n'
            'en herstelt de standaardinstellingen. SSH-sleutels blijven bewaard.\n'
            'De Pi-ID en taalvoorkeur blijven ongewijzigd.'
         ),
         'en': (
            'This removes WiFi config, sync targets, stops all services,\n'
            'and resets settings to defaults. SSH keys are kept.\n'
            'Pi ID and language preference are preserved.'
         )},
    'reset_confirm':
        {'nl': 'Weet je zeker dat je wilt resetten?',
         'en': 'Are you sure you want to reset?'},
    'reset_log_title':
        {'nl': 'Wat doen met de logbestanden?', 'en': 'What to do with log files?'},
    'reset_log_keep':
        {'nl': 'Bewaren (niet aanraken)', 'en': 'Keep (do not touch)'},
    'reset_log_backup':
        {'nl': 'Verplaatsen naar back-upmap (tijdstempel)', 'en': 'Move to backup folder (timestamped)'},
    'reset_log_delete':
        {'nl': 'Verwijderen', 'en': 'Delete'},
    'reset_stopping':
        {'nl': 'Diensten stoppen en uitschakelen...', 'en': 'Stopping and disabling services...'},
    'reset_removing_wifi':
        {'nl': 'WiFi-configuratie verwijderen...', 'en': 'Removing WiFi config...'},
    'reset_removing_sync':
        {'nl': 'Sync-doelen verwijderen...', 'en': 'Removing sync targets...'},
    'reset_resetting_mode':
        {'nl': 'Modus terugzetten naar productie...', 'en': 'Resetting mode to production...'},
    'reset_logs_kept':
        {'nl': 'Logbestanden ongewijzigd gelaten.', 'en': 'Log files left unchanged.'},
    'reset_logs_backed_up':
        {'nl': 'Logbestanden verplaatst naar {v}', 'en': 'Log files moved to {v}'},
    'reset_logs_deleted':
        {'nl': 'Logbestanden verwijderd.', 'en': 'Log files deleted.'},
    'reset_done':
        {'nl': '\nReset voltooid. De Pi is terug in de beginstatus.',
         'en': '\nReset complete. The Pi is back to its initial state.'},
    'reset_hint':
        {'nl': 'Voer otgw-setup opnieuw uit om opnieuw te configureren.',
         'en': 'Run otgw-setup again to reconfigure.'},

    # -----------------------------------------------------------------------
    # Log partition status and setup
    # -----------------------------------------------------------------------
    'log_part_fat32':
        {'nl': 'Logpartitie: FAT32 ({v}) — leesbaar op alle OS',
         'en': 'Log partition: FAT32 ({v}) — readable on any OS'},
    'log_part_other':
        {'nl': 'Logpartitie: {v} (geen FAT32)',
         'en': 'Log partition: {v} (not FAT32)'},
    'log_part_ext4':
        {'nl': 'Logpartitie: ext4 (geen aparte partitie)',
         'en': 'Log partition: ext4 (no separate partition)'},
    'log_part_title':
        {'nl': 'Logpartitie instellen', 'en': 'Set up log partition'},
    'menu_setup_log_part':
        {'nl': 'FAT32 logpartitie aanmaken',
         'en': 'Set up FAT32 log partition'},
    'log_part_already_fat32':
        {'nl': 'Logpartitie is al FAT32 op {v}. Niets te doen.',
         'en': 'Log partition already FAT32 on {v}. Nothing to do.'},
    'log_part_setup_intro':
        {'nl': (
            '/var/log/otgw bevindt zich nu op de root-partitie (ext4).\n'
            'Dit maakt een aparte 1 GB FAT32-partitie die leesbaar is op\n'
            'Windows en macOS wanneer de SD-kaart wordt ingevoerd.'
         ),
         'en': (
            '/var/log/otgw is currently on the root filesystem (ext4).\n'
            'This creates a separate 1 GB FAT32 partition readable on\n'
            'Windows and macOS when the SD card is inserted.'
         )},
    'log_part_setup_plan':
        {'nl': (
            'Stappen:\n'
            '  1. mqtt-logger stoppen\n'
            '  2. Root-partitie (p2) verkleinen om ruimte vrij te maken\n'
            '  3. 1 GB FAT32-partitie aanmaken (p3)\n'
            '  4. Bestaande logbestanden kopiëren\n'
            '  5. Partitie koppelen op /var/log/otgw\n'
            '  6. mqtt-logger herstarten'
         ),
         'en': (
            'Steps:\n'
            '  1. Stop mqtt-logger\n'
            '  2. Shrink root partition (p2) to make room\n'
            '  3. Create 1 GB FAT32 partition (p3)\n'
            '  4. Copy existing log files\n'
            '  5. Mount at /var/log/otgw\n'
            '  6. Restart mqtt-logger'
         )},
    'log_part_setup_warning':
        {'nl': (
            'WAARSCHUWING: Partitiewijzigingen zijn onomkeerbaar.\n'
            'Zorg dat er een reservekopie is van belangrijke logbestanden.'
         ),
         'en': (
            'WARNING: Partition changes cannot be undone.\n'
            'Make sure important log files are backed up first.'
         )},
    'log_part_setup_confirm':
        {'nl': 'Doorgaan met aanmaken FAT32-logpartitie?',
         'en': 'Proceed with creating FAT32 log partition?'},
    'log_part_tool_missing':
        {'nl': 'Benodigde tool niet gevonden: {v}. Installeer de ontbrekende pakketten eerst.',
         'en': 'Required tool not found: {v}. Install missing packages first.'},
    'log_part_disk_error':
        {'nl': 'Kan schijfgrootte niet lezen voor {v}.',
         'en': 'Cannot read disk size for {v}.'},
    'log_part_disk_too_small':
        {'nl': 'Schijf te klein ({v} MB) — minimaal 2 GB vereist.',
         'en': 'Disk too small ({v} MB) — at least 2 GB required.'},
    'log_part_stopping_logger':
        {'nl': 'mqtt-logger stoppen...', 'en': 'Stopping mqtt-logger...'},
    'log_part_step_resize':
        {'nl': 'Root-partitie (p2) verkleinen...', 'en': 'Resizing root partition (p2)...'},
    'log_part_step_resize2fs':
        {'nl': 'Bestandssysteem aanpassen...', 'en': 'Resizing filesystem...'},
    'log_part_step_partprobe':
        {'nl': 'Partitietabel herladen...', 'en': 'Reloading partition table...'},
    'log_part_step_mkpart':
        {'nl': 'Nieuwe partitie (p3) aanmaken...', 'en': 'Creating new partition (p3)...'},
    'log_part_step_format':
        {'nl': 'FAT32-bestandssysteem aanmaken...', 'en': 'Formatting FAT32 filesystem...'},
    'log_part_step_fstab':
        {'nl': 'Koppelpunt toevoegen aan /etc/fstab...', 'en': 'Adding mount point to /etc/fstab...'},
    'log_part_step_mount':
        {'nl': 'Partitie koppelen op /var/log/otgw...', 'en': 'Mounting at /var/log/otgw...'},
    'log_part_step_restore':
        {'nl': 'Bestaande logbestanden terugzetten...', 'en': 'Restoring existing log files...'},
    'log_part_step_failed':
        {'nl': 'Stap mislukt: {v}', 'en': 'Step failed: {v}'},
    'log_part_setup_done':
        {'nl': 'FAT32-logpartitie succesvol aangemaakt en gekoppeld.',
         'en': 'FAT32 log partition created and mounted successfully.'},
    'log_part_setup_failed':
        {'nl': 'Aanmaken logpartitie mislukt. Controleer de bovenstaande fouten.',
         'en': 'Log partition setup failed. Check errors above.'},

    # -----------------------------------------------------------------------
    # P1 dongle firmware flash
    # -----------------------------------------------------------------------
    'p1_flash_title':
        {'nl': 'P1 dongle firmware flashen',
         'en': 'Flash P1 dongle firmware'},
    'p1_flash_not_found':
        {'nl': 'Firmware niet gevonden: /opt/otgw/firmware/p1_reader.bin',
         'en': 'Firmware not found: /opt/otgw/firmware/p1_reader.bin'},
    'p1_flash_esptool_missing':
        {'nl': 'esptool niet gevonden. Installeer: apt install esptool',
         'en': 'esptool not found. Install: apt install esptool'},
    'p1_flash_flashing':
        {'nl': 'Bezig met flashen op {v}...',
         'en': 'Flashing firmware to {v}...'},
    'p1_flash_success':
        {'nl': 'Firmware succesvol geflasht!',
         'en': 'Firmware flashed successfully!'},
    'p1_flash_failed':
        {'nl': 'Flashen mislukt: {v}', 'en': 'Flash failed: {v}'},
    'p1_flash_wrong_chip':
        {'nl': ('Verkeerd apparaat gevonden op {port}.\n'
                'Gevonden chip: {chip} — maar de P1 dongle is een ESP32-S3.\n'
                'Mogelijk is dit de OTGW-simulator of het OTGW-apparaat (ESP32 of ESP8266).\n'
                'Koppel alle andere USB-apparaten los en sluit alleen de P1 dongle aan.'),
         'en': ('Wrong device detected on {port}.\n'
                'Found chip: {chip} — but the P1 dongle is an ESP32-S3.\n'
                'This might be the OTGW simulator or OTGW gateway device (ESP32 or ESP8266).\n'
                'Disconnect all other USB devices and connect only the P1 dongle.')},
    'menu_flash_p1':
        {'nl': 'Flash P1 dongle firmware', 'en': 'Flash P1 dongle firmware'},

    # -----------------------------------------------------------------------
    # Setup Wizard
    # -----------------------------------------------------------------------
    'menu_wizard':
        {'nl': 'Installatiewizard (stap voor stap)',
         'en': 'Setup Wizard (step-by-step guide)'},
    'wizard_title':
        {'nl': 'OTGW Logger — Installatiewizard',
         'en': 'OTGW Logger — Setup Wizard'},
    'wizard_intro':
        {'nl': ('Deze wizard begeleidt u stap voor stap door de installatie.\n'
                'Volg de stappen in volgorde voor een correcte installatie.'),
         'en': ('This wizard guides you step-by-step through the installation.\n'
                'Follow the steps in order for a correct setup.')},
    'wizard_step_pass':
        {'nl': '✓', 'en': '✓'},
    'wizard_step_warn':
        {'nl': '!', 'en': '!'},
    'wizard_step_fail':
        {'nl': '✗', 'en': '✗'},
    'wizard_step_pending':
        {'nl': '○', 'en': '○'},
    'wizard_progress':
        {'nl': 'Voortgang: {done}/{total} stappen voltooid',
         'en': 'Progress: {done}/{total} steps complete'},
    'wizard_all_done':
        {'nl': 'Alle stappen voltooid! De installatie is geslaagd.',
         'en': 'All steps complete! Installation successful.'},
    'wizard_finish_prompt':
        {'nl': 'Installatie afronden en config-AP/portal stoppen?',
         'en': 'Finish installation and stop config AP / portal?'},
    'wizard_step_prompt':
        {'nl': 'Stap openen (nummer) of [Enter] om terug te gaan',
         'en': 'Open step (number) or [Enter] to go back'},
    'wizard_step_done_label':
        {'nl': '(voltooid)', 'en': '(complete)'},
    'wizard_step_warn_label':
        {'nl': '(controleren)', 'en': '(check needed)'},
    'wizard_step_fail_label':
        {'nl': '(actie vereist)', 'en': '(action required)'},
    'wizard_step_pending_label':
        {'nl': '(nog niet gecontroleerd)', 'en': '(not yet checked)'},

    # -----------------------------------------------------------------------
    # Set Clock (Q1 — manual time entry when NTP unavailable)
    # -----------------------------------------------------------------------
    'menu_set_clock':
        {'nl': 'Systeemklok instellen (zonder internet)',
         'en': 'Set System Clock (no internet)'},
    'clock_title':
        {'nl': 'Systeemklok instellen', 'en': 'Set System Clock'},
    'clock_current':
        {'nl': 'Huidige tijd: {v}', 'en': 'Current time: {v}'},
    'clock_ntp_ok':
        {'nl': 'NTP gesynchroniseerd — klok is al correct.',
         'en': 'NTP synchronized — clock is already correct.'},
    'clock_ntp_not_synced':
        {'nl': ('NTP is niet gesynchroniseerd (geen internet).\n'
                'Voer de huidige lokale datum en tijd in.'),
         'en': ('NTP is not synchronized (no internet).\n'
                'Enter the current local date and time.')},
    'clock_enter_date':
        {'nl': 'Datum (JJJJ-MM-DD)', 'en': 'Date (YYYY-MM-DD)'},
    'clock_enter_time':
        {'nl': 'Tijd (UU:MM:SS)', 'en': 'Time (HH:MM:SS)'},
    'clock_invalid_date':
        {'nl': 'Ongeldig datumformaat. Gebruik JJJJ-MM-DD.',
         'en': 'Invalid date format. Use YYYY-MM-DD.'},
    'clock_invalid_time':
        {'nl': 'Ongeldig tijdformaat. Gebruik UU:MM:SS.',
         'en': 'Invalid time format. Use HH:MM:SS.'},
    'clock_set_ok':
        {'nl': 'Klok ingesteld op {v}.',
         'en': 'Clock set to {v}.'},
    'clock_set_fail':
        {'nl': 'Klok instellen mislukt: {v}',
         'en': 'Failed to set clock: {v}'},
    'clock_dry_run':
        {'nl': '[DRY-RUN] Zou klok instellen op: {v}',
         'en': '[DRY-RUN] Would set clock to: {v}'},
}
