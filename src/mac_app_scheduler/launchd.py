"""Small shell-free interface to the system launchd service."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

LABEL = "io.github.mac-app-scheduler.daemon"
SERVICE = f"system/{LABEL}"


class LaunchdClient:
    """Run modern launchctl subcommands using argument vectors."""

    def __init__(
        self,
        runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._runner = runner or self._run

    def status(self) -> subprocess.CompletedProcess[str]:
        return self._runner(["/bin/launchctl", "print", SERVICE])

    def kickstart(self) -> subprocess.CompletedProcess[str]:
        return self._runner(["/bin/launchctl", "kickstart", "-k", SERVICE])

    def bootstrap(self, plist: Path) -> subprocess.CompletedProcess[str]:
        return self._runner(["/bin/launchctl", "bootstrap", "system", str(plist)])

    def bootout(self) -> subprocess.CompletedProcess[str]:
        return self._runner(["/bin/launchctl", "bootout", SERVICE])

    @staticmethod
    def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, check=False)
