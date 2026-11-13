#!/usr/bin/env python3
"""Local Docker multi-platform builder with the installed v2rayA proxy."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

NAME = 'customizer-multiarch'
NODE = NAME + '0'
PROXY = 'http://127.0.0.1:20171'
BYPASS = 'localhost,127.0.0.0/8,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.localhost,.local'
PROXIES = {'http_proxy': PROXY, 'https_proxy': PROXY, 'no_proxy': BYPASS,
           'HTTP_PROXY': PROXY, 'HTTPS_PROXY': PROXY, 'NO_PROXY': BYPASS}
OPTIONS = {'network': 'host', 'restart-policy': 'unless-stopped',
           'env.CUSTOMIZER_MULTIARCH': '1', **{'env.' + key: value for key, value in PROXIES.items()}}
FLAGS = ['--allow-insecure-entitlement=network.host']
DOCKER = ['docker', '--config', '/root/.docker', '--context', 'default']
REGISTRATION = Path('/proc/sys/fs/binfmt_misc/customizer-aarch64')


def environment():
    # Always use the installed local, rootful daemon and root's registry credentials.
    # Inherited remote contexts/builders and shell proxies must not redirect setup.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('DOCKER_', 'BUILDX_', 'BUILDKIT_'))
           and key.lower() not in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
    env.update(HOME='/root', DOCKER_CONFIG='/root/.docker', BUILDX_CONFIG='/root/.docker/buildx')
    return env


def docker(*args, capture=False):
    return subprocess.run(DOCKER + list(args), env=environment(), check=True,
                          text=True, stdout=subprocess.PIPE if capture else None)


def check_binfmt():
    text = REGISTRATION.read_text() if REGISTRATION.exists() else ''
    if not text.startswith('enabled\n') or not re.search(r'^flags:.*F', text, re.M):
        raise RuntimeError('ARM64 binfmt registration is missing or lacks flag F. '
                           'Run sudo systemctl restart systemd-binfmt, then retry.')


def check_proxy():
    try:
        with socket.create_connection(('127.0.0.1', 20171), timeout=3):
            pass
    except OSError as error:
        raise RuntimeError('Local proxy is not ready. Open http://<server-ip>:2017, '
                           'connect a v2rayA node, then run proxy-check.') from error


def builder():
    # buildx inspect has no JSON format option; ls emits one JSON object per builder.
    result = docker('buildx', 'ls', '--format', '{{json .}}', capture=True)
    for line in result.stdout.splitlines():
        item = json.loads(line)
        if item.get('Name') == NAME:
            return item
    return None


def validate_builder(item):
    nodes = item.get('Nodes') or []
    if (item.get('Driver') != 'docker-container' or len(nodes) != 1
            or nodes[0].get('Name') != NODE or nodes[0].get('Endpoint') != 'default'
            or any(nodes[0].get('DriverOpts', {}).get(key) != value for key, value in OPTIONS.items())
            or nodes[0].get('Flags') != FLAGS):
        raise RuntimeError('An incompatible builder named ' + NAME + ' already exists. '
                           'Inspect it with sudo docker buildx inspect ' + NAME +
                           '; it has not been replaced or removed.')


def csv_option(key, value):
    # Buildx parses each --driver-opt as CSV, including commas in NO_PROXY.
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator='\n').writerow([key + '=' + value])
    return buffer.getvalue().rstrip('\n')


def setup():
    check_binfmt()
    check_proxy()
    docker('info', '--format', '{{.ServerVersion}}')
    item = builder()
    if item is None:
        args = ['buildx', 'create', '--name', NAME, '--node', NODE, '--driver', 'docker-container',
                '--buildkitd-flags', ' '.join(FLAGS)]
        for key, value in OPTIONS.items():
            args += ['--driver-opt', csv_option(key, value)]
        docker(*args, 'default')
        item = builder()
    if item is None:
        raise RuntimeError('Builder creation did not produce the expected local builder.')
    validate_builder(item)
    # Pull BuildKit only now, after a node is connected; dockerd's proxy handles this pull.
    docker('buildx', 'inspect', NAME, '--bootstrap')
    docker('buildx', 'use', NAME)
    print('Ready: ' + NAME + ' (root/local Docker; BuildKit proxy ' + PROXY + ').')
    print('Use sudo docker-multiarch-build --platform linux/amd64,linux/arm64 -t <image> --push .')


def build(arguments):
    # Prevent overriding the builder/network/proxies after the preset options.
    for index, argument in enumerate(arguments):
        key = argument.split('=', 1)[0]
        if key in ('--builder', '--network', '--allow'):
            raise ValueError(key + ' is managed by this helper; use docker buildx build for custom settings.')
        if key == '--build-arg':
            value = argument.partition('=')[2] if '=' in argument else (
                arguments[index + 1] if index + 1 < len(arguments) else '')
            if value.split('=', 1)[0].lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
                raise ValueError('Proxy build arguments are managed by this helper.')
    check_binfmt()
    check_proxy()
    item = builder()
    if item is None:
        raise RuntimeError('Run sudo docker-multiarch-setup first.')
    validate_builder(item)
    args = ['buildx', 'build', '--builder', NAME, '--network=host', '--allow=network.host']
    for key, value in PROXIES.items():
        args += ['--build-arg', key + '=' + value]
    # Do not force platforms, push, load, tags or outputs; caller chooses them.
    return docker(*args, *arguments).returncode


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    alias = Path(sys.argv[0]).name
    if alias in ('docker-multiarch-setup', 'docker-multiarch-build'):
        arguments.insert(0, 'setup' if alias.endswith('-setup') else 'build')
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        'sudo docker-multiarch setup | sudo docker-multiarch build [buildx build options]. '
        'Build uses host networking so RUN commands can reach the local proxy; use trusted Dockerfiles.'))
    parser.add_argument('action', choices=('setup', 'build'))
    parser.add_argument('options', nargs=argparse.REMAINDER)
    if arguments == ['setup', '--help'] or arguments == ['setup', '-h']:
        print('Usage: sudo docker-multiarch-setup\n'
              'Create/reuse a local BuildKit builder with v2rayA proxy. Connect a node first.\n'
              'Requires persistent ARM64 binfmt registration; does not register emulators or erase builders.')
        return 0
    args = parser.parse_args(arguments)
    if args.action == 'setup' and args.options:
        parser.error('setup takes no additional arguments')
    if args.action == 'build' and (not args.options or args.options == ['--help']):
        print('Usage: sudo docker-multiarch-build [docker buildx build options] <context>\n'
              'Adds the local proxy, builder and host network; default Docker behavior is unchanged.')
        return 0
    try:
        if sys.platform != 'linux' or os.geteuid() != 0:
            raise RuntimeError('Run this command with sudo on the installed Linux system.')
        if args.action == 'setup':
            setup()
            return 0
        return build(args.options)
    except subprocess.CalledProcessError as error:
        print('Docker command failed; exit ' + str(error.returncode) + '. See tool output above.', file=sys.stderr)
        return error.returncode
    except (OSError, ValueError, RuntimeError) as error:
        print('Multi-platform setup/build failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
