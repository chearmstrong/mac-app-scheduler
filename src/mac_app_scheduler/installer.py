"""Transactional system installation; stdlib-only so bootstrap needs no packages."""

from __future__ import annotations

import argparse
import fcntl
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

LABEL = "io.github.mac-app-scheduler.daemon"
SERVICE = f"system/{LABEL}"


@dataclass(frozen=True)
class InstallPaths:
    root: Path = Path("/")

    @property
    def libexec(self):
        return self.root / "usr/local/libexec/mac-app-scheduler"

    @property
    def wrapper(self):
        return self.root / "usr/local/bin/mac-app-scheduler"

    @property
    def support(self):
        return self.root / "Library/Application Support/MacAppScheduler"

    @property
    def config(self):
        return self.support / "config.yaml"

    @property
    def plist(self):
        return self.root / f"Library/LaunchDaemons/{LABEL}.plist"


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _secure(path: Path) -> None:
    items = [path, *path.rglob("*")] if path.is_dir() else [path]
    for item in items:
        os.chown(item, 0, 0, follow_symlinks=False)
        if not item.is_symlink():
            if item.name in {"state.json", "state.lock"} and item.is_file():
                item.chmod(0o600)
            else:
                item.chmod(
                    0o755 if item.is_dir() or os.access(item, os.X_OK) else 0o644
                )


@contextmanager
def _staging(parent):
    stage = Path(tempfile.mkdtemp(prefix=".mac-app-scheduler-", dir=parent))
    try:
        yield stage
    finally:
        if not (stage / "RECOVERY_REQUIRED").exists():
            shutil.rmtree(stage)


