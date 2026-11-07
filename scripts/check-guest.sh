#!/usr/bin/env bash
set -euo pipefail
[[ ${UBUNTU_CUSTOMIZER_GUEST:-} == 1 && -f /tmp/customizer/build-root ]] || exit 1
[[ $(id -u ubuntu) == 1000 && $(id -g ubuntu) == 1000 ]]
[[ $(getent passwd ubuntu | cut -d: -f6-7) == /home/ubuntu:/bin/bash ]]
[[ $(stat -c '%u:%g' /home/ubuntu) == 1000:1000 ]]
[[ $(passwd -S root | awk '{print $2}') == L ]]
[[ $(passwd -S ubuntu | awk '{print $2}') == P ]]
while IFS= read -r package; do
    [[ -z $package || $package == \#* ]] && continue
    [[ $(dpkg-query -W -f='${db:Status-Status}' "$package") == installed ]]
done < /tmp/customizer/packages.txt
for package in docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin tailscale; do
    [[ $(dpkg-query -W -f='${db:Status-Status}' "$package") == installed ]]
done
if id -nG ubuntu | tr ' ' '\n' | grep -qx docker; then exit 1; fi
sudo_policy=$(sudo -l -U ubuntu)
grep -q '(ALL : ALL) ALL' <<< "$sudo_policy"
if grep -q NOPASSWD <<< "$sudo_policy"; then exit 1; fi
if dpkg-query -W -f='${db:Status-Status}' snapd 2>/dev/null | grep -qx installed; then exit 1; fi
[[ $(apt-cache policy snapd | awk '/Candidate:/ {print $2}') == '(none)' ]]
for cmd in gcc g++ make cmake python3 pip3 uv uvx docker tailscale dig ss tcpdump; do command -v "$cmd" >/dev/null; done
uv --version
uvx --version
docker --version
docker buildx version
docker compose version
tailscale version
python3 -m venv /tmp/check-venv
rm -rf /tmp/check-venv
runuser -u ubuntu -- uv venv --offline --python /usr/bin/python3 /tmp/check-uv-venv
rm -rf /tmp/check-uv-venv
systemctl is-enabled ssh docker containerd tailscaled apt-daily.timer >/dev/null
[[ $(systemctl is-enabled apt-daily-upgrade.timer) == masked ]]
if grep -q registry-mirrors /etc/docker/daemon.json; then exit 1; fi
if grep -E '^[^#].*pam_motd\.so' /etc/pam.d/sshd; then exit 1; fi
install -d /run/sshd
ssh-keygen -A >/dev/null
sshd -t
sshd -T | grep -qx 'permitrootlogin no'
sshd -T | grep -qx 'passwordauthentication yes'
sshd -T | grep -qx 'printmotd no'
sshd -T | grep -qx 'printlastlog no'
sshd -T | grep -qx 'banner none'
apt-config dump | grep -q 'APT::Periodic::Unattended-Upgrade "0"'
apt-config dump | grep -q 'Unattended-Upgrade::Automatic-Reboot "false"'
grep -q '^Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg$' /etc/apt/sources.list.d/ubuntu.sources
grep -q '^Signed-By: /etc/apt/keyrings/docker.asc$' /etc/apt/sources.list.d/docker.sources
grep -q '^Signed-By: /etc/apt/keyrings/tailscale.gpg$' /etc/apt/sources.list.d/tailscale.sources
[[ $(stat -c %s /swapfile) == 2147483648 && $(stat -c %a /swapfile) == 600 ]]
kernels=(/boot/vmlinuz-*)
[[ -s ${kernels[0]} ]]
