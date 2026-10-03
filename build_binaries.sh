#!/usr/bin/env bash
# Compatibility entry point: build and smoke-test the current native platform.
# Cross-platform releases use the hosted release-build.yml runner matrix.
# Usage: ./build_binaries.sh [--local]
set -euo pipefail

if [[ "$#" -gt 1 || ( "$#" -eq 1 && "$1" != "--local" ) ]]; then
    echo "Usage: $0 [--local] (current native platform only)" >&2
    exit 2
fi

cd "$(dirname "$0")"
exec python3 scripts/release.py package --channel rc
