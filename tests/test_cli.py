from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mac_app_scheduler.cli import AppPaths, main
from mac_app_scheduler.state import StateStore

VALID = """
version: 1
user: example-user
check_interval_seconds: 45
termination_grace_seconds: 5
rules:
  - id: facetime
    name: FaceTime
    bundle_id: com.apple.FaceTime
    process_name: FaceTime
    block: {from: "20:00", until: "07:00"}
"""


def paths(tmp_path: Path) -> AppPaths:
    config = tmp_path / "config.yaml"
    config.write_text(VALID)
    return AppPaths(config, tmp_path / "state.json", tmp_path / "state.lock")


def test_allow_writes_a_utc_override_for_a_known_rule(tmp_path: Path) -> None:
    now = datetime(2026, 8, 12, 20, tzinfo=UTC)
    app_paths = paths(tmp_path)

    result = main(
        ["allow", "facetime", "30m"],
        paths=app_paths,
        euid=lambda: 0,
        now=lambda: now,
    )
    assert result == 0

    state = StateStore(app_paths.state, app_paths.lock).read()
    assert state.overrides["facetime"] == now + timedelta(minutes=30)


def test_mutating_command_requires_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["disable"], paths=paths(tmp_path), euid=lambda: 501)

    assert error.value.code == 2
    assert "sudo" in capsys.readouterr().err


def test_status_reports_state_without_enforcing_processes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app_paths = paths(tmp_path)

    assert (
        main(
            ["status"],
            paths=app_paths,
            euid=lambda: 0,
            launchd=lambda: RecordingLaunchd(),
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "scheduler: enabled" in output
    assert "launchd: loaded" in output
    assert "facetime" in output


class RecordingLaunchd:
    def __init__(self) -> None:
        self.restarted = False

    def kickstart(self) -> None:
        self.restarted = True

    def status(self):
        from subprocess import CompletedProcess

        return CompletedProcess([], 0, "service information", "")


def test_reload_validates_config_then_restarts_service(tmp_path: Path) -> None:
    service = RecordingLaunchd()

    result = main(
        ["reload"], paths=paths(tmp_path), euid=lambda: 0, launchd=lambda: service
    )

    assert result == 0
    assert service.restarted is True
