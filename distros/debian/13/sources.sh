#!/usr/bin/env bash
# Debian sources are distinct from Ubuntu's archive and security layout.
set -euo pipefail
[[ ${UBUNTU_CUSTOMIZER_GUEST:-} == 1 && -f /tmp/customizer/build-root ]] || exit 1
[[ $(sed -n 's/^ID=//p' /etc/os-release) == debian && $(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release) == trixie ]]
install -d /etc/apt/keyrings /etc/apt/preferences.d /etc/ssh/sshd_config.d
rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources
cat > /etc/apt/sources.list.d/debian.sources <<EOF
Types: deb
URIs: $SYSTEM_MIRROR
Suites: trixie trixie-updates
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: $SECURITY_MIRROR
Suites: trixie-security
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
