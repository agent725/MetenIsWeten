#!/usr/bin/env bash

set -e

PORT="${1:-/dev/ttyACM0}"
FW="${2:-firmware.bin}"

if [ ! -f "$FW" ]; then
  echo "Firmware file not found: $FW"
  exit 1
fi

echo "== ESP Flash (robust mode) =="
echo "Port: $PORT"
echo "Firmware: $FW"
echo

# ---- Helper: wait for device ----
wait_for_device() {
  echo "Waiting for device..."
  for i in {1..20}; do
    if [ -e "$PORT" ]; then
      return 0
    fi
    sleep 0.5
  done
  echo "Device not found!"
  return 1
}

# ---- Attempt function ----
attempt_flash() {
  local NAME="$1"
  shift

  echo
  echo "=== Attempt: $NAME ==="
  echo "Command: esptool $*"
  echo

  if esptool "$@"; then
    echo
    echo "✅ SUCCESS ($NAME)"
    exit 0
  else
    echo
    echo "❌ FAILED ($NAME)"
    sleep 2
    wait_for_device || true
  fi
}

# ---- Common args ----
# Note: --no-stub is a global option in esptool v5 (before the subcommand).
# On ESP32-S3 with Pi USB, the stub-based approach is more reliable than --no-stub.
# Lower baud rates (9600) keep the USB connection stable during large transfers.
COMMON=(
  --chip esp32s3
  --port "$PORT"
  --before default-reset
  --after no-reset
)

WRITE_FLASH_ARGS=(
  write-flash
  --no-compress
  --flash-mode dio
  --flash-freq 20m
)

# ---- Strategy ladder ----

# 1. Try faster baud rate first: 57600
attempt_flash "stub + no-compress @57600 + no-reset" \
  "${COMMON[@]}" \
  --baud 57600 \
  "${WRITE_FLASH_ARGS[@]}" \
  0x10000 "$FW"

# 2. Fall back to 19200 baud
attempt_flash "stub + no-compress @19200 + no-reset" \
  "${COMMON[@]}" \
  --baud 19200 \
  "${WRITE_FLASH_ARGS[@]}" \
  0x10000 "$FW"

# 3. Proven reliable on Raspberry Pi: stub + 9600 baud + no post-reset
#    (--after no-reset avoids a second USB re-enumeration that can fail)
attempt_flash "stub + no-compress @9600 + no-reset" \
  "${COMMON[@]}" \
  --baud 9600 \
  "${WRITE_FLASH_ARGS[@]}" \
  0x10000 "$FW"

# 4. Slightly faster; may fail on marginal USB power setups
attempt_flash "stub + no-compress @74880 + no-reset" \
  "${COMMON[@]}" \
  --baud 74880 \
  "${WRITE_FLASH_ARGS[@]}" \
  0x10000 "$FW"

# 5. Without baud throttle; known to drop at ~31% on Pi but worth a try
attempt_flash "stub + no-compress @115200" \
  --chip esp32s3 \
  --port "$PORT" \
  --before default-reset \
  --after hard-reset \
  --baud 115200 \
  "${WRITE_FLASH_ARGS[@]}" \
  0x10000 "$FW"

# 6. ROM bootloader (no stub); erases but may fail before writing on Pi USB
attempt_flash "no-stub + no-compress @9600 + no-reset" \
  "${COMMON[@]}" \
  --no-stub \
  --baud 9600 \
  "${WRITE_FLASH_ARGS[@]}" \
  0x10000 "$FW"

echo
echo "❌ All strategies exhausted. Flashing from a PC/Mac is recommended for this firmware size."
exit 1

echo
echo "✅ SUCCESS (chunked mode)"

