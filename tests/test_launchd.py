from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

from mac_app_scheduler.launchd import LaunchdClient


def test_reload_uses_kickstart_for_the_exact_system_service() -> None:
    commands: list[list[str]] = []

    def run(command: list[str]) -> CompletedProcess[str]:
        commands.append(command)
        return CompletedProcess(command, 0, "", "")

    LaunchdClient(run).kickstart()

    assert commands == [
        [
            "/bin/launchctl",
            "kickstart",
            "-k",
            "system/io.github.mac-app-scheduler.daemon",
        ]
    ]


def test_bootstrap_uses_the_system_domain_and_plist_path(tmp_path: Path) -> None:
    plist = tmp_path / "daemon.plist"
    commands: list[list[str]] = []

    def run(command: list[str]) -> CompletedProcess[str]:
        commands.append(command)
        return CompletedProcess(command, 0, "", "")

    LaunchdClient(run).bootstrap(plist)

    assert commands == [["/bin/launchctl", "bootstrap", "system", str(plist)]]
