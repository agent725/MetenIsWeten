#!/usr/bin/env python3
"""Remote Pi status check — run from any Mac/Linux/Windows machine.

Connects to a Pi over SSH (or optionally via the portal HTTP API) and
runs the same checks as the TUI's "Verify All Systems" screen.

Usage:
    python3 check_remote.py <host> [options]

Examples:
    python3 check_remote.py 192.168.9.51
    python3 check_remote.py 192.168.9.51 --user otgw
    python3 check_remote.py 192.168.9.51 --user otgw --key ~/.ssh/otgw_key
    python3 check_remote.py 192.168.9.51 --http    # via portal API (portal must be running)
    python3 check_remote.py 192.168.9.51 --json    # machine-readable JSON output

Exit code: 0 if all checks pass or only warnings, 1 if any check fails.
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import argparse
import json
import subprocess
import sys
import urllib.request
import urllib.error
from typing import Any


# ── SSH helpers ───────────────────────────────────────────────────────────────

def _ssh_cmd(host: str, user: str, key: str | None, port: int) -> list[str]:
    """Build the base SSH command with standard flags."""
    cmd = [
        'ssh',
        '-o', 'StrictHostKeyChecking=no',
        '-o', 'ConnectTimeout=10',
        '-o', 'BatchMode=yes',
    ]
    if key:
        cmd += ['-i', key]
    if port != 22:
        cmd += ['-p', str(port)]
    cmd.append(f'{user}@{host}')
    return cmd


def _run_remote(
    host: str, user: str, key: str | None, port: int,
    remote_cmd: str, stdin: str | None = None,
) -> tuple[int, str]:
    """Run a command on the Pi via SSH.  Returns (returncode, combined output)."""
    cmd = _ssh_cmd(host, user, key, port) + [remote_cmd]
    try:
        r = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, 'SSH command timed out after 30 s'
    except FileNotFoundError:
        return 1, "SSH client not found — is 'ssh' installed?"


# ── data sources ──────────────────────────────────────────────────────────────

REMOTE_SCRIPT = """
import sys, json
sys.path.insert(0, '/opt/otgw/scripts')
try:
    from config_portal import run_checks, get_image_id, get_wifi_info
    checks   = run_checks()
    image_id = get_image_id()
    wifi     = get_wifi_info()
    print(json.dumps({'ok': True, 'image_id': image_id, 'wifi': wifi, 'checks': checks}))
except Exception as e:
    print(json.dumps({'ok': False, 'error': str(e)}))
