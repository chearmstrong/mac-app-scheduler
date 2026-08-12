"""LaunchDaemon lifecycle and configuration reload support."""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from mac_app_scheduler.config import (
    ConfigError,
    SchedulerConfig,
    load_config,
    resolve_user,
)
from mac_app_scheduler.enforcer import Enforcer, ProcessAdapter
from mac_app_scheduler.models import SchedulerState
from mac_app_scheduler.processes import DarwinProcessAdapter
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


def run_daemon(
    config_path: Path,
    state_store: StateStore,
    adapter: ProcessAdapter | None = None,
) -> int:
    """Enforce immediately, then reconcile after each configured interval."""

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    logger = logging.getLogger("mac-app-scheduler")
    monitor = ConfigMonitor(config_path)
    process_adapter = adapter or DarwinProcessAdapter()
    logger.warning("daemon started")
    while not stop.is_set():
        config = monitor.current()
        if config is None:
            if monitor.error is not None:
                logger.error("configuration error: %s", monitor.error)
            stop.wait(45)
            continue
        state = state_store.prune_expired(datetime.now().astimezone())
        enforcer = Enforcer(
            process_adapter, lambda user: resolve_user(user).pw_uid, stop.wait
        )
        enforcer.reconcile(config, state, datetime.now().astimezone())
        stop.wait(config.check_interval_seconds)
    logger.warning("daemon stopped")
    return 0


def run_once(
    config_path: Path,
    state: SchedulerState,
    adapter: ProcessAdapter,
    *,
    dry_run: bool,
) -> None:
    """Perform one optional dry-run enforcement pass."""

    config = load_config(config_path)
    Enforcer(adapter, lambda user: resolve_user(user).pw_uid, lambda _: None).reconcile(
        config, state, datetime.now().astimezone(), dry_run
    )
