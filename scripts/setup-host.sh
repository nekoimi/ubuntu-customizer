#!/usr/bin/env bash
# Installs build tools only; never changes host sources, accounts or disks.
set -euo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run with sudo bash scripts/setup-host.sh'; exit 1; }
[[ $(uname -m) == x86_64 ]] || { echo 'x86_64 build host required'; exit 1; }
apt-get update
apt-get install -y --no-install-recommends \
  debootstrap ubuntu-keyring ca-certificates curl python3 python3-yaml \
  util-linux parted gdisk e2fsprogs zerofree dosfstools zstd squashfs-tools \
  grub-common grub-pc-bin grub-efi-amd64-bin mtools xorriso \
  cpio gzip xz-utils kmod openssl git shellcheck
install -d -m 0755 /srv/ubuntu-customizer/{work,releases}
