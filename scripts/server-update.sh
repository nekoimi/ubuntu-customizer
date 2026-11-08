#!/usr/bin/env bash
set -euo pipefail
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    echo 'Usage: server-update'
    echo 'Manually refresh APT indexes and run apt-get upgrade with its normal confirmation.'
    echo 'No automatic reboot, package removal or autoremove. Docker/Tailscale updates may restart their services.'
    exit 0
fi
(($# == 0)) || { echo 'Usage: server-update' >&2; exit 2; }
if [[ $EUID != 0 ]]; then
    exec sudo -- /usr/local/bin/server-update
fi
[[ -t 0 ]] || { echo 'Run server-update from an interactive terminal.' >&2; exit 1; }
export DEBIAN_FRONTEND=readline
echo 'Refreshing indexes; APT will ask before installing available upgrades.'
echo 'Docker/Tailscale updates may restart services and interrupt workloads or remote connections.'
apt-get update
prompt_style=''
prompt_reset=''
if [[ -t 1 && ${TERM:-} != dumb && ! ${NO_COLOR+x} ]]; then
    prompt_style=$'\033[1;33m'
    prompt_reset=$'\033[0m'
fi
printf '\n\n%s%s\n' "$prompt_style" '======================================================================'
printf '%s\n' '[INTERACTIVE STEP] REVIEW SYSTEM UPGRADES'
printf '%s\n' 'APT will list upgrades and ask for confirmation if changes are available.'
printf '%s\n' 'Read the package summary, then answer at the APT prompt below.'
printf '%s\n' 'Docker/Tailscale upgrades may restart services and interrupt connections.'
printf '%s%s\n\n' '======================================================================' "$prompt_reset"
apt-get upgrade
if [[ -f /var/run/reboot-required ]]; then
    cat /var/run/reboot-required
    if [[ -r /var/run/reboot-required.pkgs ]]; then cat /var/run/reboot-required.pkgs; fi
    echo 'Reboot manually when convenient: sudo reboot'
else
    echo 'Update completed. No reboot-required marker present.'
fi
