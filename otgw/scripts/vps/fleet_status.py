#!/usr/bin/env python3
"""
Fleet Status — reads heartbeat.json files from VPS and prints a summary.

Run on the VPS, on a machine with the log directory synced, or use --vps
to pull heartbeat files directly from the VPS over SSH.

Usage:
    fleet_status.py --vps logbackup@vps1.otgw.org  # pull from VPS via rsync
    fleet_status.py /home/backup/otgw-logs          # local path on VPS
    fleet_status.py /tmp/otgw-logs                  # local synced copy
    fleet_status.py --stale-hours 12 --vps ...      # custom stale threshold
    fleet_status.py --pi ABCDEF --vps ...           # single Pi freshness check

Output:
    Pi ID    Last seen            Uptime   Disk  Internet  Services
    a1b2c3   2026-03-12 14:30     3d 2h    42%   ok        all ok
    d4e5f6   2026-03-12 14:28     1d 5h    38%   ok        all ok
    789abc   2026-03-11 09:15    12d 0h    71%   FAIL      mqtt-logger FAILED
    *** STALE — no heartbeat in 12h ***

Requirements:
    Python 3.11+ (no external dependencies)
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def load_heartbeats(log_dir: Path) -> list[dict]:
    """Find and load all heartbeat.json files under log_dir."""
    results = []
    if not log_dir.is_dir():
        print(f"Error: {log_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    for entry in sorted(log_dir.iterdir()):
        if not entry.is_dir():
            continue
        hb_file = entry / 'heartbeat.json'
        if not hb_file.exists():
            continue
        try:
            with open(hb_file) as f:
                data = json.load(f)
            data['_dir'] = entry.name
            results.append(data)
        except (json.JSONDecodeError, OSError) as e:
            results.append({
                '_dir': entry.name,
                '_error': str(e),
            })
    return results


def format_uptime(seconds: int) -> str:
    """Format uptime as '3d 2h' or '45m'."""
    if seconds < 3600:
        return f"{seconds // 60}m"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    if days > 0:
        return f"{days}d {hours}h"
    return f"{hours}h"


def service_summary(services: dict) -> str:
    """Summarise service status: 'all ok' or list of failed."""
    if not services:
        return '?'
    failed = [name for name, status in services.items()
              if status not in ('active', 'running')]
    if not failed:
        return 'all ok'
    return ', '.join(f"{n} {services[n]}" for n in failed)


def check_duplicate_images(heartbeats: list[dict]) -> list[tuple[str, str, str]]:
    """Find cases where the same image_id appears with two different mac_ids.

    Returns list of (image_id, mac_id_1, mac_id_2) tuples for each collision.
    A collision means the same pre-baked image was flashed to two boards.
    """
    seen: dict[str, str] = {}   # image_id -> first mac_id seen
    collisions = []
    for hb in heartbeats:
        if '_error' in hb:
            continue
        image_id = hb.get('image_id', hb['_dir'])
        mac_id = hb.get('mac_id', '')
        if not mac_id:
            continue
        if image_id in seen:
            if seen[image_id] != mac_id:
                collisions.append((image_id, seen[image_id], mac_id))
        else:
            seen[image_id] = mac_id
    return collisions


def print_fleet(heartbeats: list[dict], stale_hours: float) -> None:
    """Print fleet summary table."""
    now = datetime.now(tz=timezone.utc)
    stale_delta = timedelta(hours=stale_hours)

    header = (f"{'Image ID':<12} {'Last seen':<21} {'Uptime':>8}"
              f"  {'Disk':>5}  {'Net':<6}  {'MAC ID':<19}  Services")
    print(header)
    print('-' * len(header))

    stale_count = 0
    error_count = 0

    for hb in heartbeats:
        image_id = hb.get('image_id', hb['_dir'])

        if '_error' in hb:
            print(f"{image_id:<12} *** ERROR reading heartbeat: {hb['_error']}")
            error_count += 1
            continue

        # Parse timestamp
        ts_str = hb.get('timestamp', '')
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            ts = None

        is_stale = ts and (now - ts) > stale_delta

        last_seen = ts.strftime('%Y-%m-%d %H:%M') if ts else '?'
        uptime = format_uptime(hb.get('uptime_seconds', 0))
        disk = f"{hb.get('disk_usage_pct', '?')}%"
        net = 'ok' if hb.get('internet') else 'FAIL'
        mac_id = hb.get('mac_id', '-')
        svcs = service_summary(hb.get('services', {}))

        line = (f"{image_id:<12} {last_seen:<21} {uptime:>8}"
                f"  {disk:>5}  {net:<6}  {mac_id:<19}  {svcs}")
        print(line)

        if is_stale:
            print(f"             *** STALE — no heartbeat in "
                  f"{stale_hours:.0f}h ***")
            stale_count += 1

    print()
    total = len(heartbeats)
    ok = total - stale_count - error_count
    print(f"Total: {total} Pi(s) — {ok} ok, "
          f"{stale_count} stale, {error_count} error(s)")

    # Duplicate image detection
    collisions = check_duplicate_images(heartbeats)
    if collisions:
        print()
        print("*** DUPLICATE IMAGE DETECTED — same image flashed to multiple boards: ***")
        for img_id, mac1, mac2 in collisions:
            print(f"  Image ID {img_id}: MAC {mac1} vs {mac2}")


def fetch_from_vps(vps: str) -> Path:
    """rsync heartbeat.json files from VPS into a temp dir and return it.

    Uses the rrsync restriction on the VPS (read-only, rooted at
    /home/logbackup/) so the rsync source is just 'otgw-logs/'.

    The caller is responsible for cleaning up the returned temp dir.
    """
    tmp = Path(tempfile.mkdtemp(prefix='fleet_status_'))
    # Pull only heartbeat.json files — fast even with many Pis
    cmd = [
        'rsync', '-a', '--progress',
        '--include=*/',
        '--include=heartbeat.json',
        '--exclude=*',
        f'{vps}:otgw-logs/',
        str(tmp) + '/',
    ]
    print(f'Fetching heartbeats from {vps} …', flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print('rsync failed — check SSH key and VPS address', file=sys.stderr)
        sys.exit(1)
    return tmp


def print_single_pi(heartbeats: list[dict], image_id: str,
                    stale_hours: float) -> int:
    """Print status for a single Pi by image_id.

    Returns exit code: 0 = healthy, 1 = stale or not found.
    """
    now = datetime.now(tz=timezone.utc)
    stale_delta = timedelta(hours=stale_hours)
    image_id = image_id.upper()

    match = next(
        (hb for hb in heartbeats
         if hb.get('image_id', hb.get('_dir', '')).upper() == image_id),
        None,
    )

    if match is None:
        print(f'Pi {image_id}: NOT FOUND in heartbeat data', file=sys.stderr)
        return 1

    if '_error' in match:
        print(f'Pi {image_id}: ERROR reading heartbeat — {match["_error"]}',
              file=sys.stderr)
        return 1

    ts_str = match.get('timestamp', '')
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        ts = None

    is_stale = ts is None or (now - ts) > stale_delta
    age_str = 'unknown'
    if ts:
        delta = now - ts
        total_s = int(delta.total_seconds())
        if total_s < 3600:
            age_str = f'{total_s // 60}m ago'
        elif total_s < 86400:
            age_str = f'{total_s // 3600}h ago'
        else:
            age_str = f'{total_s // 86400}d ago'

    uptime = format_uptime(match.get('uptime_seconds', 0))
    disk = f"{match.get('disk_usage_pct', '?')}%"
    net = 'ok' if match.get('internet') else 'FAIL'
    mac_id = match.get('mac_id', '-')
    svcs = service_summary(match.get('services', {}))
    last_seen = ts.strftime('%Y-%m-%d %H:%M') if ts else '?'

    status = 'STALE' if is_stale else 'ok'
    print(f'Pi:        {image_id}')
    print(f'Status:    {status}')
    print(f'Last seen: {last_seen} ({age_str})')
    print(f'Uptime:    {uptime}')
    print(f'Disk:      {disk}')
    print(f'Network:   {net}')
    print(f'MAC ID:    {mac_id}')
    print(f'Services:  {svcs}')

    return 1 if is_stale else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Print fleet status from synced heartbeat files'
    )
    parser.add_argument(
        'log_dir', nargs='?',
        help='Path to otgw-logs directory (local). Required unless --vps is set.'
    )
    parser.add_argument(
        '--vps', metavar='USER@HOST',
        help='Pull heartbeats from VPS via rsync (e.g. logbackup@vps1.otgw.org)'
    )
    parser.add_argument(
        '--stale-hours', type=float, default=12,
        help='Hours without heartbeat before marking stale (default: 12)'
    )
    parser.add_argument(
        '--pi', metavar='IMAGE_ID',
        help='Show status for a single Pi only; exit 0 if healthy, 1 if stale/absent'
    )
    args = parser.parse_args()

    if args.vps:
        tmp = fetch_from_vps(args.vps)
        try:
            heartbeats = load_heartbeats(tmp)
            if not heartbeats:
                print(f'No heartbeat.json files found on {args.vps}')
                sys.exit(0)
            if args.pi:
                sys.exit(print_single_pi(heartbeats, args.pi, args.stale_hours))
            print_fleet(heartbeats, args.stale_hours)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    elif args.log_dir:
        heartbeats = load_heartbeats(Path(args.log_dir))
        if not heartbeats:
            print(f'No heartbeat.json files found under {args.log_dir}')
            sys.exit(0)
        if args.pi:
            sys.exit(print_single_pi(heartbeats, args.pi, args.stale_hours))
        print_fleet(heartbeats, args.stale_hours)
    else:
        parser.error('provide log_dir or --vps USER@HOST')


if __name__ == '__main__':
    main()
