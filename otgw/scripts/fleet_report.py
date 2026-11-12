#!/usr/bin/env python3
"""Fleet data quality report — per-Pi completeness, temperature ranges, gas consumption.

Usage:
    fleet_report.py                            # All active Pis, last 7 days
    fleet_report.py --days 14                  # Longer window
    fleet_report.py --start-date 2026-05-01    # Hard floor (ignore older data)
    fleet_report.py --skip-pi 875704           # Exclude Pi(s) by ID (comma-separated)
    fleet_report.py --pi-id 875877             # Only one Pi
    fleet_report.py --html                     # Also write fleet_report.html
    fleet_report.py --html report.html         # Custom HTML path
    fleet_report.py --vps user@host            # Override VPS

Requirements: ssh access to VPS (joachim@vps1.otgw.org by default); Python 3.11+.
Output file (--html) is not committed to git (see .gitignore).
"""

import argparse
import json
import subprocess
import sys
import textwrap
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


# ── defaults ──────────────────────────────────────────────────────────────────

DEFAULT_VPS        = "joachim@vps1.otgw.org"
LOG_BASE           = "/home/logbackup/otgw-logs"
DEFAULT_DAYS       = 365
DEFAULT_START_DATE = "2026-05-01"      # ignore spurious pre-May data
DEFAULT_HTML_PATH  = "fleet_report.html"

HB_PER_DAY = 1440  # kept for temperature completeness denominator


# ── terminal colours ──────────────────────────────────────────────────────────

_tty = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text


def green(t: str)  -> str: return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def red(t: str)    -> str: return _c("31", t)
def bold(t: str)   -> str: return _c("1",  t)
def dim(t: str)    -> str: return _c("2",  t)
def cyan(t: str)   -> str: return _c("36", t)


# ── helpers ───────────────────────────────────────────────────────────────────

def age_str(seconds: float | None) -> str:
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


def heartbeat_age(ts_str: str | None) -> float | None:
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return time.time() - ts.timestamp()
    except ValueError:
        return None


def completeness_pct(lines: int, expected: int) -> float:
    if expected <= 0:
        return 0.0
    return min(lines / expected * 100.0, 100.0)


def pct_colour(pct: float) -> str:
    s = f"{pct:.0f}%"
    if pct >= 90:
        return green(s)
    if pct >= 60:
        return yellow(s)
    return red(s)


def date_range(start_date: date, end_date: date) -> list[str]:
    """Return ISO date strings from start_date to end_date (inclusive), newest first."""
    dates = []
    d = end_date
    while d >= start_date:
        dates.append(d.isoformat())
        d -= timedelta(days=1)
    return dates


# ── VPS: Pi discovery ─────────────────────────────────────────────────────────

_DISCOVER_SCRIPT = r"""
import json
from pathlib import Path

base = Path('/home/logbackup/otgw-logs')
results = []
for hb_file in sorted(base.glob('*/*/heartbeat.json')):
    image_id = hb_file.parts[-3]
    pi_id    = hb_file.parts[-2]
    try:
        hb = json.loads(hb_file.read_text())
    except Exception as e:
        hb = {'_error': str(e)}
    hb['_image_id'] = image_id
    hb['_pi_id']    = pi_id
    results.append(hb)
print(json.dumps(results))
"""


