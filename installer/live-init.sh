#!/bin/sh
# Small, static-busybox initramfs. Copy Alpine root to RAM; payload stays on USB/CD.
export PATH=/bin
rescue() {
    echo "Live boot failed: $*"
    echo 'Recovery shell. No target disk has been written.'
    exec /bin/sh
}
mount -t devtmpfs devtmpfs /dev || rescue devtmpfs
mount -t proc proc /proc || rescue proc
mount -t sysfs sysfs /sys || rescue sysfs
for module in loop isofs squashfs sd_mod sr_mod usb_storage uas xhci_pci ehci_pci ahci ata_piix nvme virtio_pci virtio_blk virtio_scsi megaraid_sas mpt3sas; do
    modprobe "$module" 2>/dev/null || true
done
echo 'Looking for Ubuntu Customizer installation media...'
found=no
attempt=0
while [ "$attempt" -lt 40 ] && [ "$found" = no ]; do
    mdev -s
    for item in /sys/class/block/*; do
        name=${item##*/}
        case "$name" in loop*|ram*|dm-*|md*) continue ;; esac
        device=/dev/$name
        # Busybox blkid has no util-linux -s/-o flags.
        case $(blkid "$device" 2>/dev/null) in
            *'LABEL="UCINSTALL"'*) ;;
            *) continue ;;
        esac
        mount -t iso9660 -o ro "$device" /media/installer 2>/dev/null || continue
        if [ -s /media/installer/UBUNTU_CUSTOMIZER_MEDIA ] && [ -s /media/installer/live.squashfs ]; then
            found=yes
            break
        fi
        umount /media/installer
    done
    [ "$found" = yes ] && break
    sleep 1
    attempt=$((attempt + 1))
done
[ "$found" = yes ] || rescue 'cannot identify direct-written USB or ISO media'
mount -t squashfs -o loop,ro /media/installer/live.squashfs /lower || rescue squashfs
mount -t tmpfs -o mode=0755 tmpfs /newroot || rescue tmpfs
cp -a /lower/. /newroot/ || rescue 'copy Alpine root to RAM (check available memory)'
umount /lower || rescue 'unmount squashfs'
mkdir -p /newroot/dev /newroot/proc /newroot/sys /newroot/run /newroot/tmp /newroot/media/installer
chmod 1777 /newroot/tmp
mount --move /media/installer /newroot/media/installer || rescue 'move installation media'
mount --move /dev /newroot/dev || rescue 'move devtmpfs'
mount --move /proc /newroot/proc || rescue 'move proc'
mount --move /sys /newroot/sys || rescue 'move sysfs'
exec switch_root /newroot /sbin/init
