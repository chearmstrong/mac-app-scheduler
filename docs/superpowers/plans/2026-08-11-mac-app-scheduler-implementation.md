# Mac App Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a root-administered macOS daemon and CLI that continuously enforces per-app blocking windows for one standard user.

**Architecture:** Pure dataclass-based configuration, schedule, and state modules feed a reconciliation service through a narrow process-adapter protocol. A Darwin adapter identifies only the configured user's app-bundle processes and signals them safely; a long-running launchd-supervised daemon invokes reconciliation every 45 seconds by default.

**Tech Stack:** Python 3.11+, PyYAML, pytest, Ruff, stdlib `argparse`, `plistlib`, `pwd`, `fcntl`, `signal`, `subprocess`, and macOS `libproc` via `ctypes`.

## Global Constraints

- Support macOS 13 Ventura or newer on Apple silicon and Intel.
- Require administrator-installed Python 3.11+; never depend on `/usr/bin/python3`.
- Manage exactly one configured non-administrator user in V1.
- Use bundle IDs as primary app identity and exact process names only as fallback.
- Filter every process action by the runtime-resolved target UID.
- Reconcile at a configurable 30–60 second interval; default to 45 seconds.
- Send `SIGTERM`, wait 1–30 seconds, rescan identity, then send `SIGKILL` only when necessary.
- Keep permanent config separate from root-only operational state and overrides.
- Do not commit usernames, numeric UIDs, `/Users/<name>` paths, or real machine config.
- Use test-first red/green/refactor cycles for production behavior.
- Do not perform privileged install or real GUI-app termination in automated tests.

---

## File Map

```text
src/mac_app_scheduler/
  __init__.py       package version
  __main__.py       python -m entry point
  models.py         immutable policy/state/process value objects
  config.py         strict YAML parsing and account resolution
  schedule.py       local-wall-clock interval and transition calculations
  state.py          locked, atomic operational-state persistence
  processes.py      ProcessAdapter protocol and Darwin implementation
  enforcer.py       one desired-state reconciliation pass
  daemon.py         lifecycle, config reload, signals, and sparse logging
  launchd.py        launchctl query/restart helpers
  cli.py            administrator command UX
tests/               portable and Darwin-specific pytest coverage
launchd/             plist template
scripts/             bootstrap installer and standalone uninstall shim
```

Each module owns one policy boundary. `schedule.py` never reads files;
`enforcer.py` never invokes launchctl; `processes.py` never decides whether a
rule is blocked; and `cli.py` delegates persistence and evaluation.

### Task 1: Project Foundation and Strict Configuration

**Files:**
- Create: `pyproject.toml`
- Create: `src/mac_app_scheduler/__init__.py`
- Create: `src/mac_app_scheduler/models.py`
- Create: `src/mac_app_scheduler/config.py`
- Create: `tests/test_config.py`
- Create: `.gitignore`
- Create: `config.example.yaml`

**Interfaces:**
- Produces: `TimeWindow(start: datetime.time, until: datetime.time)`, `Rule(id, name, bundle_id, process_name, block)`, and `SchedulerConfig(version, user, check_interval_seconds, termination_grace_seconds, rules)` frozen dataclasses.
- Produces: `ConfigError`, `load_config(path: Path) -> SchedulerConfig`, `parse_config(text: str) -> SchedulerConfig`, and `resolve_user(username: str) -> pwd.struct_passwd`.

- [ ] **Step 1: Add packaging and lint/test configuration**

Use a `src` layout, console script `mac-app-scheduler = mac_app_scheduler.cli:main`,
runtime dependency `PyYAML>=6.0.2,<7`, and dev dependencies pytest and Ruff.
Configure Ruff for Python 3.11, line length 88, and rules `E`, `F`, `I`, `B`,
`UP`, and `SIM`. Add the neutral FaceTime/Chrome example from the design as
`config.example.yaml`, and ignore only the root-level real `config.yaml` plus
standard Python build, cache, coverage, and virtual-environment artifacts.

- [ ] **Step 2: Write failing configuration tests**

