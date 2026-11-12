#!/usr/bin/env python3
"""
P1 USB buffer sync — retrieve buffered gas readings from the P1 dongle.

Sends P1DUMP: commands over USB serial to stream records stored in the
dongle's LittleFS flash and merges them into the Pi's date-partitioned
gas log files, deduplicating by meter timestamp.

CLI usage:
    python3 p1_usb_sync.py --port /dev/ttyACM0 \\
        --effective-dir /var/log/otgw/a1b2c3 \\
        --image-id a1b2c3

Library usage:
    from p1_usb_sync import run_sync, SyncResult
    result = run_sync(port='/dev/ttyACM0', effective_dir='/var/log/otgw/a1b2c3',
                      image_id='a1b2c3', timezone='Europe/Amsterdam')
"""
__author__ = 'Nikolaas Oosterhof'
__copyright__ = 'Copyright 2026 Nikolaas Oosterhof'

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import serial

# Serial settings — must match P1 dongle USB CDC defaults
_BAUD = 115200
_LINE_TIMEOUT_S = 30.0   # max wait for next P1DUMP: line during streaming
_CMD_TIMEOUT_S = 5.0     # max wait for STATUS / BEGIN / CLEAR response
_POLL_TIMEOUT_S = 0.2    # pyserial readline poll interval
_MAX_RETRIES = 2         # automatic retries on mid-stream timeout (NFR-4.79)
_RETRY_DELAY_S = 3.0     # pause between retries (lets USB settle)


@dataclass
class SyncResult:
    success: bool
    received: int = 0      # total records streamed from dongle
    merged: int = 0        # records written to log (new)
    skipped: int = 0       # records already in log (duplicates)
    malformed: int = 0     # records with unparseable JSON or missing fields
    error: Optional[str] = None

    def summary(self) -> str:
        if not self.success:
            return f'Error: {self.error}'
        return (f'{self.merged} new, {self.skipped} duplicate, '
                f'{self.malformed} malformed ({self.received} total received)')


# ── serial helpers ─────────────────────────────────────────────────────────────

def _read_p1dump_line(ser: serial.Serial, timeout_s: float) -> Optional[str]:
    """Read from serial until a P1DUMP: response line arrives or timeout."""
    deadline = time.monotonic() + timeout_s
    ser.timeout = _POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode('utf-8', errors='replace').strip()
        if line.startswith('P1DUMP:'):
            return line
    return None


def _send_cmd(ser: serial.Serial, cmd: str) -> None:
    ser.write((cmd + '\n').encode())
    ser.flush()


# ── record parsing ─────────────────────────────────────────────────────────────

@dataclass
class _Record:
    raw_json: str
    meter_ts: str        # ISO 8601 string from current.timestamp
    meter_date: str      # YYYY-MM-DD in configured timezone


def _parse_record(line: str, tz: ZoneInfo) -> Optional['_Record']:
    """Parse a P1DUMP:DATA: line into a _Record. Returns None on failure."""
    json_str = line[len('P1DUMP:DATA:'):]
    try:
        doc = json.loads(json_str)
        ts_str = doc['current']['timestamp']
        dt = datetime.fromisoformat(ts_str).astimezone(tz)
        date_str = dt.strftime('%Y-%m-%d')
        return _Record(raw_json=json_str, meter_ts=ts_str, meter_date=date_str)
    except (KeyError, ValueError, TypeError):
        return None


# ── log file helpers ───────────────────────────────────────────────────────────

def _log_path(effective_dir: str, image_id: str, date_str: str) -> Path:
    return Path(effective_dir) / date_str / f'gas_{image_id}.log'


def _load_existing_timestamps(log_file: Path) -> set[str]:
    """Return the set of meter timestamp strings already in the log file."""
    if not log_file.exists():
        return set()
    found: set[str] = set()
    try:
        for line in log_file.read_text(errors='replace').splitlines():
            # Timestamp appears both as the log prefix and inside current.timestamp.
            # Extracting from JSON is more precise than splitting on ' | '.
            if '"timestamp":' in line:
                try:
                    payload_start = line.index(' : ') + 3
                    doc = json.loads(line[payload_start:])
                    ts = doc.get('current', {}).get('timestamp', '')
                    if ts:
                        found.add(ts)
                except (ValueError, KeyError):
                    pass
    except OSError:
        pass
    return found


def _format_log_entry(record: _Record, image_id: str, mode: str) -> str:
    root = 'test' if mode == 'test' else 'home'
    topic = f'{root}/{image_id}/energy/gas'
    return f'{record.meter_ts} | {topic} : {record.raw_json}\n'


