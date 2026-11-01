"""Process identity helpers with conservative app-bundle matching."""

from __future__ import annotations

import ctypes
import os
import plistlib
import signal
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
    start_time: tuple[int, int] = (0, 0)


class _BSDInfo(ctypes.Structure):
    # Layout from macOS sys/proc_info.h, PROC_PIDTBSDINFO.
    _fields_ = [
        (name, ctypes.c_uint32)
        for name in (
            "flags",
            "status",
            "xstatus",
            "pid",
            "ppid",
            "uid",
            "gid",
            "ruid",
            "rgid",
            "svuid",
            "svgid",
            "reserved",
        )
    ] + [
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("jobc", ctypes.c_uint32),
        ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("seconds", ctypes.c_uint64),
        ("microseconds", ctypes.c_uint64),
    ]


class DarwinProcessAdapter:
    """Inventory target-user executable paths and signal verified PIDs."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise RuntimeError("process enforcement requires macOS")
        self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_listallpids.restype = ctypes.c_int
        self.lib.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.lib.proc_pidinfo.restype = ctypes.c_int
        self.lib.proc_pidpath.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self.lib.proc_pidpath.restype = ctypes.c_int

    def list_processes(self, uid: int) -> list[ProcessInfo]:
        size = self.lib.proc_listallpids(None, 0)
        if size <= 0:
            raise OSError("unable to enumerate processes")
        for _ in range(3):
            pids = (ctypes.c_int * (size + 256))()
            count = self.lib.proc_listallpids(pids, ctypes.sizeof(pids))
            if count <= 0:
                raise OSError("unable to enumerate processes")
            if count < len(pids):
                break
            size = count
        else:
            raise OSError("process inventory changed too quickly; retry next cycle")
        return [p for pid in pids[:count] if pid > 0 and (p := self.refresh(pid, uid))]

    def refresh(self, pid: int, uid: int) -> ProcessInfo | None:
        info = _BSDInfo()
        if self.lib.proc_pidinfo(
            pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)
        ) != ctypes.sizeof(info):
            return None
        if info.uid != uid or info.pid != pid:
            return None
        buffer = ctypes.create_string_buffer(4096)
        path = self.lib.proc_pidpath(pid, buffer, len(buffer))
        executable = Path(os.fsdecode(buffer.value)) if path > 0 else None
        name = executable.name if executable else os.fsdecode(info.name or info.comm)
        return ProcessInfo(
            pid,
            uid,
            name,
            executable,
            bundle_ids_for_executable(executable) if executable else frozenset(),
            (info.seconds, info.microseconds),
        )

    def send_signal(self, process: ProcessInfo, value: signal.Signals) -> bool:
        """Recheck owner, executable, bundle and birth time immediately before kill."""
        if self.refresh(process.pid, process.uid) != process:
            return False
        os.kill(process.pid, value)
        return True


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

    return process.name == rule.process_name
