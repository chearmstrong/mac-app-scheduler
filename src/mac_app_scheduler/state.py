"""Locked, atomic persistence for scheduler operational state."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mac_app_scheduler.models import SchedulerState

_DURATION = re.compile(r"([1-9][0-9]*)([smhd])\Z")
_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class StateStore:
    """Read and update state under an advisory lock."""

    def __init__(self, path: Path, lock_path: Path) -> None:
        self.path = path
        self.lock_path = lock_path
        self.error: str | None = None

    def read(self) -> SchedulerState:
        with self._lock():
            return self._read_unlocked()

    def set_enabled(self, enabled: bool) -> SchedulerState:
        with self._lock():
            state = self._read_unlocked()
            updated = SchedulerState(enabled=enabled, overrides=dict(state.overrides))
            self._write_unlocked(updated)
            return updated

    def set_override(self, rule_id: str, expiry: datetime) -> SchedulerState:
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            raise ValueError("expiry must be timezone-aware")
        with self._lock():
            state = self._read_unlocked()
            overrides = dict(state.overrides)
            overrides[rule_id] = expiry.astimezone(UTC)
            updated = SchedulerState(enabled=state.enabled, overrides=overrides)
            self._write_unlocked(updated)
            return updated

    def prune_expired(self, now: datetime) -> SchedulerState:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = now.astimezone(UTC)
        with self._lock():
            state = self._read_unlocked()
            overrides = {
                rule_id: expiry
                for rule_id, expiry in state.overrides.items()
                if expiry > current
            }
            updated = SchedulerState(enabled=state.enabled, overrides=overrides)
            if updated != state:
                self._write_unlocked(updated)
            return updated

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as lock_file:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> SchedulerState:
        self.error = None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1:
                raise ValueError("unsupported state schema")
            enabled = raw.get("enabled")
            overrides = raw.get("overrides")
            if not isinstance(enabled, bool) or not isinstance(overrides, dict):
                raise ValueError("invalid enabled or overrides fields")
            parsed = {
                rule_id: _parse_expiry(value)
                for rule_id, value in overrides.items()
                if isinstance(rule_id, str) and isinstance(value, str)
            }
            if len(parsed) != len(overrides):
                raise ValueError("invalid override entry")
            return SchedulerState(enabled=enabled, overrides=parsed)
        except FileNotFoundError:
            return SchedulerState()
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            return SchedulerState()

    def _write_unlocked(self, state: SchedulerState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "enabled": state.enabled,
            "overrides": {
                rule_id: expiry.astimezone(UTC).isoformat()
                for rule_id, expiry in state.overrides.items()
            },
        }
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as temporary:
            json.dump(payload, temporary, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, self.path)


def parse_duration(value: str) -> timedelta:
    """Parse a positive whole-number duration such as ``30m``."""

    match = _DURATION.fullmatch(value)
    if match is None:
        raise ValueError(
            "duration must be a positive whole number followed by s, m, h, or d"
        )
    return timedelta(seconds=int(match.group(1)) * _MULTIPLIERS[match.group(2)])


def _parse_expiry(value: str) -> datetime:
    expiry = datetime.fromisoformat(value)
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        raise ValueError("expiry must be timezone-aware")
    return expiry.astimezone(UTC)