```python
def test_parse_config_accepts_documented_schema():
    config = parse_config(VALID_CONFIG)
    assert config.user == "example-user"
    assert [rule.id for rule in config.rules] == ["facetime", "chrome"]

@pytest.mark.parametrize("replacement, field", [
    ("from: '20:00'", "block.until"),
    ("check_interval_seconds: 10", "check_interval_seconds"),
    ("surprise: true", "surprise"),
])
def test_parse_config_rejects_malformed_fields(replacement, field):
    with pytest.raises(ConfigError, match=field):
        parse_config(make_invalid_config(replacement))

def test_resolve_user_reports_missing_account(monkeypatch):
    monkeypatch.setattr(pwd, "getpwnam", Mock(side_effect=KeyError))
    with pytest.raises(ConfigError, match="does not exist"):
        resolve_user("missing")
```

- [ ] **Step 3: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_config.py -q`

Expected: collection fails because `mac_app_scheduler.config` does not exist.

- [ ] **Step 4: Implement immutable models and strict parsing**

Use `yaml.safe_load`, explicit allowed-key sets at every mapping level, exact
`%H:%M` parsing, unique IDs matching `^[a-z0-9][a-z0-9-]*$`, non-empty strings,
unequal endpoints, and range checks. Catch YAML/type/value errors and raise a
single `ConfigError` whose message includes the field path.

- [ ] **Step 5: Run tests and Ruff, then refactor while green**

Run: `python3 -m pytest tests/test_config.py -q && python3 -m ruff check src tests`

Expected: all configuration tests pass and Ruff reports no errors.

- [ ] **Step 6: Commit the independently working configuration slice**

```bash
git add pyproject.toml .gitignore config.example.yaml src/mac_app_scheduler tests/test_config.py
git commit -m "feat(config): add strict scheduler configuration"
```

### Task 2: Scheduling and Effective Rule State

**Files:**
- Create: `src/mac_app_scheduler/schedule.py`
- Create: `tests/test_schedule.py`

**Interfaces:**
- Consumes: `TimeWindow`, `Rule`, and aware `datetime` values.
- Produces: `is_in_window(window: TimeWindow, now: datetime) -> bool`, `is_rule_blocked(rule: Rule, now: datetime, override_until: datetime | None) -> bool`, and `next_transition(rule: Rule, now: datetime) -> datetime`.

- [ ] **Step 1: Write failing schedule tests**

```python
@pytest.mark.parametrize(("clock", "blocked"), [
    ("19:59", False), ("20:00", True), ("23:59", True),
    ("00:00", True), ("06:59", True), ("07:00", False),
])
def test_overnight_window_boundaries(clock, blocked):
    assert is_in_window(window("20:00", "07:00"), local_datetime(clock)) is blocked

def test_active_override_permits_blocked_rule():
    now = aware("2026-08-11T21:00:00+01:00")
    assert not is_rule_blocked(FACETIME, now, now.astimezone(UTC) + timedelta(minutes=30))

def test_fall_back_hour_uses_wall_clock_in_both_folds():
    zone = ZoneInfo("Europe/London")
    first = datetime(2026, 10, 25, 1, 30, tzinfo=zone, fold=0)
    second = datetime(2026, 10, 25, 1, 30, tzinfo=zone, fold=1)
    assert is_in_window(window("01:00", "02:00"), first)
    assert is_in_window(window("01:00", "02:00"), second)
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_schedule.py -q`

Expected: import failure for `mac_app_scheduler.schedule`.

- [ ] **Step 3: Implement wall-clock comparisons and transitions**

Compare `now.timetz().replace(tzinfo=None)` to endpoints. For transitions,
construct local candidates for today's and tomorrow's endpoints, preserve the
current timezone object, and choose the first candidate strictly after `now`.
Override comparison converts both values to UTC.

- [ ] **Step 4: Run targeted and full tests**

Run: `python3 -m pytest tests/test_schedule.py -q && python3 -m pytest -q`

Expected: all tests pass, including normal windows, overnight windows, exact
boundaries, override expiry, spring-forward, fall-back, and multiple rules.

- [ ] **Step 5: Commit**

```bash
git add src/mac_app_scheduler/schedule.py tests/test_schedule.py
git commit -m "feat(schedule): evaluate app blocking windows"
```

### Task 3: Root-Only Operational State

**Files:**
- Modify: `src/mac_app_scheduler/models.py`
- Create: `src/mac_app_scheduler/state.py`
- Create: `tests/test_state.py`

**Interfaces:**
- Produces: `SchedulerState(version: int = 1, enabled: bool = True, overrides: Mapping[str, datetime])`.
- Produces: `StateStore(path: Path, lock_path: Path)` with `read()`, `update(transform)`, `set_enabled(bool)`, `set_override(rule_id, expiry)`, and `prune_expired(now)`.
- Produces: `parse_duration(value: str) -> timedelta`.

- [ ] **Step 1: Write failing persistence tests**

```python
def test_missing_state_defaults_to_enabled(tmp_path):
    state = store(tmp_path).read()
    assert state.enabled is True
    assert state.overrides == {}

