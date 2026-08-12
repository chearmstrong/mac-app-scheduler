"""Root-administered command-line interface."""

from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from mac_app_scheduler.config import load_config
from mac_app_scheduler.daemon import run_daemon, run_once
from mac_app_scheduler.launchd import LaunchdClient
from mac_app_scheduler.processes import DarwinProcessAdapter
from mac_app_scheduler.state import StateStore, parse_duration


@dataclass(frozen=True)
class AppPaths:
    """Installed permanent and mutable policy locations."""

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
) -> int:
    """Run one administration command."""

    paths = paths or AppPaths()
    parser = _parser()
    args = parser.parse_args(argv)
    state_store = StateStore(paths.state, paths.lock)
    if args.command == "daemon":
        return run_daemon(paths.config, state_store)
    if args.command == "validate-config":
        load_config(paths.config)
        print("configuration is valid")
        return 0
    if euid() != 0:
        parser.error("this command requires sudo")
    if args.command == "dry-run":
        run_once(
            paths.config,
            state_store.prune_expired(now()),
            DarwinProcessAdapter(),
            dry_run=True,
        )
        print("dry run complete")
        return 0
    if args.command == "status":
        config = load_config(paths.config)
        state = state_store.read()
        print(f"scheduler: {'enabled' if state.enabled else 'disabled'}")
        print(f"user: {config.user}")
        for rule in config.rules:
            print(f"rule: {rule.id}")
        return 0
    if args.command == "reload":
        load_config(paths.config)
        launchd().kickstart()
        print("daemon reloaded")
        return 0
    if args.command in {"install", "uninstall"}:
        script = (
            Path("/usr/local/libexec/mac-app-scheduler/source")
            / "scripts"
            / (f"{args.command}.sh")
        )
        return subprocess.run([str(script), *args.script_args], check=False).returncode
    if args.command in {"enable", "disable"}:
        state_store.set_enabled(args.command == "enable")
        print(f"scheduler {args.command}d")
        return 0
    if args.command == "allow":
        config = load_config(paths.config)
        if args.rule_id not in {rule.id for rule in config.rules}:
            parser.error(f"unknown rule id: {args.rule_id}")
        expiry = now().astimezone() + parse_duration(args.duration)
        state_store.set_override(args.rule_id, expiry)
        print(f"allowed {args.rule_id} until {expiry.isoformat()}")
        return 0
    parser.error(f"unsupported command: {args.command}")
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mac-app-scheduler")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-config")
    commands.add_parser("daemon")
    commands.add_parser("status")
    commands.add_parser("reload")
    commands.add_parser("dry-run")
    for command in ("install", "uninstall"):
        subparser = commands.add_parser(command)
        subparser.add_argument("script_args", nargs=argparse.REMAINDER)
    commands.add_parser("enable")
    commands.add_parser("disable")
    allow = commands.add_parser("allow")
    allow.add_argument("rule_id")
    allow.add_argument("duration")
    return parser
