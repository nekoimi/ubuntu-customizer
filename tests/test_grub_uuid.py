"""GRUB must survive NVMe renumbering even when Alpine has no udev UUID links."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'components/server/bootloader.sh').read_text()
CHECK = SCRIPT.split("<<'GRUB_UUID_CHECK'\n", 1)[1].split('\nGRUB_UUID_CHECK', 1)[0]
UUID = '7acc50fc-9501-4a72-a900-9f17ef7def13'
BASH = shutil.which('bash') if os.name != 'nt' else shutil.which('bash', path=r'C:\Program Files\Git\bin')


class GrubUUID(unittest.TestCase):
    def check(self, text):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'grub.cfg'
            config.write_text(text)
            return subprocess.run([sys.executable, '-', UUID, str(config)], input=CHECK,
                                  text=True, capture_output=True, timeout=10)

    def test_all_normal_recovery_and_efi_entries_require_current_uuid(self):
        for command, initrd in (('linux', 'initrd'), ('linuxefi', 'initrdefi')):
            with self.subTest(command=command):
                config = '\n'.join(f'{command} /boot/vmlinuz root=UUID={UUID} ro {flags}\n'
                                   f'{initrd} /boot/initrd.img' for flags in ('', 'single nomodeset'))
                result = self.check(config)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('2 Linux entries', result.stdout)

    def test_renumbered_devices_old_image_uuids_and_duplicate_root_arguments_are_rejected(self):
        for argument in ('root=/dev/nvme0n1p3', 'root=/dev/nvme1n1p3', 'root=/dev/sda3',
                         'root=/dev/loop0p3', 'root=UUID=old-image-uuid', 'root=PARTUUID=other',
                         '', f'root=UUID={UUID} root=/dev/nvme0n1p3'):
            with self.subTest(argument=argument):
                result = self.check(f'linux /boot/vmlinuz {argument} ro\ninitrd /boot/initrd.img\n')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('GRUB UUID validation failed', result.stderr)
                self.assertIn('line 1', result.stderr)

    def test_bad_recovery_entry_is_not_hidden_by_valid_default(self):
        result = self.check(f'linux /boot/vmlinuz root=UUID={UUID} ro\ninitrd /boot/initrd.img\n'
                            'linux /boot/vmlinuz root=/dev/nvme0n1p3 ro single\ninitrd /boot/initrd.img\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('line 3', result.stderr)

    def test_empty_configs_and_entries_without_initrd_are_rejected(self):
        for config in ('# linux /boot/vmlinuz root=/dev/nvme0n1p3\n',
                       f'linux /boot/vmlinuz root=UUID={UUID} ro\n',
                       f'linux /boot/vmlinuz root=UUID={UUID} ro\ninitrd\n',
                       f'linux /boot/vmlinuz root=UUID={UUID} ro\n'
                       f'linux /boot/vmlinuz root=UUID={UUID} ro\ninitrd /boot/initrd.img\n'):
            with self.subTest(config=config):
                self.assertNotEqual(self.check(config).returncode, 0)


@unittest.skipUnless(BASH, 'Bash required for boot adapter regression')
class GrubUUIDAdapter(unittest.TestCase):
    def test_mdev_fallback_is_prevented_and_private_mount_is_cleaned_on_success_or_failure(self):
        # Execute the actual shell adapter with harmless command stubs. Only block
        # device tests and absolute target paths are mapped to temporary fixtures.
        stub = r'''
grub-probe() {
    if [[ $1 == --target=device ]]; then echo /dev/nvme1n1p3; else echo "$EXPECTED_UUID"; fi
}
blkid() { echo "$EXPECTED_UUID"; }
update-initramfs() { :; }
grub-install() { :; }
findmnt() { echo "$TEST_PROPAGATION"; }
mount() { echo mount >> "$TEST_EVENTS"; }
umount() { echo umount >> "$TEST_EVENTS"; }
ln() {
    [[ $(cat "$TEST_EVENTS") == mount ]] || return 99
    printf '%s\n' "$2" > "$3"
}
update-grub() {
    echo update-grub >> "$TEST_EVENTS"
    # This models 10_linux's fallback in the absence of a UUID link.
    if [[ -f "$TEST_DISK/by-uuid/$EXPECTED_UUID" ]]; then
        root="UUID=$EXPECTED_UUID"
    else
        root=/dev/nvme0n1p3
    fi
    if [[ $TEST_BAD_CONFIG == yes ]]; then root=/dev/nvme0n1p3; fi
    printf 'linux /boot/vmlinuz root=%s ro\ninitrd /boot/initrd.img\n' "$root" > "$TEST_CFG"
    return "$TEST_UPDATE_STATUS"
}
grub-script-check() { return "$TEST_SYNTAX_STATUS"; }
python3() { "$TEST_PYTHON" "$@"; }
'''
        for bad, update_status, syntax_status, propagation, expected in (
                ('no', 0, 0, 'slave', 0), ('no', 0, 0, 'private', 0),
                ('yes', 0, 0, 'slave', 1), ('no', 7, 0, 'slave', 7),
                ('no', 0, 9, 'slave', 9), ('no', 0, 0, 'shared', 1)):
            with self.subTest(bad=bad, update=update_status, syntax=syntax_status, propagation=propagation), \
                    tempfile.TemporaryDirectory() as folder:
                temp = Path(folder)
                (temp / 'disk').mkdir()
                script = SCRIPT.replace('[[ -b $disk ]]', '[[ $disk == /dev/nvme1n1 ]]')
                script = script.replace('[[ -b $root_device &&', '[[ $root_device == /dev/nvme1n1p3 &&')
                script = script.replace('[[ -b /dev/disk/by-uuid/$root_uuid ]]',
                                        '[[ -f /dev/disk/by-uuid/$root_uuid ]]')
                script = script.replace('/dev/disk', temp.as_posix() + '/disk')
                script = script.replace('/etc/default/grub.d', temp.as_posix() + '/defaults')
                script = script.replace('/boot/grub/grub.cfg', temp.as_posix() + '/grub.cfg')
                env = {**os.environ, 'EXPECTED_UUID': UUID, 'TEST_EVENTS': (temp / 'events').as_posix(),
                       'TEST_DISK': (temp / 'disk').as_posix(), 'TEST_CFG': (temp / 'grub.cfg').as_posix(),
                       'TEST_BAD_CONFIG': bad, 'TEST_UPDATE_STATUS': str(update_status),
                       'TEST_SYNTAX_STATUS': str(syntax_status), 'TEST_PROPAGATION': propagation,
                       'TEST_PYTHON': Path(sys.executable).as_posix()}
                result = subprocess.run([BASH, '-s', '--', '/dev/nvme1n1', UUID], input=stub + script,
                                        env=env, text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, expected, result.stderr)
                if propagation == 'shared':
                    self.assertFalse((temp / 'events').exists())
                else:
                    events = (temp / 'events').read_text().splitlines()
                    self.assertEqual(events, ['mount', 'update-grub', 'umount'])
                    defaults = (temp / 'defaults/99-customizer-uuid.cfg').read_text()
                    self.assertIn('GRUB_DISABLE_LINUX_UUID=false', defaults)
                if bad == 'yes':
                    self.assertIn('GRUB UUID validation failed', result.stderr)


if __name__ == '__main__':
    unittest.main()
