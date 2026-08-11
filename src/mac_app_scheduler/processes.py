"""Process identity helpers with conservative app-bundle matching."""

from __future__ import annotations

import plistlib
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
