from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mac_app_scheduler.models import SchedulerState
from mac_app_scheduler.state import StateStore, parse_duration


def store(tmp_path):
    return StateStore(tmp_path / "state.json", tmp_path / "state.lock")


def test_missing_state_defaults_to_enabled(tmp_path) -> None:
    state = store(tmp_path).read()

    assert state == SchedulerState()


def test_malformed_state_fails_closed_without_overwriting(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("not json")

    assert store(tmp_path).read() == SchedulerState()
    assert path.read_text() == "not json"


def test_malformed_state_exposes_error_for_daemon_diagnostics(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json")
    state_store = store(tmp_path)
    state_store.read()
    assert state_store.error is not None
    state_store.set_enabled(True)
    state_store.read()
    assert state_store.error is None


def test_override_round_trips_as_utc_and_expires(tmp_path) -> None:
    expiry = datetime(2026, 8, 11, 20, 30, tzinfo=UTC)
    state_store = store(tmp_path)
    state_store.set_override("facetime", expiry)

    assert state_store.read().overrides["facetime"] == expiry
    assert state_store.prune_expired(expiry) == SchedulerState()


@pytest.mark.parametrize("value", ["30s", "30m", "2h", "1d"])
def test_parse_duration_accepts_positive_whole_units(value: str) -> None:
    assert parse_duration(value) > timedelta()


@pytest.mark.parametrize("value", ["0m", "-1m", "1.5h", "30", "3w"])
def test_parse_duration_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(value)
