#!/usr/bin/env bash
# Compatibility entry for the generated guest checks.
set -euo pipefail
exec bash /tmp/customizer/check.sh "$@"
