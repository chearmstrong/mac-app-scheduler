"""Strict permanent-policy parsing and account lookup."""

from __future__ import annotations

import pwd
import re
from datetime import datetime, time
from pathlib import Path
from typing import Any

import yaml

from mac_app_scheduler.models import Rule, SchedulerConfig, TimeWindow

_CONFIG_KEYS = {
    "version",
    "user",
    "check_interval_seconds",
    "termination_grace_seconds",
    "rules",
}
_RULE_KEYS = {"id", "name", "bundle_id", "process_name", "block"}
_BLOCK_KEYS = {"from", "until"}
_RULE_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


class ConfigError(ValueError):
    """Raised when configuration cannot be safely activated."""


def load_config(path: Path) -> SchedulerConfig:
    """Load and validate a UTF-8 YAML configuration file."""

    try:
        return parse_config(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"config: unable to read {path}: {exc}") from exc


def parse_config(text: str) -> SchedulerConfig:
    """Parse the complete version-1 configuration schema."""

    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config: invalid YAML: {exc}") from exc

    mapping = _mapping(value, "config")
    _check_keys(mapping, _CONFIG_KEYS, "config")
    version = _integer(mapping, "version", "config")
    if version != 1:
        raise ConfigError("config.version: must be 1")
    user = _string(mapping, "user", "config")
    interval = _integer(mapping, "check_interval_seconds", "config")
    if not 30 <= interval <= 60:
        raise ConfigError("config.check_interval_seconds: must be between 30 and 60")
    grace = _integer(mapping, "termination_grace_seconds", "config")
    if not 1 <= grace <= 30:
        raise ConfigError("config.termination_grace_seconds: must be between 1 and 30")

    raw_rules = mapping.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ConfigError("config.rules: must be a non-empty list")
    rules = tuple(
        _parse_rule(raw_rule, index) for index, raw_rule in enumerate(raw_rules)
    )
    ids = [rule.id for rule in rules]
    if len(ids) != len(set(ids)):
        raise ConfigError("config.rules: rule ids must be unique")
    return SchedulerConfig(version, user, interval, grace, rules)


def resolve_user(username: str) -> pwd.struct_passwd:
    """Resolve a configured account without retaining a numeric UID in policy."""

    try:
        return pwd.getpwnam(username)
    except KeyError as exc:
        raise ConfigError(f"config.user: account {username!r} does not exist") from exc


def _parse_rule(value: Any, index: int) -> Rule:
    path = f"config.rules[{index}]"
    mapping = _mapping(value, path)
    _check_keys(mapping, _RULE_KEYS, path)
    rule_id = _string(mapping, "id", path)
    if not _RULE_ID.fullmatch(rule_id):
        raise ConfigError(f"{path}.id: must use lowercase letters, digits, and hyphens")
    block = _mapping(mapping.get("block"), f"{path}.block")
    _check_keys(block, _BLOCK_KEYS, f"{path}.block")
    start = _parse_time(_string(block, "from", f"{path}.block"), f"{path}.block.from")
    until = _parse_time(_string(block, "until", f"{path}.block"), f"{path}.block.until")
    if start == until:
        raise ConfigError(f"{path}.block: from and until must differ")
    return Rule(
        id=rule_id,
        name=_string(mapping, "name", path),
        bundle_id=_string(mapping, "bundle_id", path),
        process_name=_string(mapping, "process_name", path),
        block=TimeWindow(start, until),
    )


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ConfigError(f"{path}: keys must be strings")
    return value


def _check_keys(value: dict[str, Any], allowed: set[str], path: str) -> None:
    for key in value:
        if key not in allowed:
            raise ConfigError(f"{path}.{key}: unknown field")


def _string(value: dict[str, Any], key: str, path: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ConfigError(f"{path}.{key}: must be a non-empty string")
    return item


def _integer(value: dict[str, Any], key: str, path: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ConfigError(f"{path}.{key}: must be an integer")
    return item


def _parse_time(value: str, path: str) -> time:
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise ConfigError(f"{path}: must use HH:MM") from exc
    return parsed.time()
