#!/usr/bin/env bash
# compress_logs.sh — gzip log files older than N days on the VPS.
#
# Run as the logbackup user (or root).  Safe to run repeatedly — already
# compressed files (.gz) are skipped.  Uncompressed files are replaced by
# .gz counterparts; no data is lost.
#
# Install as a daily cron job:
#   crontab -e   (as logbackup)
#   0 3 * * * /home/logbackup/compress_logs.sh >> /home/logbackup/compress_logs.log 2>&1
#
# Reading compressed files:
#   zcat file.log.gz              # dump to stdout
#   zless file.log.gz             # page through
#   zgrep "pattern" file.log.gz   # search
#   zcat *.gz | grep "pattern"    # search across multiple files
#
# Typical compression ratio for OTGW/temperature/gas text logs: 90–95%.

set -euo pipefail

LOG_ROOT="${1:-/home/logbackup/otgw-logs}"   # directory containing per-Pi subdirs
MIN_AGE_DAYS="${2:-7}"                        # compress files older than this many days
GZIP_LEVEL="-9"                              # maximum compression

# Validate
if [[ ! -d "$LOG_ROOT" ]]; then
    echo "$(date -Iseconds) ERROR: LOG_ROOT '$LOG_ROOT' not found" >&2
    exit 1
fi

compressed=0
skipped=0
errors=0

# Find all uncompressed log files older than MIN_AGE_DAYS, excluding:
#   heartbeat.json  — always-current, never compress (and never excluded from sync)
#   *.gz            — already compressed
while IFS= read -r -d '' file; do
    if gzip $GZIP_LEVEL --keep --suffix .gz "$file" 2>/dev/null; then
        # Verify the .gz is readable before removing the original
        if gzip -t "${file}.gz" 2>/dev/null; then
            rm "$file"
            # Append to do_not_sync.list so the Pi stops re-syncing this file.
            # List is append-only (sort -u deduplicates in-place) — entries are
            # never removed, so archived files stay excluded even if .gz is deleted.
            #
            # The list lives at LOG_ROOT/<IMAGE_ID>/do_not_sync.list — the first
            # path component below LOG_ROOT.  This matches the gateway's BASE_DIR
            # (the rrsync root, also scoped to IMAGE_ID/), so the Pi can fetch the
            # list with "cat do_not_sync.list" without needing to know its DEVICE_ID.
            # Bare filenames in the list match in any subdirectory (rsync behaviour).
            relative="${file#${LOG_ROOT}/}"          # IMAGE_ID/[DEVICE/][DATE/]file
            image_id="${relative%%/*}"               # first component = IMAGE_ID
            if [[ -n "$image_id" && "$image_id" != "$relative" ]]; then
                list_file="${LOG_ROOT}/${image_id}/do_not_sync.list"
            else
                list_file="$(dirname "$file")/do_not_sync.list"  # flat fallback
            fi
            printf '%s\n' "$(basename "$file")" >> "$list_file"
            sort -u "$list_file" -o "$list_file"
            (( compressed++ )) || true
        else
            echo "$(date -Iseconds) WARN: compressed file failed verification, keeping original: $file" >&2
            rm -f "${file}.gz"
            (( errors++ )) || true
        fi
    else
        echo "$(date -Iseconds) WARN: gzip failed for: $file" >&2
        (( errors++ )) || true
    fi
done < <(find "$LOG_ROOT" \
    -type f \
    -name "*.log" \
    -not -name "heartbeat.json" \
    -mtime +"$MIN_AGE_DAYS" \
    -print0)

echo "$(date -Iseconds) compress_logs: compressed=$compressed skipped=$skipped errors=$errors (age>=${MIN_AGE_DAYS}d, root=$LOG_ROOT)"
