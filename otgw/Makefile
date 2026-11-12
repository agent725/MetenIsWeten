# /opt/otgw/Makefile — installed on the Pi
# Run from /opt/otgw/ or anywhere with: make -C /opt/otgw <target>
#
# Usage:
#   make status      Show service status + data ages
#   make logs        Tail live MQTT log output
#   make sync        Force an immediate VPS log sync
#   make flush       Flush RAM logs to SD card
#   make check       Run all system checks (same as portal /checks page)
#   make restart     Restart all core services
#   make portal      Open config AP (same as short hall sensor press)
#   make id          Show Pi ID and Image ID

.PHONY: help status logs sync flush check restart portal id

PI_ID  := $(shell cat /etc/otgw/pi-id 2>/dev/null || echo unknown)
IMG_ID := $(shell cat /etc/otgw/pi-id.env 2>/dev/null | grep IMAGE_ID | cut -d= -f2 || echo unknown)
LOG_DIR := /run/otgw/$(PI_ID)

help:
	@echo "Pi targets (run as root or otgw user):"
	@echo "  make status    Service status + data ages"
	@echo "  make logs      Tail live MQTT log (Ctrl-C to stop)"
	@echo "  make sync      Force immediate VPS log sync"
	@echo "  make flush     Flush RAM logs to SD card"
	@echo "  make check     Run all system checks"
	@echo "  make restart   Restart all core services"
	@echo "  make portal    Open config AP (5-min window)"
	@echo "  make id        Show Pi ID and Image ID"

status:
	@echo "Pi ID:    $(PI_ID)"
	@echo "Image ID: $(IMG_ID)"
	@echo ""
	@systemctl is-active --quiet mosquitto          && echo "  ✓ mosquitto"         || echo "  ✗ mosquitto"
	@systemctl is-active --quiet otgw-listener      && echo "  ✓ otgw-listener"     || echo "  ✗ otgw-listener"
	@systemctl is-active --quiet ds18b20-listener   && echo "  ✓ ds18b20-listener"  || echo "  ✗ ds18b20-listener"
	@systemctl is-active --quiet mqtt-logger        && echo "  ✓ mqtt-logger"       || echo "  ✗ mqtt-logger"
	@systemctl is-active --quiet log-sync.timer     && echo "  ✓ log-sync.timer"    || echo "  ✗ log-sync.timer"
	@systemctl is-active --quiet heartbeat.timer    && echo "  ✓ heartbeat.timer"   || echo "  ✗ heartbeat.timer"
	@echo ""
	@python3 -c "
import json, time
try:
    d = json.load(open('$(LOG_DIR)/heartbeat.json'))
    now = time.time()
    from datetime import datetime, timezone
    ts = datetime.fromisoformat(d['timestamp'])
    if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
    hb_age = int(now - ts.timestamp())
    def age(s): return '-' if s is None else (f'{int(s)}s' if s < 60 else f'{int(s)//60}m')
    print(f'  Heartbeat:   {hb_age}s ago')
    print(f'  OTGW data:   {age(d.get(\"otgw_age_s\"))}')
    print(f'  Temperature: {age(d.get(\"temperature_age_s\"))}')
    print(f'  Gas:         {age(d.get(\"gas_age_s\"))}')
    print(f'  Internet:    {\"yes\" if d.get(\"internet\") else \"no\"}')
    print(f'  DS18B20:     {d.get(\"ds18b20_count\", 0)} sensor(s)')
except Exception as e:
    print(f'  (heartbeat not available: {e})')
"

logs:
	@echo "Tailing MQTT log for Pi $(PI_ID) — Ctrl-C to stop"
	@tail -f $(LOG_DIR)/otgw_$(PI_ID)_$$(date +%Y-%m-%d).log \
	          $(LOG_DIR)/temperature_$(PI_ID)_$$(date +%Y-%m-%d).log \
	          $(LOG_DIR)/gas_$(PI_ID)_$$(date +%Y-%m-%d).log 2>/dev/null || \
	    echo "No log files yet for today"

sync:
	@echo "Forcing VPS log sync..."
	systemctl start log-sync.service
	@echo "Done. Check VPS for new files."

flush:
	@echo "Flushing RAM logs to SD card..."
	systemctl start log-flush.service
	@echo "Done."

check:
	python3 /opt/otgw/scripts/check_system.py 2>/dev/null || \
	    python3 /opt/otgw/scripts/otgw_api.py --checks 2>/dev/null || \
	    echo "check_system.py not available — use the portal at http://localhost:8080/checks"

restart:
	@echo "Restarting core services..."
	systemctl restart mosquitto otgw-listener ds18b20-listener mqtt-logger
	@echo "Done."

portal:
	@echo "Opening config AP for 5 minutes..."
	touch /run/otgw/ap-toggle
	@echo "AP open — connect to SSID otgw-$(PI_ID) or check the ACT LED (double-blink)"

id:
	@echo "Pi ID:    $(PI_ID)"
	@echo "Image ID: $(IMG_ID)"
	@cat /etc/otgw/image-version 2>/dev/null | xargs -I{} echo "Version:  {}" || true
