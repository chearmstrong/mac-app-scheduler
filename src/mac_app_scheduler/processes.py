"""Process identity helpers with conservative app-bundle matching."""

from __future__ import annotations

import os
import plistlib
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from mac_app_scheduler.models import Rule


@dataclass(frozen=True)
class ProcessInfo:
    """The minimum verified identity needed before signalling a process."""

    pid: int
    uid: int
    name: str
    executable: Path | None
    bundle_ids: frozenset[str]


class DarwinProcessAdapter:
    """Inventory target-user executable paths and signal verified PIDs."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("process enforcement requires macOS")

    def list_processes(self, uid: int) -> list[ProcessInfo]:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,uid=,comm="],
            check=True,
            capture_output=True,
            text=True,
        )
        processes = []
        for line in result.stdout.splitlines():
            fields = line.strip().split(maxsplit=2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                continue
            pid, process_uid, command = int(fields[0]), int(fields[1]), fields[2]
            if process_uid == uid:
                executable = Path(command)
                processes.append(
                    ProcessInfo(
                        pid,
                        uid,
                        executable.name,
                        executable,
                        bundle_ids_for_executable(executable),
                    )
                )
        return processes

    def send_signal(self, pid: int, value: signal.Signals) -> None:
        os.kill(pid, value)


def bundle_ids_for_executable(executable: Path) -> frozenset[str]:
    """Return identifiers from all enclosing ``.app`` bundles."""

    identifiers: set[str] = set()
    for parent in executable.parents:
        if parent.suffix != ".app":
            continue
        info = parent / "Contents" / "Info.plist"
        try:
            parsed = plistlib.loads(info.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            continue
        identifier = (
            parsed.get("CFBundleIdentifier") if isinstance(parsed, dict) else None
        )
        if isinstance(identifier, str) and identifier:
            identifiers.add(identifier)
    return frozenset(identifiers)


def matches_rule(process: ProcessInfo, rule: Rule, expected_uid: int) -> bool:
    """Match only the configured user's bundle, or an exact main-name fallback."""

    if process.uid != expected_uid:
        return False
    return rule.bundle_id in process.bundle_ids or (
        not process.bundle_ids and process.name == rule.process_name
    )


def is_main_process(process: ProcessInfo, rule: Rule) -> bool:
    """Identify a main process eligible for graceful termination."""

    return process.name == rule.process_name and rule.bundle_id in process.bundle_ids
