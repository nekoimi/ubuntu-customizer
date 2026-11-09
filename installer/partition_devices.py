#!/usr/bin/env python3
"""Resolve target partitions using kernel sysfs, without the udev database."""
import os
from pathlib import Path
import stat
import sys
import time


def probe(disk, sys_class=Path('/sys/class/block'), device_root=Path('/dev')):
    disk = Path(disk)
    disk_info = disk.stat()
    if not stat.S_ISBLK(disk_info.st_mode):
        raise RuntimeError(f'Target is not a block device: {disk}')
    parent = (sys_class / disk.name).resolve(strict=True)
    disk_number = f'{os.major(disk_info.st_rdev)}:{os.minor(disk_info.st_rdev)}'
    if (parent / 'partition').exists() or (parent / 'dev').read_text().strip() != disk_number:
        raise RuntimeError(f'Target sysfs identity mismatch: {disk}')
    found = {}
    for entry in parent.glob('*/partition'):
        try:
            number = entry.read_text().strip()
            child = entry.parent.resolve(strict=True)
            expected = (child / 'dev').read_text().strip()
        except FileNotFoundError:
            return None  # Partition table reread can recreate sysfs children.
        if number not in ('2', '3'):
            continue
        if child.parent != parent:
            raise RuntimeError(f'Partition is outside target disk: {child}')
        if number in found:
            raise RuntimeError(f'Duplicate partition number {number} on {disk}')
        device = device_root / child.name
        try:
            info = device.stat()
        except FileNotFoundError:
            continue  # mdev may not yet have created the device node.
        actual = f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}'
        if not stat.S_ISBLK(info.st_mode) or actual != expected:
            raise RuntimeError(f'Partition device identity mismatch: {device}')
        found[number] = str(device)
    if '2' in found and '3' in found:
        return found['3'], found['2']
    return None


def wait_for_partitions(disk, timeout=10):
    deadline = time.monotonic() + timeout
    original = Path(disk).stat().st_rdev
    while True:
        if Path(disk).stat().st_rdev != original:
            raise RuntimeError(f'Target disk changed while waiting for partitions: {disk}')
        result = probe(disk)
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Root partition 3 or EFI partition 2 did not become ready on {disk} within {timeout}s')
        time.sleep(0.2)


if __name__ == '__main__':
    try:
        root, esp = wait_for_partitions(sys.argv[1])
        print(root)
        print(esp)
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Partition discovery failed: {error}', file=sys.stderr)
        sys.exit(1)
