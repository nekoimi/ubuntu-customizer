#!/usr/bin/env python3
"""Resolve target partitions using kernel sysfs, without the udev database."""
import argparse
import os
from pathlib import Path
import re
import stat
import sys
import time


def probe(disk, sys_class=Path('/sys/class/block'), device_root=Path('/dev'), observations=None, create_missing=False):
    observations = [] if observations is None else observations
    disk = Path(disk)
    disk_info = disk.stat()
    if not stat.S_ISBLK(disk_info.st_mode):
        raise RuntimeError(f'Target is not a block device: {disk}')
    parent = (sys_class / disk.name).resolve(strict=True)
    observations.append(f'Kernel disk directory: {parent}')
    disk_number = f'{os.major(disk_info.st_rdev)}:{os.minor(disk_info.st_rdev)}'
    if (parent / 'partition').exists() or (parent / 'dev').read_text().strip() != disk_number:
        raise RuntimeError(f'Target sysfs identity mismatch: {disk}')
    found = {}
    for entry in parent.glob('*/partition'):
        try:
            number = entry.read_text().strip()
            child = entry.parent.resolve(strict=True)
            expected = (child / 'dev').read_text().strip()
        except FileNotFoundError as error:
            observations.append(f'Sysfs partition entry disappeared: {error.filename or entry}')
            return None  # Partition table reread can recreate sysfs children.
        observations.append(f'Kernel partition: {child.name}, number={number!r}, device={expected!r}')
        if number not in ('1', '2', '3'):
            continue
        if child.parent != parent:
            raise RuntimeError(f'Partition is outside target disk: {child}')
        if number in found:
            raise RuntimeError(f'Duplicate partition number {number} on {disk}')
        device = device_root / child.name
        try:
            info = device.lstat()
        except FileNotFoundError:
            observations.append(f'Missing block device node: {device}')
            if not create_missing:
                continue
            # devtmpfs/mdev may lose nodes during partition-table rereads.
            # Create only kernel-enumerated children of this verified disk;
            # never guess NVMe minor numbers or overwrite an existing entry.
            if not re.fullmatch(r'[0-9]+:[0-9]+', expected):
                raise RuntimeError(f'Invalid kernel device number: {child}')
            if disk.stat().st_rdev != disk_info.st_rdev or (sys_class / disk.name).resolve(strict=True) != parent:
                raise RuntimeError(f'Target changed before creating partition node: {disk}')
            if (child / 'partition').read_text().strip() != number or (child / 'dev').read_text().strip() != expected:
                raise RuntimeError(f'Kernel partition changed before creating node: {child}')
            major, minor = map(int, expected.split(':'))
            try:
                os.mknod(device, stat.S_IFBLK | 0o600, os.makedev(major, minor))
                print(f'Created missing partition node: {device} ({expected})', file=sys.stderr, flush=True)
            except FileExistsError:
                pass  # A concurrent kernel/mdev add is checked below.
            info = device.lstat()
            if (child / 'dev').read_text().strip() != expected or disk.stat().st_rdev != disk_info.st_rdev:
                raise RuntimeError(f'Target changed while creating partition node: {device}')
        actual = f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}'
        if not stat.S_ISBLK(info.st_mode) or actual != expected:
            raise RuntimeError(f'Partition device identity mismatch: {device}')
        found[number] = str(device)
        observations.append(f'Verified partition {number}: {device}')
    if '2' in found and '3' in found:
        return found['3'], found['2']
    observations.append('Missing verified partitions: ' + ', '.join(number for number in ('2', '3') if number not in found))
    return None


def wait_for_partitions(disk, timeout=10, create_missing=False):
    deadline = time.monotonic() + timeout
    original = Path(disk).stat().st_rdev
    while True:
        if Path(disk).stat().st_rdev != original:
            raise RuntimeError(f'Target disk changed while waiting for partitions: {disk}')
        observations = []
        result = probe(disk, observations=observations, create_missing=create_missing)
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Root partition 3 or EFI partition 2 did not become ready on {disk} within {timeout}s\n' + '\n'.join(observations))
        time.sleep(0.2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--diagnose', action='store_true', help='Print one read-only kernel/device snapshot without waiting')
    mode.add_argument('--create-missing', action='store_true', help='Create absent target partition nodes using verified kernel device numbers')
    parser.add_argument('disk')
    args = parser.parse_args()
    observations = []
    try:
        if args.diagnose:
            result = probe(args.disk, observations=observations)
            print('\n'.join(observations))
            sys.exit(0 if result is not None else 1)
        root, esp = wait_for_partitions(args.disk, create_missing=args.create_missing)
        print(root)
        print(esp)
    except (OSError, ValueError, RuntimeError) as error:
        if observations:
            print('\n'.join(observations), file=sys.stderr)
        print(f'Partition discovery failed: {error}', file=sys.stderr)
        sys.exit(1)
