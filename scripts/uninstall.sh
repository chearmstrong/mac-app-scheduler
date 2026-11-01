#!/bin/bash
set -euo pipefail

if [[ ${1:-} == --help ]]; then
  echo 'Usage: sudo ./scripts/uninstall.sh --yes'
  exit 0
fi
SOURCE_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
PYTHON="/usr/local/libexec/mac-app-scheduler/venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON=$(command -v python3)
fi
exec "$PYTHON" "$SOURCE_DIR/src/mac_app_scheduler/installer.py" uninstall "$@"
