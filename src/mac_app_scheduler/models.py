"""Immutable values shared by scheduler modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class TimeWindow:
    """A daily blocking window expressed in local wall-clock time."""

    start: time
    until: time


@dataclass(frozen=True)
class Rule:
    """A named application blocking rule."""

    id: str
    name: str
    bundle_id: str
    process_name: str
    block: TimeWindow


@dataclass(frozen=True)
class SchedulerConfig:
    """The permanent policy loaded from YAML."""

    version: int
    user: str
    check_interval_seconds: int
    termination_grace_seconds: int
    rules: tuple[Rule, ...]
