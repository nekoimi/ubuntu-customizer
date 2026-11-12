#!/usr/bin/env bash
# Debian-family boot adapter. Only run in a verified image chroot.
set -euo pipefail
disk=${1:?verified disk}
[[ -b $disk ]] || exit 1
update-initramfs -u -k all
grub-install --target=i386-pc --recheck "$disk"
grub-install --target=x86_64-efi --efi-directory=/boot/efi \
    --removable --no-nvram --no-uefi-secure-boot --recheck
update-grub
