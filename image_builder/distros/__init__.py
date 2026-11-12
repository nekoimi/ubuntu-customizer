"""Supported targets. Never select build scripts from untrusted profile paths."""
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DebianFamily:
    name: str
    version: str
    suite: str
    keyring: str
    bootstrap_script: str
    username: str

    @property
    def directory(self):
        return REPO / 'distros' / self.name / self.version

    @property
    def iso_name(self):
        return f'{self.name}-custom-server.iso'

    def bootstrap(self, build, root, cache):
        options = ['--cache-dir=' + str(cache)] if cache else []
        build.run(['debootstrap', *options, '--arch=amd64', '--merged-usr', '--variant=minbase',
                   '--keyring=' + self.keyring, self.suite, root, build.config['system_mirror'],
                   self.bootstrap_script], label=f'Installing {self.name} base system (debootstrap)')

    def guest_script(self, stage):
        # Both supported families use initramfs-tools and grub-install today.
        # A new family must supply its own adapter instead of extending shell eval.
        common = REPO / 'components/server'
        if stage == 'customize':
            return (self.directory / 'sources.sh').read_text() + '\n' + (common / 'customize.sh').read_text()
        if stage == 'check':
            return (common / 'check.sh').read_text() + '\n' + (self.directory / 'check.sh').read_text()
        if stage == 'bootloader':
            return (common / 'bootloader.sh').read_text()
        raise ValueError('Unknown guest stage')


TARGETS = {
    ('ubuntu', '24.04'): DebianFamily('ubuntu', '24.04', 'noble',
        '/usr/share/keyrings/ubuntu-archive-keyring.gpg', '/usr/share/debootstrap/scripts/gutsy', 'ubuntu'),
    ('debian', '13'): DebianFamily('debian', '13', 'trixie',
        str(REPO / 'distros/debian/13/archive.gpg'), '/usr/share/debootstrap/scripts/sid', 'debian'),
}


def target_for(name, version):
    try:
        return TARGETS[(name, version)]
    except (KeyError, TypeError):
        raise ValueError('Supported targets: ubuntu 24.04, debian 13') from None