# ── merge ──────────────────────────────────────────────────────────────────────

def merge_records(
    records: list[_Record],
    effective_dir: str,
    image_id: str,
    mode: str = 'production',
) -> tuple[int, int]:
    """Write new records to dated log files, skipping duplicates.

    Returns (merged_count, skipped_count).
    """
    # Group records by date
    by_date: dict[str, list[_Record]] = {}
    for rec in records:
        by_date.setdefault(rec.meter_date, []).append(rec)

    merged = 0
    skipped = 0
    for date_str, recs in sorted(by_date.items()):
        log_file = _log_path(effective_dir, image_id, date_str)
        existing = _load_existing_timestamps(log_file)

        to_write = []
        for rec in recs:
            if rec.meter_ts in existing:
                skipped += 1
            else:
                to_write.append(rec)
                existing.add(rec.meter_ts)  # prevent intra-batch duplicates

        if to_write:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open('a') as fh:
                for rec in to_write:
                    fh.write(_format_log_entry(rec, image_id, mode))
            merged += len(to_write)

    return merged, skipped


# ── protocol ───────────────────────────────────────────────────────────────────

def query_status(port: str) -> Optional[dict]:
    """Return {'pending_count': N, 'file_bytes': M, 'offset': K} or None on error."""
    try:
        with serial.Serial(port, _BAUD) as ser:
            _send_cmd(ser, 'P1DUMP:STATUS')
            line = _read_p1dump_line(ser, _CMD_TIMEOUT_S)
            if line and line.startswith('P1DUMP:STATUS:'):
                return json.loads(line[len('P1DUMP:STATUS:'):])
    except (serial.SerialException, json.JSONDecodeError, OSError):
        pass
    return None


def send_clear(port: str) -> tuple[bool, str]:
    """Send P1DUMP:CLEAR to advance the dongle's MQTT offset to EOF.

    Returns (success, message).
    """
    try:
        with serial.Serial(port, _BAUD) as ser:
            _send_cmd(ser, 'P1DUMP:CLEAR')
            line = _read_p1dump_line(ser, _CMD_TIMEOUT_S)
            if line and line.startswith('P1DUMP:CLEAR:OK:'):
                info = json.loads(line[len('P1DUMP:CLEAR:OK:'):])
                cleared = info.get('cleared_count', '?')
                return True, f'Cleared {cleared} records from dongle MQTT offset'
            if line and line.startswith('P1DUMP:ERROR:'):
                return False, line[len('P1DUMP:ERROR:'):]
            return False, f'Unexpected response: {line!r}'
    except (serial.SerialException, OSError) as exc:
        return False, str(exc)


def _attempt_dump(
    port: str,
    cmd: str,
    tz: ZoneInfo,
    progress_cb: Optional[Callable[[int], None]],
) -> tuple[bool, list['_Record'], int, int, Optional[str]]:
    """Single dump attempt.  Returns (ok, records, received, malformed, error)."""
    try:
        ser = serial.Serial(port, _BAUD)
    except (serial.SerialException, OSError) as exc:
        return False, [], 0, 0, f'Cannot open {port}: {exc}'

    try:
        _send_cmd(ser, cmd)

        begin_line = _read_p1dump_line(ser, _CMD_TIMEOUT_S)
        if not begin_line:
            return False, [], 0, 0, 'No response from dongle (timeout)'
        if begin_line.startswith('P1DUMP:ERROR:'):
            return False, [], 0, 0, begin_line[len('P1DUMP:ERROR:'):]
        if not begin_line.startswith('P1DUMP:BEGIN:'):
            return False, [], 0, 0, f'Unexpected response: {begin_line!r}'

        received_records: list[_Record] = []
        malformed = 0
        received_count = 0

        while True:
            line = _read_p1dump_line(ser, _LINE_TIMEOUT_S)
            if line is None:
                # Timeout mid-stream — caller will retry if attempts remain.
                return False, received_records, received_count, malformed, (
                    f'Timeout after {received_count} records'
                )

            if line.startswith('P1DUMP:DATA:'):
                received_count += 1
                rec = _parse_record(line, tz)
                if rec:
                    received_records.append(rec)
                else:
                    malformed += 1
                if progress_cb:
                    progress_cb(received_count)

            elif line.startswith('P1DUMP:END:'):
                return True, received_records, received_count, malformed, None

            elif line.startswith('P1DUMP:ERROR:'):
                return False, received_records, received_count, malformed, (
                    f'Dongle error mid-stream: {line[len("P1DUMP:ERROR:"):]}'
                )
    finally:
        ser.close()


