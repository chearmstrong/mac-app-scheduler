from __future__ import annotations

from pathlib import Path

from mac_app_scheduler.daemon import ConfigMonitor

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


def test_invalid_edit_keeps_last_good_configuration(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    monitor = ConfigMonitor(path)
    good = monitor.current()

    path.write_text("rules: [")

    assert monitor.current() is good
    assert monitor.error is not None
