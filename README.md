# Mac App Scheduler

A lightweight macOS utility for enforcing per-app availability schedules.

It is intended for one standard, non-administrator macOS account. A root-owned
LaunchDaemon repeatedly reconciles the running apps for that account against a
small YAML policy. It closes FaceTime after 20:00 and Chrome after 21:00 in the
included example, and continues enforcing after restart, wake, login, or an
attempt to reopen a blocked app.

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

`alice` must be a local, non-administrator account. The installer validates the
account and Python, copies root-owned files, validates the plist and YAML, then
uses `launchctl bootstrap system` to start the daemon. It prints an error and
preserves the previous library installation if replacing it fails.

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

`allow` creates a separate UTC expiry record and does not edit YAML. Durations
are positive whole numbers with `s`, `m`, `h`, or `d` units. `disable` pauses
enforcement but does not unload the supervised daemon. `dry-run` inventories
the target account but must not signal any process. `reload` validates the
policy before restarting the service through `launchctl kickstart -k`.

Use `sudo mac-app-scheduler validate-config` to check the current YAML before
reloading. The CLI's `install` and `uninstall` commands delegate to the installed
scripts; the checkout script remains the canonical first-install route.

## How enforcement works

The daemon starts at boot and stays running through `KeepAlive=true`. It makes
one check on startup and then checks every 45 seconds by default. It filters
processes by the resolved target UID, identifies apps through enclosing `.app`
bundle IDs, sends `SIGTERM` to a blocked main process, waits five seconds, then
rescans and sends `SIGKILL` only to remaining matching bundle processes. This
includes Chrome helpers only when they are still associated with Chrome's
bundle. A simple exact-name fallback is never force-killed.

The bounded delay means a newly opened blocked app or a cutoff transition can
remain visible for up to the configured interval. Graceful Chrome termination
normally gives Chrome a chance to save session state, but session restoration
is not guaranteed if the process is hung and requires `SIGKILL`.

## Logs and troubleshooting

Launchd directs fallback output to `/var/log/mac-app-scheduler.log`. Check the
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
4. Reopen it during the block and confirm closure within 45 seconds.
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
