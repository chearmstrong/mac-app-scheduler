# Mac App Scheduler

A lightweight macOS utility for enforcing per-app availability schedules.

It is intended for one standard, non-administrator macOS account. A root-owned
LaunchDaemon repeatedly reconciles the running apps for that account against a
small YAML policy. The included example blocks FaceTime after 20:00 and Chrome
after 21:00. The daemon checks the current policy again after restart, wake and
login, and closes apps reopened during a blocked period.

## What this is

This is a command-line system utility, not an Xcode project or a macOS `.app`.
V1 does not require an Apple Developer account, signing, notarisation, or App
Store distribution. It supports macOS 13 Ventura or later and an
administrator-installed Python 3.11 or later.

The installer creates an isolated Python virtual environment under
`/usr/local/libexec/mac-app-scheduler/venv`; it deliberately does not rely on
Apple's bundled Python. Initial installation uses pip and may download PyYAML.

## Install

Clone the project on the target Mac, review the example policy, then install as
an administrator:

```bash
sudo ./scripts/install.sh --user alice
sudo mac-app-scheduler status
```

Replace `alice` with the account's short username. It must be a local,
non-administrator account. If `sudo` cannot find the intended Python, pass its
absolute path explicitly (the lookup below runs in your own shell):

```bash
sudo ./scripts/install.sh --user alice --python "$(command -v python3)"
```

The selected interpreter must be Python 3.11+. The shell locates it without
starting an interactive interpreter. A small Python installer stages the source
before replacing anything, creates the venv at its final location, validates
configuration and the plist, then uses `launchctl bootstrap system` to load the
daemon. It retains the old library, wrapper, configuration and plist until
bootstrap and the service lookup succeed. This lookup confirms registration;
the smoke test below checks actual app enforcement.

On failure, it restores previous files and, if stopped during the update,
reloads the previous service. Interrupting an update with Ctrl+C also restores
the previous files. If recovery itself fails, the error reports the
retained backup directory. Existing configuration and overrides are preserved
on repair; `--user` does not silently retarget an existing configuration. Use
the checkout installer again to update from newer source. After installation,
`sudo mac-app-scheduler install --user alice --python /absolute/path/to/python3`
repairs from the installed source snapshot.

## Configure schedules

Edit `/Library/Application Support/MacAppScheduler/config.yaml` as an
administrator, then reload:

```bash
sudo mac-app-scheduler reload
```

The installed configuration starts from [config.example.yaml](config.example.yaml):

```yaml
rules:
  - id: facetime
    name: FaceTime
    bundle_id: com.apple.FaceTime
    process_name: FaceTime
    block: {from: "20:00", until: "07:00"}
  - id: chrome
    name: Google Chrome
    bundle_id: com.google.Chrome
    process_name: Google Chrome
    block: {from: "21:00", until: "07:00"}
```

`from` is inclusive and `until` is exclusive. A range that crosses midnight is
supported: `20:00` to `07:00` blocks from 20:00 through 06:59. Times use the
Mac's local wall clock, so normal timezone and daylight-saving changes are
picked up on the next reconciliation. Equal endpoints are rejected.

Rules require unique lower-case `id` values, a bundle ID, and an exact
process-name fallback. The daemon resolves the configured username to a UID at
runtime; configuration never stores a UID or home-directory path.

## Commands

```bash
sudo mac-app-scheduler status
sudo mac-app-scheduler enable
sudo mac-app-scheduler disable
sudo mac-app-scheduler reload
sudo mac-app-scheduler allow facetime 30m
sudo mac-app-scheduler dry-run
sudo mac-app-scheduler uninstall --yes
```

`status` shows service registration, account resolution, enabled state, each
rule's current state, upcoming schedule boundaries, and active overrides.
`allow` creates a separate UTC expiry record and does not edit YAML. Durations
are positive whole numbers with `s`, `m`, `h`, or `d` units. `disable` pauses
enforcement but does not unload the supervised daemon. `dry-run` inventories
the target account and prints rule IDs, PIDs and planned `SIGTERM` actions,
plus a warning that survivors may require escalation. It does not send signals,
wait, or change overrides. No matches produce an explicit no-action message.
`reload` validates the policy before restarting the service through
`launchctl kickstart -k`; launchctl failures produce diagnostics and a non-zero
exit status. Validation checks the schema, while status/dry-run also resolve
the managed account.

Use `sudo mac-app-scheduler validate-config` to check the current YAML before
reloading. The CLI's `install` and `uninstall` commands delegate to the installed
scripts; the checkout script remains the canonical first-install route.
To validate another file without installing it, run
`mac-app-scheduler --config config.example.yaml validate-config`.

## How enforcement works

