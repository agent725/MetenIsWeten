#!/usr/bin/env python3
"""Fleet health check — reads heartbeat.json files from the VPS.

Usage:
    check_vps.py                                  # Fleet summary (all images + Pis)
    check_vps.py --image-id YGTTWW               # All Pis for one image
    check_vps.py --image-id YGTTWW --pi-id a1b2c3  # Full detail for one Pi
    check_vps.py --vps user@host                  # Override VPS (default: joachim@vps1.otgw.org)

Requirements: ssh access to the VPS; python3 on both sides.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone


# ── configuration ─────────────────────────────────────────────────────────────

DEFAULT_VPS   = "joachim@vps1.otgw.org"
LOG_BASE      = "/home/logbackup/otgw-logs"

# Data is considered stale after this many seconds
STALE_OTGW_S  = 300     # 5 min — OTGW sends every ~30 s
STALE_TEMP_S  = 300     # 5 min — DS18B20 reads every 15–60 s
STALE_GAS_S   = 7200    # 2 h  — P1 dongle sends on change
STALE_HB_S    = 300     # 5 min — heartbeat every 1 min


# ── terminal colours ──────────────────────────────────────────────────────────

_tty = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text

def green(t: str)  -> str: return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def red(t: str)    -> str: return _c("31", t)
def bold(t: str)   -> str: return _c("1",  t)
def dim(t: str)    -> str: return _c("2",  t)


# ── helpers ───────────────────────────────────────────────────────────────────

def age_str(seconds: float | None) -> str:
    """Human-readable age, e.g. '2m', '3h', '2d'. Returns '—' if None."""
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def colour_age(seconds: float | None, stale_s: int) -> str:
    t = age_str(seconds)
    if seconds is None:
        return dim(t)
    if seconds > stale_s:
        return red(t)
    return green(t)


def services_summary(services: dict) -> str:
    """Return a compact coloured summary of service states."""
    CORE = ["mosquitto", "otgw_listener", "ds18b20_listener", "mqtt_logger"]
    bad = [k for k in CORE if services.get(k) != "active"]
    if not bad:
        return green("OK")
    return red(f"{'  '.join(bad)}")


def heartbeat_age(ts_str: str | None) -> float | None:
    """Return seconds since the heartbeat timestamp, or None."""
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return time.time() - ts.timestamp()
    except ValueError:
        return None


# ── VPS data collection ───────────────────────────────────────────────────────

# Python snippet that runs on the VPS, collects all heartbeat.json files,
# and emits a JSON array. Arguments: image_filter pi_filter (empty string = all).
_REMOTE_SCRIPT = r"""
import json, sys
from pathlib import Path

base        = Path('/home/logbackup/otgw-logs')
img_filter  = sys.argv[1] if len(sys.argv) > 1 else ''
pi_filter   = sys.argv[2] if len(sys.argv) > 2 else ''

results = []
for hb in sorted(base.glob('*/*/heartbeat.json')):
    parts = hb.parts
    img = parts[-3]
    pi  = parts[-2]
    if img_filter and img != img_filter:
        continue
    if pi_filter and pi != pi_filter:
        continue
    try:
        d = json.loads(hb.read_text())
    except Exception as e:
        d = {'_error': str(e)}
    d['_img_dir'] = img
    d['_pi_dir']  = pi
    results.append(d)
