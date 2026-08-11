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

    def send_signal(self, pid, value):
        self.signals.append((pid, value))
        if value == signal.SIGTERM:
            self.processes = [
                process for process in self.processes if process.pid != pid
            ]


class UnresponsiveProcesses(FakeProcesses):
    def send_signal(self, pid, value):
        self.signals.append((pid, value))


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
