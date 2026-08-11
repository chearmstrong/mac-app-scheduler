"""Local-wall-clock schedule evaluation."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from mac_app_scheduler.models import Rule, TimeWindow


def is_in_window(window: TimeWindow, now: datetime) -> bool:
    """Return whether an aware local timestamp is inside a daily window."""

    _require_aware(now)
    current = now.timetz().replace(tzinfo=None)
    if window.start < window.until:
        return window.start <= current < window.until
    return current >= window.start or current < window.until


def is_rule_blocked(rule: Rule, now: datetime, override_until: datetime | None) -> bool:
    """Return effective blocking state after an optional UTC override."""

    _require_aware(now)
    if override_until is not None:
        _require_aware(override_until)
        if override_until.astimezone(UTC) > now.astimezone(UTC):
            return False
    return is_in_window(rule.block, now)


def next_transition(rule: Rule, now: datetime) -> datetime:
    """Return the next start or end transition strictly after ``now``."""

    _require_aware(now)
    candidates = [
        _at_local_time(now, rule.block.start, day_offset) for day_offset in range(0, 3)
    ] + [
        _at_local_time(now, rule.block.until, day_offset) for day_offset in range(0, 3)
    ]
    return min(candidate for candidate in candidates if candidate > now)


def _at_local_time(now: datetime, value: time, day_offset: int) -> datetime:
    return datetime.combine(
        now.date() + timedelta(days=day_offset), value, tzinfo=now.tzinfo
    )


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
