"""Load public host settings and target profiles separately from private .env."""
import json
from pathlib import Path
import re
from image_builder.distros import REPO, target_for

COMPONENTS = ['docker', 'tailscale', 'v2raya', 'development', 'maintenance']


def load_config(host_path, profile_path=None):
    host = json.loads(Path(host_path).read_text())
    profile = json.loads(Path(profile_path or REPO / 'profiles/ubuntu-server.json').read_text())
    if not isinstance(host, dict) or not isinstance(profile, dict):
        raise ValueError('Host config and profile must be JSON objects')
    allowed = {'distro', 'version', 'architecture', 'account', 'components', 'mirrors', 'disk'}
    if set(profile) - allowed:
        raise ValueError('Unknown profile field')
    target = target_for(profile.get('distro'), profile.get('version'))
    if profile.get('architecture') != 'amd64':
        raise ValueError('Only amd64 profiles are supported')
    if profile.get('components') != COMPONENTS:
        raise ValueError('First version supports the complete server component bundle only')
    disk = profile.get('disk', {})
    if not isinstance(disk, dict) or set(disk) - {'template_gib', 'root_filesystem'} or disk.get('root_filesystem') != 'ext4':
        raise ValueError('Only the ext4 GPT layout is supported')
    account = profile.get('account', {})
    if not isinstance(account, dict):
        raise ValueError('account must be an object')
    username = account.get('username', target.username)
    if set(account) - {'username'} or not isinstance(username, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', username) or username in ('root', 'daemon', 'nobody'):
        raise ValueError('Invalid target username')
    defaults = json.loads((target.directory / 'defaults.json').read_text())
    mirrors = profile.get('mirrors', {})
    if not isinstance(mirrors, dict) or set(mirrors) - {'system_mirror', 'security_mirror', 'docker_mirror', 'python_index'}:
        raise ValueError('Unknown target mirror')
    config = {**defaults, **host, **mirrors, 'distro': target.name, 'version': target.version,
              'suite': target.suite, 'username': username, 'components': COMPONENTS,
              'template_gib': disk.get('template_gib', host.get('template_gib', 24))}
    # Old --config config/build.json remains supported for the Ubuntu target.
    if 'ubuntu_suite' in host:
        if target.name != 'ubuntu' or host['ubuntu_suite'] != 'noble':
            raise ValueError('Legacy Ubuntu config cannot be used for another target; use config/host.json')
        config['system_mirror'] = host['ubuntu_mirror']
        config['security_mirror'] = host['ubuntu_mirror']
        config['template_gib'] = host.get('template_gib', 24)
        config['legacy_packages'] = True
    for key, lower, upper in (('template_gib', 16, 128), ('compression_threads', 1, 16), ('minimum_build_free_gib', 40, 1024)):
        if type(config.get(key)) is not int or not lower <= config[key] <= upper:
            raise ValueError('Invalid resource setting: ' + key)
    for key in ('system_mirror', 'security_mirror', 'docker_mirror', 'python_index', 'alpine_mirror'):
        value = config.get(key)
        if not isinstance(value, str) or not value.startswith('https://') or any(c.isspace() for c in value):
            raise ValueError('HTTPS URL required: ' + key)
    return config, target
