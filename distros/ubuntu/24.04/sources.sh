#!/usr/bin/env bash
# Sourced inside the private target chroot, before common configuration.
set -euo pipefail
[[ ${UBUNTU_CUSTOMIZER_GUEST:-} == 1 && -f /tmp/customizer/build-root ]] || exit 1
[[ $(sed -n 's/^ID=//p' /etc/os-release) == ubuntu && $(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release) == noble ]]
install -d /etc/apt/keyrings /etc/apt/preferences.d /etc/ssh/sshd_config.d
rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources
cat > /etc/apt/sources.list.d/ubuntu.sources <<EOF
Types: deb
URIs: $SYSTEM_MIRROR
Suites: noble noble-updates noble-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: $SECURITY_MIRROR
Suites: noble-security
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
