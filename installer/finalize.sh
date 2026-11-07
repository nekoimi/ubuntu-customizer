#!/usr/bin/env bash
# Runs in Alpine only, AFTER explicit confirmation, write and readback.
set -euo pipefail
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
trap 'cleanup || true' EXIT
sgdisk --move-second-header --randomize-guids "$disk"
parted --script "$disk" resizepart 3 100%
partprobe "$disk"
partition() {
    lsblk --noheadings --raw --paths --output PATH,PARTN "$disk" | awk -v n="$1" '$2 == n { print $1 }'
}
root=$(partition 3)
esp=$(partition 2)
[[ -b $root && -b $esp ]]
set +e
e2fsck -f -p "$root"
check_status=$?
set -e
((check_status <= 1)) || exit "$check_status"
resize2fs "$root"
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
chroot "$target" update-initramfs -u -k all
chroot "$target" grub-install --target=i386-pc --recheck "$disk"
chroot "$target" grub-install --target=x86_64-efi --efi-directory=/boot/efi \
    --removable --no-nvram --no-uefi-secure-boot --recheck
chroot "$target" update-grub
printf 'build_id=%s\ninstalled_at=%s\n' "$build_id" "$(date -u +%FT%TZ)" > "$target/var/lib/ubuntu-customizer/installation"
sync
cleanup
mounted=()
trap - EXIT
sync
