"""Root-administered command-line interface."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import yaml

from mac_app_scheduler.config import load_config, parse_config, resolve_user
from mac_app_scheduler.daemon import run_daemon
from mac_app_scheduler.enforcer import Enforcer, ProcessAdapter
from mac_app_scheduler.launchd import LaunchdClient
from mac_app_scheduler.processes import DarwinProcessAdapter
from mac_app_scheduler.schedule import is_rule_blocked, next_transition
from mac_app_scheduler.state import StateStore, parse_duration

LOGGER = logging.getLogger("mac-app-scheduler")


@dataclass(frozen=True)
class AppPaths:
    config: Path = Path("/Library/Application Support/MacAppScheduler/config.yaml")
    state: Path = Path("/Library/Application Support/MacAppScheduler/state.json")
    lock: Path = Path("/Library/Application Support/MacAppScheduler/state.lock")


def main(
    argv: Sequence[str] | None = None,
    *,
    paths: AppPaths | None = None,
    euid: Callable[[], int] = os.geteuid,
    now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    launchd: Callable[[], LaunchdClient] = LaunchdClient,
    script_runner: Callable = subprocess.run,
    adapter: ProcessAdapter | None = None,
    uid_resolver: Callable[[str], int] = lambda user: resolve_user(user).pw_uid,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    paths = paths or AppPaths()
    if args.config is not None:
        paths = replace(paths, config=args.config)
    if args.command != "validate-config" and euid() != 0:
        parser.error("this command requires sudo")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        return _execute(args, paths, now, launchd, script_runner, adapter, uid_resolver)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"mac-app-scheduler: {exc}", file=sys.stderr)
        return 1


def _execute(args, paths, now, launchd, script_runner, adapter, uid_resolver) -> int:
    store = StateStore(paths.state, paths.lock)
    if args.command == "daemon":
        return run_daemon(paths.config, store, adapter)
    if args.command == "validate-config":
        load_config(paths.config)
        print("configuration is valid")
        return 0
    if args.command == "render-config":
        raw = yaml.safe_load(args.example.read_text(encoding="utf-8"))
        raw["user"] = args.user
        rendered = yaml.safe_dump(raw, sort_keys=False)
        parse_config(rendered)
        paths.config.write_text(rendered, encoding="utf-8")
        return 0
    if args.command in {"install", "uninstall"}:
        source = Path("/usr/local/libexec/mac-app-scheduler/source")
        command = [str(source / "scripts" / f"{args.command}.sh")]
        if args.command == "install":
            command.extend(["--user", args.user])
            if args.python:
                command.extend(["--python", args.python])
        elif args.yes:
            command.append("--yes")
        return script_runner(command, check=False).returncode
    if args.command in {"enable", "disable"}:
        store.set_enabled(args.command == "enable")
        message = f"scheduler {args.command}d"
        LOGGER.info(message)
        print(message)
        return 0
    config = load_config(paths.config)
    current = now()
    if args.command == "allow":
        if args.rule_id not in {rule.id for rule in config.rules}:
            raise ValueError(f"unknown rule id: {args.rule_id}")
        expiry = current.astimezone(UTC) + parse_duration(args.duration)
        store.set_override(args.rule_id, expiry)
        LOGGER.info("override created: %s until %s", args.rule_id, expiry.isoformat())
        print(f"allowed {args.rule_id} until {expiry.isoformat()}")
        return 0
    if args.command == "reload":
        result = launchd().kickstart()
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                f"launchctl kickstart failed ({result.returncode}): {detail}"
            )
        print("daemon reloaded")
        return 0
    state = store.read()
    if args.command == "status":
        print(f"scheduler: {'enabled' if state.enabled else 'disabled'}")
        service = launchd().status()
        print(f"launchd: {'loaded' if service.returncode == 0 else 'not loaded'}")
        try:
            uid_resolver(config.user)
            print(f"user: {config.user} (resolved)")
        except ValueError as exc:
            print(f"user: {config.user} (unresolved: {exc})")
        for rule in config.rules:
            blocked = state.enabled and is_rule_blocked(
                rule, current, state.overrides.get(rule.id)
            )
            print(
                f"rule: {rule.id}: {'blocked' if blocked else 'allowed'}; "
                f"next schedule boundary {next_transition(rule, current).isoformat()}"
            )
        for rule_id, expiry in state.overrides.items():
            if expiry > current.astimezone(UTC):
                print(f"override: {rule_id} until {expiry.isoformat()}")
        return 0
    result = Enforcer(
        adapter or DarwinProcessAdapter(), uid_resolver, lambda _: None
    ).reconcile(config, state, current, dry_run=True)
    for action in result.actions:
        print(
            f"would send {action.signal.name} to {action.rule_id} (PID {action.pid}); "
            "surviving bundle processes may require SIGKILL after the grace period"
        )
    for error in result.errors:
        print(error, file=sys.stderr)
    if not result.actions and not result.errors:
        print("no processes would be terminated")
    return 1 if result.errors else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mac-app-scheduler")
    parser.add_argument(
        "--config", type=Path, help="configuration path (before command)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "validate-config",
        "daemon",
        "status",
        "reload",
        "dry-run",
        "enable",
        "disable",
    ):
        commands.add_parser(name)
    install = commands.add_parser("install")
    install.add_argument("--user", required=True)
    install.add_argument("--python")
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--yes", action="store_true")
    render = commands.add_parser("render-config")
    render.add_argument("--example", type=Path, required=True)
    render.add_argument("--user", required=True)
    allow = commands.add_parser("allow")
    allow.add_argument("rule_id")
    allow.add_argument("duration")
    return parser
