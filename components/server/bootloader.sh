#!/usr/bin/env bash
# Debian-family boot adapter. Only run in a verified image chroot.
set -euo pipefail
disk=${1:?verified disk}
root_uuid=${2:?verified root filesystem UUID}
[[ -b $disk ]] || exit 1
[[ $root_uuid =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]
root_device=$(grub-probe --target=device /)
[[ -b $root_device && $(grub-probe --target=fs_uuid /) == "$root_uuid" ]]
[[ $(blkid -p -s UUID -o value "$root_device") == "$root_uuid" ]]
update-initramfs -u -k all
grub-install --target=i386-pc --recheck "$disk"
grub-install --target=x86_64-efi --efi-directory=/boot/efi \
    --removable --no-nvram --no-uefi-secure-boot --recheck

# Older 10_linux scripts fall back to /dev/nvme... when mdev has no UUID
# links, even if grub-probe returns the UUID. /dev is a slave/private bind
# mount in both callers. Shadow its disk directory for this operation only;
# never create or replace UUID links in the build host's devtmpfs.
[[ -d /dev/disk && ! -L /dev/disk ]]
dev_propagation=$(findmnt --noheadings --output PROPAGATION --target /dev)
[[ $dev_propagation == private || $dev_propagation == slave ]]
mount -t tmpfs -o mode=0755,nosuid,nodev,noexec tmpfs /dev/disk
cleanup_uuid_links() {
    local status=$?
    trap - EXIT
    if ! umount /dev/disk; then
        echo 'Failed to unmount temporary GRUB UUID directory.' >&2
        exit 1
    fi
    exit "$status"
}
trap cleanup_uuid_links EXIT
install -d /dev/disk/by-uuid
ln -s "$root_device" "/dev/disk/by-uuid/$root_uuid"
[[ -b /dev/disk/by-uuid/$root_uuid ]]
install -d /etc/default/grub.d
cat > /etc/default/grub.d/99-customizer-uuid.cfg <<'EOF'
# Root device numbering can change between boots. Use filesystem UUIDs.
GRUB_DISABLE_UUID=false
GRUB_DISABLE_LINUX_UUID=false
GRUB_DISABLE_LINUX_PARTUUID=true
EOF
update-grub
grub-script-check /boot/grub/grub.cfg
python3 - "$root_uuid" /boot/grub/grub.cfg <<'GRUB_UUID_CHECK'
from pathlib import Path
import shlex
import sys

expected = 'root=UUID=' + sys.argv[1]
entries = 0
pending_kernel = None
for number, line in enumerate(Path(sys.argv[2]).read_text().splitlines(), 1):
    # Parse kernel/initrd commands only; other GRUB lines need not be shell syntax.
    command = line.strip().split(None, 1)
    if not command or command[0] not in ('linux', 'linuxefi', 'initrd', 'initrdefi'):
        continue
    tokens = shlex.split(line, comments=True)
    if tokens[0] in ('linux', 'linuxefi'):
        roots = [token for token in tokens[2:] if token.startswith('root=')]
        if roots != [expected]:
            raise SystemExit(f'GRUB UUID validation failed at line {number}: expected {expected}; found {roots}. '
                             'Refusing a device-number-dependent or mismatched root filesystem.')
        if pending_kernel is not None:
            raise SystemExit(f'GRUB kernel at line {pending_kernel} has no initrd.')
        entries += 1
        pending_kernel = number
    elif len(tokens) > 1:
        pending_kernel = None
if not entries or pending_kernel is not None:
    raise SystemExit('GRUB UUID validation failed: no Linux entries, or a kernel has no initrd.')
print(f'GRUB UUID validation passed: {entries} Linux entries use {expected} with initrd.')
GRUB_UUID_CHECK
