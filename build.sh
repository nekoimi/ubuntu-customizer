#!/usr/bin/env bash
# Local command-line entry. GitHub Actions and a Runner are not required.
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
usage() {
    cat <<'EOF'
Usage: bash build.sh [command] [options]

  setup                 Install build dependencies (uses sudo if needed)
  check [--config FILE] Check host tools, resources and output paths; no writes
  test                  Run safety tests and script static checks
  build [options]       Build the ISO (default command; uses sudo if needed)
  help                  Show this help

Examples:
  bash build.sh setup
  bash build.sh check
  bash build.sh build
  bash build.sh build --build-id local-001
  bash build.sh build --config /path/to/build.json

Password: config/local.env or UBUNTU_INITIAL_PASSWORD environment variable.
Default ISO: /srv/ubuntu-customizer/releases/<build-id>/ubuntu-custom-server.iso
EOF
}
as_root() {
    if [[ $EUID == 0 ]]; then
        exec "$@"
    fi
    command -v sudo >/dev/null || { echo 'sudo is required, or run this command as root.' >&2; exit 1; }
    if [[ -n ${UBUNTU_INITIAL_PASSWORD:-} ]]; then
        exec sudo --preserve-env=UBUNTU_INITIAL_PASSWORD -- "$@"
    fi
    exec sudo -- "$@"
}
command=${1:-build}
if (($#)); then shift; fi
case "$command" in
    help|-h|--help) usage ;;
    setup)
        (($# == 0)) || { usage >&2; exit 2; }
        as_root bash "$repo/scripts/setup-host.sh"
        ;;
    check) exec python3 "$repo/scripts/build.py" --check "$@" ;;
    test)
        (($# == 0)) || { usage >&2; exit 2; }
        exec bash "$repo/scripts/check.sh"
        ;;
    build)
        if [[ ${1:-} == --help || ${1:-} == -h ]]; then
            exec python3 "$repo/scripts/build.py" --help
        fi
        as_root bash "$repo/scripts/build.sh" "$@"
        ;;
    *) echo "Unknown command: $command" >&2; usage >&2; exit 2 ;;
esac
