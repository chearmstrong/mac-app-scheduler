#!/bin/bash
set -euo pipefail
umask 022

if [[ ${1:-} == --help ]]; then
  echo 'Usage: sudo ./scripts/install.sh --user USER [--python /absolute/path/to/python3]'
  exit 0
fi

PYTHON=""
INSTALL_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      [[ $# -ge 2 && -n "$2" ]] || { echo '--python needs an executable path' >&2; exit 2; }
      PYTHON="$2"
      shift 2 ;;
    *) INSTALL_ARGS+=("$1"); shift ;;
  esac
done
if [[ -z "$PYTHON" ]]; then
  PYTHON=$(command -v python3) || { echo 'Install Python 3.11+ or pass --python /absolute/path' >&2; exit 1; }
fi
[[ -x "$PYTHON" ]] || { echo "Python is not executable: $PYTHON" >&2; exit 1; }
"$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || {
  echo 'Administrator-installed Python 3.11+ is required; use --python /absolute/path' >&2
  exit 1
}
SOURCE_DIR=$(cd "$(dirname "$0")/.." && pwd -P)
exec "$PYTHON" "$SOURCE_DIR/src/mac_app_scheduler/installer.py" install --source "$SOURCE_DIR" --python "$PYTHON" "${INSTALL_ARGS[@]}"
