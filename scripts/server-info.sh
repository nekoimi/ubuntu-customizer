#!/usr/bin/env bash
set -euo pipefail
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    echo 'Usage: server-info (read-only; sudo server-info also shows firewall rules and Docker daemon information)'
    exit 0
fi
(($# == 0)) || { echo 'Usage: server-info' >&2; exit 2; }
section() {
    printf '\n== %s ==\n' "$1"
}
inspect() {
    if ! timeout 10s "$@"; then
        printf 'Unavailable: %s (service inactive, permission denied, or command failed/timed out)\n' "$1"
    fi
}
section 'Host and time'
hostname
date --iso-8601=seconds
uptime
inspect timedatectl show --property=NTPSynchronized --property=NTP
section 'Network addresses and routes'
inspect ip -brief address
inspect ip route
inspect ip -6 route
section 'Memory'
free -h
section 'Disks and filesystems'
inspect lsblk --output NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS
df -hT -x tmpfs -x devtmpfs
section 'Service state'
for unit in ssh ufw docker containerd tailscaled v2raya systemd-timesyncd; do
    state=$(systemctl is-active "$unit" 2>/dev/null) || true
    printf '%s: %s\n' "$unit" "${state:-unavailable}"
done
section 'Host firewall'
if [[ $EUID == 0 ]]; then
    inspect ufw status verbose
else
    echo 'Run sudo server-info or sudo ufw status verbose to view live rules.'
fi
section 'Docker'
inspect docker --version
inspect docker info --format 'Server={{.ServerVersion}} Containers={{.Containers}} Running={{.ContainersRunning}} Images={{.Images}}'
echo 'Multi-platform setup: sudo docker-multiarch-setup (connect a v2rayA node first).'
echo 'Build with proxy for FROM and RUN: sudo docker-multiarch-build [buildx build options] .'
if [[ -r /proc/sys/fs/binfmt_misc/customizer-aarch64 ]]; then
    cat /proc/sys/fs/binfmt_misc/customizer-aarch64
else
    echo 'ARM64 binfmt registration unavailable; check systemd-binfmt.service.'
fi
section 'Tailscale'
inspect tailscale status
section 'v2rayA'
inspect v2raya --version
echo 'Web UI: http://<server-ip>:2017 (create an account, import and connect a node)'
echo 'Docker HTTP/HTTPS proxy: http://127.0.0.1:20171; no node means external pulls fail.'
echo 'Bash helpers: proxy-on / proxy-off / proxy-status. Run proxy-check for connectivity checks.'
inspect ss -lnt '( sport = :2017 or sport = :20170 or sport = :20171 or sport = :20172 )'
section 'Maintenance timers'
inspect systemctl list-timers --all --no-pager fstrim.timer apt-daily.timer ubuntu-ip-notify.timer
section 'LAN IP notifications'
if [[ $EUID == 0 ]]; then
    inspect ntfy-ip-notify --check-config
else
    echo 'Run sudo ntfy-ip-notify --check-config to validate the private ntfy configuration.'
fi
section 'Reboot status'
if [[ -f /var/run/reboot-required ]]; then
    cat /var/run/reboot-required
    if [[ -r /var/run/reboot-required.pkgs ]]; then cat /var/run/reboot-required.pkgs; fi
else
    echo 'No reboot-required marker present.'
fi
