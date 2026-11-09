#!/usr/bin/env python3
"""Run an interactive installer test on a private file-backed QEMU NVMe disk."""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess as sp
import sys

REPO = Path(__file__).resolve().parents[1]


def regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f'A regular file is required (no device or symlink): {path}')


def firmware_pair(directory=Path('/usr/share/OVMF')):
    for suffix in ('_4M', ''):
        code = directory / f'OVMF_CODE{suffix}.fd'
        variables = directory / f'OVMF_VARS{suffix}.fd'
        if code.is_file() and variables.is_file():
            return code, variables
    raise RuntimeError('Matching OVMF CODE/VARS files missing; install ovmf')


def preflight():
    if sys.platform != 'linux' or os.uname().machine != 'x86_64':
        raise RuntimeError('Linux x86_64 is required for the KVM test')
    for tool in ('qemu-system-x86_64', 'qemu-img'):
        if not shutil.which(tool):
            raise RuntimeError('Install test tools: sudo apt-get install qemu-system-x86 qemu-utils ovmf')
    descriptor = os.open('/dev/kvm', os.O_RDWR | os.O_CLOEXEC)
    os.close(descriptor)


def state_directory(args):
    state = args.state_dir.absolute()
    if state.is_symlink():
        raise ValueError('VM state directory cannot be a symlink')
    state = state.resolve()
    if state == REPO or state in REPO.parents or state == Path('/'):
        raise ValueError('Use a dedicated VM state directory')
    return state


def prepare(args):
    state = state_directory(args)
    if args.boot == 'installer':
        if args.iso is None:
            raise ValueError('--iso is required when booting the installer')
        regular(args.iso)
    marker = state / 'vm.json'
    disk = state / 'disk.qcow2'
    if marker.exists():
        regular(marker)
        settings = json.loads(marker.read_text())
        if settings.get('format') != 1 or settings.get('firmware') not in ('uefi', 'bios'):
            raise ValueError('Unrecognized VM state directory')
        if settings['firmware'] == 'uefi' and not isinstance(settings.get('ovmf_code'), str):
            raise ValueError('Missing UEFI firmware path in VM state')
        if args.firmware and args.firmware != settings['firmware']:
            raise ValueError('Firmware differs from existing VM; use a new state directory')
        regular(disk)
        information = json.loads(sp.check_output(['qemu-img', 'info', '--output=json', str(disk)], text=True))
        if (information.get('format') != 'qcow2' or information.get('backing-filename')
                or information.get('format-specific', {}).get('data', {}).get('data-file')):
            raise ValueError('Only a standalone qcow2 test disk is supported')
    else:
        if args.boot == 'installed':
            raise ValueError('No existing test VM; install into a new state directory first')
        if state.exists() and any(path.name != '.running.lock' for path in state.iterdir()):
            raise ValueError('New VM state directory must be empty; existing files are never replaced')
        settings = {'format': 1, 'firmware': args.firmware or 'uefi', 'disk_gib': args.disk_gib}
        # Validate firmware before creating files.
        if settings['firmware'] == 'uefi':
            code, variables = firmware_pair()
            settings['ovmf_code'] = str(code)
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        sp.run(['qemu-img', 'create', '-f', 'qcow2', str(disk), f'{args.disk_gib}G'], check=True)
        if settings['firmware'] == 'uefi':
            shutil.copyfile(variables, state / 'OVMF_VARS.fd')
        marker.write_text(json.dumps(settings, indent=2) + '\n')
    return state, settings


