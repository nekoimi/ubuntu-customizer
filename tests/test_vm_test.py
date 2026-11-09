"""VM tests use standalone files, local-only networking and persistent firmware."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('vm_lab', ROOT / 'scripts/vm_test.py')
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class VmLab(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.iso = self.base / 'installer.iso'
        self.iso.write_bytes(b'test ISO')
        self.code = self.base / 'OVMF_CODE_4M.fd'
        self.variables = self.base / 'OVMF_VARS_4M.fd'
        self.code.write_bytes(b'firmware')
        self.variables.write_bytes(b'variables template')
        self.args = SimpleNamespace(state_dir=self.base / 'vm,with comma', iso=self.iso, boot='installer',
                                    firmware=None, disk_gib=80, cpus=4, memory_mib=4096, vnc_display=1,
                                    ssh_port=2222, log_port=18000, internet=False)

    def prepare(self):
        def create(argv, **kwargs):
            self.assertEqual(argv[:4], ['qemu-img', 'create', '-f', 'qcow2'])
            self.assertFalse(Path(argv[4]).exists())
            Path(argv[4]).write_bytes(b'qcow2 placeholder')

        with patch.object(lab, 'firmware_pair', return_value=(self.code, self.variables)), \
                patch.object(lab.sp, 'run', side_effect=create):
            return lab.prepare(self.args)

    def test_new_vm_creates_only_new_standalone_disk_and_private_firmware_variables(self):
        state, settings = self.prepare()
        self.assertEqual(settings['firmware'], 'uefi')
        self.assertEqual(settings['ovmf_code'], str(self.code))
        self.assertEqual((state / 'OVMF_VARS.fd').read_bytes(), self.variables.read_bytes())
        self.assertEqual(self.variables.read_bytes(), b'variables template')
        self.assertEqual(self.iso.read_bytes(), b'test ISO')
        self.assertTrue((state / 'vm.json').is_file())

    def test_installer_uses_nvme_files_and_json_paths_and_loopback_only_endpoints(self):
        state, settings = self.prepare()
        argv = lab.command(self.args, state, settings)
        devices = [argv[i + 1] for i, value in enumerate(argv) if value == '-device']
        self.assertIn('nvme,drive=test-disk,serial=UCVMTEST0001', devices)
        self.assertEqual(argv[argv.index('-vnc') + 1], '127.0.0.1:1')
        network = argv[argv.index('-netdev') + 1]
        self.assertIn('restrict=on', network)
        self.assertIn('hostfwd=tcp:127.0.0.1:2222-:22', network)
        self.assertNotIn('tap', network)
        blocks = [json.loads(argv[i + 1]) for i, value in enumerate(argv) if value == '-blockdev']
        self.assertEqual(blocks[0]['filename'], str(state / 'disk.qcow2'))
        code = next(block for block in blocks if block['node-name'] == 'firmware-code')
        self.assertTrue(code['read-only'])
        self.assertEqual(code['file']['filename'], str(self.code))
        self.assertIn('-cdrom', argv)

    def test_installed_boot_preserves_vm_disk_and_vars_and_detaches_iso(self):
        state, settings = self.prepare()
        (state / 'OVMF_VARS.fd').write_bytes(b'changed by guest')
        self.args.boot = 'installed'
        self.args.iso = None
        with patch.object(lab.sp, 'check_output', return_value=json.dumps({'format': 'qcow2'})), \
                patch.object(lab.sp, 'run') as create:
            lab.prepare(self.args)
        create.assert_not_called()
        self.assertEqual((state / 'OVMF_VARS.fd').read_bytes(), b'changed by guest')
        argv = lab.command(self.args, state, settings)
        self.assertNotIn('-cdrom', argv)
        self.assertEqual(argv[argv.index('-boot') + 1], 'order=c,menu=on')

    def test_existing_unknown_directory_and_non_file_iso_are_rejected(self):
        self.args.state_dir.mkdir()
        (self.args.state_dir / 'keep.txt').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'must be empty'):
            lab.prepare(self.args)
        self.args.iso = self.base
        with self.assertRaisesRegex(ValueError, 'regular file'):
            lab.prepare(self.args)
        self.assertEqual((self.args.state_dir / 'keep.txt').read_text(), 'keep')

    def test_existing_disk_with_backing_file_or_external_data_is_refused(self):
        self.prepare()
        for info in ({'format': 'raw'}, {'format': 'qcow2', 'backing-filename': '/dev/sda'},
                     {'format': 'qcow2', 'format-specific': {'data': {'data-file': '/dev/sda'}}}):
            with self.subTest(info=info), patch.object(lab.sp, 'check_output', return_value=json.dumps(info)), \
                    self.assertRaisesRegex(ValueError, 'standalone qcow2'):
                lab.prepare(self.args)

    def test_bios_does_not_need_ovmf_and_firmware_mode_cannot_change_existing_vm(self):
        self.args.firmware = 'bios'
        with patch.object(lab, 'firmware_pair', side_effect=AssertionError('should not use OVMF')):
            state, settings = self.prepare()
            argv = lab.command(self.args, state, settings)
        self.assertNotIn('pflash', ' '.join(argv))
        self.args.firmware = 'uefi'
        with self.assertRaisesRegex(ValueError, 'Firmware differs'):
            lab.prepare(self.args)

    def test_ovmf_template_and_code_have_matching_sizes_family(self):
        firmware = self.base / 'ovmf'
        firmware.mkdir()
        (firmware / 'OVMF_CODE_4M.fd').write_bytes(b'4M')
        (firmware / 'OVMF_VARS.fd').write_bytes(b'2M')
        with self.assertRaisesRegex(RuntimeError, 'Matching OVMF'):
            lab.firmware_pair(firmware)
        (firmware / 'OVMF_VARS_4M.fd').write_bytes(b'4M vars')
        code, variables = lab.firmware_pair(firmware)
        self.assertEqual(code.name, 'OVMF_CODE_4M.fd')
        self.assertEqual(variables.name, 'OVMF_VARS_4M.fd')


if __name__ == '__main__':
    unittest.main()
