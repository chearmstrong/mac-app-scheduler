# Mac App Scheduler Design

## Purpose

Mac App Scheduler is a lightweight macOS utility for enforcing per-app
availability schedules for one standard, non-administrator user. An
administrator installs and configures it once; a system LaunchDaemon then
continuously reconciles running processes with the configured schedule.

Version 1 supports multiple app rules, overnight blocking windows, temporary
allow overrides, a root-administered CLI, and clean installation and removal.
It deliberately excludes a GUI, remote administration, multiple managed users,
notifications, usage quotas, network filtering, MDM integration, automatic
updates, and App Store distribution.

## Platform and Runtime

- macOS 13 Ventura or newer, on Apple silicon and Intel.
- Python 3.11 or newer, installed by an administrator from a maintained source.
- The installer creates a dedicated virtual environment under
  `/usr/local/libexec/mac-app-scheduler/venv`.
- The project does not depend on `/usr/bin/python3`: Apple's bundled scripting
  runtimes are not a stable third-party application runtime.
- PyYAML is the only required runtime package. The installer uses an isolated
  virtual environment and may need network access during its initial install.
- V1 is a CLI and daemon, not a macOS `.app` bundle. It does not require Xcode,
  an Apple Developer account, code signing, notarization, or App Store tooling.

These constraints and their rationale must be prominent in the README and in
installer preflight failures.

## Architecture

```text
launchd system domain
  └── io.github.mac-app-scheduler.daemon
        └── Python reconciliation daemon (root)
              ├── strict YAML configuration loader
              ├── root-only mutable state store
              ├── schedule evaluator
              ├── Darwin process inventory
              ├── app identity matcher
              └── graceful/forced process terminator
```

The daemon performs one reconciliation immediately after startup, then repeats
every 45 seconds by default. It remains resident and handles `SIGTERM` cleanly.
The LaunchDaemon uses `KeepAlive=true`; this implicitly launches it when loaded
and asks launchd to restart it after unexpected exit. A continuously running
job is appropriate because continuous desired-state enforcement is the service,
and it avoids repeatedly starting Python. `StartInterval` is not used because
interval events can be missed during sleep and do not provide crash supervision.

No LaunchAgent is installed. A root daemon belongs to the system bootstrap
domain and should not depend on GUI-session APIs. The process adapter uses
kernel process metadata and POSIX signals instead.

## Installed Layout

```text
/usr/local/bin/mac-app-scheduler
/usr/local/libexec/mac-app-scheduler/
  ├── venv/
  ├── package source and metadata
  ├── install.sh
  └── uninstall.sh
/Library/Application Support/MacAppScheduler/
  ├── config.yaml
  ├── state.json
  └── state.lock
/Library/LaunchDaemons/io.github.mac-app-scheduler.daemon.plist
```

Directories and executable content are owned by `root:wheel` and are not
writable by the managed user. The plist and permanent configuration use mode
`0644`; the mutable state and lock use mode `0600`. The configuration contains
only a username and app policy, not credentials. Installation refuses unsafe
symlinked destinations and validates the completed configuration and plist
before bootstrapping the service.

## Configuration

The repository contains `config.example.yaml`; it does not contain a real
`config.yaml`. The latter is ignored by Git. The installed configuration is:

```yaml
version: 1
user: example-user
check_interval_seconds: 45
termination_grace_seconds: 5

rules:
  - id: facetime
    name: FaceTime
    bundle_id: com.apple.FaceTime
    process_name: FaceTime
    block:
      from: "20:00"
      until: "07:00"

  - id: chrome
    name: Google Chrome
    bundle_id: com.google.Chrome
    process_name: Google Chrome
    block:
      from: "21:00"
      until: "07:00"
```

Validation is strict and reports field paths. Required values include version
`1`, a non-empty username, at least one rule, unique lowercase CLI-safe rule
IDs, a bundle ID, a process-name fallback, and `HH:MM` endpoints. Intervals are
bounded to 30–60 seconds and termination grace to 1–30 seconds. Unknown keys
are rejected so misspellings cannot silently weaken enforcement.

The configured username is resolved with the system account database each time
configuration is activated. Numeric UIDs and home-directory paths are never
stored. A missing account is an actionable error and no processes are touched.

At startup, malformed configuration leaves the daemon alive but inactive and
logs the validation error. After one valid configuration has been loaded, a
malformed edit retains the last known-good policy and emits one transition log
instead of weakening enforcement. The daemon checks file metadata each cycle;
valid edits are adopted without requiring a manual reload.

## Schedule Semantics

