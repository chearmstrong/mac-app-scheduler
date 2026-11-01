"""Desired-state enforcement for blocked applications."""

from __future__ import annotations

import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from mac_app_scheduler.models import SchedulerConfig, SchedulerState
from mac_app_scheduler.processes import ProcessInfo, is_main_process, matches_rule
from mac_app_scheduler.schedule import is_rule_blocked


class ProcessAdapter(Protocol):
    def list_processes(self, uid: int) -> list[ProcessInfo]: ...
    def refresh(self, pid: int, uid: int) -> ProcessInfo | None: ...
    def send_signal(
        self, process: ProcessInfo, value: signal.Signals
    ) -> bool | None: ...


@dataclass(frozen=True)
class Action:
    rule_id: str
    pid: int
    signal: signal.Signals


@dataclass
class ReconcileResult:
    actions: list[Action] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Enforcer:
    def __init__(
        self,
        adapter: ProcessAdapter,
        uid_resolver: Callable[[str], int],
        sleeper: Callable[[float], object],
    ) -> None:
        self.adapter, self.uid_resolver, self.sleeper = adapter, uid_resolver, sleeper

    def reconcile(
        self,
        config: SchedulerConfig,
        state: SchedulerState,
        now: datetime,
        dry_run: bool = False,
    ) -> ReconcileResult:
        result = ReconcileResult()
        if not state.enabled:
            return result
        uid = self.uid_resolver(config.user)
        for rule in config.rules:
            if not is_rule_blocked(rule, now, state.overrides.get(rule.id)):
                continue
            try:
                matches = [
                    p
                    for p in self.adapter.list_processes(uid)
                    if matches_rule(p, rule, uid)
                ]
                mains = [p for p in matches if is_main_process(p, rule)]
                targets = mains or matches
                if dry_run:
                    result.actions.extend(
                        Action(rule.id, p.pid, signal.SIGTERM) for p in targets
                    )
                    continue
                sent = False
                for process in targets:
                    sent |= self._signal(process, rule.id, signal.SIGTERM, result)
                if not sent:
                    continue
                if self.sleeper(config.termination_grace_seconds):
                    # Shutdown must not shorten the grace period into a force kill.
                    break
                for process in matches:
                    if rule.bundle_id in process.bundle_ids:
                        self._signal(process, rule.id, signal.SIGKILL, result)
            except Exception as exc:
                result.errors.append(f"{rule.id}: {type(exc).__name__}: {exc}")
        return result

    def _signal(
        self,
        process: ProcessInfo,
        rule_id: str,
        value: signal.Signals,
        result: ReconcileResult,
    ) -> bool:
        try:
            if self.adapter.refresh(process.pid, process.uid) != process:
                return False
            if self.adapter.send_signal(process, value) is False:
                return False
        except ProcessLookupError:
            return False
        except OSError as exc:
            result.errors.append(f"{rule_id}: PID {process.pid}: {exc}")
            return False
        result.actions.append(Action(rule_id, process.pid, value))
        return True
