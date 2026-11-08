#!/usr/bin/env bash
# Runs INSIDE the mounted Ubuntu image. Public configuration comes from build.py.
set -euo pipefail
trap 'printf "Guest customization failed (shell line %s, exit code %s): %s\n" "$LINENO" "$?" "$BASH_COMMAND" >&2' ERR
[[ ${UBUNTU_CUSTOMIZER_GUEST:-} == 1 && -f /tmp/customizer/build-root ]] || {
    echo 'Use scripts/build.sh; this script must run inside the build image.' >&2
    exit 1
}
export DEBIAN_FRONTEND=noninteractive
[[ $(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release) == noble ]]
install -d /etc/apt/keyrings /etc/apt/preferences.d /etc/ssh/sshd_config.d
rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources
cat > /etc/apt/sources.list.d/ubuntu.sources <<EOF
Types: deb
URIs: $UBUNTU_MIRROR
Suites: noble noble-updates noble-security noble-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
cat > /etc/apt/preferences.d/no-snap.pref <<'EOF'
Package: snapd
Pin: version *
Pin-Priority: -1
EOF
# policy-rc.d prevents package postinst scripts starting services during image build.
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d
echo 'iperf3 iperf3/start_daemon boolean false' | debconf-set-selections
echo 'ufw ufw/enable boolean false' | debconf-set-selections
echo 'Installing Ubuntu base tools and development packages'
apt-get update
apt-get dist-upgrade -y --no-install-recommends
mapfile -t packages < <(sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' /tmp/customizer/packages.txt)
apt-get install -y --no-install-recommends "${packages[@]}"
apt-get purge -y snapd cloud-init || exit 1
rm -rf /snap /var/snap /var/lib/snapd /var/cache/snapd /etc/cloud /var/lib/cloud

echo 'Configuring Docker and Tailscale signed package repositories'
curl --fail --silent --show-error --location "$DOCKER_MIRROR/gpg" -o /etc/apt/keyrings/docker.asc
chmod 644 /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: $DOCKER_MIRROR
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
# Tailscale has its own signed stable repository; Ubuntu/Docker stay on domestic mirrors.
curl --fail --silent --show-error --location \
  https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg -o /etc/apt/keyrings/tailscale.gpg
cat > /etc/apt/sources.list.d/tailscale.sources <<'EOF'
Types: deb
URIs: https://pkgs.tailscale.com/stable/ubuntu
Suites: noble
Components: main
Architectures: amd64
Signed-By: /etc/apt/keyrings/tailscale.gpg
EOF
apt-get update
echo 'Installing Docker and Tailscale packages'
apt-get install -y --no-install-recommends \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin tailscale
install -d /etc/docker /etc/uv
echo 'Installing verified v2rayA package with its bundled core and rule data'
apt-get install -y --no-install-recommends /tmp/customizer/v2raya.deb
install -d -m 0700 /etc/v2raya
cat > /etc/default/v2raya <<'EOF'
V2RAYA_ADDRESS=0.0.0.0:2017
V2RAYA_CONFIG=/etc/v2raya
V2RAYA_V2RAY_BIN=/usr/bin/v2raya_core
V2RAYA_V2RAY_ASSETSDIR=/usr/share/v2raya
V2RAYA_LITE=true
V2RAYA_LOG_FILE=
V2RAYA_LOG_DISABLE_COLOR=true
EOF
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {"max-size": "10m", "max-file": "3"},
  "proxies": {
    "http-proxy": "http://127.0.0.1:20171",
    "https-proxy": "http://127.0.0.1:20171",
    "no-proxy": "localhost,127.0.0.0/8,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.localhost,.local"
  }
}
EOF
install -d /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/20-v2raya.conf <<'EOF'
[Unit]
Wants=v2raya.service
After=v2raya.service
EOF
printf '[global]\nindex-url = %s\n' "$PYTHON_INDEX" > /etc/pip.conf
cat > /etc/uv/uv.toml <<EOF
python-preference = "only-system"
python-downloads = "never"

[[index]]
name = "domestic"
url = "$PYTHON_INDEX"
default = true
EOF
install -m 0755 /tmp/customizer/uv /tmp/customizer/uvx /usr/local/bin/

