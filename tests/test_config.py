from __future__ import annotations

import re
from unittest.mock import Mock

import pytest

from mac_app_scheduler.config import ConfigError, parse_config, resolve_user

VALID_CONFIG = """
version: 1
user: example-user
check_interval_seconds: 45
termination_grace_seconds: 5
rules:
  - id: facetime
    name: FaceTime
    bundle_id: com.apple.FaceTime
    process_name: FaceTime
    block:
      from: "20:00"
      until: "07:00"
  - id: chrome
    name: Google Chrome
    bundle_id: com.google.Chrome
    process_name: Google Chrome
    block:
      from: "21:00"
      until: "07:00"
"""


def test_parse_config_accepts_documented_schema() -> None:
    config = parse_config(VALID_CONFIG)

    assert config.user == "example-user"
    assert [rule.id for rule in config.rules] == ["facetime", "chrome"]


@pytest.mark.parametrize(
    ("config", "field"),
    [
        (VALID_CONFIG.replace('      until: "07:00"\n', ""), "rules[0].block.until"),
        (
            VALID_CONFIG.replace(
                "check_interval_seconds: 45", "check_interval_seconds: 10"
            ),
            "check_interval_seconds",
        ),
        (VALID_CONFIG.replace("rules:\n", "surprise: true\nrules:\n"), "surprise"),
    ],
)
def test_parse_config_rejects_invalid_fields(config: str, field: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(field)):
        parse_config(config)


def test_resolve_user_reports_missing_account(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = Mock(side_effect=KeyError)
    monkeypatch.setattr("mac_app_scheduler.config.pwd.getpwnam", missing)

    with pytest.raises(ConfigError, match="does not exist"):
        resolve_user("missing")
