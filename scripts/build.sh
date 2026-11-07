#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $(id -u) == 0 ]] || { echo 'Run with sudo bash scripts/build.sh [options]'; exit 1; }
# All temporary guest mounts live in a private mount namespace.
exec unshare --mount --propagation private python3 "$repo/scripts/build.py" "$@"
