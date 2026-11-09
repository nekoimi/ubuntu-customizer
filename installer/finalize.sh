#!/usr/bin/env bash
# Runs in Alpine only, AFTER explicit confirmation, write and readback.
set -Eeuo pipefail
finalize_stage='Checking finalization prerequisites'
finalize_error_reported=0
report_stage() {
    echo "$finalize_stage"
    if [[ -n ${UBUNTU_CUSTOMIZER_STAGE_FILE:-} ]]; then
        printf '%s\n' "$finalize_stage" > "$UBUNTU_CUSTOMIZER_STAGE_FILE"
    fi
}
record_error() {
    local status=$1 line=$2 command=$3
    finalize_error_reported=1
    printf 'Finalization failed: %s (line %s, exit %s): %s\n' "$finalize_stage" "$line" "$status" "$command" >&2
    if [[ -n ${UBUNTU_CUSTOMIZER_ERROR_FILE:-} ]]; then
        printf 'line %s, exit %s: %s\n' "$line" "$status" "$command" >> "$UBUNTU_CUSTOMIZER_ERROR_FILE" || true
    fi
}
finalize_error() {
    local status=$1 line=$2 command=$3
    record_error "$status" "$line" "$command"
    exit "$status"
}
finalize_exit() {
    local status=$1 line=$2 command=$3
    if ((status != 0 && finalize_error_reported == 0)); then
        record_error "$status" "$line" "$command"
    fi
    if [[ ${cleanup_ready:-0} == 1 ]]; then
        cleanup || true
    fi
    exit "$status"
}
trap 'finalize_error "$?" "$LINENO" "$BASH_COMMAND"' ERR
trap 'finalize_exit "$?" "$LINENO" "$BASH_COMMAND"' EXIT
report_stage
[[ ${UBUNTU_CUSTOMIZER_WRITE_VERIFIED:-} == 1 && -f /etc/alpine-release && -s /media/installer/UBUNTU_CUSTOMIZER_MEDIA ]] || {
    echo 'Finalization is only available after the Live installer verifies its disk write.' >&2
    exit 1
}
disk=${1:?target disk}
build_id=${2:?build id}
[[ -b $disk && $disk =~ ^/dev/[a-zA-Z0-9/_-]+$ ]]
[[ $build_id =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]
target=/mnt/ubuntu-target
mkdir -p "$target"
mounted=()
cleanup() {
    local failed=0 item
    for ((item=${#mounted[@]}-1; item>=0; item--)); do
        umount --recursive "${mounted[item]}" || failed=1
    done
    return "$failed"
}
cleanup_ready=1
finalize_stage='Repairing GPT and randomizing partition GUIDs'
report_stage
sgdisk --move-second-header --randomize-guids "$disk"
finalize_stage='Expanding root partition to target disk size'
report_stage
parted --script "$disk" resizepart 3 100%
finalize_stage='Refreshing kernel partition table'
report_stage
partprobe "$disk"
finalize_stage='Locating root and EFI partition devices'
report_stage
partition() {
    lsblk --noheadings --raw --paths --output PATH,PARTN "$disk" | awk -v n="$1" '$2 == n { print $1 }'
}
root=$(partition 3)
esp=$(partition 2)
if [[ ! -b $root || ! -b $esp ]]; then
    printf 'Missing or invalid partition devices: root=%q EFI=%q\n' "$root" "$esp" >&2
    lsblk --paths --output NAME,TYPE,SIZE,PARTN "$disk" >&2 || true
    exit 1
fi
finalize_stage='Checking and expanding ext4 filesystem'
report_stage
check_status=0
e2fsck -f -p "$root" || check_status=$?
if ((check_status > 1)); then
    printf 'Root filesystem check failed: %s (e2fsck exit %s)\n' "$root" "$check_status" >&2
    exit "$check_status"
fi
resize2fs "$root"
finalize_stage='Generating filesystem identifiers and mounting installed system'
report_stage
tune2fs -U random "$root"
fat_id=$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')
fatlabel --volume-id "$esp" "$fat_id"
root_uuid=$(blkid -s UUID -o value "$root")
esp_uuid=$(blkid -s UUID -o value "$esp")
mount "$root" "$target"
mounted+=("$target")
mount "$esp" "$target/boot/efi"
mounted+=("$target/boot/efi")
for path in dev sys; do
    mount --rbind "/$path" "$target/$path"
    mounted+=("$target/$path")
    mount --make-rslave "$target/$path"
done
mount -t proc proc "$target/proc"
mounted+=("$target/proc")
mount -t tmpfs tmpfs "$target/run"
mounted+=("$target/run")
finalize_stage='Configuring installed system identity and SSH'
report_stage
printf 'UUID=%s / ext4 defaults 0 1\nUUID=%s /boot/efi vfat umask=0077 0 2\n/swapfile none swap sw 0 0\n' "$root_uuid" "$esp_uuid" > "$target/etc/fstab"
machine_id=$(tr -d '-' < /proc/sys/kernel/random/uuid)
printf '%s\n' "$machine_id" > "$target/etc/machine-id"
hostname=ubuntu-${machine_id:0:8}
printf '%s\n' "$hostname" > "$target/etc/hostname"
printf '127.0.0.1 localhost\n127.0.1.1 %s\n::1 localhost ip6-localhost ip6-loopback\n' "$hostname" > "$target/etc/hosts"
install -d -m 0755 "$target/var/lib/systemd"
dd if=/dev/urandom of="$target/var/lib/systemd/random-seed" bs=512 count=1 status=none
chmod 600 "$target/var/lib/systemd/random-seed"
[[ $(chroot "$target" id -u ubuntu) == 1000 && $(chroot "$target" id -g ubuntu) == 1000 ]]
[[ $(chroot "$target" passwd -S root | awk '{print $2}') == L ]]
chroot "$target" ssh-keygen -A
chroot "$target" mkswap /swapfile
mkdir -p "$target/run/sshd"
chroot "$target" /usr/sbin/sshd -t
finalize_stage='Generating initramfs and installing BIOS / UEFI bootloaders'
report_stage
chroot "$target" update-initramfs -u -k all
chroot "$target" grub-install --target=i386-pc --recheck "$disk"
chroot "$target" grub-install --target=x86_64-efi --efi-directory=/boot/efi \
    --removable --no-nvram --no-uefi-secure-boot --recheck
chroot "$target" update-grub
finalize_stage='Checking root and EFI filesystem detection using installed Ubuntu tools'
report_stage
# Alpine and the installed Ubuntu can ship different libblkid versions and
# detect different signatures. Test with Ubuntu's own probe before claiming
# success; a successful ext4 mount alone does not prove UUID discovery works.
chroot "$target" /bin/bash -se -- "$root" "$root_uuid" "$esp" "$esp_uuid" <<'FILESYSTEM_CHECK'
set -euo pipefail
root_probe=$(blkid -p -o export "$1") || {
    echo 'Ubuntu cannot unambiguously identify the root filesystem; possible conflicting signatures. Installation is incomplete.' >&2
    exit 1
}
if ! grep -qx 'TYPE=ext4' <<< "$root_probe" || ! grep -Fxq "UUID=$2" <<< "$root_probe"; then
    echo 'Ubuntu root filesystem type or UUID does not match the installed configuration.' >&2
    exit 1
fi
esp_probe=$(blkid -p -o export "$3") || {
    echo 'Ubuntu cannot unambiguously identify the EFI filesystem. Installation is incomplete.' >&2
    exit 1
}
if ! grep -qx 'TYPE=vfat' <<< "$esp_probe" || ! grep -Fxq "UUID=$4" <<< "$esp_probe"; then
    echo 'Ubuntu EFI filesystem type or UUID does not match the installed configuration.' >&2
    exit 1
fi
FILESYSTEM_CHECK
finalize_stage='Recording installation and unmounting target filesystems'
report_stage
printf 'build_id=%s\ninstalled_at=%s\n' "$build_id" "$(date -u +%FT%TZ)" > "$target/var/lib/ubuntu-customizer/installation"
sync
cleanup
mounted=()
trap - EXIT
sync