def discover_pis(vps: str) -> list[dict]:
    """SSH to VPS, return heartbeat data for all known Pis."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", vps,
           "python3 -"]
    try:
        result = subprocess.run(
            cmd, input=_DISCOVER_SCRIPT, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        print(red("Error: SSH to VPS timed out."), file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(red(f"Error: SSH failed ({result.returncode})"), file=sys.stderr)
        if result.stderr:
            print(dim(result.stderr.strip()), file=sys.stderr)
        sys.exit(1)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(red(f"Error: could not parse VPS response: {e}"), file=sys.stderr)
        print(dim(result.stdout[:500]), file=sys.stderr)
        sys.exit(1)


def filter_pis(
    all_pis: list[dict],
    *,
    pi_id: str,
    skip_pis: set[str],
) -> list[dict]:
    """Apply --pi-id and --skip-pi filters. When a pi_id appears under multiple
    image IDs (re-imaged device), keep only the most recently heard-from entry."""
    # Deduplicate by pi_id: keep entry with newest heartbeat
    best: dict[str, dict] = {}
    for pi in all_pis:
        if pi.get("_error"):
            continue
        pid = pi.get("_pi_id", "")
        if pi_id and pid != pi_id:
            continue
        if pid in skip_pis:
            continue
        age = heartbeat_age(pi.get("timestamp"))
        if pid not in best:
            best[pid] = pi
        else:
            existing_age = heartbeat_age(best[pid].get("timestamp"))
            if age is not None and (existing_age is None or age < existing_age):
                best[pid] = pi
    # Sort by heartbeat recency (most recent first)
    return sorted(best.values(),
                  key=lambda p: heartbeat_age(p.get("timestamp")) or float("inf"))


# ── VPS: log aggregation ──────────────────────────────────────────────────────

# Runs on the VPS — aggregates per-day log stats for a list of (image_id, pi_id, dates).
_AGGREGATE_SCRIPT = r"""
import json, sys
from datetime import datetime, timezone
from pathlib import Path

base    = Path('/home/logbackup/otgw-logs')
request = json.loads(sys.stdin.read())


def _parse_log_ts(line: str) -> float | None:
    # Parse Unix timestamp from log line: '2026-05-17T11:07:06+02:00 | topic : {...}'
    sep = line.find(' | ')
    if sep < 0:
        return None
    try:
        dt = datetime.fromisoformat(line[:sep].strip())
        return dt.timestamp()
    except ValueError:
        return None


def _last_line(path) -> str:
    # Return the last non-empty line of a file
    last = ''
    with path.open() as fh:
        for line in fh:
            if line.strip():
                last = line
    return last


output = []
for item in request:
    image_id = item['image_id']
    pi_id    = item['pi_id']
    dates    = item['dates']
    pi_dir   = base / image_id / pi_id

    days_data  = {}
    last_data_ts: float | None = None  # max ts across all data files in window

    for date_str in dates:
        date_dir = pi_dir / date_str
        if not date_dir.is_dir():
            continue

        day = {
            'hb_lines':   0,
            'otgw_lines': 0,
            'temp_lines': 0,
            'gas_lines':  0,
            'temp_stats': {},   # sensor_id -> {min, max, sum, count}
            'gas_start':  None, # first current.value_m3
            'gas_end':    None, # last  current.value_m3
        }

        for f in sorted(date_dir.iterdir()):
            name = f.name
            try:
                if name.startswith('heartbeat_'):
                    day['hb_lines'] = sum(1 for _ in f.open())

                elif name.startswith('otgw_'):
                    day['otgw_lines'] = sum(1 for _ in f.open())
                    ts = _parse_log_ts(_last_line(f))
                    if ts and (last_data_ts is None or ts > last_data_ts):
                        last_data_ts = ts

                elif name.startswith('temperature_'):
                    ts_stats = day['temp_stats']
                    last_ts_in_file: float | None = None
                    for line in f.open():
                        sep = line.find(' : ')
                        if sep < 0:
                            continue
                        try:
                            payload = json.loads(line[sep + 3:])
                        except Exception:
                            continue
                        sid = payload.get('sensor_id', 'unknown')
                        t   = payload.get('temperature_c')
                        if t is None:
                            continue
                        day['temp_lines'] += 1
                        if sid not in ts_stats:
                            ts_stats[sid] = {'min': t, 'max': t, 'sum': 0.0, 'count': 0}
                        ts_stats[sid]['min']    = min(ts_stats[sid]['min'], t)
                        ts_stats[sid]['max']    = max(ts_stats[sid]['max'], t)
                        ts_stats[sid]['sum']   += t
                        ts_stats[sid]['count'] += 1
                        lt = _parse_log_ts(line)
                        if lt:
                            last_ts_in_file = lt
                    if last_ts_in_file and (last_data_ts is None or last_ts_in_file > last_data_ts):
                        last_data_ts = last_ts_in_file

                elif name.startswith('home_energy_gas_'):
                    last_ts_in_file = None
                    for line in f.open():
                        sep = line.find(' : ')
                        if sep < 0:
                            continue
                        try:
                            payload = json.loads(line[sep + 3:])
                        except Exception:
                            continue
                        val = (payload.get('current') or {}).get('value_m3')
                        if val is None:
                            continue
                        day['gas_lines'] += 1
                        if day['gas_start'] is None:
                            day['gas_start'] = val
                        day['gas_end'] = val
                        lt = _parse_log_ts(line)
                        if lt:
                            last_ts_in_file = lt
                    if last_ts_in_file and (last_data_ts is None or last_ts_in_file > last_data_ts):
                        last_data_ts = last_ts_in_file

            except Exception:
                pass

        days_data[date_str] = day

    output.append({
        'image_id':     image_id,
        'pi_id':        pi_id,
        'days':         days_data,
        'last_data_ts': last_data_ts,
    })

