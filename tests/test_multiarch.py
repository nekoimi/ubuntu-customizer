"""Offline tests for binfmt presets, local-only setup and both proxy layers."""
import copy
import csv
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('multiarch', ROOT / 'scripts/docker_multiarch.py')
multiarch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(multiarch)
BASH = shutil.which('bash') if os.name != 'nt' else shutil.which('bash', path=r'C:\Program Files\Git\bin')


def configured_builder():
    return {'Name': multiarch.NAME, 'Driver': 'docker-container', 'Nodes': [
        {'Name': multiarch.NODE, 'Endpoint': 'unix:///var/run/docker.sock', 'DriverOpts': dict(multiarch.OPTIONS),
         'Flags': list(multiarch.FLAGS)}]}


class Multiarch(unittest.TestCase):
    def test_default_context_and_both_local_socket_spellings_are_accepted(self):
        for endpoint in ('default', 'unix:///var/run/docker.sock', 'unix:///run/docker.sock'):
            item = configured_builder()
            item['Nodes'][0]['Endpoint'] = endpoint
            with self.subTest(endpoint=endpoint):
                multiarch.validate_builder(item)

    def test_remote_endpoints_and_other_local_sockets_are_not_accepted(self):
        for endpoint in ('ssh://remote', 'tcp://127.0.0.1:2375', 'remote-context',
                         'unix:///run/user/1000/docker.sock', 'unix:///other/docker.sock', None):
            item = configured_builder()
            item['Nodes'][0]['Endpoint'] = endpoint
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(RuntimeError, 'local Docker endpoint'):
                multiarch.validate_builder(item)

    def test_mismatch_diagnostic_names_fields_without_exposing_credentials(self):
        item = configured_builder()
        item['Nodes'][0]['DriverOpts']['env.HTTPS_PROXY'] = 'http://private-user:private-pass@proxy:80'
        item['Nodes'][0]['Endpoint'] = 'ssh://private-host'
        with self.assertRaises(RuntimeError) as error:
            multiarch.validate_builder(item)
        message = str(error.exception)
        self.assertIn('driver option env.HTTPS_PROXY', message)
        self.assertIn('local Docker endpoint', message)
        self.assertNotIn('private', message)

    def test_setup_creates_once_with_csv_escaped_proxy_and_bootstraps_after_validation(self):
        with patch.object(multiarch, 'check_binfmt'), patch.object(multiarch, 'check_proxy'), \
                patch.object(multiarch, 'builder', side_effect=[None, configured_builder()]), \
                patch.object(multiarch, 'docker') as docker, redirect_stdout(io.StringIO()):
            multiarch.setup()
        calls = [call.args for call in docker.call_args_list]
        create = next(args for args in calls if args[:2] == ('buildx', 'create'))
        options = [next(csv.reader([create[index + 1]]))[0]
                   for index, value in enumerate(create) if value == '--driver-opt']
        parsed = dict(option.split('=', 1) for option in options)
        self.assertEqual(parsed, multiarch.OPTIONS)
        self.assertEqual(parsed['env.NO_PROXY'], multiarch.BYPASS)
        self.assertEqual(parsed['network'], 'host')
        self.assertIn(('buildx', 'inspect', multiarch.NAME, '--bootstrap'), calls)
        self.assertEqual(calls[-1], ('buildx', 'use', multiarch.NAME))
        self.assertFalse(any('rm' in args for args in calls))

    def test_repeated_setup_reuses_builder_without_erasing_cache(self):
        with patch.object(multiarch, 'check_binfmt'), patch.object(multiarch, 'check_proxy'), \
                patch.object(multiarch, 'builder', return_value=configured_builder()), \
                patch.object(multiarch, 'docker') as docker, redirect_stdout(io.StringIO()):
            multiarch.setup()
        self.assertFalse(any('create' in call.args or 'rm' in call.args for call in docker.call_args_list))

    def test_existing_mismatched_builder_is_not_bootstrapped_selected_or_replaced(self):
        for change in ('proxy', 'network', 'endpoint', 'flags', 'driver'):
            item = copy.deepcopy(configured_builder())
            if change == 'proxy': item['Nodes'][0]['DriverOpts']['env.HTTPS_PROXY'] = 'http://other:80'
            if change == 'network': item['Nodes'][0]['DriverOpts']['network'] = 'bridge'
            if change == 'endpoint': item['Nodes'][0]['Endpoint'] = 'ssh://remote'
            if change == 'flags': item['Nodes'][0]['Flags'] = []
            if change == 'driver': item['Driver'] = 'remote'
            with self.subTest(change=change), patch.object(multiarch, 'check_binfmt'), \
                    patch.object(multiarch, 'check_proxy'), patch.object(multiarch, 'builder', return_value=item), \
                    patch.object(multiarch, 'docker') as docker:
                with self.assertRaisesRegex(RuntimeError, 'incompatible builder'):
                    multiarch.setup()
                self.assertEqual([call.args[0] for call in docker.call_args_list], ['info'])

    def test_build_proxies_run_commands_and_preserves_requested_outputs_platforms_and_stdin(self):
        arguments = ['--platform', 'linux/amd64,linux/arm64', '-t', 'registry.test/app:latest', '--push', '.']
        with patch.object(multiarch, 'check_binfmt'), patch.object(multiarch, 'check_proxy'), \
                patch.object(multiarch, 'builder', return_value=configured_builder()), \
                patch.object(multiarch, 'docker', return_value=Mock(returncode=0)) as docker:
            self.assertEqual(multiarch.build(arguments), 0)
        args = docker.call_args.args
        self.assertIn('--network=host', args)
        self.assertIn('--allow=network.host', args)
        self.assertEqual(args[-len(arguments):], tuple(arguments))
        for key, value in multiarch.PROXIES.items():
            self.assertIn(key + '=' + value, args)
        self.assertNotIn('--load', args)

    def test_managed_build_options_cannot_silently_override_proxy_or_builder(self):
        for arguments in (['--builder=remote', '.'], ['--network', 'none', '.'],
                          ['--allow=security.insecure', '.'], ['--build-arg=HTTPS_PROXY=http://other', '.'],
                          ['--build-arg', 'no_proxy=*', '.']):
            with self.subTest(arguments=arguments), patch.object(multiarch, 'docker') as docker:
                with self.assertRaises(ValueError): multiarch.build(arguments)
                docker.assert_not_called()

    def test_missing_emulator_or_proxy_stops_before_any_docker_calls(self):
        for failed in ('check_binfmt', 'check_proxy'):
            with self.subTest(failed=failed), patch.object(multiarch, 'check_binfmt') as emulation, \
                    patch.object(multiarch, 'check_proxy') as proxy, patch.object(multiarch, 'docker') as docker:
                (emulation if failed == 'check_binfmt' else proxy).side_effect = RuntimeError('not ready')
                with self.assertRaises(RuntimeError): multiarch.setup()
                docker.assert_not_called()

    def test_binfmt_requires_enabled_fix_binary_registration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'registration'
            with patch.object(multiarch, 'REGISTRATION', path):
                for content, valid in (('', False), ('disabled\nflags: F\n', False),
                                       ('enabled\nflags: OC\n', False), ('enabled\nflags: F\n', True)):
                    path.write_text(content)
                    if valid: multiarch.check_binfmt()
                    else:
                        with self.assertRaises(RuntimeError): multiarch.check_binfmt()

    def test_docker_commands_replace_inherited_proxies_for_client_auth_without_writing_client_config(self):
        with patch.dict(os.environ, {'DOCKER_HOST': 'tcp://remote:2375', 'DOCKER_CONTEXT': 'remote',
                                     'DOCKER_CONFIG': '/user/config', 'BUILDX_BUILDER': 'other',
                                     'BUILDKIT_HOST': 'tcp://other', 'HTTPS_PROXY': 'http://other',
                                     'NO_PROXY': '*', 'ALL_PROXY': 'socks5://other', 'HOME': '/user'}, clear=True), \
                patch.object(multiarch.subprocess, 'run') as run:
            multiarch.docker('buildx', 'ls', capture=True)
        self.assertEqual(run.call_args.args[0][:5], ['docker', '--config', '/root/.docker', '--context', 'default'])
        env = run.call_args.kwargs['env']
        self.assertEqual(env['HOME'], '/root')
        self.assertEqual(env['BUILDX_CONFIG'], '/root/.docker/buildx')
        for key in ('DOCKER_CONTEXT', 'DOCKER_HOST', 'BUILDX_BUILDER', 'BUILDKIT_HOST', 'ALL_PROXY'):
            self.assertNotIn(key, env)
        for key, value in multiarch.PROXIES.items():
            self.assertEqual(env[key], value)
        self.assertNotIn('stdin', run.call_args.kwargs)

    def test_client_subprocess_token_request_uses_proxy_even_with_inherited_bypass_all(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"token":"test-token"}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        proxy = 'http://127.0.0.1:' + str(server.server_port)
        proxies = {key: (multiarch.BYPASS if key.lower() == 'no_proxy' else proxy)
                   for key in multiarch.PROXIES}
        url = 'http://auth.docker.invalid/token?scope=repository%3Alibrary%2Falpine%3Apull'
        code = 'import urllib.request; print(urllib.request.urlopen(' + repr(url) + ', timeout=3).read().decode())'
        try:
            # A real child process makes an auth-like HTTP request to an invalid
            # external name through our local fake proxy. No Docker or Internet.
            with patch.object(multiarch, 'DOCKER', [sys.executable, '-c', code]), \
                    patch.object(multiarch, 'PROXIES', proxies), \
                    patch.dict(os.environ, {'NO_PROXY': '*', 'HTTPS_PROXY': 'http://other'}, clear=False):
                result = multiarch.docker(capture=True)
            self.assertEqual(json.loads(result.stdout), {'token': 'test-token'})
            self.assertEqual(requests, [url])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_ls_json_selects_only_our_builder(self):
        output = '\n'.join(json.dumps(item) for item in ({'Name': 'default'}, configured_builder()))
        with patch.object(multiarch, 'docker', return_value=Mock(stdout=output)):
            self.assertEqual(multiarch.builder(), configured_builder())

    def test_help_is_offline_and_build_failure_preserves_exit_code(self):
        with patch.object(multiarch.sys, 'argv', ['docker-multiarch']), \
                patch.object(multiarch, 'docker') as docker, redirect_stdout(io.StringIO()):
            self.assertEqual(multiarch.main(['setup', '--help']), 0)
            self.assertEqual(multiarch.main(['build', '--help']), 0)
            docker.assert_not_called()
        with patch.object(multiarch.sys, 'argv', ['docker-multiarch']), \
                patch.object(multiarch.sys, 'platform', 'linux'), \
                patch.object(multiarch.os, 'geteuid', return_value=0, create=True), \
                patch.object(multiarch, 'build', side_effect=subprocess.CalledProcessError(7, ['docker'])), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(multiarch.main(['build', '.']), 7)

    @unittest.skipUnless(BASH, 'Bash required')
    def test_emulator_postinst_is_deferred_until_target_boot_and_diversion_is_restored(self):
        script = (ROOT / 'components/server/customize.sh').read_text()
        guard = script.split('dpkg-divert --local --add', 1)[1].split("echo 'iperf3", 1)[0]
        guard = 'dpkg-divert --local --add' + guard
        restore = script.split('rm -f /usr/sbin/update-binfmts\n', 1)[1].split("\necho 'Target guest", 1)[0]
        for status in (0, 9):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'update-binfmts'
                edited = guard.replace('/usr/sbin/update-binfmts', '"$STUB"')
                # The divert command receives real paths only as inert shell-function arguments.
                stub = 'set -euo pipefail\ndpkg-divert() { return "$DIVERT_STATUS"; }\n'
                result = subprocess.run([BASH, '-s'], input=stub + edited + '\necho PACKAGES_ALLOWED\n',
                                        env={**os.environ, 'STUB': path.as_posix(), 'DIVERT_STATUS': str(status)},
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual('PACKAGES_ALLOWED' in result.stdout, status == 0)
                if status == 0:
                    self.assertEqual(path.read_text(), '#!/bin/sh\nexit 0\n')
                    result = subprocess.run([BASH, '-s'], input='set -euo pipefail\n'
                                            'dpkg-divert() { printf "%s\\n" "$*"; }\n' + restore,
                                            text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('--remove --rename', result.stdout)

    @unittest.skipUnless(BASH, 'Bash required')
    def test_generated_binfmt_rule_matches_arm64_elf_and_guest_validation(self):
        script = (ROOT / 'components/server/customize.sh').read_text()
        command = next(line for line in script.splitlines() if line.startswith("printf ':customizer-aarch64:"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'preset'
            command = command.replace('/etc/binfmt.d/customizer-aarch64.conf', '"$PRESET"')
            result = subprocess.run([BASH, '-s'], input='qemu=/usr/bin/qemu-aarch64-static\n' + command,
                                    env={**os.environ, 'PRESET': path.as_posix()}, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            fields = path.read_text().strip().split(':')
            self.assertEqual(len(fields), 8)
            self.assertEqual(fields[7], 'F')
            magic = bytes.fromhex(fields[4].replace('\\x', '').replace('ELF', '454c46'))
            mask = bytes.fromhex(fields[5].replace('\\x', ''))
            self.assertEqual(len(magic), len(mask))
            for elf_type in (2, 3):  # ET_EXEC and ET_DYN (PIE)
                header = bytearray(magic)
                header[16] = elf_type
                self.assertEqual(bytes(a & b for a, b in zip(header, mask)),
                                 bytes(a & b for a, b in zip(magic, mask)))
            self.assertEqual(magic[18:20], b'\xb7\x00')


if __name__ == '__main__':
    unittest.main()
