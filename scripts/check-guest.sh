#!/usr/bin/env bash
set -euo pipefail
trap 'printf "Guest validation failed (shell line %s, exit code %s): %s\n" "$LINENO" "$?" "$BASH_COMMAND" >&2' ERR
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
for package in docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin tailscale v2raya; do
    [[ $(dpkg-query -W -f='${db:Status-Status}' "$package") == installed ]]
done
ubuntu_groups=$(runuser -u ubuntu -- id -nG)
if grep -qw docker <<< "$ubuntu_groups"; then exit 1; fi
sudo_policy=$(sudo -l -U ubuntu)
grep -q '(ALL : ALL) ALL' <<< "$sudo_policy"
if grep -q NOPASSWD <<< "$sudo_policy"; then exit 1; fi
if dpkg-query -W -f='${db:Status-Status}' snapd 2>/dev/null | grep -qx installed; then exit 1; fi
[[ $(apt-cache policy snapd | awk '/Candidate:/ {print $2}') == '(none)' ]]
for cmd in gcc g++ make cmake python3 pip3 uv uvx docker dockerd tailscale v2raya v2raya_core dig ss tcpdump tmux ufw server-info server-update; do command -v "$cmd" >/dev/null; done
uv --version
uvx --version
runuser -u ubuntu -- docker --version
runuser -u ubuntu -- docker buildx version
runuser -u ubuntu -- docker compose version
tailscale version
v2raya --version
v2raya_core version
dockerd --validate --config-file=/etc/docker/daemon.json
python3 -m venv /tmp/check-venv
rm -rf /tmp/check-venv
runuser -u ubuntu -- uv venv --offline --python /usr/bin/python3 /tmp/check-uv-venv
rm -rf /tmp/check-uv-venv
echo 'Checking service enablement and automatic update policy'
for unit in ssh ufw docker containerd tailscaled v2raya systemd-timesyncd apt-daily.timer fstrim.timer; do
    systemctl is-enabled "$unit" >/dev/null