The daemon starts at boot and stays running through `KeepAlive=true`. It makes
one check on startup and then checks every 45 seconds by default. It filters
processes by the resolved target UID and reads executable paths and process
start times through macOS `libproc`. Enclosing `.app` bundle IDs establish app
identity. The daemon sends `SIGTERM` to matching main processes, waits five
seconds, then revalidates each original process before sending `SIGKILL` to
surviving bundle processes, including Chrome helpers. If only orphan helpers
remain, they receive `SIGTERM` and their own grace period first. Exact-name
fallback matches receive `SIGTERM` but are never force-killed.

UID, start time, executable path and bundle identity are refreshed immediately
before signalling. A changed identity or vanished process is skipped. An app
restarted during the grace period is considered afresh on the next cycle and
does not inherit a previous process's force-kill decision. These checks reduce
PID-reuse races; macOS PID-based signalling still has a small gap between the
last identity check and delivery, so this is not an atomic process handle.

The interval is the pause between passes; grace periods and the time spent on
other rules add to detection latency. A cutoff or newly opened app is therefore
usually handled within about 45 seconds, plus any work already in progress.
Graceful Chrome termination
normally gives Chrome a chance to save session state, but session restoration
is not guaranteed if the process is hung and requires `SIGKILL`.

## Logs and troubleshooting

The daemon logs startup/shutdown, configuration activation/errors, rule-state
changes, override expiry, signals sent and runtime errors. CLI state changes
and override creation are logged to stderr. Successful no-action cycles are
silent, and repeated identical errors are suppressed until the condition
changes. Errors in one app rule do not prevent later rules from being checked;
cycle errors, including missing accounts, are retried on the next interval.

Launchd directs daemon stderr to `/var/log/mac-app-scheduler.log`. Check the
service and configuration first:

```bash
sudo mac-app-scheduler validate-config
sudo mac-app-scheduler status
sudo launchctl print system/io.github.mac-app-scheduler.daemon
sudo tail -n 100 /var/log/mac-app-scheduler.log
```

Malformed configuration at first start leaves enforcement inactive rather than
guessing. Once a valid policy has loaded, a malformed edit retains the last
known-good policy until a valid replacement is written.

Malformed mutable state defaults to enforcement enabled with no overrides and
produces an error log. The damaged file is retained for diagnosis; a state
command such as `enable` writes a fresh valid state.

## Uninstall and recovery

```bash
sudo mac-app-scheduler uninstall --yes
```

The explicit `--yes` confirms removal of installed code, configuration and
overrides. The uninstaller stops the loaded service before removing files. A
stop failure aborts removal with an error. Diagnostic logs are retained.
You can also run `sudo ./scripts/uninstall.sh --yes` from a checkout if the
installed command is unavailable.

If an update reports failed recovery, retain the reported backup directory.
It contains `previous-library` when the old library could not be moved back,
and snapshots `file-0` (config), `file-1` (plist), and `file-2` (wrapper) for
previously existing files. Resolve the reported filesystem/launchctl error
before restoring those files to their documented locations and bootstrapping
the previous plist. A successfully recovered failure removes the temporary
backup automatically. The installer lock file is retained for future repairs.

## Security model and limitations

All installed code, policy, state, and launchd configuration are root-owned;
the managed standard user cannot edit them. This prevents casual bypassing, not
deliberate administrative control. It is not MDM, malware protection, a
hardened security boundary, or a replacement for Apple Screen Time.

It does not block alternate browsers, web versions of services, virtual
machines, copied executables that evade bundle/process identity, recovery-mode
changes, administrators, or other deliberate system-level bypasses.

## Smoke test before relying on it

1. Back up the current config and use `validate-config` plus `dry-run`.
2. Configure a short, near-future test window for a disposable app/profile.
3. Confirm the already-running app is closed at the cutoff.
4. Reopen it during the block and confirm closure on the next pass, allowing
   for the interval and grace periods of other rules.
5. Check `allow`, expiry, `disable`, and `enable`.
6. Test logout/login, reboot, and sleep/wake across a cutoff.
7. Confirm another user's similarly named process is untouched.
8. For Chrome, confirm its normal session-restoration behaviour on this Mac.
9. Run `sudo mac-app-scheduler uninstall --yes` only when you intend to remove
   the policy and state.

Process visibility across all GUI sessions and third-party-app termination
behaviour remain real-Mac validation points; do not broaden process matching if
one of these checks fails.

## Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -ra
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
bash -n scripts/install.sh scripts/uninstall.sh
plutil -lint launchd/io.github.mac-app-scheduler.daemon.plist
```

The test suite uses fakes for signal behaviour. It does not install a daemon or
terminate GUI apps. Those actions belong to the smoke test above.
