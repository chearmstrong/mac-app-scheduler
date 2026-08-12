from __future__ import annotations

import os
import plistlib
import sys
from datetime import time
from pathlib import Path

from mac_app_scheduler.models import Rule, TimeWindow
from mac_app_scheduler.processes import (
    DarwinProcessAdapter,
    ProcessInfo,
    bundle_ids_for_executable,
    matches_rule,
)

FACETIME = Rule(
    id="facetime",
    name="FaceTime",
    bundle_id="com.apple.FaceTime",
    process_name="FaceTime",
    block=TimeWindow(time(20), time(7)),
)


def make_nested_helper_bundle(tmp_path: Path) -> Path:
    app = tmp_path / "Google Chrome.app"
    helper = app / "Contents/Frameworks/Google Chrome Helper.app"
    executable = helper / "Contents/MacOS/Google Chrome Helper"
    executable.parent.mkdir(parents=True)
    executable.touch()
    for bundle, bundle_id in (
        (app, "com.google.Chrome"),
        (helper, "com.google.Chrome.helper"),
    ):
        info = bundle / "Contents/Info.plist"
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}))
    return executable


def test_nested_helper_matches_outer_bundle(tmp_path: Path) -> None:
    executable = make_nested_helper_bundle(tmp_path)

    assert "com.google.Chrome" in bundle_ids_for_executable(executable)


def test_matching_requires_target_uid_before_bundle_identity() -> None:
    process = ProcessInfo(17, 502, "FaceTime", None, frozenset({"com.apple.FaceTime"}))

    assert not matches_rule(process, FACETIME, expected_uid=501)


def test_name_fallback_is_exact() -> None:
    matching = ProcessInfo(17, 501, "FaceTime", None, frozenset())
    different = ProcessInfo(18, 501, "FaceTimeHelper", None, frozenset())

    assert matches_rule(matching, FACETIME, expected_uid=501)
    assert not matches_rule(different, FACETIME, expected_uid=501)


def test_darwin_adapter_can_inventory_the_current_user() -> None:
    if sys.platform != "darwin":
        return
    processes = DarwinProcessAdapter().list_processes(os.getuid())
    assert any(process.pid == os.getpid() for process in processes)