print(json.dumps(results))
"""


def fetch_data(vps: str, image_id: str = "", pi_id: str = "") -> list[dict]:
    """SSH to VPS, run the collection script, return parsed list of dicts."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", vps,
           f"python3 - '{image_id}' '{pi_id}'"]
    try:
        result = subprocess.run(
            cmd, input=_REMOTE_SCRIPT, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        print(red("Error: SSH connection to VPS timed out."), file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(red(f"Error: SSH failed ({result.returncode})"), file=sys.stderr)
        if result.stderr:
            print(dim(result.stderr.strip()), file=sys.stderr)
        sys.exit(1)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(red(f"Error: could not parse VPS output: {e}"), file=sys.stderr)
        print(dim(result.stdout[:500]), file=sys.stderr)
        sys.exit(1)


# ── display modes ─────────────────────────────────────────────────────────────

def show_fleet(data: list[dict]) -> None:
    """Summary table: one row per Pi, grouped by image."""
    if not data:
        print(yellow("No Pis found on VPS."))
        return

    # Group by image
    images: dict[str, list[dict]] = {}
    for d in data:
        img = d.get("_img_dir", "?")
        images.setdefault(img, []).append(d)

    HDR  = f"{'IMAGE':8}  {'PI_ID':8}  {'HB AGE':7}  {'OTGW':7}  {'TEMP':7}  {'GAS':7}  {'SERVICES'}"
    SEP  = "─" * 72

    print()
    print(bold(HDR))
    print(dim(SEP))

    for img_id, pis in sorted(images.items()):
        for d in pis:
            pi_id   = d.get("_pi_dir", d.get("pi_id", "?"))
            err     = d.get("_error")
            if err:
                print(f"{img_id:8}  {pi_id:8}  {red('read error: ' + err[:40])}")
                continue

            hb_age  = heartbeat_age(d.get("timestamp"))
            otgw_a  = d.get("otgw_age_s")
            temp_a  = d.get("temperature_age_s")
            gas_a   = d.get("gas_age_s")
            svcs    = d.get("services", {})

            hb_col  = colour_age(hb_age, STALE_HB_S)
            otgw_c  = colour_age(otgw_a, STALE_OTGW_S)
            temp_c  = colour_age(temp_a, STALE_TEMP_S)
            gas_c   = colour_age(gas_a,  STALE_GAS_S)
            svc_c   = services_summary(svcs)

            print(f"{img_id:8}  {pi_id:8}  {hb_col:>7}  {otgw_c:>7}  {temp_c:>7}  {gas_c:>7}  {svc_c}")

    print()
    n_pi  = len(data)
    n_img = len(images)
    print(dim(f"{n_pi} Pi{'s' if n_pi != 1 else ''} across {n_img} image{'s' if n_img != 1 else ''}"))
    print()


def show_image(data: list[dict], image_id: str) -> None:
    """All Pis for one image — same table as fleet but with more columns."""
    if not data:
        print(yellow(f"No Pis found for image {image_id}."))
        return
    print()
    print(bold(f"Image: {image_id}  ({len(data)} Pi{'s' if len(data) != 1 else ''})"))
    show_fleet(data)


def show_pi(d: dict) -> None:
    """Full detail for one Pi."""
    if d.get("_error"):
        print(red(f"Error reading heartbeat: {d['_error']}"))
        return

    img_id  = d.get("_img_dir", d.get("image_id", "?"))
    pi_id   = d.get("_pi_dir",  d.get("pi_id",    "?"))
    hb_age  = heartbeat_age(d.get("timestamp"))

    print()
    print(bold(f"Pi:  {img_id} / {pi_id}"))
    print(dim(f"Heartbeat: {d.get('timestamp', '?')}  ({age_str(hb_age)} ago)"))
    print()

    # Connectivity
    inet = d.get("internet")
    print(f"  Internet:    {green('yes') if inet else red('no')}")
    print(f"  Time synced: {green('yes') if d.get('time_synced') else red('no')}")
    print(f"  Hostname:    {d.get('hostname', '?')}")
    print(f"  OS:          {d.get('os_version', '?')}")
    print(f"  Python:      {d.get('python_version', '?')}")
    print()

    # Data ages
    print(f"  OTGW data:   {colour_age(d.get('otgw_age_s'),       STALE_OTGW_S)}")
    print(f"  Temperature: {colour_age(d.get('temperature_age_s'), STALE_TEMP_S)}")
    print(f"  Gas:         {colour_age(d.get('gas_age_s'),         STALE_GAS_S)}")
    print(f"  DS18B20:     {d.get('ds18b20_count', 0)} sensor(s)  "
          f"{', '.join(d.get('ds18b20_sensors', []))}")
    print(f"  OTGW device: {d.get('otgw_device', '?')}")
    print()

    # Services
    print("  Services:")
    CORE = {"mosquitto", "otgw_listener", "ds18b20_listener", "mqtt_logger"}
    for svc, state in sorted(d.get("services", {}).items()):
        label = bold(svc) if svc in CORE else svc
        col   = green(state) if state == "active" else (dim(state) if state == "inactive" else red(state))
        print(f"    {label:30} {col}")
    print()

    # System
    print(f"  Disk:        {d.get('disk_usage_pct', '?')}% used  "
          f"({d.get('disk_avail_mb', '?')} MB free)")
    print(f"  RAM free:    {d.get('mem_avail_mb', '?')} MB")
    print(f"  Load (1m):   {d.get('load_1m', '?')}")
    print(f"  Uptime:      {age_str(d.get('uptime_seconds'))}")
    print(f"  Image ID:    {d.get('image_id', '?')}")
    print(f"  Code ver:    {d.get('code_version', '?')}")
    print(f"  Reboot:      {d.get('reboot_reason', '?')}")
    print()


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fleet health check — reads heartbeat data from the VPS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  check_vps.py                                    # Fleet overview\n"
            "  check_vps.py --image-id YGTTWW                 # All Pis for one image\n"
            "  check_vps.py --image-id YGTTWW --pi-id a1b2c3  # One Pi — full detail\n"
            "  check_vps.py --pi-id a1b2c3                    # This Pi across all images\n"
        ),
    )
    parser.add_argument("--vps",      default=DEFAULT_VPS,
                        help=f"VPS SSH target (default: {DEFAULT_VPS})")
    parser.add_argument("--image-id", default="",
                        help="Filter by image ID (e.g. YGTTWW)")
    parser.add_argument("--pi-id",    default="",
                        help="Filter by Pi ID; without --image-id shows all images this Pi appears in")
    args = parser.parse_args()

    data = fetch_data(args.vps, args.image_id, args.pi_id)

    if args.pi_id and args.image_id:
        # Exact match — full detail for one Pi
        if not data:
            print(yellow(f"No heartbeat found for {args.image_id}/{args.pi_id}."))
            sys.exit(1)
        show_pi(data[0])
    elif args.pi_id:
        # Pi ID only — show all image entries for this Pi (it may have been
        # reflashed, appearing under multiple image directories)
        if not data:
            print(yellow(f"No heartbeat found for pi_id '{args.pi_id}' under any image."))
            sys.exit(1)
        if len(data) == 1:
            show_pi(data[0])
        else:
            print(yellow(f"Pi '{args.pi_id}' found under {len(data)} images:"))
            show_fleet(data)
    elif args.image_id:
        show_image(data, args.image_id)
    else:
        show_fleet(data)


if __name__ == "__main__":
    main()
