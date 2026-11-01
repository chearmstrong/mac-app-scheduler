from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess

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
        return CompletedProcess([], 0, "", "")

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


@pytest.mark.parametrize(
    "args, suffix",
    [
        (
            ["install", "--user", "example-user", "--python", "/python"],
            ["--user", "example-user", "--python", "/python"],
        ),
        (["uninstall", "--yes"], ["--yes"]),
    ],
)
def test_install_commands_forward_documented_options(tmp_path, args, suffix):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return CompletedProcess(command, 0)

    assert main(args, paths=paths(tmp_path), euid=lambda: 0, script_runner=run) == 0
    assert calls[0][1:] == suffix


def test_reload_reports_launchctl_failure(tmp_path, capsys):
    class Failed(RecordingLaunchd):
        def kickstart(self):
            return CompletedProcess([], 5, "", "service missing")

    assert main(["reload"], paths=paths(tmp_path), euid=lambda: 0, launchd=Failed) != 0
    output = capsys.readouterr()
    assert "service missing" in output.err
    assert "daemon reloaded" not in output.out


def test_reload_does_not_restart_with_invalid_config(tmp_path):
    app_paths = paths(tmp_path)
    app_paths.config.write_text("bad: [")
    service = RecordingLaunchd()
    assert (
        main(["reload"], paths=app_paths, euid=lambda: 0, launchd=lambda: service) != 0
    )
    assert not service.restarted


def test_dry_run_prints_actions_and_does_not_mutate_state(tmp_path, capsys):
    from mac_app_scheduler.processes import ProcessInfo

    class Processes:
        def list_processes(self, uid):
            return [
                ProcessInfo(
                    123, uid, "FaceTime", None, frozenset({"com.apple.FaceTime"})
                )
            ]

        def send_signal(self, *args):
            pytest.fail("dry run signalled a process")

    app_paths = paths(tmp_path)
    assert (
        main(
            ["dry-run"],
            paths=app_paths,
            euid=lambda: 0,
            adapter=Processes(),
            uid_resolver=lambda _: 501,
            now=lambda: datetime(2026, 11, 1, 21, tzinfo=UTC),
        )
        == 0
    )
    assert "would send SIGTERM to facetime (PID 123)" in capsys.readouterr().out
    assert not app_paths.state.exists()


def test_cli_logs_override_creation_and_enable_disable(tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO)
    app_paths = paths(tmp_path)
    main(["allow", "facetime", "30m"], paths=app_paths, euid=lambda: 0)
    main(["disable"], paths=app_paths, euid=lambda: 0)
    assert "override created: facetime" in caplog.text
    assert "scheduler disabled" in caplog.text


def test_render_config_validates_real_example_and_writes_requested_user(tmp_path):
    from mac_app_scheduler.config import load_config

    config = tmp_path / "config.yaml"
    root = Path(__file__).parents[1]
    app_paths = AppPaths(config, tmp_path / "state.json", tmp_path / "state.lock")
    assert (
        main(
            [
                "render-config",
                "--example",
                str(root / "config.example.yaml"),
                "--user",
                "example-user",
            ],
            paths=app_paths,
            euid=lambda: 0,
        )
        == 0
    )
    rendered = load_config(config)
    assert rendered.user == "example-user"
    assert [r.id for r in rendered.rules] == ["facetime", "chrome"]


def test_cli_help_exposes_installer_options_in_real_entrypoint():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "mac_app_scheduler", "install", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--user" in result.stdout
    assert "--python" in result.stdout
