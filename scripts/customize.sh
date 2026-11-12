#!/usr/bin/env bash
# Compatibility entry; run only inside a prepared target image.
set -euo pipefail
exec bash /tmp/customizer/customize.sh "$@"
