import signal
from datetime import UTC, datetime, time

from mac_app_scheduler.enforcer import Enforcer
from mac_app_scheduler.models import Rule, SchedulerConfig, SchedulerState, TimeWindow
from mac_app_scheduler.processes import ProcessInfo

RULE = Rule(
    "facetime",
    "FaceTime",
    "com.apple.FaceTime",
    "FaceTime",
    TimeWindow(time(20), time(7)),
)
CONFIG = SchedulerConfig(1, "example-user", 45, 1, (RULE,))
NOW = datetime(2026, 8, 11, 21, tzinfo=UTC)


class FakeProcesses:
    def __init__(self, processes):
        self.processes, self.signals = processes, []

    def list_processes(self, uid):
        return list(self.processes)

    def refresh(self, pid, uid):
        return next((p for p in self.processes if p.pid == pid and p.uid == uid), None)

    def send_signal(self, pid, value):
        pid = pid.pid
        self.signals.append((pid, value))
        if value == signal.SIGTERM:
            self.processes = [
                process for process in self.processes if process.pid != pid
            ]


class UnresponsiveProcesses(FakeProcesses):
    def send_signal(self, pid, value):
        self.signals.append((pid.pid, value))


def test_blocked_main_process_is_terminated_gracefully() -> None:
    adapter = FakeProcesses(
        [ProcessInfo(101, 501, "FaceTime", None, frozenset({"com.apple.FaceTime"}))]
    )
    Enforcer(adapter, uid_resolver=lambda _: 501, sleeper=lambda _: None).reconcile(
        CONFIG, SchedulerState(), NOW
    )
    assert adapter.signals == [(101, signal.SIGTERM)]


def test_disabled_scheduler_never_signals() -> None:
    adapter = UnresponsiveProcesses(
        [ProcessInfo(101, 501, "FaceTime", None, frozenset({"com.apple.FaceTime"}))]
    )
    Enforcer(adapter, uid_resolver=lambda _: 501, sleeper=lambda _: None).reconcile(
        CONFIG, SchedulerState(enabled=False), NOW
    )
    assert adapter.signals == []


def test_unresponsive_main_and_helper_are_force_terminated_after_rescan() -> None:
    adapter = UnresponsiveProcesses(
        [
            ProcessInfo(101, 501, "FaceTime", None, frozenset({"com.apple.FaceTime"})),
            ProcessInfo(
                102, 501, "FaceTime Helper", None, frozenset({"com.apple.FaceTime"})
            ),
        ]
    )
    Enforcer(adapter, uid_resolver=lambda _: 501, sleeper=lambda _: None).reconcile(
        CONFIG, SchedulerState(), NOW
    )

    assert adapter.signals == [
        (101, signal.SIGTERM),
        (101, signal.SIGKILL),
        (102, signal.SIGKILL),
    ]


def test_name_fallback_gets_term_but_never_force_kill():
    adapter = UnresponsiveProcesses(
        [ProcessInfo(101, 501, "FaceTime", None, frozenset())]
    )
    Enforcer(adapter, lambda _: 501, lambda _: None).reconcile(
        CONFIG, SchedulerState(), NOW
    )
    assert adapter.signals == [(101, signal.SIGTERM)]


def test_orphan_helper_receives_grace_before_force_kill():
    adapter = UnresponsiveProcesses(
        [ProcessInfo(102, 501, "Helper", None, frozenset({"com.apple.FaceTime"}))]
    )
    waits = []
    Enforcer(adapter, lambda _: 501, waits.append).reconcile(
        CONFIG, SchedulerState(), NOW
    )
    assert adapter.signals == [(102, signal.SIGTERM), (102, signal.SIGKILL)]
    assert waits == [1]


def test_reused_pid_is_never_signalled():
    from dataclasses import replace

    original = ProcessInfo(
        101, 501, "FaceTime", None, frozenset({"com.apple.FaceTime"}), (1, 2)
    )

    class Reused(UnresponsiveProcesses):
        def refresh(self, pid, uid):
            return replace(original, start_time=(3, 4))

    adapter = Reused([original])
    Enforcer(adapter, lambda _: 501, lambda _: None).reconcile(
        CONFIG, SchedulerState(), NOW
    )
    assert adapter.signals == []


def test_relaunched_app_during_grace_is_not_force_killed():
    from dataclasses import replace

    original = ProcessInfo(
        101, 501, "FaceTime", None, frozenset({"com.apple.FaceTime"}), (1, 2)
    )
    adapter = UnresponsiveProcesses([original])

    def relaunch(_):
        adapter.processes = [replace(original, start_time=(3, 4))]

    Enforcer(adapter, lambda _: 501, relaunch).reconcile(CONFIG, SchedulerState(), NOW)
    assert adapter.signals == [(101, signal.SIGTERM)]


def test_vanished_process_does_not_prevent_later_rule():
    from dataclasses import replace

    second_rule = replace(
        RULE, id="second", process_name="Second", bundle_id="org.example.Second"
    )
    adapter = FakeProcesses(
        [
            ProcessInfo(101, 501, "FaceTime", None, frozenset({"com.apple.FaceTime"})),
            ProcessInfo(102, 501, "Second", None, frozenset({"org.example.Second"})),
        ]
    )
    original_send = adapter.send_signal

    def send(process, value):
        if process.pid == 101:
            raise ProcessLookupError("gone")
        original_send(process, value)

    adapter.send_signal = send
    Enforcer(adapter, lambda _: 501, lambda _: None).reconcile(
        replace(CONFIG, rules=(RULE, second_rule)), SchedulerState(), NOW
    )
    assert adapter.signals == [(102, signal.SIGTERM)]


def test_dry_run_returns_planned_actions_without_waiting_or_signalling():
    adapter = FakeProcesses(
        [ProcessInfo(101, 501, "FaceTime", None, frozenset({"com.apple.FaceTime"}))]
    )
    waits = []
    result = Enforcer(adapter, lambda _: 501, waits.append).reconcile(
        CONFIG, SchedulerState(), NOW, dry_run=True
    )
    assert [(a.rule_id, a.pid, a.signal) for a in result.actions] == [
        ("facetime", 101, signal.SIGTERM)
    ]
    assert adapter.signals == []
    assert waits == []


def test_permission_error_is_reported_and_later_rule_still_runs():
    from dataclasses import replace

    second = replace(RULE, id="second")
    adapter = UnresponsiveProcesses(
        [ProcessInfo(101, 501, "FaceTime", None, frozenset({"com.apple.FaceTime"}))]
    )
    attempts = []

    def inventory(uid):
        attempts.append(uid)
        if len(attempts) == 1:
            raise PermissionError("inventory denied")
        return adapter.processes

    adapter.list_processes = inventory
    result = Enforcer(adapter, lambda _: 501, lambda _: None).reconcile(
        replace(CONFIG, rules=(RULE, second)), SchedulerState(), NOW
    )
    assert "inventory denied" in result.errors[0]
    assert adapter.signals[0] == (101, signal.SIGTERM)


def test_shutdown_during_grace_does_not_force_kill():
    adapter = UnresponsiveProcesses(
        [ProcessInfo(101, 501, "FaceTime", None, frozenset({"com.apple.FaceTime"}))]
    )
    Enforcer(adapter, lambda _: 501, lambda _: True).reconcile(
        CONFIG, SchedulerState(), NOW
    )
    assert adapter.signals == [(101, signal.SIGTERM)]
