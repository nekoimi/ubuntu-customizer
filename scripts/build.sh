#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $(id -u) == 0 ]] || { echo 'Run with sudo bash scripts/build.sh [options]'; exit 1; }
for namespace in mnt pid uts ipc; do
    export "UBUNTU_CUSTOMIZER_HOST_${namespace^^}=$(readlink "/proc/self/ns/$namespace")"
done
exec unshare --mount --pid --fork --kill-child=SIGKILL --mount-proc --uts --ipc \
    --propagation private python3 "$repo/scripts/build.py" "$@"