def run_sync(
    port: str,
    effective_dir: str,
    image_id: str,
    timezone: str = 'Europe/Amsterdam',
    mode: str = 'production',
    full: bool = False,
    progress_cb: Optional[Callable[[int], None]] = None,
) -> SyncResult:
    """Stream buffered records from the P1 dongle and merge into gas logs.

    Retries up to _MAX_RETRIES times on mid-stream timeout (USB hiccup or
    slow flash read). Duplicate records from a retry are harmless — merged
    records are deduplicated by meter timestamp. (NFR-4.79)

    Args:
        port:          Serial device (e.g. '/dev/ttyACM0').
        effective_dir: Root directory for gas logs ({LOG_DIR}/{image_id}).
        image_id:      Image/device ID for log filenames and MQTT topic.
        timezone:      IANA timezone name for date directory calculation.
        mode:          'production' or 'test' — sets topic root in log entries.
        full:          True → dump entire buffer (P1DUMP:START:full);
                       False → dump only undelivered records (P1DUMP:START).
        progress_cb:   Called with current received count each record.

    Returns:
        SyncResult with counts and success flag.
    """
    tz = ZoneInfo(timezone)
    cmd = 'P1DUMP:START:full' if full else 'P1DUMP:START'

    last_error: Optional[str] = None
    for attempt in range(_MAX_RETRIES + 1):
        if attempt > 0:
            print(
                f'p1_usb_sync: retry {attempt}/{_MAX_RETRIES} '
                f'after error: {last_error}',
                flush=True,
            )
            time.sleep(_RETRY_DELAY_S)

        ok, received_records, received_count, malformed, last_error = (
            _attempt_dump(port, cmd, tz, progress_cb)
        )

        if ok:
            break

        # Only retry on timeout — not on serial open failure or dongle errors
        if last_error and 'Timeout' not in last_error:
            return SyncResult(
                success=False,
                received=received_count,
                malformed=malformed,
                error=last_error,
            )
    else:
        return SyncResult(
            success=False,
            received=received_count,
            malformed=malformed,
            error=f'Sync failed after {_MAX_RETRIES + 1} attempts: {last_error}',
        )

    # All records received — merge into log files
    merged, skipped = merge_records(
        received_records, effective_dir, image_id, mode,
    )
    return SyncResult(
        success=True,
        received=received_count,
        merged=merged,
        skipped=skipped,
        malformed=malformed,
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def _find_p1_port() -> str:
    import glob as _glob
    for pattern in ['/dev/ttyACM*', '/dev/ttyUSB*']:
        devs = sorted(_glob.glob(pattern))
        if devs:
            return devs[0]
    return ''


def main() -> None:
    parser = argparse.ArgumentParser(description='Sync buffered gas readings from P1 dongle via USB.')
    parser.add_argument('--port', help='Serial port (auto-detected if omitted)')
    parser.add_argument('--effective-dir', required=True,
                        help='Gas log root directory (e.g. /var/log/otgw/a1b2c3)')
    parser.add_argument('--image-id', required=True, help='Image/device ID')
    parser.add_argument('--timezone', default='Europe/Amsterdam')
    parser.add_argument('--mode', default='production', choices=['production', 'test'])
    parser.add_argument('--full', action='store_true',
                        help='Dump full buffer from byte 0 (default: undelivered only)')
    parser.add_argument('--status', action='store_true',
                        help='Query dongle buffer status and exit')
    parser.add_argument('--clear', action='store_true',
                        help='Send P1DUMP:CLEAR after successful sync')
    args = parser.parse_args()

    port = args.port or _find_p1_port()
    if not port:
        print('Error: no P1 dongle found. Use --port to specify.')
        raise SystemExit(1)

    if args.status:
        status = query_status(port)
        if status:
            print(f"Pending: {status.get('pending_count', '?')} records")
            print(f"File:    {status.get('file_bytes', '?')} bytes")
            print(f"Offset:  {status.get('offset', '?')} bytes")
        else:
            print('Error: could not query dongle status')
            raise SystemExit(1)
        return

    print(f'Syncing from {port} → {args.effective_dir} ...')

    def _progress(n: int) -> None:
        print(f'\r  Received {n} records...', end='', flush=True)

    result = run_sync(
        port=port,
        effective_dir=args.effective_dir,
        image_id=args.image_id,
        timezone=args.timezone,
        mode=args.mode,
        full=args.full,
        progress_cb=_progress,
    )
    print()

    if result.success:
        print(f'Done: {result.summary()}')
        if args.clear and result.received > 0:
            ok, msg = send_clear(port)
            print(f'Clear: {msg}')
            if not ok:
                raise SystemExit(1)
    else:
        print(f'Failed: {result.error}')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