echo 'Configuring accounts, hostname, locale and networking'
getent passwd ubuntu >/dev/null || useradd --uid 1000 --user-group --create-home --shell /bin/bash ubuntu
[[ $(id -u ubuntu) == 1000 && $(id -g ubuntu) == 1000 ]]
usermod -aG sudo ubuntu
ubuntu_groups=$(id -nG ubuntu)
if grep -qw docker <<< "$ubuntu_groups"; then gpasswd -d ubuntu docker; fi
passwd -l root
chage -M -1 -I -1 -E -1 ubuntu
printf 'ubuntu-template\n' > /etc/hostname
printf '127.0.0.1 localhost\n127.0.1.1 ubuntu-template\n::1 localhost ip6-localhost ip6-loopback\n' > /etc/hosts
ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime
echo Asia/Shanghai > /etc/timezone
sed -i 's/^# *\(en_US.UTF-8 UTF-8\)/\1/; s/^# *\(zh_CN.UTF-8 UTF-8\)/\1/' /etc/locale.gen
locale-gen
update-locale LANG=en_US.UTF-8
rm -f /etc/netplan/*.yaml
cat > /etc/netplan/01-ethernet.yaml <<'EOF'
network:
  version: 2
  renderer: networkd
  ethernets:
    predictable:
      match:
        name: "en*"
      dhcp4: true
      dhcp6: true
      optional: true
    legacy:
      match:
        name: "eth*"
      dhcp4: true
      dhcp6: true
      optional: true
EOF
chmod 600 /etc/netplan/01-ethernet.yaml
cat > /etc/ssh/sshd_config.d/00-customizer.conf <<'EOF'
Port 22
PasswordAuthentication yes
PermitRootLogin no
PrintMotd no
PrintLastLog no
Banner none
ClientAliveInterval 60
ClientAliveCountMax 3
EOF
sed -i '/^[^#].*pam_motd\.so/s/^/# customizer: /' /etc/pam.d/sshd
# Ubuntu's default ~/.bashrc displays a sudo introduction until this file exists.
install -o ubuntu -g ubuntu -m 0644 /dev/null /home/ubuntu/.sudo_as_admin_successful
cat > /etc/apt/apt.conf.d/99-customizer-updates <<'EOF'
APT::Periodic::Enable "1";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::Unattended-Upgrade "0";
Unattended-Upgrade::Automatic-Reboot "false";
EOF
echo 'Configuring log limits, time synchronization and terminal defaults'
install -d /etc/systemd/journald.conf.d /etc/systemd/timesyncd.conf.d \
  /etc/systemd/coredump.conf.d /etc/systemd/system.conf.d /etc/security/limits.d
cat > /etc/systemd/journald.conf.d/99-customizer.conf <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=500M
SystemKeepFree=1G
RuntimeMaxUse=128M
MaxRetentionSec=30day
EOF
cat > /etc/systemd/timesyncd.conf.d/99-customizer.conf <<'EOF'
[Time]
NTP=ntp.aliyun.com ntp.tencent.com
FallbackNTP=ntp.ubuntu.com
EOF
cat > /etc/tmux.conf <<'EOF'
set -g mouse on
set -g history-limit 50000
set -g base-index 1
setw -g pane-base-index 1
set -g renumber-windows on
set -g status-right '#H | %Y-%m-%d %H:%M'
EOF
cat > /etc/systemd/coredump.conf.d/99-customizer.conf <<'EOF'
[Coredump]
Storage=none
ProcessSizeMax=0
EOF
cat > /etc/systemd/system.conf.d/99-customizer-core.conf <<'EOF'
[Manager]
DefaultLimitCORE=0:0
EOF
cat > /etc/security/limits.d/99-customizer-core.conf <<'EOF'
* soft core 0
* hard core 0
root soft core 0
root hard core 0
EOF
install -m 0755 /tmp/customizer/server-info.sh /usr/local/bin/server-info
install -m 0755 /tmp/customizer/server-update.sh /usr/local/bin/server-update
install -m 0755 /tmp/customizer/proxy-check.py /usr/local/bin/proxy-check
install -m 0644 /tmp/customizer/proxy-shell.bash /etc/profile.d/customizer-proxy.sh
printf '\nsource /etc/profile.d/customizer-proxy.sh\n' >> /etc/bash.bashrc
echo 'Configuring and testing UFW in an isolated network namespace'
original_netns=$(readlink /proc/self/ns/net)
UBUNTU_CUSTOMIZER_ORIGINAL_NETNS="$original_netns" unshare --net -- bash -se <<'FIREWALL_CONFIG'
set -euo pipefail
[[ -n ${UBUNTU_CUSTOMIZER_ORIGINAL_NETNS:-} && $(readlink /proc/self/ns/net) != "$UBUNTU_CUSTOMIZER_ORIGINAL_NETNS" ]] || {
    echo 'Refusing to configure UFW without a private network namespace.' >&2
    exit 1
}
sed -i 's/^IPV6=.*/IPV6=yes/; s/^MANAGE_BUILTINS=.*/MANAGE_BUILTINS=no/' /etc/default/ufw
ufw default deny incoming
ufw default allow outgoing
ufw default deny routed
ufw allow 22/tcp
ufw allow 2017/tcp
ufw --force enable
firewall_status=$(ufw status verbose)
printf '%s\n' "$firewall_status"
grep -qx 'Status: active' <<< "$firewall_status"
grep -q '^Default: deny (incoming), allow (outgoing), deny (routed)' <<< "$firewall_status"
FIREWALL_CONFIG
echo 'Configuring enabled services and automatic update policy'
systemctl disable ssh.socket
systemctl enable ssh ufw docker containerd tailscaled v2raya systemd-networkd systemd-resolved systemd-timesyncd apt-daily.timer fstrim.timer
systemctl mask apt-daily-upgrade.timer apt-daily-upgrade.service
for unit in iperf3.service vnstat.service smartmontools.service sysstat.service sysstat-collect.timer sysstat-summary.timer nftables.service; do
  systemctl disable "$unit" 2>/dev/null || true
done
# Keep portable storage/network modules instead of tailoring initramfs to the builder.
sed -i 's/^MODULES=.*/MODULES=most/' /etc/initramfs-tools/initramfs.conf
printf 'RESUME=none\n' > /etc/initramfs-tools/conf.d/resume
cat > /etc/default/grub <<'EOF'
GRUB_DEFAULT=0
GRUB_TIMEOUT=3
GRUB_DISTRIBUTOR=Ubuntu
GRUB_CMDLINE_LINUX_DEFAULT=""
GRUB_CMDLINE_LINUX=""
GRUB_DISABLE_OS_PROBER=true
EOF
echo 'Creating swapfile and recording installed package versions'
dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
chmod 600 /swapfile
mkswap /swapfile
install -d /var/lib/ubuntu-customizer
install -m 0644 /tmp/customizer/v2raya-release.json /var/lib/ubuntu-customizer/v2raya-release.json
dpkg-query -W -f='${Package}\t${Version}\n' | sort > /var/lib/ubuntu-customizer/packages.txt
apt-cache policy docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin tailscale \
  > /var/lib/ubuntu-customizer/package-origins.txt
rm -f /usr/sbin/policy-rc.d
echo 'Ubuntu guest customization completed'