"""


def _fetch_ssh(host: str, user: str, key: str | None, port: int) -> dict:
    """Fetch status via SSH — runs config_portal.run_checks() on the Pi."""
    # Pipe the script via stdin to avoid any shell-quoting issues.
    rc, output = _run_remote(
        host, user, key, port,
        'python3 -',
        stdin=REMOTE_SCRIPT,
    )
    if rc != 0:
        return {'ok': False, 'error': f'SSH failed (rc={rc}): {output}'}
    # Find first JSON line in output (SSH may print banner lines before it)
    for line in output.splitlines():
        line = line.strip()
        if line.startswith('{'):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return {'ok': False, 'error': f'No JSON found in SSH output:\n{output}'}


def _fetch_http(host: str) -> dict:
    """Fetch status via portal HTTP API (/status.json + /checks page)."""
    # Use /status.json for quick service + reading data
    url = f'http://{host}/status.json'
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as exc:
        return {'ok': False, 'error': f'HTTP request failed: {exc}'}
    except json.JSONDecodeError as exc:
        return {'ok': False, 'error': f'Invalid JSON from portal: {exc}'}

    # Convert status.json format to checks format
    checks = []

    def chk(name: str, ok: bool, detail: str = '', warn: bool = False) -> None:
        checks.append({
            'name':   name,
            'status': 'pass' if ok else ('warn' if warn else 'fail'),
            'detail': detail,
        })

    for svc, status in data.get('services', {}).items():
        is_active = (status == 'active')
        is_support = svc in ('wifi-switch', 'config-sensor', 'led-status',
                             'config-portal')
        if is_support:
            chk(f'Service: {svc}', is_active, status, warn=not is_active)
        else:
            chk(f'Service: {svc}', is_active, status)

    for key, info in data.get('readings', {}).items():
        age = info.get('age', 'unknown')
        css = info.get('css', 'age-old')
        ok = css == 'age-ok'
        warn = css == 'age-warn'
        chk(f'Recent {key}', ok, age, warn=warn)

    return {'ok': True, 'image_id': 'unknown', 'wifi': data.get('wifi', {}),
            'checks': checks}


# ── formatting ────────────────────────────────────────────────────────────────

# ANSI colour codes (disabled on Windows unless terminal supports them)
def _supports_color() -> bool:
    import os
    return (
        sys.stdout.isatty()
        and os.environ.get('NO_COLOR') is None
        and sys.platform != 'win32'
    )


def _fmt(text: str, color: str) -> str:
    if not _supports_color():
        return text
    codes = {'green': '32', 'yellow': '33', 'red': '31', 'bold': '1',
             'cyan': '36', 'reset': '0'}
    return f"\033[{codes.get(color, '0')}m{text}\033[0m"


ICONS = {
    'pass': '✅',
    'warn': '⚠️ ',
    'fail': '❌',
}


def print_report(host: str, data: dict) -> int:
    """Print human-readable status report.  Returns exit code (0=ok, 1=fail)."""
    width = 60
    print()
    print(_fmt(f'━━━ OTGW Pi Status — {host} ', 'bold') +
          '━' * max(0, width - 20 - len(host)))

    if not data.get('ok'):
        print(_fmt(f"  ERROR: {data.get('error', 'unknown error')}", 'red'))
        print()
        return 1

    image_id = data.get('image_id', 'unknown')
    wifi  = data.get('wifi', {})
    print(f"  Image ID : {_fmt(image_id, 'cyan')}")
    if wifi:
        ssid = wifi.get('ssid', '')
        ip   = wifi.get('ip', '')
        if ssid:
            print(f"  WiFi     : {ssid}  ({ip})")

    checks: list[dict] = data.get('checks', [])
    if not checks:
        print(_fmt('  No checks returned.', 'yellow'))
        print()
        return 0

    # Group by rough category for readability
    categories: dict[str, list[dict]] = {
        'Services': [],
        'Data': [],
        'System': [],
    }
    for c in checks:
        name = c['name']
        if name.startswith('Service:'):
            categories['Services'].append(c)
        elif 'data' in name.lower() or 'sensor' in name.lower():
            categories['Data'].append(c)
        else:
            categories['System'].append(c)

    # Any uncategorised
    shown = {id(c) for cats in categories.values() for c in cats}
    extras = [c for c in checks if id(c) not in shown]
    if extras:
        categories['Other'] = extras

    any_fail = False
    for cat, items in categories.items():
        if not items:
            continue
        print()
        print(_fmt(f'  {cat.upper()}', 'bold'))
        for c in items:
            icon   = ICONS.get(c['status'], '?')
            name   = c['name']
            detail = c['detail']
            status = c['status']
            color  = 'green' if status == 'pass' else ('yellow' if status == 'warn' else 'red')
            line   = f"  {icon}  {name:<30}"
            if detail:
                line += f"  {_fmt(detail, color)}"
            print(line)
            if status == 'fail':
                any_fail = True

    passed = sum(1 for c in checks if c['status'] == 'pass')
    warned = sum(1 for c in checks if c['status'] == 'warn')
    failed = sum(1 for c in checks if c['status'] == 'fail')
    total  = len(checks)

    print()
    summary = f'  {passed}/{total} passed'
    if warned:
        summary += f', {warned} warning(s)'
    if failed:
        summary += f', {failed} failed'
    color = 'green' if not any_fail else 'red'
    print(_fmt(summary, color))
    print('━' * width)
    print()

    return 1 if any_fail else 0


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description='Check OTGW Pi status remotely',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument('host', help='Pi hostname or IP address')
    p.add_argument('--user', '-u', default='otgw',
                   help='SSH username (default: otgw)')
    p.add_argument('--key', '-i', default=None,
                   help='SSH private key file')
    p.add_argument('--port', '-p', type=int, default=22,
                   help='SSH port (default: 22)')
    p.add_argument('--http', action='store_true',
                   help='Use portal HTTP API instead of SSH '
                        '(portal must be running — only during hall-sensor AP mode)')
    p.add_argument('--json', dest='json_out', action='store_true',
                   help='Print raw JSON output (for scripting)')
    args = p.parse_args()

    if args.http:
        data = _fetch_http(args.host)
    else:
        data = _fetch_ssh(args.host, args.user, args.key, args.port)

    if args.json_out:
        print(json.dumps(data, indent=2))
        return 0 if data.get('ok') else 1

    return print_report(args.host, data)


if __name__ == '__main__':
    sys.exit(main())
