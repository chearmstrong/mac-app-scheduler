#!/bin/bash
set -euo pipefail
umask 022

LABEL="io.github.mac-app-scheduler.daemon"
LIBEXEC="/usr/local/libexec/mac-app-scheduler"
BIN="/usr/local/bin/mac-app-scheduler"
SUPPORT="/Library/Application Support/MacAppScheduler"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"

die() { echo "mac-app-scheduler install: $*" >&2; exit 1; }

[[ ${EUID} -eq 0 ]] || die "run with sudo"
[[ $(/usr/bin/uname -s) == "Darwin" ]] || die "macOS is required"

USER_NAME=""
PYTHON=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) USER_NAME=${2:-}; shift 2 ;;
    --python) PYTHON=${2:-}; shift 2 ;;
    *) die "unknown option: $1" ;;
  esac
done
[[ -n "$USER_NAME" ]] || die "--user is required"
[[ -n "$PYTHON" ]] || PYTHON=$(/usr/bin/env python3)
[[ -x "$PYTHON" ]] || die "Python is not executable: $PYTHON"
"$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || die "Python 3.11+ is required"

UID_VALUE=$(/usr/bin/dscl . -read "/Users/$USER_NAME" UniqueID 2>/dev/null | /usr/bin/awk '{print $2}') || die "user does not exist: $USER_NAME"
[[ "$UID_VALUE" =~ ^[0-9]+$ ]] && [[ "$UID_VALUE" -ge 500 ]] || die "managed user must be a standard local account"
/usr/bin/dsmemberutil checkmembership -U "$USER_NAME" -G admin | /usr/bin/grep -q 'not a member' || die "managed user must not be an administrator"

SOURCE_DIR=$(cd "$(dirname "$0")/.." && /bin/pwd -P)
[[ -f "$SOURCE_DIR/pyproject.toml" ]] || die "source checkout is incomplete"
[[ ! -L "$LIBEXEC" && ! -L "$SUPPORT" && ! -L "$PLIST" ]] || die "installed paths must not be symlinks"

STAGE=$(/usr/bin/mktemp -d /private/tmp/mac-app-scheduler.XXXXXX)
BACKUP="${LIBEXEC}.previous"
cleanup() { /bin/rm -rf "$STAGE"; }
trap cleanup EXIT

/bin/mkdir -p /usr/local/libexec /usr/local/bin "$SUPPORT"
if [[ -d "$LIBEXEC" ]]; then
  /bin/rm -rf "$BACKUP"
  /bin/mv "$LIBEXEC" "$BACKUP"
fi
if ! /bin/mkdir -p "$STAGE/source" || ! /bin/cp -R "$SOURCE_DIR/src" "$STAGE/source/src" || ! /bin/cp "$SOURCE_DIR/pyproject.toml" "$STAGE/source/pyproject.toml" || ! /bin/cp "$SOURCE_DIR/config.example.yaml" "$STAGE/source/config.example.yaml" || ! /bin/cp -R "$SOURCE_DIR/launchd" "$STAGE/source/launchd" || ! /bin/cp -R "$SOURCE_DIR/scripts" "$STAGE/source/scripts" || ! /bin/cp -R "$STAGE/source" "$LIBEXEC/source" || ! "$PYTHON" -m venv "$LIBEXEC/venv" || ! "$LIBEXEC/venv/bin/python" -m pip install "$LIBEXEC/source"; then
  /bin/rm -rf "$LIBEXEC"
  [[ -d "$BACKUP" ]] && /bin/mv "$BACKUP" "$LIBEXEC"
  die "installation failed; previous library restored"
fi
/bin/rm -rf "$BACKUP"
/usr/sbin/chown -R root:wheel "$LIBEXEC" "$SUPPORT"
/bin/chmod -R go-w "$LIBEXEC" "$SUPPORT"
/bin/ln -sf "$LIBEXEC/venv/bin/mac-app-scheduler" "$BIN"

if [[ ! -f "$SUPPORT/config.yaml" ]]; then
  /bin/cp "$SOURCE_DIR/config.example.yaml" "$SUPPORT/config.yaml"
  "$LIBEXEC/venv/bin/python" - "$SUPPORT/config.yaml" "$USER_NAME" <<'PY'
from pathlib import Path
import sys
import yaml
path = Path(sys.argv[1])
data = yaml.safe_load(path.read_text())
data["user"] = sys.argv[2]
path.write_text(yaml.safe_dump(data, sort_keys=False))
PY
fi
/usr/sbin/chown root:wheel "$SUPPORT/config.yaml"
/bin/chmod 0644 "$SUPPORT/config.yaml"
"$BIN" validate-config

/bin/cp "$SOURCE_DIR/launchd/${LABEL}.plist" "$PLIST"
/usr/sbin/chown root:wheel "$PLIST"
/bin/chmod 0644 "$PLIST"
/usr/bin/plutil -lint "$PLIST"
/bin/launchctl bootout "system/$LABEL" >/dev/null 2>&1 || true
/bin/launchctl bootstrap system "$PLIST" || { /bin/launchctl print "system/$LABEL" >&2 || true; die "launchd bootstrap failed"; }
echo "Installed. Configure $SUPPORT/config.yaml, then run: sudo $BIN status"
