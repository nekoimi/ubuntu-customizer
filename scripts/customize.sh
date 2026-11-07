#!/usr/bin/env bash
# Runs INSIDE the mounted Ubuntu image. Public configuration comes from build.py.
set -euo pipefail
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
apt-get update
apt-get dist-upgrade -y --no-install-recommends
mapfile -t packages < <(sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' /tmp/customizer/packages.txt)
apt-get install -y --no-install-recommends "${packages[@]}"
apt-get purge -y snapd cloud-init || exit 1
rm -rf /snap /var/snap /var/lib/snapd /var/cache/snapd /etc/cloud /var/lib/cloud

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
apt-get install -y --no-install-recommends \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin tailscale
install -d /etc/docker /etc/uv
cat > /etc/docker/daemon.json <<'EOF'
{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}
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

getent passwd ubuntu >/dev/null || useradd --uid 1000 --user-group --create-home --shell /bin/bash ubuntu
[[ $(id -u ubuntu) == 1000 && $(id -g ubuntu) == 1000 ]]
usermod -aG sudo ubuntu
if id -nG ubuntu | tr ' ' '\n' | grep -qx docker; then gpasswd -d ubuntu docker; fi
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
systemctl disable ssh.socket
systemctl enable ssh docker containerd tailscaled systemd-networkd systemd-resolved systemd-timesyncd apt-daily.timer
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
dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
chmod 600 /swapfile
mkswap /swapfile
install -d /var/lib/ubuntu-customizer
dpkg-query -W -f='${Package}\t${Version}\n' | sort > /var/lib/ubuntu-customizer/packages.txt
apt-cache policy docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin tailscale \
  > /var/lib/ubuntu-customizer/package-origins.txt
rm -f /usr/sbin/policy-rc.d