done
[[ $(systemctl is-enabled apt-daily-upgrade.timer) == masked ]]
[[ $(systemctl is-enabled nftables.service) == disabled ]]
echo 'Checking persistent UFW configuration without changing live firewall rules'
grep -qx 'ENABLED=yes' /etc/ufw/ufw.conf
grep -qx 'IPV6=yes' /etc/default/ufw
grep -qx 'MANAGE_BUILTINS=no' /etc/default/ufw
grep -qx 'DEFAULT_INPUT_POLICY="DROP"' /etc/default/ufw
grep -qx 'DEFAULT_OUTPUT_POLICY="ACCEPT"' /etc/default/ufw
grep -qx 'DEFAULT_FORWARD_POLICY="DROP"' /etc/default/ufw
grep -qx -- '-A ufw-user-input -p tcp --dport 22 -j ACCEPT' /etc/ufw/user.rules
grep -qx -- '-A ufw6-user-input -p tcp --dport 22 -j ACCEPT' /etc/ufw/user6.rules
grep -qx -- '-A ufw-user-input -p tcp --dport 2017 -j ACCEPT' /etc/ufw/user.rules
grep -qx -- '-A ufw6-user-input -p tcp --dport 2017 -j ACCEPT' /etc/ufw/user6.rules
if grep -q registry-mirrors /etc/docker/daemon.json; then exit 1; fi
if grep -E '^[^#].*pam_motd\.so' /etc/pam.d/sshd; then exit 1; fi
install -d /run/sshd
ssh-keygen -A >/dev/null
echo 'Checking SSH configuration'
sshd -t
# grep -q can close a pipe early and make the producer fail with SIGPIPE under
# pipefail. Capture complete output once, preserving errors from the producer.
sshd_config=$(sshd -T)
grep -qx 'permitrootlogin no' <<< "$sshd_config"
grep -qx 'passwordauthentication yes' <<< "$sshd_config"
grep -qx 'printmotd no' <<< "$sshd_config"
grep -qx 'printlastlog no' <<< "$sshd_config"
grep -qx 'banner none' <<< "$sshd_config"
grep -qx 'clientaliveinterval 60' <<< "$sshd_config"
grep -qx 'clientalivecountmax 3' <<< "$sshd_config"
echo 'Checking APT configuration'
apt_configuration=$(apt-config dump)
grep -q 'APT::Periodic::Unattended-Upgrade "0"' <<< "$apt_configuration"
grep -q 'Unattended-Upgrade::Automatic-Reboot "false"' <<< "$apt_configuration"
grep -q '^Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg$' /etc/apt/sources.list.d/ubuntu.sources
grep -q '^Signed-By: /etc/apt/keyrings/docker.asc$' /etc/apt/sources.list.d/docker.sources
grep -q '^Signed-By: /etc/apt/keyrings/tailscale.gpg$' /etc/apt/sources.list.d/tailscale.sources
[[ $(stat -c %s /swapfile) == 2147483648 && $(stat -c %a /swapfile) == 600 ]]
kernels=(/boot/vmlinuz-*)
[[ -s ${kernels[0]} ]]
echo 'Checking maintenance presets'
grep -qx 'Storage=persistent' /etc/systemd/journald.conf.d/99-customizer.conf
grep -qx 'SystemMaxUse=500M' /etc/systemd/journald.conf.d/99-customizer.conf
grep -qx 'RuntimeMaxUse=128M' /etc/systemd/journald.conf.d/99-customizer.conf
grep -qx 'SystemKeepFree=1G' /etc/systemd/journald.conf.d/99-customizer.conf
grep -qx 'MaxRetentionSec=30day' /etc/systemd/journald.conf.d/99-customizer.conf
grep -qx 'NTP=ntp.aliyun.com ntp.tencent.com' /etc/systemd/timesyncd.conf.d/99-customizer.conf
grep -qx 'FallbackNTP=ntp.ubuntu.com' /etc/systemd/timesyncd.conf.d/99-customizer.conf
grep -qx 'Storage=none' /etc/systemd/coredump.conf.d/99-customizer.conf
grep -qx 'ProcessSizeMax=0' /etc/systemd/coredump.conf.d/99-customizer.conf
grep -qx 'DefaultLimitCORE=0:0' /etc/systemd/system.conf.d/99-customizer-core.conf
grep -qx '\* hard core 0' /etc/security/limits.d/99-customizer-core.conf
server-info --help
server-update --help
proxy-check --help
runuser -u ubuntu -- bash -ic 'declare -F proxy-on proxy-off proxy-status >/dev/null'
echo 'Checking shell proxy helpers without network requests'
runuser -u ubuntu -- bash --noprofile --norc -se <<'PROXY_CHECK'
set -euo pipefail
source /etc/profile.d/customizer-proxy.sh
proxy-on
[[ $http_proxy == http://127.0.0.1:20171 && $HTTPS_PROXY == "$http_proxy" && $ALL_PROXY == "$http_proxy" ]]
[[ $no_proxy == "$NO_PROXY" && $no_proxy == *'.aliyun.com'* ]]
proxy-status
proxy-off
[[ ! -v http_proxy && ! -v https_proxy && ! -v HTTP_PROXY && ! -v HTTPS_PROXY ]]
[[ ! -v all_proxy && ! -v ALL_PROXY && ! -v no_proxy && ! -v NO_PROXY ]]
PROXY_CHECK
ubuntu_home=$(getent passwd ubuntu | cut -d: -f6)
runuser -u ubuntu -- env HOME="$ubuntu_home" bash -se <<'TMUX_CHECK'
set -euo pipefail
trap 'tmux -L customizer-check kill-server 2>/dev/null || true' EXIT
tmux -L customizer-check new-session -d -s check
[[ $(tmux -L customizer-check show-options -gv mouse) == on ]]
[[ $(tmux -L customizer-check show-options -gv history-limit) == 50000 ]]
[[ $(tmux -L customizer-check show-options -gv base-index) == 1 ]]
TMUX_CHECK
echo 'Checking v2rayA defaults and Web UI in an isolated network namespace'
original_netns=$(readlink /proc/self/ns/net)
UBUNTU_CUSTOMIZER_ORIGINAL_NETNS="$original_netns" unshare --net -- python3 /tmp/customizer/check-v2raya.py
echo 'Ubuntu guest validation passed'