Time windows use the Mac's current local wall clock. The scheduler constructs
timezone-aware current timestamps on every cycle, so timezone and DST changes
are picked up without restart. A normal interval blocks when
`from <= local_time < until`. An overnight interval blocks when
`local_time >= from or local_time < until`. Equal endpoints are rejected rather
than interpreted as either zero hours or 24 hours. The `from` boundary is
inclusive and the `until` boundary is exclusive.

Spring-forward nonexistent wall times simply have no matching instant. During
a fall-back repeated hour, the same wall-clock rule applies in both folds. This
is predictable for availability schedules and is covered by timezone-sensitive
tests. Up to the 45-second reconciliation interval is expected between a wall
clock boundary or app launch and enforcement.

## Mutable State and Temporary Overrides

Permanent policy and operational state are separate. `state.json` has a schema
version, an `enabled` boolean, and a mapping of rule IDs to UTC expiry
timestamps. CLI and daemon writes take an advisory lock and use an atomic
same-directory replacement with explicit root-only permissions.

An active override permits its rule even when the permanent schedule blocks it.
Durations use a positive integer plus `s`, `m`, `h`, or `d`, for example `30m`.
Expiry is an absolute UTC instant, so it survives daemon restart and reboot.
Expired overrides are ignored immediately and pruned during reconciliation.
Unknown rule IDs are rejected. Overrides do not modify YAML.

Malformed or unreadable state is treated as `enabled: true` with no overrides,
which preserves permanent enforcement instead of silently disabling it. The
daemon logs the error and leaves the damaged file in place for diagnosis; an
administrator must repair it with a CLI state-changing command.

Disabling the scheduler changes `enabled` to false but leaves the supervised
daemon running. This makes status and re-enabling dependable and avoids
confusing launchd's persistent disabled-service state with product policy.

## Process Identity and Termination

Every candidate process must be owned by the resolved target UID. The Darwin
adapter enumerates PIDs and UIDs, obtains executable paths with supported macOS
process APIs, and inspects every enclosing `.app` bundle's `Contents/Info.plist`.
A process matches a rule when an enclosing bundle has the configured bundle ID.
This associates nested Chrome Helper app bundles with the outer Google Chrome
bundle. The configured process name is a documented fallback for the main
process when bundle metadata cannot be read; fallback matching is exact, not a
substring or shell-command-line match.

The daemon never shells out to `pkill` with user-provided text. It rescans and
revalidates UID and app identity immediately before each signal to limit PID
reuse races.

For a blocked rule, termination proceeds as follows:

1. Identify matching main processes using both bundle identity and exact
   configured process name.
2. Send `SIGTERM` to those main processes and log one concise enforcement event.
3. Wait up to the configured grace period, checking whether matching processes
   remain.
4. Rescan and send `SIGKILL` only to remaining processes that still match the
   target UID and bundle, including helpers orphaned by an unresponsive main
   process.

Sending `SIGTERM` to Chrome's main process is the best non-GUI-session graceful
request available to the root daemon and normally allows profile/session state
to be written. It cannot guarantee tab recovery if Chrome is hung or requires
`SIGKILL`. This behavior must be validated on the target macOS and Chrome
versions before relying on it.

An app that is absent, not installed, or not running requires no action and
does not create a per-cycle log. When the target user is logged out there will
normally be no matching GUI processes; enforcement resumes automatically when
matching processes appear.

## CLI

The checkout bootstrap is:

```bash
sudo ./scripts/install.sh --user alice
```

After installation, the supported administration commands are:

```bash
sudo mac-app-scheduler install --user alice
sudo mac-app-scheduler status
sudo mac-app-scheduler enable
sudo mac-app-scheduler disable
sudo mac-app-scheduler reload
sudo mac-app-scheduler allow facetime 30m
sudo mac-app-scheduler dry-run
sudo mac-app-scheduler uninstall
```

`install` repairs or updates an installation from a source checkout when its
packaged assets are available. The shell installer remains the canonical first
install because the CLI does not yet exist in `/usr/local/bin`. Mutating and
process-inspecting commands require root and fail with a direct `sudo` hint.

`status` reports launchd state, scheduler enabled state, target-account
resolution, configuration validity, each rule's current effective state and
next wall-clock transition, and active overrides. It does not expose unrelated
processes. `reload` validates configuration before using
`launchctl kickstart -k system/io.github.mac-app-scheduler.daemon`; install and
uninstall use modern `bootstrap` and `bootout` operations. `dry-run` executes one
reconciliation through the real process inventory but prints actions rather
than sending signals.

## Installation and Removal

The installer:

1. Requires root and macOS 13 or newer.
2. Resolves and validates the requested non-root, non-administrator account.
3. Locates an explicit or `PATH` Python 3.11+ and records its actual path in
   diagnostics.
4. Creates a staging virtual environment, installs the local package and pinned
   compatible runtime dependencies, then moves the completed installation into
   place.
