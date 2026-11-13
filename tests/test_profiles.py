"""Target selection, signed bootstrap and installation metadata contracts."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from image_builder.config import load_config
from image_builder.distros import target_for
from image_builder.pipeline import Build

spec = importlib.util.spec_from_file_location('profile_installer', ROOT / 'installer/installer.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class Profiles(unittest.TestCase):
    def test_packaging_produces_target_iso_and_valid_shared_manifest(self):
        from contextlib import nullcontext
        from image_builder import packaging
        for distro in ('ubuntu', 'debian'):
            with self.subTest(distro=distro), tempfile.TemporaryDirectory() as folder:
                build = Build.__new__(Build)
                build.config, build.target = load_config(ROOT / 'config/host.json', ROOT / f'profiles/{distro}-server.json')
                build.work = Path(folder) / 'work'
                build.release = Path(folder) / 'release'
                build.work.mkdir()
                build.release.mkdir()
                build.id = 'package-test'
                build.details = {'distro': distro, 'version': build.target.version,
                                 'username': distro, 'architecture': 'amd64', 'build_id': build.id}
                build.checks = {}
                build.note = Mock()
                build.progress = Mock(side_effect=lambda *args: nullcontext())
                build.alpine = Mock(side_effect=lambda iso: (iso / 'boot').mkdir())
                raw = build.work / 'system.raw'
                raw.write_bytes(b'test-raw'.ljust(1024, b'\0'))
                def command(argv, **kwargs):
                    if argv[0] == 'zstd' and '-o' in argv:
                        argv[-1].write_bytes(b'test-compressed')
                    elif argv[0] == 'grub-mkrescue':
                        argv[2].write_bytes(b'test-iso')
                build.run = Mock(side_effect=command)
                with patch.object(packaging.shutil, 'which', return_value=None), \
                        patch.object(packaging.sp, 'check_output', return_value='BIOS UEFI'), \
                        patch.object(packaging.os, 'sync', create=True):
                    build.package(raw)
                self.assertTrue((build.release / build.target.iso_name).is_file())
                manifest = json.loads((build.release / 'manifest.json').read_text())
                installer.validate_manifest(manifest)
                self.assertEqual(manifest['distro'], distro)
                self.assertEqual(manifest['payload'], 'system.img.zst')
                self.assertTrue((build.work / 'iso/payload/system.img.zst').is_file())
                self.assertNotIn('config/local.env', manifest['input_sha256'])
                self.assertIn(f'distros/{distro}/{build.target.version}/sources.sh',
                              {name.replace('\\', '/') for name in manifest['input_sha256']})

    def test_each_profile_selects_own_signed_archive_and_account(self):
        for distro, version, suite in (('ubuntu', '24.04', 'noble'), ('debian', '13', 'trixie')):
            with self.subTest(distro=distro):
                config, target = load_config(ROOT / 'config/host.json', ROOT / f'profiles/{distro}-server.json')
                self.assertEqual((target.name, target.version, target.suite), (distro, version, suite))
                self.assertEqual(config['username'], distro)
                self.assertIn('/' + distro, config['system_mirror'])
                self.assertTrue(config['docker_mirror'].endswith('/' + distro))
                build = Mock(config=config)
                target.bootstrap(build, Path('/private/target'), Path('/private/cache'))
                args = build.run.call_args.args[0]
                self.assertIn('--keyring=' + target.keyring, args)
                self.assertIn('--cache-dir=' + str(Path('/private/cache')), args)
                self.assertIn(suite, args)
                self.assertNotIn('--no-check-gpg', args)
                text = target.guest_script('customize')
                self.assertIn(f'ID=//p\' /etc/os-release) == {distro}', text)
                archive_key = f'/usr/share/keyrings/{distro}-archive-keyring.gpg'
                self.assertIn('Signed-By: ' + archive_key, text)

    def test_legacy_ubuntu_config_keeps_existing_settings_and_rejects_debian(self):
        config, target = load_config(ROOT / 'config/build.json')
        original = json.loads((ROOT / 'config/build.json').read_text())
        self.assertEqual(config['system_mirror'], original['ubuntu_mirror'])
        self.assertEqual(config['template_gib'], original['template_gib'])
        self.assertTrue(config['legacy_packages'])
        with self.assertRaisesRegex(ValueError, 'Legacy Ubuntu config'):
            load_config(ROOT / 'config/build.json', ROOT / 'profiles/debian-server.json')

    def test_unsupported_or_malformed_profiles_fail_before_any_commands(self):
        original = json.loads((ROOT / 'profiles/debian-server.json').read_text())
        changes = [{'distro': 'centos'}, {'version': '12'}, {'architecture': 'arm64'},
                   {'components': ['docker']}, {'account': []}, {'account': {'username': 'root'}},
                   {'account': {'username': 'user; command'}}, {'disk': []},
                   {'disk': {'root_filesystem': 'xfs'}}, {'disk': {'root_filesystem': 'ext4', 'template_gib': True}},
                   {'mirrors': []}, {'mirrors': {'system_mirror': 'http://insecure.test'}},
                   {'script': '/arbitrary/script'}]
        with tempfile.TemporaryDirectory() as folder, patch('subprocess.run') as run:
            path = Path(folder) / 'profile.json'
            for change in changes:
                with self.subTest(change=change):
                    path.write_text(json.dumps({**copy.deepcopy(original), **change}))
                    with self.assertRaises(ValueError):
                        load_config(ROOT / 'config/host.json', path)
            run.assert_not_called()

    def test_cache_namespaces_do_not_mix_targets(self):
        with tempfile.TemporaryDirectory() as folder:
            build = Build.__new__(Build)
            build.config = {'system_mirror': 'https://mirror.test/ubuntu'}
            build.cache_base = Path(folder)
            build.note = Mock()
            build.target = target_for('ubuntu', '24.04')
            ubuntu = build.package_cache()
            build.target = target_for('debian', '13')
            debian = build.package_cache()
            self.assertNotEqual(ubuntu, debian)
            self.assertIn('noble', ubuntu.parts)
            self.assertIn('trixie', debian.parts)
            build.config['system_mirror'] = 'https://other.test/debian'
            self.assertNotEqual(build.package_cache(), debian)

    def test_missing_boot_adapter_refuses_before_disk_discovery(self):
        with patch.object(installer.os, 'geteuid', return_value=0, create=True), \
                patch.object(installer.shutil, 'which', return_value='/usr/bin/tool'), \
                patch.object(installer.Path, 'is_file', side_effect=[True, False]), \
                patch.object(installer, 'discover') as discover, \
                patch.object(installer.os, 'open') as opened:
            with self.assertRaisesRegex(installer.Refusal, 'Missing target boot adapter'):
                installer.main()
            discover.assert_not_called()
            opened.assert_not_called()

    def test_debian_packages_are_explicit_and_do_not_include_ubuntu_metapackages(self):
        common = (ROOT / 'components/server/packages.txt').read_text()
        debian = (target_for('debian', '13').directory / 'packages.txt').read_text()
        self.assertIn('fdisk\n', common)
        self.assertIn('linux-image-amd64', debian)
        for package in ('ubuntu-minimal', 'ubuntu-standard', 'linux-generic', 'linux-firmware'):
            self.assertNotIn(package + '\n', common + debian)

    def test_initial_password_alias_keeps_environment_precedence(self):
        from build_env import password_input
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'local.env'
            path.write_text("INITIAL_PASSWORD='local-new'\nUBUNTU_INITIAL_PASSWORD='local-old'\n")
            self.assertEqual(password_input({}, path), 'local-new')
            self.assertEqual(password_input({'UBUNTU_INITIAL_PASSWORD': 'env-old'}, path), 'env-old')
            self.assertEqual(password_input({'INITIAL_PASSWORD': 'env-new', 'UBUNTU_INITIAL_PASSWORD': 'env-old'}, path), 'env-new')

    def test_bundled_debian_bootstrap_keyring_matches_reviewed_input(self):
        keyring = ROOT / 'distros/debian/13/archive.gpg'
        self.assertEqual(hashlib.sha256(keyring.read_bytes()).hexdigest(),
                         'dbc72305245c79a3087dd1cffb35e9fdf56ee0df7cc0a8637a5662a9110305db')

    def test_target_pipeline_stages_correct_scripts_packages_and_password_account(self):
        from image_builder import pipeline
        from contextlib import nullcontext
        for distro in ('ubuntu', 'debian'):
            with self.subTest(distro=distro), tempfile.TemporaryDirectory() as folder:
                build = Build.__new__(Build)
                build.config, build.target = load_config(ROOT / 'config/host.json', ROOT / f'profiles/{distro}-server.json')
                build.work = Path(folder) / 'work'
                build.release = Path(folder) / 'release'
                build.work.mkdir()
                build.release.mkdir()
                build.id = 'test'
                build.password_hash = 'test-hash'
                build.details = {'v2raya': {}}
                build.checks = {}
                build.note = Mock()
                build.run = Mock()
                build.output = Mock(side_effect=lambda argv: '/dev/loop99' if argv[0] == 'losetup' else 'uuid')
                build.mount = Mock(side_effect=lambda args, target: target.mkdir(parents=True, exist_ok=True))
                build.package_cache = Mock(return_value=None)
                def bootstrap(root, cache):
                    (root / 'etc').mkdir()
                    (root / 'tmp').mkdir()
                    state = root / 'var/lib/ubuntu-customizer'
                    state.mkdir(parents=True)
                    for name in ('packages.txt', 'package-origins.txt'):
                        (state / name).write_text('test')
                build.bootstrap_system = Mock(side_effect=bootstrap)
                build.api_mounts = Mock()
                build.mounts = []
                build.install_ntfy_config = Mock()
                build.progress = Mock(side_effect=lambda *args: nullcontext())
                build.uv = Mock()
                build.v2raya = Mock()
                build.guest = Mock()
                build.clean_system = Mock()
                build.unmount_to = Mock()
                # Bound to 1 MiB for this regular-file test; no mounts/chroots.
                with patch.object(pipeline, 'GIB', 1024 ** 2), patch.object(pipeline, 'wait_loop_partitions'):
                    raw = build.system_image()
                self.assertTrue(raw.is_file())
                staged = build.work / 'target/tmp/customizer'
                self.assertTrue((staged / 'docker_multiarch.py').is_file())
                packages = (staged / 'packages.txt').read_text()
                self.assertIn('linux-generic' if distro == 'ubuntu' else 'linux-image-amd64', packages)
                self.assertIn('qemu-user-static\n' if distro == 'ubuntu' else 'qemu-user\n', packages)
                self.assertIn('fdisk\n', packages)
                customize = build.guest.call_args_list[0]
                self.assertEqual(customize.args[2]['TARGET_USER'], distro)
                self.assertEqual(customize.args[2]['TARGET_DISTRO'], distro)
                password = next(call for call in build.run.call_args_list if call.args[0][-2:] == ['chpasswd', '--encrypted'])
                self.assertEqual(password.kwargs['input'], (distro + ':test-hash\n').encode())
                boot = next(call for call in build.guest.call_args_list
                            if call.kwargs.get('label') == 'Generating initramfs and installing BIOS / UEFI bootloaders')
                self.assertTrue(boot.args[1].startswith('set -- /dev/loop99 uuid\n'))
                self.assertEqual(build.checks['grub_root_uuid'], 'passed')
                self.assertEqual(build.checks['target_packages_and_config'], 'passed')


class InstallerProfiles(unittest.TestCase):
    def manifest(self):
        return {'format': 2, 'architecture': 'amd64', 'distro': 'debian', 'version': '13',
                'username': 'debian', 'payload': 'system.img.zst', 'root_filesystem': 'ext4',
                'finalizer': 'debian-family-v1', 'sector_size': 512, 'root_partition': 3, 'esp_partition': 2,
                'raw_bytes': 1024, 'raw_sha256': '0' * 64, 'compressed_sha256': '0' * 64, 'build_id': 'test'}

    def test_new_manifest_rejects_unknown_systems_paths_and_finalizers(self):
        valid = self.manifest()
        installer.validate_manifest(valid)
        for key, value in (('distro', 'centos'), ('version', 'unknown'), ('username', 'root'),
                           ('username', 'user\ncommand'), ('payload', '../other.img'),
                           ('finalizer', '/arbitrary/script'), ('root_filesystem', 'xfs'), ('format', True)):
            with self.subTest(key=key), self.assertRaises(installer.Refusal):
                installer.validate_manifest({**valid, key: value})

    def test_supported_targets_keep_layout_checks_and_old_manifest_compatibility(self):
        valid = self.manifest()
        installer.validate_manifest({**valid, 'distro': 'ubuntu', 'version': '24.04', 'username': 'ubuntu'})
        installer.validate_manifest({**valid, 'format': 1})
        with self.assertRaises(installer.Refusal):
            installer.validate_manifest({**valid, 'sector_size': 4096})


if __name__ == '__main__':
    unittest.main()
