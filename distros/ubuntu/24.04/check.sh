#!/usr/bin/env bash
set -euo pipefail
[[ $(sed -n 's/^ID=//p' /etc/os-release) == ubuntu ]]
grep -qx 'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' /etc/apt/sources.list.d/ubuntu.sources