class Installer:
    def __init__(self, paths: InstallPaths, runner=subprocess.run, ownership=_secure):
        self.paths, self.runner, self.ownership = paths, runner, ownership

    def command(self, args, *, check=True):
        result = self.runner([str(arg) for arg in args], capture_output=True, text=True)
        if check and result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"{' '.join(map(str, args))}: {detail}")
        return result

    def _check_paths(self):
        for path in (
            self.paths.libexec,
            self.paths.config,
            self.paths.plist,
            self.paths.wrapper,
        ):
            for parent in path.parents:
                if parent == self.paths.root:
                    break
                if parent.is_symlink():
                    raise RuntimeError(f"unsafe symlinked directory: {parent}")
            if path != self.paths.wrapper and path.is_symlink():
                raise RuntimeError(f"unsafe symlinked destination: {path}")
        if self.paths.wrapper.is_dir():
            raise RuntimeError("executable destination is a directory")

    def install(self, source: Path, user: str, python: str) -> None:
        self._check_paths()
        self.paths.libexec.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.paths.libexec.parent / ".mac-app-scheduler.install.lock"
        with lock_path.open("a") as lock:
            lock_path.chmod(0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._install(source, user, python)

    def _install(self, source: Path, user: str, python: str) -> None:
        paths = self.paths
        with _staging(paths.libexec.parent) as stage:
            staged_source = stage / "source"
            staged_source.mkdir()
            for name in (
                "src",
                "scripts",
                "launchd",
                "pyproject.toml",
                "config.example.yaml",
            ):
                item = source / name
                if item.is_dir():
                    shutil.copytree(
                        item,
                        staged_source / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                    )
                else:
                    shutil.copy2(item, staged_source / name)
            # Repair may run from the installed source, so snapshot it first.
            files = (paths.config, paths.plist, paths.wrapper)
            snapshots = {}
            for index, item in enumerate(files):
                backup = stage / f"file-{index}"
                if item.exists() or item.is_symlink():
                    shutil.copy2(item, backup, follow_symlinks=False)
                    snapshots[item] = backup
                else:
                    snapshots[item] = None
            old_library = stage / "previous-library"
            status = self.command(["/bin/launchctl", "print", SERVICE], check=False)
            if status.returncode not in (0, 113):
                raise RuntimeError(f"cannot determine service state: {status.stderr}")
            loaded = status.returncode == 0
            stopped = False
            replacement_started = False
            try:
                if paths.libexec.exists():
                    paths.libexec.rename(old_library)
                replacement_started = True
                paths.libexec.mkdir()
                shutil.copytree(staged_source, paths.libexec / "source")
                self.command([python, "-m", "venv", paths.libexec / "venv"])
                runtime = paths.libexec / "venv/bin/python"
                self.command(
                    [runtime, "-m", "pip", "install", paths.libexec / "source"]
                )
                paths.support.mkdir(parents=True, exist_ok=True)
                cli = [runtime, "-m", "mac_app_scheduler", "--config", paths.config]
                if snapshots[paths.config] is None:
                    self.command(
                        [
                            *cli,
                            "render-config",
                            "--example",
                            staged_source / "config.example.yaml",
                            "--user",
                            user,
                        ]
                    )
                self.command([*cli, "validate-config"])
                paths.plist.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staged_source / f"launchd/{LABEL}.plist", paths.plist)
                paths.plist.chmod(0o644)
                self.command(["/usr/bin/plutil", "-lint", paths.plist])
                self.ownership(paths.libexec)
                self.ownership(paths.support)
                self.ownership(paths.plist)
                for state in (
                    paths.support / "state.json",
                    paths.support / "state.lock",
                ):
                    if state.exists():
                        state.chmod(0o600)
                paths.wrapper.parent.mkdir(parents=True, exist_ok=True)
                _remove(paths.wrapper)
                paths.wrapper.symlink_to(paths.libexec / "venv/bin/mac-app-scheduler")
                if loaded:
                    self.command(["/bin/launchctl", "bootout", SERVICE])
                stopped = True
                self.command(["/bin/launchctl", "bootstrap", "system", paths.plist])
                self.command(["/bin/launchctl", "print", SERVICE])
            except BaseException as exc:
                try:
                    if stopped:
                        self.command(
                            ["/bin/launchctl", "bootout", SERVICE], check=False
                        )
                    if replacement_started:
                        _remove(paths.libexec)
                    if old_library.exists():
                        old_library.rename(paths.libexec)
                    for item, backup in snapshots.items():
                        _remove(item)
                        if backup is not None:
                            shutil.copy2(backup, item, follow_symlinks=False)
                    if loaded and stopped:
                        self.command(
                            ["/bin/launchctl", "bootstrap", "system", paths.plist]
                        )
                except BaseException as recovery:
                    (stage / "RECOVERY_REQUIRED").write_text(str(recovery))
                    raise RuntimeError(
                        f"installation failed: {exc}; recovery failed: {recovery}; "
                        f"backup retained at {stage}"
                    ) from exc
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise RuntimeError(
                    f"installation failed; previous files restored: {exc}"
                ) from exc

    def uninstall(self) -> None:
        self._check_paths()
        status = self.command(["/bin/launchctl", "print", SERVICE], check=False)
        if status.returncode == 0:
            self.command(["/bin/launchctl", "bootout", SERVICE])
        elif status.returncode != 113:
            raise RuntimeError(f"cannot determine service state: {status.stderr}")
        for item in (
            self.paths.wrapper,
            self.paths.plist,
            self.paths.libexec,
            self.paths.support,
        ):
            _remove(item)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["install", "uninstall"])
    parser.add_argument("--user")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() != 0 or sys.platform != "darwin":
        parser.error("installation requires sudo on macOS")
    installer = Installer(InstallPaths())
    try:
        if args.command == "uninstall":
            if not args.yes:
                parser.error(
                    "pass --yes to remove installed code, configuration and overrides"
                )
            installer.uninstall()
            print(
                "Removed Mac App Scheduler, configuration and overrides. "
                "Existing diagnostic logs are retained."
            )
        else:
            if not args.user:
                parser.error("--user is required")
            if sys.version_info < (3, 11):  # noqa: UP036 -- bootstrap preflight
                parser.error("administrator-installed Python 3.11+ is required")
            version = installer.command(
                ["/usr/bin/sw_vers", "-productVersion"]
            ).stdout.strip()
            if int(version.split(".")[0]) < 13:
                parser.error("macOS 13 or newer is required")
            account = pwd.getpwnam(args.user)
            membership = installer.command(
                [
                    "/usr/bin/dsmemberutil",
                    "checkmembership",
                    "-U",
                    args.user,
                    "-G",
                    "admin",
                ]
            ).stdout
            if account.pw_uid < 500 or "not a member" not in membership:
                parser.error("managed user must be a standard local account")
            print(f"Using Python: {sys.executable}", flush=True)
            installer.install(args.source.resolve(), args.user, args.python)
            print("Installed. Run: sudo mac-app-scheduler status")
    except (OSError, RuntimeError, KeyError, ValueError) as exc:
        print(f"mac-app-scheduler: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
