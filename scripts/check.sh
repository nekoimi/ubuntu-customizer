#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
python3 -m unittest discover -s tests -v
bash -n build.sh
shellcheck build.sh
while IFS= read -r -d '' script; do
    bash -n "$script"
    shellcheck "$script"
done < <(find scripts installer distros components \( -name '*.sh' -o -name '*.bash' \) -print0)