5. Installs the wrapper, example-derived configuration, and plist with explicit
   `root:wheel` ownership and modes. Existing configuration and state are
   preserved on repair/update.
6. Validates YAML and runs `plutil -lint` before changing launchd state.
7. Uses `launchctl bootstrap system <plist>` and prints `launchctl print`
   diagnostics on failure. It does not use deprecated `load` or `unload`.

Rollback keeps the prior installation until the staged replacement validates.
Installation errors identify the failed precondition and leave existing policy
intact. The initial dependency download is a documented network and
supply-chain boundary; release packaging may later provide prebuilt, hashed
wheels without changing runtime architecture.

Uninstall first uses `launchctl bootout system/<label>` when loaded, then removes
only the exact installed executable, library directory, plist, and application
support directory. It is idempotent, reports what was removed, and warns before
removing configuration/state. The README also documents a manual recovery path.

## Logging and Failures

Logs cover daemon startup and shutdown, configuration activation/failure,
effective rule transitions, processes terminated, forced termination,
temporary override creation/expiry, enable/disable transitions, and unexpected
exceptions. Successful no-op cycles are silent. Repeated identical errors are
deduplicated until their condition changes.

The implementation emits sparse records to macOS system logging and keeps
stderr useful as a fallback; launchd captures stderr in a root-owned diagnostic
log. CLI documentation includes `log show` and fallback-log commands.
Usernames appear only when needed to diagnose account resolution, and command
lines, document names, browser URLs, and unrelated process details are never
logged.

One failed rule does not prevent evaluation of other rules. Unexpected loop
errors are caught, logged, and retried on the next interval. A process adapter
failure fails safe by taking no destructive action for the affected inventory;
it never broadens matching.

## Security Model

The utility prevents a standard user from casually bypassing configured app
schedules. Root ownership prevents that user from editing policy, state,
installed code, or the LaunchDaemon. UID filtering prevents similarly named
processes belonging to another user from being terminated. Exact identity
matching and rescan-before-signal reduce unintended termination.

It is not MDM, malware protection, a hardened security boundary, or a complete
replacement for Screen Time. Administrators can change or remove it. V1 does
not stop alternate browsers, web versions of services, renamed or extracted
executables that evade both bundle and process-name identity, scripts invoking
equivalent services, virtual machines, Safe Mode/recovery changes, SIP changes,
or other deliberate technical bypasses. These limits are explicit in the
README rather than implied guarantees.

## Test Strategy

Pure modules contain configuration validation, interval evaluation, effective
rule state, override expiry, transition calculation, and bundle-path matching.
They run without macOS or root privileges. Unit tests cover:

- same-day and overnight windows;
- exact inclusive/exclusive boundaries;
- multiple independent rules;
- active, expired, and reboot-persisted temporary overrides;
- malformed YAML and every schema constraint;
- missing users through an injected account resolver;
- UID, bundle-ID, nested-helper, exact-name fallback, and non-match cases;
- disabled scheduling;
- DST spring-forward and fall-back behavior with `zoneinfo`;
- state locking/atomic persistence and malformed state recovery;
- graceful termination, escalation, and rescan behavior through a fake process
  adapter;
- configuration reload retaining the last known-good policy.

macOS-focused tests validate executable-path discovery, plist syntax, and shell
scripts without terminating GUI apps. GitHub Actions runs pytest and Ruff on
Python 3.11–3.13, with Linux for portable logic and macOS for Darwin-specific
checks. Shell scripts receive `bash -n`; the LaunchDaemon receives
`plutil -lint` on macOS.

Tests are written before production behavior and observed failing for the
intended reason. Each component keeps macOS calls behind narrow interfaces so
tests assert decisions and observable effects rather than mock call trivia.

## Real-Mac Validation

Automated tests cannot prove launchd bootstrap, login-session process
visibility, signal behavior of each third-party app, sleep/wake timing, or
Chrome session restoration. The README's recommended smoke test uses temporary
near-future rules and a disposable browser profile where practical:

1. Validate config and run `dry-run`.
2. Bootstrap and inspect daemon status/logs.
3. Test FaceTime and Chrome while already open at a near-future cutoff.
4. Reopen each blocked app and confirm closure within 45 seconds.
5. Confirm an allow override, its expiry, disable, and enable.
6. Test logout/login, reboot, and sleep across a cutoff.
7. Confirm another user's similarly named processes are untouched.
8. Confirm Chrome offers normal session restoration after graceful termination.
9. Exercise uninstall and verify the exact installed paths and service are gone.

Any failure in process identity or cross-session enforcement blocks production
use until diagnosed; the utility must not substitute broader name-based kills.
