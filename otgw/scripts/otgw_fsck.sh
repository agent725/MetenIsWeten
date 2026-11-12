#!/bin/bash
# otgw_fsck.sh — check and repair FAT32 partitions after boot (NFR-4.54)
#
# Runs dosfsck -n (read-only check) on each partition. If errors are found,
# unmounts, runs dosfsck -a (auto-repair), and remounts. Logs FSCK_OK,
# FSCK_REPAIR_DONE, FSCK_REPAIR_FAILED, or FSCK_SKIPPED_BUSY.
# Called by otgw-fsck.service before data-writing services start.
set -uo pipefail

check_and_repair() {
    local device="$1"
    local mountpoint="$2"

    if dosfsck -n "$device" 2>&1; then
        echo "$(date -Iseconds) FSCK_OK: $device is clean"
        return 0
    fi

    echo "$(date -Iseconds) FSCK_ERRORS: $device needs repair — attempting unmount"
    if umount -l "$device" 2>/dev/null; then
        if dosfsck -a "$device"; then
            echo "$(date -Iseconds) FSCK_REPAIR_DONE: $device repaired"
        else
            echo "$(date -Iseconds) FSCK_REPAIR_FAILED: $device — manual intervention needed"
        fi
        mount "$device" "$mountpoint"
    else
        echo "$(date -Iseconds) FSCK_SKIPPED_BUSY: $device is busy, cannot unmount for repair"
    fi
}

check_and_repair /dev/mmcblk0p3 /etc/otgw
check_and_repair /dev/mmcblk0p4 /var/log/otgw
