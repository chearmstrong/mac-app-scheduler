from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mac_app_scheduler.daemon import ConfigMonitor, Daemon
from mac_app_scheduler.state import StateStore

VALID = """
version: 1
user: example-user
check_interval_seconds: 45
termination_grace_seconds: 5
rules:
  - id: facetime
    name: FaceTime
    bundle_id: com.apple.FaceTime
    process_name: FaceTime
    block: {from: "20:00", until: "07:00"}
"""


def test_invalid_edit_keeps_last_good_configuration(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    monitor = ConfigMonitor(path)
    good = monitor.current()

    path.write_text("rules: [")

    assert monitor.current() is good
    assert monitor.error is not None


def test_invalid_utf8_edit_keeps_last_good_policy(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    monitor = ConfigMonitor(path)
    good = monitor.current()
    path.write_bytes(b"\xff")
    assert monitor.current() is good
    assert monitor.error is not None


class EmptyProcesses:
    def list_processes(self, uid):
        return []


def daemon(tmp_path, adapter=None, resolver=lambda _: 501):
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    store = StateStore(tmp_path / "state.json", tmp_path / "state.lock")
    return Daemon(path, store, adapter or EmptyProcesses(), resolver, lambda _: False)


def test_invalid_edit_is_logged_once_while_last_good_policy_is_used(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    worker = daemon(tmp_path)
    now = datetime(2026, 11, 1, 21, tzinfo=UTC)
    worker.cycle(now)
    worker.monitor.path.write_text("bad: [")
    worker.cycle(now)
    worker.cycle(now)
    assert caplog.text.count("configuration error") == 1
    assert worker.monitor.last_good is not None


def test_missing_user_retries_without_crash_and_logs_recovery(tmp_path, caplog):
    attempts = []

    def resolve(_):
        attempts.append(1)
        if len(attempts) <= 2:
            raise ValueError("account missing")
        return 501

    worker = daemon(tmp_path, resolver=resolve)
    now = datetime(2026, 11, 1, 21, tzinfo=UTC)
    for _ in range(3):
        assert worker.cycle(now) == 45
    assert len(attempts) == 3
    assert caplog.text.count("account missing") == 1


def test_rule_transitions_and_override_expiry_are_logged_without_cycle_noise(
    tmp_path, caplog
):
    caplog.set_level(logging.INFO)
    worker = daemon(tmp_path)
    now = datetime(2026, 11, 1, 19, tzinfo=UTC)
    worker.state_store.set_override("facetime", now + timedelta(minutes=30))
    worker.cycle(now)
    worker.cycle(now)
    worker.cycle(now + timedelta(hours=2))
    assert caplog.text.count("rule facetime: allowed") == 1
    assert caplog.text.count("rule facetime: blocked") == 1
    assert caplog.text.count("override expired: facetime") == 1


def test_termination_actions_are_logged(tmp_path, caplog):
    from mac_app_scheduler.processes import ProcessInfo

    class Processes:
        def list_processes(self, uid):
            return [ProcessInfo(123, uid, "FaceTime", None, frozenset())]

        def refresh(self, pid, uid):
            return self.list_processes(uid)[0]

        def send_signal(self, process, value):
            return True

    caplog.set_level(logging.INFO)
    worker = daemon(tmp_path, Processes())
    worker.cycle(datetime(2026, 11, 1, 21, tzinfo=UTC))
    assert "facetime: sent SIGTERM to PID 123" in caplog.text


def test_malformed_state_error_is_logged_once(tmp_path, caplog):
    worker = daemon(tmp_path)
    worker.state_store.path.write_text("not json")
    now = datetime(2026, 11, 1, 21, tzinfo=UTC)
    worker.cycle(now)
    worker.cycle(now)
    assert caplog.text.count("state error") == 1


def test_one_pass_enforcement_honours_grace_period(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import mac_app_scheduler.daemon as module
    from mac_app_scheduler.daemon import run_once
    from mac_app_scheduler.models import SchedulerState
    from mac_app_scheduler.processes import ProcessInfo

    class Processes:
        def list_processes(self, uid):
            return [ProcessInfo(123, uid, "FaceTime", None, frozenset())]

        def refresh(self, pid, uid):
            return self.list_processes(uid)[0]

        def send_signal(self, process, value):
            return True

    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    monkeypatch.setattr(module, "resolve_user", lambda _: SimpleNamespace(pw_uid=501))
    waits = []
    monkeypatch.setattr(module.time, "sleep", waits.append)
    run_once(
        path,
        SchedulerState(),
        Processes(),
        dry_run=False,
        now=datetime(2026, 11, 1, 21, tzinfo=UTC),
    )
    assert waits == [5]
