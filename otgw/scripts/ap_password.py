#!/usr/bin/env python3
"""Deterministic AP password derivation for Case A (no home WiFi).

Algorithm (NFR-4.45):
  payload  = "otgw-ap-pw" + "|" + wifi_mac + "|" + cpu_serial
  digest   = SHA256(payload.encode("utf-8"))
  value    = int.from_bytes(digest[:10], "big") >> 5   # 75 bits
  password = encode75(value)   →   xxxxx-xxxxx-xxxxx (17 chars)

Custom alphabet: abcdefghjklmnpqrtuvwxy0123456789
  Lowercase a-z minus {i, o, s, z} (ambiguous on phone screens) + digits 0-9.

The domain prefix "otgw-ap-pw" is not a secret — it is in the repository.
Its purpose is to make this hash distinct from any other use of the same
hardware identifiers.  The CPU serial is the secret ingredient: it is never
sent to the VPS, so an attacker who observes only VPS data cannot derive
the password.
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import hashlib
from pathlib import Path

_ALPHABET = "abcdefghjklmnpqrtuvwxy0123456789"
_DOMAIN = "otgw-ap-pw"


def _read_wifi_mac() -> str:
    path = Path("/sys/class/net/wlan0/address")
    return path.read_text().strip() if path.exists() else ""


def _read_cpu_serial() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("Serial"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def derive_ap_password() -> str:
    """Return the deterministic WPA2 passphrase for the Case A AP.

    Format: xxxxx-xxxxx-xxxxx (17 chars, 75 bits of entropy).
    Requires the Pi's WiFi MAC and CPU serial number.
    """
    payload = f"{_DOMAIN}|{_read_wifi_mac()}|{_read_cpu_serial()}"
    digest = hashlib.sha256(payload.encode()).digest()
    value = int.from_bytes(digest[:10], "big") >> 5  # 75 bits
    chars = []
    for _ in range(15):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    chars.reverse()
    return f"{''.join(chars[:5])}-{''.join(chars[5:10])}-{''.join(chars[10:])}"
