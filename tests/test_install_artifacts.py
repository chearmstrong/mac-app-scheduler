from __future__ import annotations

import plistlib
from pathlib import Path

ROOT = Path(__file__).parents[1]
PLIST = ROOT / "launchd/io.github.mac-app-scheduler.daemon.plist"


def test_launchdaemon_is_continuously_supervised_without_interval_polling() -> None:
    policy = plistlib.loads(PLIST.read_bytes())

    assert policy["Label"] == "io.github.mac-app-scheduler.daemon"
    assert policy["KeepAlive"] is True
    assert "StartInterval" not in policy
    assert policy["ProgramArguments"][-1] == "daemon"