print(json.dumps(output))
"""


def aggregate_logs(
    vps: str,
    pis: list[dict],
    dates: list[str],
) -> list[dict]:
    """Run _AGGREGATE_SCRIPT on VPS for all selected Pis and dates."""
    request = [
        {
            "image_id": pi["_image_id"],
            "pi_id":    pi["_pi_id"],
            "dates":    dates,
        }
        for pi in pis
    ]
    request_json = json.dumps(request)

    # Stream the request JSON via stdin, receive aggregated stats via stdout
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", vps,
           f"python3 - <<'__EOF__'\n{_AGGREGATE_SCRIPT}\n__EOF__"]

    # Use pipe: send script via stdin (first arg to python3), pipe request_json
    # Actually, run the embedded script directly and pass JSON on stdin.
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", vps,
           "python3 /dev/stdin"]

    combined_stdin = _AGGREGATE_SCRIPT + "\n# ---request---\n"
    # Use a wrapper that reads the script from stdin and the request from stdin too
    # Better: pass script as a file via process substitution isn't available here.
    # Use the cleanest approach: embed request in the script itself.
    script = _AGGREGATE_SCRIPT.replace(
        "request = json.loads(sys.stdin.read())",
        f"request = {request_json!r}\nrequest = json.loads(request)",
    )

    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", vps,
           "python3 -"]
    try:
        result = subprocess.run(
            cmd, input=script, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        print(red("Error: VPS aggregation timed out."), file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(red(f"Error: VPS aggregation failed ({result.returncode})"), file=sys.stderr)
        if result.stderr:
            print(dim(result.stderr.strip()), file=sys.stderr)
        sys.exit(1)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(red(f"Error: could not parse VPS aggregation output: {e}"), file=sys.stderr)
        print(dim(result.stdout[:500]), file=sys.stderr)
        sys.exit(1)


# ── terminal display ──────────────────────────────────────────────────────────

def show_report(
    pis_hb: list[dict],
    agg: list[dict],
    dates: list[str],
    today_str: str,
) -> None:
    """Print the full fleet report to stdout."""
    # Index aggregated data by (image_id, pi_id)
    agg_index: dict[tuple[str, str], dict] = {
        (a["image_id"], a["pi_id"]): a for a in agg
    }
    hb_index: dict[tuple[str, str], dict] = {
        (pi["_image_id"], pi["_pi_id"]): pi for pi in pis_hb
    }

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    print()
    print(bold(f"Fleet Data Quality Report — {now_str}"))
    print(dim("=" * 64))

    for pi_hb in pis_hb:
        image_id = pi_hb["_image_id"]
        pi_id    = pi_hb["_pi_id"]
        key      = (image_id, pi_id)
        pi_agg   = agg_index.get(key, {})
        days_agg = pi_agg.get("days", {})

        # ── Pi header ──
        last_data_ts = pi_agg.get("last_data_ts")
        data_age     = (time.time() - last_data_ts) if last_data_ts else None
        age_s        = age_str(data_age)
        active       = data_age is not None and data_age < 2 * 3600
        dot          = green("●") if active else red("●")
        uptime_s     = pi_hb.get("uptime_seconds", 0)
        uptime_d     = uptime_s // 86400
        uptime_h     = (uptime_s % 86400) // 3600
        n_sensors    = pi_hb.get("ds18b20_count", 0)
        has_gas      = pi_hb.get("gas_age_s") is not None
        gas_label    = "yes" if has_gas else "no"

        print()
        print(bold(f"Pi {pi_id}") + f"  [{image_id}]  {dot}  Last data: {age_s} ago")
        print(f"  Uptime: {uptime_d}d {uptime_h}h  |  DS18B20 sensors: {n_sensors}"
              f"  |  Gas (P1): {gas_label}")

        # ── completeness table ──
        _show_completeness(days_agg, dates, today_str, n_sensors)

        # ── temperature ranges ──
        _show_temperature(days_agg, dates)

        # ── gas consumption ──
        if has_gas:
            _show_gas(days_agg, dates)

    print()
    print(dim(f"Pis shown: {len(pis_hb)}  |  Dates: {dates[-1]} → {dates[0]}"))
    print()


def _show_completeness(
    days_agg: dict,
    dates: list[str],
    today_str: str,
    n_sensors: int,
) -> None:
    print()
    print(f"  {'Date':12}  {'Temp':>8}  {'OTGW':>6}  {'Gas':>6}")
    print(f"  {dim('─' * 40)}")

    temp_pcts: list[float] = []
    otgw_days = 0
    gas_days  = 0
    n_dates   = 0

    for date_str in dates:
        day = days_agg.get(date_str)
        if day is None:
            print(f"  {dim(date_str):12}  {dim('no data'):>8}  {dim('—'):>6}  {dim('—'):>6}")
            continue

        n_dates += 1
        is_today = date_str == today_str

        # Temperature completeness
        expected_temp = HB_PER_DAY * max(n_sensors, 1)
        temp_pct      = completeness_pct(day["temp_lines"], expected_temp)
        temp_col      = pct_colour(temp_pct) + (dim("~") if is_today else "")

        # OTGW / gas: presence
        otgw_present = day["otgw_lines"] > 0
        gas_present  = day["gas_lines"] > 0

        otgw_col = green("✓") if otgw_present else red("✗")
        gas_col  = green("✓") if gas_present  else dim("—")

        temp_pcts.append(temp_pct)
        if otgw_present:
            otgw_days += 1
        if gas_present:
            gas_days  += 1

        print(f"  {date_str:12}  {temp_col:>8}  {otgw_col:>6}  {gas_col:>6}")

    # Overall row
    if n_dates:
        avg_temp = sum(temp_pcts) / len(temp_pcts)
        otgw_s   = f"{otgw_days}/{n_dates}"
        gas_s    = f"{gas_days}/{n_dates}"
        print(f"  {dim('─' * 40)}")
        print(f"  {'Overall':12}  {pct_colour(avg_temp):>8}  {cyan(otgw_s):>6}  {cyan(gas_s):>6}")


def _show_temperature(days_agg: dict, dates: list[str]) -> None:
    # Aggregate per sensor across all days; sort IDs for stable numbering
    sensors: dict[str, dict] = {}
    for date_str in dates:
        day = days_agg.get(date_str, {})
        for sid, stats in day.get("temp_stats", {}).items():
            if sid not in sensors:
                sensors[sid] = {"min": stats["min"], "max": stats["max"],
                                 "sum": 0.0, "count": 0}
            sensors[sid]["min"]    = min(sensors[sid]["min"], stats["min"])
            sensors[sid]["max"]    = max(sensors[sid]["max"], stats["max"])
            sensors[sid]["sum"]   += stats["sum"]
            sensors[sid]["count"] += stats["count"]

    if not sensors:
        return

    print()
    print(f"  {'Temperature (DS18B20)':20}  {'Min':>6}  {'Avg':>6}  {'Max':>6}")
    print(f"  {dim('─' * 44)}")
    for i, (_, s) in enumerate(sorted(sensors.items()), start=1):
        avg = s["sum"] / s["count"] if s["count"] else 0.0
        print(f"  {f'Sensor {i}':20}  {s['min']:>5.1f}°  {avg:>5.1f}°  {s['max']:>5.1f}°")


def _show_gas(days_agg: dict, dates: list[str]) -> None:
    deltas: list[float] = []
    for date_str in dates:
        day   = days_agg.get(date_str, {})
        start = day.get("gas_start")
        end   = day.get("gas_end")
        if start is not None and end is not None:
            deltas.append(end - start)

    if not deltas:
        return

    g_min  = min(deltas)
    g_max  = max(deltas)
    g_mean = sum(deltas) / len(deltas)
    print()
    print(f"  {'Gas consumption (m³/day)':30}  {'Min':>6}  {'Mean':>6}  {'Max':>6}")
    print(f"  {dim('─' * 54)}")
    print(f"  {f'{len(deltas)} days with data':30}  {g_min:>6.3f}  {g_mean:>6.3f}  {g_max:>6.3f}")


# ── HTML report ───────────────────────────────────────────────────────────────

def write_html(
    pis_hb: list[dict],
    agg: list[dict],
    dates: list[str],
    today_str: str,
    path: str,
) -> None:
    """Generate a self-contained HTML report."""
    agg_index = {(a["image_id"], a["pi_id"]): a for a in agg}
    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M")

    rows_html = ""
    for pi_hb in pis_hb:
        image_id = pi_hb["_image_id"]
        pi_id    = pi_hb["_pi_id"]
        pi_agg   = agg_index.get((image_id, pi_id), {})
        days_agg = pi_agg.get("days", {})
        rows_html += _html_pi_section(pi_hb, pi_agg, days_agg, dates, today_str)

    html = textwrap.dedent(f"""\
    <!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>otgw-pi Fleet Report — {now_str}</title>
    <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #222; background: #fafafa; }}
    h1   {{ font-size: 1.3rem; margin-bottom: 0.2rem; }}
    .subtitle {{ color: #666; font-size: 0.85rem; margin-bottom: 2rem; }}
    .pi-card   {{ background: #fff; border: 1px solid #ddd; border-radius: 8px;
                  padding: 1.2rem 1.5rem; margin-bottom: 1.5rem; }}
    .pi-header {{ font-size: 1.05rem; font-weight: 600; margin-bottom: 0.2rem; }}
    .pi-meta   {{ color: #555; font-size: 0.85rem; margin-bottom: 1rem; }}
    .dot-ok  {{ color: #22a55b; }}
    .dot-bad {{ color: #d63031; }}
    table    {{ border-collapse: collapse; font-size: 0.88rem; margin-bottom: 1.2rem; }}
    th, td   {{ padding: 0.25rem 0.8rem; text-align: right; border-bottom: 1px solid #eee; }}
    th       {{ font-weight: 600; text-align: right; color: #444; }}
    td:first-child, th:first-child {{ text-align: left; }}
    tr.overall td {{ font-weight: 600; border-top: 2px solid #ccc; }}
    .ok      {{ color: #22a55b; }}
    .warn    {{ color: #e17055; }}
    .bad     {{ color: #d63031; }}
    .dim     {{ color: #999; }}
    .section-title {{ font-size: 0.8rem; font-weight: 600; text-transform: uppercase;
                      color: #888; letter-spacing: 0.05em; margin-bottom: 0.4rem; }}
    </style>
    </head>
    <body>
    <h1>otgw-pi Fleet Report</h1>
    <div class="subtitle">Generated {now_str} &nbsp;|&nbsp; {len(pis_hb)} Pi(s) &nbsp;|&nbsp;
    {dates[-1]} → {dates[0]}</div>
    {rows_html}
    </body>
    </html>
    """)

    Path(path).write_text(html)
    print(f"HTML report written to: {path}")


def _html_pi_section(
    pi_hb: dict,
    pi_agg: dict,
    days_agg: dict,
    dates: list[str],
    today_str: str,
) -> str:
    image_id     = pi_hb["_image_id"]
    pi_id        = pi_hb["_pi_id"]
    last_data_ts = pi_agg.get("last_data_ts")
    data_age     = (time.time() - last_data_ts) if last_data_ts else None
    active       = data_age is not None and data_age < 2 * 3600
    dot_cls      = "dot-ok" if active else "dot-bad"
    dot_chr      = "●"
    uptime_s     = pi_hb.get("uptime_seconds", 0)
    uptime_d     = uptime_s // 86400
    uptime_h     = (uptime_s % 86400) // 3600
    n_sensors    = pi_hb.get("ds18b20_count", 0)
    has_gas      = pi_hb.get("gas_age_s") is not None
    age_s        = age_str(data_age)

    header = (
        f'<div class="pi-header"><span class="{dot_cls}">{dot_chr}</span> '
        f"Pi {pi_id} &nbsp; <span style='font-weight:400;color:#666'>[{image_id}]</span></div>"
        f'<div class="pi-meta">Last data: {age_s} ago &nbsp;|&nbsp; '
        f"Uptime: {uptime_d}d {uptime_h}h &nbsp;|&nbsp; "
        f"DS18B20: {n_sensors} sensor(s) &nbsp;|&nbsp; "
        f"Gas (P1): {'yes' if has_gas else 'no'}</div>"
    )

    # Completeness table
    comp_rows = ""
    temp_pcts: list[float] = []
    otgw_days = gas_days = n_dates = 0

    def _pct_cls(p: float) -> str:
        return "ok" if p >= 90 else ("warn" if p >= 60 else "bad")

    for date_str in dates:
        day = days_agg.get(date_str)
        if day is None:
            comp_rows += (
                f"<tr><td>{date_str}</td>"
                f"<td class='dim'>—</td>"
                f"<td class='dim'>—</td><td class='dim'>—</td></tr>"
            )
            continue
        n_dates += 1
        is_today = date_str == today_str
        t_pct    = completeness_pct(day["temp_lines"], HB_PER_DAY * max(n_sensors, 1))
        otgw_ok  = day["otgw_lines"] > 0
        gas_ok   = day["gas_lines"]  > 0
        temp_pcts.append(t_pct)
        if otgw_ok: otgw_days += 1
        if gas_ok:  gas_days  += 1

        today_mark = " ~" if is_today else ""
        comp_rows += (
            f"<tr><td>{date_str}{today_mark}</td>"
            f"<td class='{_pct_cls(t_pct)}'>{t_pct:.0f}%</td>"
            f"<td class='{'ok' if otgw_ok else 'bad'}'>{'✓' if otgw_ok else '✗'}</td>"
            f"<td class='{'ok' if gas_ok else 'dim'}'>{'✓' if gas_ok else '—'}</td></tr>"
        )

    overall_temp = sum(temp_pcts) / len(temp_pcts) if temp_pcts else 0.0
    comp_rows += (
        f"<tr class='overall'><td>Overall</td>"
        f"<td class='{_pct_cls(overall_temp)}'>{overall_temp:.0f}%</td>"
        f"<td>{otgw_days}/{n_dates}</td>"
        f"<td>{gas_days}/{n_dates}</td></tr>"
    )

    comp_table = (
        "<div class='section-title'>Data completeness</div>"
        "<table><tr><th>Date</th><th>Temperature</th>"
        "<th>OTGW</th><th>Gas</th></tr>"
        f"{comp_rows}</table>"
    )

    # Temperature table — one row per sensor, numbered, no raw IDs
    temp_table = ""
    sensors: dict[str, dict] = {}
    for date_str in dates:
        for sid, s in days_agg.get(date_str, {}).get("temp_stats", {}).items():
            if sid not in sensors:
                sensors[sid] = {"min": s["min"], "max": s["max"],
                                 "sum": 0.0, "count": 0}
            sensors[sid]["min"]    = min(sensors[sid]["min"], s["min"])
            sensors[sid]["max"]    = max(sensors[sid]["max"], s["max"])
            sensors[sid]["sum"]   += s["sum"]
            sensors[sid]["count"] += s["count"]

    if sensors:
        sensor_rows = ""
        for i, (_, s) in enumerate(sorted(sensors.items()), start=1):
            avg = s["sum"] / s["count"] if s["count"] else 0.0
            sensor_rows += (
                f"<tr><td>Sensor {i}</td>"
                f"<td>{s['min']:.1f} °C</td><td>{avg:.1f} °C</td>"
                f"<td>{s['max']:.1f} °C</td></tr>"
            )
        temp_table = (
            "<div class='section-title'>Temperature ranges (DS18B20)</div>"
            "<table><tr><th></th><th>Min</th><th>Avg</th><th>Max</th></tr>"
            f"{sensor_rows}</table>"
        )

    # Gas table — min/mean/max across days
    gas_table = ""
    if has_gas:
        deltas: list[float] = []
        for date_str in dates:
            day   = days_agg.get(date_str, {})
            start = day.get("gas_start")
            end   = day.get("gas_end")
            if start is not None and end is not None:
                deltas.append(end - start)
        if deltas:
            g_min  = min(deltas)
            g_max  = max(deltas)
            g_mean = sum(deltas) / len(deltas)
            gas_table = (
                "<div class='section-title'>Gas consumption (m³/day)</div>"
                "<table><tr><th></th><th>Min</th><th>Mean</th><th>Max</th></tr>"
                f"<tr><td>{len(deltas)} days with data</td>"
                f"<td>{g_min:.3f}</td><td>{g_mean:.3f}</td><td>{g_max:.3f}</td></tr>"
                "</table>"
            )

    return (
        f'<div class="pi-card">{header}'
        f"{comp_table}{temp_table}{gas_table}</div>"
    )


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fleet data quality report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--vps",        default=DEFAULT_VPS,
                        help=f"VPS SSH target (default: {DEFAULT_VPS})")
    parser.add_argument("--days",       type=int, default=DEFAULT_DAYS,
                        help=f"Days of history to show (default: {DEFAULT_DAYS})")
    parser.add_argument("--start-date", default=DEFAULT_START_DATE,
                        help=f"Ignore data before this date (default: {DEFAULT_START_DATE})")
    parser.add_argument("--pi-id",      default="",
                        help="Only report on this Pi ID")
    parser.add_argument("--skip-pi",    default="",
                        help="Comma-separated Pi IDs to exclude")
    parser.add_argument("--html",       nargs="?", const=DEFAULT_HTML_PATH, metavar="PATH",
                        help="Write HTML report (default path: fleet_report.html)")
    args = parser.parse_args()

    skip_pis   = {s.strip() for s in args.skip_pi.split(",") if s.strip()}
    today      = date.today()
    today_str  = today.isoformat()
    floor_date = date.fromisoformat(args.start_date)
    end_date   = today
    start_date = max(floor_date, today - timedelta(days=args.days - 1))
    dates      = date_range(start_date, end_date)

    print(f"Connecting to {args.vps} …", end=" ", flush=True)
    all_pis = discover_pis(args.vps)
    print(f"found {len(all_pis)} Pi record(s)")

    active_pis = filter_pis(
        all_pis,
        pi_id=args.pi_id,
        skip_pis=skip_pis,
    )
    if not active_pis:
        print(yellow("No active Pis matched the filters."))
        sys.exit(0)

    print(f"Aggregating logs for {len(active_pis)} Pi(s) over {len(dates)} day(s) …",
          end=" ", flush=True)
    agg = aggregate_logs(args.vps, active_pis, dates)
    print("done")

    show_report(active_pis, agg, dates, today_str)

    if args.html:
        write_html(active_pis, agg, dates, today_str, args.html)


if __name__ == "__main__":
    main()
