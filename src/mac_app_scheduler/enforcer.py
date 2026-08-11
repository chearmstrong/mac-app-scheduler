"""Desired-state enforcement for blocked applications."""

from __future__ import annotations

import signal
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from mac_app_scheduler.models import SchedulerConfig, SchedulerState
from mac_app_scheduler.processes import ProcessInfo, is_main_process, matches_rule
from mac_app_scheduler.schedule import is_rule_blocked


class ProcessAdapter(Protocol):
    def list_processes(self, uid: int) -> list[ProcessInfo]: ...
    def send_signal(self, pid: int, value: signal.Signals) -> None: ...


class Enforcer:
    def __init__(
        self,
        adapter: ProcessAdapter,
        uid_resolver: Callable[[str], int],
        sleeper: Callable[[float], None],
    ) -> None:
        self.adapter, self.uid_resolver, self.sleeper = adapter, uid_resolver, sleeper

    def reconcile(
        self,
        config: SchedulerConfig,
        state: SchedulerState,
        now: datetime,
        dry_run: bool = False,
    ) -> None:
        if not state.enabled:
            return
        uid = self.uid_resolver(config.user)
        for rule in config.rules:
            if not is_rule_blocked(rule, now, state.overrides.get(rule.id)):
                continue
            candidates = [
                p
                for p in self.adapter.list_processes(uid)
                if matches_rule(p, rule, uid) and is_main_process(p, rule)
            ]
            if not dry_run:
                for process in candidates:
                    self.adapter.send_signal(process.pid, signal.SIGTERM)
                if candidates:
                    self.sleeper(config.termination_grace_seconds)
                    remaining = [
                        process
                        for process in self.adapter.list_processes(uid)
                        if process.uid == uid and rule.bundle_id in process.bundle_ids
                    ]
                    for process in remaining:
                        self.adapter.send_signal(process.pid, signal.SIGKILL)
