"""LaunchDaemon lifecycle and configuration reload support."""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from mac_app_scheduler.config import (
    ConfigError,
    SchedulerConfig,
    load_config,
    resolve_user,
)
from mac_app_scheduler.enforcer import Enforcer, ProcessAdapter, ReconcileResult
from mac_app_scheduler.models import SchedulerState
from mac_app_scheduler.processes import DarwinProcessAdapter
from mac_app_scheduler.schedule import is_rule_blocked
from mac_app_scheduler.state import StateStore


class ConfigMonitor:
    """Keep a valid policy active when an on-disk edit is malformed."""

    def __init__(
        self, path: Path, loader: Callable[[Path], SchedulerConfig] = load_config
    ) -> None:
        self.path = path
        self.loader = loader
        self.last_good: SchedulerConfig | None = None
        self.error: ConfigError | None = None
        self._signature: tuple[int, int, int, int] | None = None

    def current(self) -> SchedulerConfig | None:
        signature = self._file_signature()
        if self.last_good is not None and signature == self._signature:
            return self.last_good
        try:
            config = self.loader(self.path)
        except ConfigError as exc:
            self.error = exc
            self._signature = signature
            return self.last_good
        self.last_good = config
        self.error = None
        self._signature = signature
        return config

    def _file_signature(self) -> tuple[int, int, int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


class Daemon:
    """One recoverable reconciliation cycle and its transition log state."""

    def __init__(self, config_path, state_store, adapter, uid_resolver, sleeper):
        self.monitor = ConfigMonitor(config_path)
        self.state_store = state_store
        self.enforcer = Enforcer(adapter, uid_resolver, sleeper)
        self.logger = logging.getLogger("mac-app-scheduler")
        self._conditions = {}

    def _changed(self, key, value, message, *, error=False):
        if key not in self._conditions or self._conditions[key] != value:
            self._conditions[key] = value
            if message:
                (self.logger.error if error else self.logger.info)(message)

    def cycle(self, now: datetime) -> int:
        config = None
        try:
            config = self.monitor.current()
            error = str(self.monitor.error) if self.monitor.error else None
            self._changed(
                "config-error",
                error,
                f"configuration error: {error}" if error else None,
                error=True,
            )
            if config is None:
                return 45
            self._changed("config", config, "configuration activated")
            before = self.state_store.read()
            state_error = self.state_store.error
            self._changed(
                "state-error",
                state_error,
                f"state error: {state_error}" if state_error else None,
                error=True,
            )
            state = self.state_store.prune_expired(now)
            for rule_id in before.overrides.keys() - state.overrides.keys():
                self.logger.info("override expired: %s", rule_id)
            self._changed(
                "enabled",
                state.enabled,
                f"scheduler {'enabled' if state.enabled else 'disabled'}",
            )
            for rule in config.rules:
                blocked = state.enabled and is_rule_blocked(
                    rule, now, state.overrides.get(rule.id)
                )
                self._changed(
                    f"rule:{rule.id}",
                    blocked,
                    f"rule {rule.id}: {'blocked' if blocked else 'allowed'}",
                )
            result = self.enforcer.reconcile(config, state, now)
            for action in result.actions:
                self.logger.info(
                    "%s: sent %s to PID %s",
                    action.rule_id,
                    action.signal.name,
                    action.pid,
                )
            errors = tuple(result.errors)
            self._changed("enforcement-errors", errors, "; ".join(errors), error=True)
            self._changed("cycle-error", None, None)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._changed(
                "cycle-error", error, f"enforcement error: {error}", error=True
            )
        return config.check_interval_seconds if config else 45


def run_daemon(
    config_path: Path, state_store: StateStore, adapter: ProcessAdapter | None = None
) -> int:
    """Enforce immediately, then reconcile until launchd requests shutdown."""
    stop = threading.Event()
    previous = {
        sig: signal.signal(sig, lambda *_: stop.set())
        for sig in (signal.SIGTERM, signal.SIGINT)
    }
    logger = logging.getLogger("mac-app-scheduler")
    try:
        worker = Daemon(
            config_path,
            state_store,
            adapter or DarwinProcessAdapter(),
            lambda user: resolve_user(user).pw_uid,
            stop.wait,
        )
        logger.info("daemon started")
        while not stop.is_set():
            stop.wait(worker.cycle(datetime.now().astimezone()))
    finally:
        logger.info("daemon stopped")
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def run_once(
    config_path: Path,
    state: SchedulerState,
    adapter: ProcessAdapter,
    *,
    dry_run: bool,
    now: datetime | None = None,
) -> ReconcileResult:
    """Perform one optional dry-run enforcement pass."""

    config = load_config(config_path)
    return Enforcer(
        adapter, lambda user: resolve_user(user).pw_uid, time.sleep
    ).reconcile(config, state, now or datetime.now().astimezone(), dry_run)
