#!/bin/bash
set -euo pipefail

LABEL="io.github.mac-app-scheduler.daemon"
LIBEXEC="/usr/local/libexec/mac-app-scheduler"
BIN="/usr/local/bin/mac-app-scheduler"
SUPPORT="/Library/Application Support/MacAppScheduler"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
LOG="/var/log/mac-app-scheduler.log"

[[ ${EUID} -eq 0 ]] || { echo "mac-app-scheduler uninstall: run with sudo" >&2; exit 1; }
[[ ${1:-} == "--yes" ]] || { echo "Refusing to remove policy without --yes" >&2; exit 2; }
/bin/launchctl bootout "system/$LABEL" >/dev/null 2>&1 || true
/bin/rm -f "$BIN" "$PLIST" "$LOG"
/bin/rm -rf "$LIBEXEC" "$SUPPORT"
echo "Removed Mac App Scheduler and its configuration."