def command(args, state, settings):
    disk = state / 'disk.qcow2'
    argv = ['qemu-system-x86_64', '-name', 'ubuntu-customizer-test', '-machine', 'q35',
            '-accel', 'kvm', '-cpu', 'host', '-smp', str(args.cpus), '-m', str(args.memory_mib),
            '-display', 'none', '-vnc', f'127.0.0.1:{args.vnc_display}', '-monitor', 'none',
            '-blockdev', json.dumps({'driver': 'file', 'filename': str(disk), 'node-name': 'test-file'}),
            '-blockdev', json.dumps({'driver': 'qcow2', 'file': 'test-file', 'node-name': 'test-disk'}),
            '-device', 'nvme,drive=test-disk,serial=UCVMTEST0001',
            '-netdev', f'user,id=net0,restrict={"off" if args.internet else "on"},'
            f'hostfwd=tcp:127.0.0.1:{args.ssh_port}-:22,hostfwd=tcp:127.0.0.1:{args.log_port}-:8000',
            '-device', 'e1000,netdev=net0', '-boot', 'order=d,menu=on' if args.boot == 'installer' else 'order=c,menu=on']
    if settings['firmware'] == 'uefi':
        code = Path(settings['ovmf_code']).resolve()
        regular(code)
        variables = state / 'OVMF_VARS.fd'
        regular(variables)
        argv[argv.index('-machine') + 1] = 'q35,pflash0=firmware-code,pflash1=firmware-vars'
        for name, readonly, node in ((code, True, 'firmware-code'), (variables, False, 'firmware-vars')):
            argv.extend(['-blockdev', json.dumps({'driver': 'raw', 'node-name': node, 'read-only': readonly,
                                                'file': {'driver': 'file', 'filename': str(name)}})])
    if args.boot == 'installer':
        # CD-ROM is read-only; the boot target is always the standalone qcow2.
        argv.extend(['-cdrom', str(args.iso.absolute())])
    return argv


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iso', type=Path)
    parser.add_argument('--state-dir', type=Path, default=REPO / 'work/vm-test')
    parser.add_argument('--boot', choices=('installer', 'installed'), default='installer')
    parser.add_argument('--firmware', choices=('uefi', 'bios'))
    parser.add_argument('--disk-gib', type=int, default=80)
    parser.add_argument('--cpus', type=int, default=4)
    parser.add_argument('--memory-mib', type=int, default=4096)
    parser.add_argument('--vnc-display', type=int, default=1)
    parser.add_argument('--ssh-port', type=int, default=2222)
    parser.add_argument('--log-port', type=int, default=18000)
    parser.add_argument('--internet', action='store_true', help='Allow guest outbound Internet (disabled by default)')
    args = parser.parse_args()
    if not 32 <= args.disk_gib <= 2048 or not 1 <= args.cpus <= 32 or not 2048 <= args.memory_mib <= 65536:
        parser.error('Use disk 32..2048 GiB, CPU 1..32 and memory 2048..65536 MiB')
    if not 1 <= args.vnc_display <= 99 or not all(1024 <= port <= 65535 for port in (args.ssh_port, args.log_port)):
        parser.error('Use VNC display 1..99 and TCP ports 1024..65535')
    if len({5900 + args.vnc_display, args.ssh_port, args.log_port}) != 3:
        parser.error('VNC, SSH and log ports must differ')
    return args


def main():
    args = arguments()
    try:
        preflight()
        state = state_directory(args)
        if args.boot == 'installed' and not state.is_dir():
            raise ValueError('No existing test VM; install into a new state directory first')
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A directory-local lock prevents two test processes using the same VM.
        import fcntl
        with (state / '.running.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state, settings = prepare(args)
            argv = command(args, state, settings)
            print(f'Test disk: {state / "disk.qcow2"}\nFirmware: {settings["firmware"]}', flush=True)
            print(f'VNC: 127.0.0.1:{5900 + args.vnc_display}; SSH: 127.0.0.1:{args.ssh_port}; Live HTTP logs: 127.0.0.1:{args.log_port}', flush=True)
            print('Guest networking uses QEMU user mode; outbound Internet: ' + ('enabled' if args.internet else 'disabled'), flush=True)
            print('Ctrl+C stops the VM. Copy Live RAM logs before stopping. QEMU log: ' + str(state / 'qemu.log'), flush=True)
            with (state / 'qemu.log').open('ab') as log, sp.Popen(argv, stdout=log, stderr=sp.STDOUT) as vm:
                try:
                    result = vm.wait()
                except KeyboardInterrupt:
                    vm.terminate()
                    try:
                        vm.wait(timeout=5)
                    except sp.TimeoutExpired:
                        vm.kill()
                        vm.wait()
                    return 130
            print('VM exited. Installation/boot acceptance must be checked in the guest; QEMU exit is not a passing test.')
            if result:
                print('Recent QEMU output:\n' + (state / 'qemu.log').read_text(errors='replace')[-4096:], file=sys.stderr)
            return result
    except (OSError, ValueError, RuntimeError, sp.SubprocessError) as error:
        print(f'VM test failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
