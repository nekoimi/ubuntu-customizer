#!/usr/bin/env bash
set -euo pipefail
[[ $(sed -n 's/^ID=//p' /etc/os-release) == debian ]]
grep -qx 'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' /etc/apt/sources.list.d/debian.sources
grep -qx 'Suites: trixie-security' /etc/apt/sources.list.d/debian.sources
[[ $(systemctl is-enabled networking.service) == masked ]]