def test_malformed_state_fails_closed_without_overwriting(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json")
    assert store(tmp_path).read() == SchedulerState()
    assert path.read_text() == "not json"

def test_override_round_trips_as_utc_and_expires(tmp_path):
    expiry = datetime(2026, 8, 11, 20, 30, tzinfo=UTC)
    state_store = store(tmp_path)
    state_store.set_override("facetime", expiry)
    assert state_store.read().overrides["facetime"] == expiry
    assert state_store.prune_expired(expiry).overrides == {}
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_state.py -q`

Expected: import failure for `mac_app_scheduler.state`.

- [ ] **Step 3: Implement locked atomic state updates**

Use `fcntl.flock(LOCK_EX)`, a same-directory `NamedTemporaryFile`, `flush`,
`os.fsync`, `os.chmod(0o600)`, and `os.replace`. Parse only version 1, booleans,
and UTC-aware ISO 8601 expiry values. Treat all read errors as default-enabled
state while retaining an error property for logging.

- [ ] **Step 4: Add duration tests and implementation**

Test positive `30s`, `30m`, `2h`, and `1d`; reject zero, negatives, decimals,
missing units, and unknown units with `ValueError`. Implement with one anchored
regular expression and checked multipliers.

- [ ] **Step 5: Run tests, inspect file modes, and commit**

Run: `python3 -m pytest tests/test_state.py -q && python3 -m ruff check src tests`

```bash
git add src/mac_app_scheduler/models.py src/mac_app_scheduler/state.py tests/test_state.py
git commit -m "feat(state): persist scheduler overrides safely"
```

### Task 4: macOS Process Inventory and App Matching

**Files:**
- Modify: `src/mac_app_scheduler/models.py`
- Create: `src/mac_app_scheduler/processes.py`
- Create: `tests/test_processes.py`

**Interfaces:**
- Produces: `ProcessInfo(pid: int, uid: int, name: str, executable: Path | None, bundle_ids: frozenset[str])`.
- Produces: `ProcessAdapter` protocol with `list_processes(uid)`, `refresh(pid, uid)`, and `send_signal(pid, sig)`.
- Produces: `DarwinProcessAdapter`, `bundle_ids_for_executable(path: Path) -> frozenset[str]`, `matches_rule(process, rule, expected_uid, allow_name_fallback=True) -> bool`, and `is_main_process(process, rule) -> bool`.

- [ ] **Step 1: Write failing pure matching tests**

```python
def test_nested_chrome_helper_matches_outer_bundle(tmp_path):
    executable = make_nested_helper_bundle(tmp_path, outer_id="com.google.Chrome")
    assert "com.google.Chrome" in bundle_ids_for_executable(executable)

def test_matching_requires_target_uid_before_identity():
    process = proc(uid=502, bundle_ids={"com.apple.FaceTime"})
    assert not matches_rule(process, FACETIME, expected_uid=501)

def test_name_fallback_is_exact_and_main_process_only():
    assert matches_rule(proc(name="FaceTime", executable=None), FACETIME, expected_uid=501)
    assert not matches_rule(proc(name="FaceTimeHelper", executable=None), FACETIME, expected_uid=501)
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_processes.py -q`

Expected: import failure for `mac_app_scheduler.processes`.

- [ ] **Step 3: Implement pure bundle traversal and matching**

Walk executable parents, inspect each `<name>.app/Contents/Info.plist` with
`plistlib`, collect every valid `CFBundleIdentifier`, and never follow a missing
or malformed plist into a broader match. Require expected UID inside the matcher.

- [ ] **Step 4: Add Darwin adapter tests and implementation**

On macOS, bind `proc_listallpids`, `proc_pidinfo(PROC_PIDTBSDINFO)`, and
`proc_pidpath` from `/usr/lib/libproc.dylib` with explicit `ctypes` signatures.
Use the BSD info UID and short name; tolerate processes disappearing or denying
inspection. On non-Darwin systems, constructing `DarwinProcessAdapter` raises
`UnsupportedPlatformError`.

Test adapter parsing with an injected libproc wrapper. Mark one smoke test
`@pytest.mark.skipif(sys.platform != "darwin", ...)` and assert it can inspect
the current process without signals.

- [ ] **Step 5: Run portable and macOS-conditional tests, then commit**

Run: `python3 -m pytest tests/test_processes.py -q && python3 -m ruff check src tests`

```bash
git add src/mac_app_scheduler/models.py src/mac_app_scheduler/processes.py tests/test_processes.py
git commit -m "feat(processes): identify macOS app bundle processes"
```

### Task 5: Desired-State Reconciliation and Termination Escalation

**Files:**
- Create: `src/mac_app_scheduler/enforcer.py`
- Create: `tests/test_enforcer.py`

**Interfaces:**
- Consumes: config, state, aware current time, and `ProcessAdapter`.
- Produces: `Action(rule_id, pid, signal, reason)`, `ReconcileResult(actions, transitions, errors)`, and `Enforcer.reconcile(config, state, now, dry_run=False) -> ReconcileResult`.

- [ ] **Step 1: Write failing reconciliation tests with a deterministic fake adapter**

```python
def test_blocked_running_app_gets_term_then_no_kill_if_it_exits():
    adapter = FakeAdapter([facetime_main()])
    adapter.remove_on_signal(signal.SIGTERM)
    result = enforcer(adapter).reconcile(BLOCKED_CONFIG, ENABLED, NOW)
    assert [(a.pid, a.signal) for a in result.actions] == [(101, signal.SIGTERM)]

def test_unresponsive_app_is_rescanned_before_kill():
    adapter = FakeAdapter([chrome_main(), chrome_helper()])
    result = enforcer(adapter, sleep=lambda _: None).reconcile(BLOCKED_CONFIG, ENABLED, NOW)
    assert adapter.signals == [(201, signal.SIGTERM), (201, signal.SIGKILL), (202, signal.SIGKILL)]

def test_disabled_override_and_other_uid_never_signal():
    adapter = FakeAdapter([facetime_main(uid=502)])
    enforcer(adapter).reconcile(BLOCKED_CONFIG, DISABLED, NOW)
    assert adapter.signals == []
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_enforcer.py -q`

Expected: import failure for `mac_app_scheduler.enforcer`.

- [ ] **Step 3: Implement one-pass reconciliation**

Evaluate each rule independently. Inventory once for graceful candidates, send
`SIGTERM` only to bundle-matched main processes (or exact-name fallback main),
wait once when any signal was sent, then call `refresh`/reinventory and reapply
UID plus bundle identity before `SIGKILL`. Never force-kill a name-only fallback
process because helper ownership cannot be established safely. In dry-run mode,
record planned TERM actions and do not sleep, rescan, or signal.

- [ ] **Step 4: Cover multiple rules and adapter failures**

Add tests showing one rule's inventory/signal error appears in `errors` without
preventing another rule, duplicate matching PIDs are signalled once, missing apps
are silent, and active/expired overrides have the expected effect.

- [ ] **Step 5: Run full suite and commit**

Run: `python3 -m pytest -q && python3 -m ruff check src tests`

```bash
git add src/mac_app_scheduler/enforcer.py tests/test_enforcer.py
git commit -m "feat(enforcer): reconcile blocked app processes"
```

### Task 6: Daemon Lifecycle, Reloading, and Sparse Logging

**Files:**
- Create: `src/mac_app_scheduler/daemon.py`
- Create: `tests/test_daemon.py`

**Interfaces:**
- Produces: `ConfigMonitor(path, loader)` retaining `last_good` and file signature.
- Produces: `EventLogger` that deduplicates stable repeated conditions.
- Produces: `run_daemon(paths, *, clock, sleeper, adapter) -> int` and `run_once(..., dry_run) -> ReconcileResult`.

- [ ] **Step 1: Write failing config-reload and loop tests**

```python
def test_invalid_edit_keeps_last_good_config(tmp_path):
    monitor = configured_monitor(tmp_path)
    good = monitor.current()
    monitor.path.write_text("bad: [")
    assert monitor.current() is good
    assert monitor.error is not None

def test_startup_reconciles_before_sleep_and_sigterm_stops_loop():
    sleeper = StoppingSleeper()
    run_daemon(TEST_PATHS, clock=fixed_clock, sleeper=sleeper, adapter=adapter)
    assert adapter.inventory_calls == 1
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_daemon.py -q`

Expected: import failure for `mac_app_scheduler.daemon`.

- [ ] **Step 3: Implement lifecycle and error-transition logging**

Install handlers for `SIGTERM` and `SIGINT`, use `threading.Event.wait(interval)`
so shutdown interrupts waiting, reload only when `(st_dev, st_ino, st_size,
st_mtime_ns)` changes, retain last-good config, and catch unexpected per-cycle
errors. Emit startup/shutdown, config state changes, effective rule transitions,
termination, forced termination, overrides, and unexpected failures; suppress
identical repeated messages.

Send sparse events through `/usr/bin/logger -t mac-app-scheduler -- <message>`
using fixed arguments and no shell. If logger fails, write the same record to
stderr. Never include command lines or unrelated process metadata.

- [ ] **Step 4: Run daemon tests and full suite**

Run: `python3 -m pytest tests/test_daemon.py -q && python3 -m pytest -q`

- [ ] **Step 5: Commit**

```bash
git add src/mac_app_scheduler/daemon.py tests/test_daemon.py
git commit -m "feat(daemon): supervise continuous enforcement"
```

### Task 7: CLI and launchctl Administration

**Files:**
- Create: `src/mac_app_scheduler/launchd.py`
- Create: `src/mac_app_scheduler/cli.py`
- Create: `src/mac_app_scheduler/__main__.py`
- Create: `tests/test_cli.py`
- Create: `tests/test_launchd.py`

**Interfaces:**
- Produces: `LaunchdClient.status()`, `kickstart()`, `bootstrap(plist)`, and `bootout()` using argument arrays and captured diagnostics.
- Produces: `build_parser() -> argparse.ArgumentParser` and `main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_allow_writes_utc_override_for_known_rule(cli):
    result = cli.run("allow", "facetime", "30m", now=NOW)
    assert result.exit_code == 0
    assert cli.state.read().overrides["facetime"] == NOW + timedelta(minutes=30)

def test_mutating_command_requires_root(cli):
    result = cli.run("disable", euid=501)
    assert result.exit_code == 2
    assert "sudo" in result.stderr

def test_dry_run_never_signals(cli):
    result = cli.run("dry-run")
    assert "would terminate" in result.stdout
    assert cli.adapter.signals == []
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_cli.py tests/test_launchd.py -q`

Expected: import failures for CLI and launchd modules.

- [ ] **Step 3: Implement launchctl wrapper without shell invocation**

Use `/bin/launchctl print system/io.github.mac-app-scheduler.daemon`,
`kickstart -k`, `bootstrap system <plist>`, and `bootout
system/io.github.mac-app-scheduler.daemon`. Normalize “not found/not loaded” as
an idempotent state only for status and bootout; preserve stdout, stderr, and
exit codes for actionable errors.

- [ ] **Step 4: Implement CLI commands**

Add internal `daemon`, `validate-config`, and `render-config` commands plus public `install`,
`status`, `enable`, `disable`, `reload`, `allow`, `dry-run`, and `uninstall`.
Inject paths, clock, EUID getter, state store, adapter, and launchd client into
command handlers for tests. `status` prints config/account/service validity,
effective states, next transitions, and active overrides. `reload` validates
before kickstart. `install`/`uninstall` exec the packaged scripts with the
original argument array and never construct a shell string. `render-config`
safe-loads the example, changes only its `user` value, safe-dumps to a requested
path, and validates the result; the installer uses it instead of text substitution.

- [ ] **Step 5: Test help/error UX and run full suite**

Run: `python3 -m pytest tests/test_cli.py tests/test_launchd.py -q && python3 -m pytest -q && python3 -m ruff check src tests`

- [ ] **Step 6: Commit**

```bash
git add src/mac_app_scheduler/cli.py src/mac_app_scheduler/launchd.py src/mac_app_scheduler/__main__.py tests/test_cli.py tests/test_launchd.py
git commit -m "feat(cli): add scheduler administration commands"
```

### Task 8: LaunchDaemon and Safe Installer/Uninstaller

**Files:**
- Create: `launchd/io.github.mac-app-scheduler.daemon.plist`
- Create: `scripts/install.sh`
- Create: `scripts/uninstall.sh`
- Create: `tests/test_install_scripts.py`

**Interfaces:**
- Installer accepts `--user USER` and optional `--python /absolute/path`.
- Installer calls installed `validate-config`, installs the plist, then bootstraps.
- Uninstaller accepts `--yes` for non-interactive removal; otherwise confirms removal of config/state.

- [ ] **Step 1: Write failing static and fake-root installer tests**

```python
def test_plist_uses_keepalive_without_start_interval():
    plist = plistlib.loads(PLIST.read_bytes())
    assert plist["KeepAlive"] is True
    assert "StartInterval" not in plist
    assert plist["ProgramArguments"][-1] == "daemon"

def test_scripts_avoid_deprecated_launchctl_commands():
    text = INSTALL.read_text() + UNINSTALL.read_text()
    assert "launchctl load" not in text
    assert "launchctl unload" not in text
    assert "launchctl bootstrap" in text
    assert "launchctl bootout" in text
```

Use command-path injection variables only under `MAC_APP_SCHEDULER_TESTING=1`
to test preflight and command ordering in a temporary fake root without sudo.

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_install_scripts.py -q`

Expected: missing plist and scripts.

- [ ] **Step 3: Implement the LaunchDaemon plist**

Set label, absolute venv console-script path plus `daemon`, `KeepAlive=true`,
`ProcessType=Background`, `LowPriorityIO=true`, `Umask=022`, and root-owned
stdout/stderr fallback paths. Do not set `UserName`, `StartInterval`,
`StartCalendarInterval`, or redundant `RunAtLoad`.

- [ ] **Step 4: Implement installer preflight and rollback**

Use `#!/bin/bash`, `set -euo pipefail`, absolute system command paths where
stable, and `umask 022`. Validate root, Darwin, major version >= 22 (Ventura),
Python >= 3.11, account existence, UID >= 500, nonzero UID, and absence from the
admin group. Reject symlinked install destinations.

Stage source/config/plist assets under `mktemp -d`, preserve existing config and
state, move an existing libexec directory to an explicit backup, then create
the venv at `/usr/local/libexec/mac-app-scheduler/venv` and run `pip install`
against the staged source. Restore the backup on any failure before launchd is
changed. Render the configured username through the installed Python validation
helper, not `sed`. Apply `root:wheel` ownership and exact modes, run
`validate-config` and `plutil -lint`, then bootstrap or bootout/bootstrap when
updating. Print `launchctl print` diagnostics on failure.

- [ ] **Step 5: Implement exact, idempotent uninstall**

Confirm before deleting application support unless `--yes`; boot out the exact
label; remove only the exact wrapper, libexec directory, plist, diagnostic log,
and (after confirmation) support directory. Never use an unresolved variable,
glob, home directory, or workspace root as a deletion target.

- [ ] **Step 6: Validate scripts without installing**

Run: `bash -n scripts/install.sh scripts/uninstall.sh && plutil -lint launchd/io.github.mac-app-scheduler.daemon.plist && python3 -m pytest tests/test_install_scripts.py -q`

- [ ] **Step 7: Commit**

```bash
git add launchd scripts tests/test_install_scripts.py
git commit -m "feat(install): add launchd system installation"
```

### Task 9: Example Policy and Beginner-Friendly Documentation

**Files:**
- Modify: `config.example.yaml`
- Create: `README.md`
- Create: `LICENSE`
- Modify: `.gitignore`
- Create: `tests/test_repository_contract.py`

**Interfaces:**
- Documents the public install/config/CLI contract and real-Mac smoke-test gate.
- Provides the FaceTime 20:00 and Chrome 21:00 example with neutral username.

- [ ] **Step 1: Write failing repository-contract tests**

```python
def test_example_config_is_valid_and_machine_neutral():
    text = EXAMPLE.read_text()
    assert "/Users/" not in text
    assert load_config(EXAMPLE).rules[0].id == "facetime"

def test_real_config_is_ignored():
    assert "config.yaml" in GITIGNORE.read_text().splitlines()

def test_readme_contains_required_operator_sections():
    for heading in REQUIRED_HEADINGS:
        assert heading in README.read_text()
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `python3 -m pytest tests/test_repository_contract.py -q`

Expected: missing example, README, and license.

- [ ] **Step 3: Add example config, MIT license, and ignore rules**

Use only `example-user`; include no numeric UID or home path. Ignore root
`config.yaml`, virtual environments, caches, coverage, build output, and editor
metadata without hiding the committed example.

- [ ] **Step 4: Write the README as an operator guide**

Lead with “A lightweight macOS utility for enforcing per-app availability
schedules.” Explain that this is a daemon/CLI, not an Xcode app. Include:
problem/example, support and Python recommendation, architecture, why KeepAlive,
install, config schema and time semantics, every CLI command, overrides,
status/log commands, config editing/reload, file ownership, uninstall/manual
recovery, security model, known bypasses, development/tests, and the nine-step
real-Mac smoke checklist. Label unverified Mac behaviors, especially Chrome
session restoration and cross-login-session process visibility.

- [ ] **Step 5: Run documentation contract and machine-data scans**

Run: `python3 -m pytest tests/test_repository_contract.py -q && ! rg -n '/Users/|user: (?!example-user)' README.md config.example.yaml --pcre2`

- [ ] **Step 6: Commit**

```bash
git add README.md LICENSE config.example.yaml .gitignore tests/test_repository_contract.py
git commit -m "docs: add setup and security guidance"
```

### Task 10: CI and Final Verification

**Files:**
- Create: `.github/workflows/test.yml`
- Modify: `pyproject.toml`

**Interfaces:**
- CI runs portable tests/lint on Ubuntu and Darwin-specific validation on macOS.

- [ ] **Step 1: Add the GitHub Actions workflow**

Create an Ubuntu matrix for Python 3.11, 3.12, and 3.13 running
`pip install -e '.[dev]'`, `ruff check`, `ruff format --check`, and `pytest`.
Create one macOS Python 3.13 job running the same commands plus `bash -n` and
`plutil -lint`. Use least-privilege `contents: read`, action version pins, pip
caching, and no privileged installer execution.

- [ ] **Step 2: Run the complete local verification from a clean environment**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m pytest -ra
bash -n scripts/install.sh scripts/uninstall.sh
plutil -lint launchd/io.github.mac-app-scheduler.daemon.plist
git diff --check
```

Expected: every command exits 0, with no failed tests or lint errors.

- [ ] **Step 3: Audit the approved design requirement by requirement**

Check every section of
`docs/superpowers/specs/2026-08-11-mac-app-scheduler-design.md` against code,
tests, example config, installer, and README. Record any real-Mac-only claim as
unverified rather than implying automated coverage.

- [ ] **Step 4: Inspect the final diff for machine-specific or sensitive data**

Run: `git status --short && git diff --check && ! rg -n '/Users/|user: (?!example-user)|[0-9]{3,5}:[0-9]{2}' --pcre2 --glob '!docs/superpowers/**' .`

Review any numeric-match false positives manually; the scan must find no real
username, child name, UID, or home path.

- [ ] **Step 5: Commit CI after verification**

```bash
git add .github/workflows/test.yml pyproject.toml
git commit -m "ci: test scheduler on Linux and macOS"
```

- [ ] **Step 6: Hand off real-Mac validation**

Report the architecture, files added, exact install/test commands, automated
results, assumptions still requiring target-machine validation, limitations,
and the ordered smoke-test checklist. Do not claim production readiness until
the real-Mac process-termination and sleep/login tests pass.
