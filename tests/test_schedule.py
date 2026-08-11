from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from mac_app_scheduler.models import Rule, TimeWindow
from mac_app_scheduler.schedule import is_in_window, is_rule_blocked, next_transition

LONDON = ZoneInfo("Europe/London")
FACETIME = Rule(
    id="facetime",
    name="FaceTime",
    bundle_id="com.apple.FaceTime",
    process_name="FaceTime",
    block=TimeWindow(time(20), time(7)),
)


def local(clock: str) -> datetime:
    return datetime.fromisoformat(f"2026-08-11T{clock}:00").replace(tzinfo=LONDON)


@pytest.mark.parametrize(
    ("clock", "blocked"),
    [
        ("19:59", False),
        ("20:00", True),
        ("23:59", True),
        ("00:00", True),
        ("06:59", True),
        ("07:00", False),
    ],
)
def test_overnight_window_has_inclusive_start_and_exclusive_end(
    clock: str, blocked: bool
) -> None:
    assert is_in_window(FACETIME.block, local(clock)) is blocked


def test_active_override_permits_a_rule_during_its_block_window() -> None:
    now = local("21:00")
    override_until = now.astimezone(UTC) + timedelta(minutes=30)

    assert not is_rule_blocked(FACETIME, now, override_until)


def test_expired_override_does_not_permit_a_rule() -> None:
    now = local("21:00")

    assert is_rule_blocked(FACETIME, now, now.astimezone(UTC) - timedelta(seconds=1))


def test_fall_back_hour_uses_wall_clock_in_both_folds() -> None:
    first = datetime(2026, 10, 25, 1, 30, tzinfo=LONDON, fold=0)
    second = datetime(2026, 10, 25, 1, 30, tzinfo=LONDON, fold=1)
    window = TimeWindow(time(1), time(2))

    assert is_in_window(window, first)
    assert is_in_window(window, second)


def test_next_transition_is_the_end_of_the_current_block_window() -> None:
    now = local("21:00")

    assert next_transition(FACETIME, now) == local("07:00") + timedelta(days=1)
