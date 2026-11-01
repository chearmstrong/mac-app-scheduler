import os
import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from mac_app_scheduler.installer import Installer, InstallPaths, _secure

ROOT = Path(__file__).parents[1]


class Commands:
    def __init__(self, paths, fail=None, loaded=False):
        self.paths, self.fail, self.loaded = paths, fail, loaded
        self.commands = []

    def __call__(self, args, **kwargs):
        args = [str(arg) for arg in args]
        self.commands.append(args)
        code = 0
        if "-m" in args and "venv" in args:
            (self.paths.libexec / "venv/bin").mkdir(parents=True)
            (self.paths.libexec / "venv/bin/python").touch()
        if "render-config" in args:
            self.paths.config.write_text("rendered config")
        if args[0] == "/bin/launchctl":
            if args[1] == "print":
                code = 0 if self.loaded else 113
            if args[1] == "bootstrap":
                self.loaded = True
            if args[1] == "bootout":
                self.loaded = False
        if self.fail and self.fail in args:
            self.fail = None  # Rollback commands can succeed.
            code = 5
        return CompletedProcess(args, code, "", "injected failure" if code else "")


def test_fresh_install_creates_final_venv_and_starts_validated_service(tmp_path):
    paths = InstallPaths(tmp_path)
    commands = Commands(paths)
    Installer(paths, runner=commands, ownership=lambda _: None).install(
        ROOT, "example-user", "/python"
    )
    assert paths.libexec.joinpath("source/scripts/install.sh").is_file()
    assert paths.wrapper.is_symlink()
    assert paths.config.read_text() == "rendered config"
    assert commands.loaded
    validation = next(
        i for i, c in enumerate(commands.commands) if "validate-config" in c
    )
    bootstrap = next(i for i, c in enumerate(commands.commands) if "bootstrap" in c)
    assert validation < bootstrap
    assert paths.plist.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("failure", ["venv", "validate-config", "-lint", "bootstrap"])
def test_failed_update_restores_previous_files_and_service(tmp_path, failure):
    paths = InstallPaths(tmp_path)
    paths.libexec.mkdir(parents=True)
    paths.libexec.joinpath("old").write_text("old code")
    paths.config.parent.mkdir(parents=True)
    paths.config.write_text("old config")
    paths.plist.parent.mkdir(parents=True)
    paths.plist.write_text("old plist")
    paths.wrapper.parent.mkdir(parents=True)
    paths.wrapper.symlink_to(paths.libexec / "old")
    commands = Commands(paths, failure, loaded=True)
    with pytest.raises(RuntimeError, match="injected failure"):
        Installer(paths, runner=commands, ownership=lambda _: None).install(
            ROOT, "example-user", "/python"
        )
    assert paths.libexec.joinpath("old").read_text() == "old code"
    assert paths.config.read_text() == "old config"
    assert paths.plist.read_text() == "old plist"
    assert paths.wrapper.readlink() == paths.libexec / "old"
    assert commands.loaded


def test_scripts_are_executable_and_default_python_lookup_does_not_run_interpreter():
    for name in ("install", "uninstall"):
        script = ROOT / f"scripts/{name}.sh"
        assert os.access(script, os.X_OK)
        result = subprocess.run(
            [str(script), "--help"], capture_output=True, text=True, timeout=5
        )
        assert result.returncode == 0
        assert "Usage:" in result.stdout


@pytest.mark.parametrize("explicit", [False, True])
def test_shell_bootstrap_locates_python_and_forwards_arguments(tmp_path, explicit):
    interpreter = tmp_path / "python3"
    record = tmp_path / "arguments"
    interpreter.write_text(
        '#!/bin/bash\nif [[ "$1" == -c ]]; then exit 0; fi\n'
        'printf "%s\\n" "$@" > "$BOOTSTRAP_RECORD"\n'
    )
    interpreter.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "BOOTSTRAP_RECORD": str(record),
    }
    args = [str(ROOT / "scripts/install.sh"), "--user", "example-user"]
    if explicit:
        args.extend(["--python", str(interpreter)])
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    forwarded = record.read_text().splitlines()
    assert forwarded[0].endswith("/src/mac_app_scheduler/installer.py")
    assert forwarded[-2:] == ["--user", "example-user"]


def test_repair_can_stage_from_its_installed_source_before_replacement(tmp_path):
    paths = InstallPaths(tmp_path)
    installer = Installer(paths, runner=Commands(paths), ownership=lambda _: None)
    installer.install(ROOT, "example-user", "/python")
    installer.install(paths.libexec / "source", "example-user", "/python")
    assert paths.libexec.joinpath("source/config.example.yaml").is_file()


def test_unknown_launchd_status_prevents_mutation(tmp_path):
    paths = InstallPaths(tmp_path)
    paths.libexec.mkdir(parents=True)
    (paths.libexec / "old").touch()
    commands = Commands(paths, fail="print")
    with pytest.raises(RuntimeError):
        Installer(paths, commands, lambda _: None).install(
            ROOT, "example-user", "/python"
        )
    assert (paths.libexec / "old").exists()


def test_uninstall_requires_successful_bootout_before_removing_files(tmp_path):
    paths = InstallPaths(tmp_path)
    paths.libexec.mkdir(parents=True)
    (paths.libexec / "old").touch()
    commands = Commands(paths, fail="bootout", loaded=True)
    with pytest.raises(RuntimeError):
        Installer(paths, commands, lambda _: None).uninstall()
    assert (paths.libexec / "old").exists()


def test_rollback_keeps_backup_if_old_files_cannot_be_restored(tmp_path, monkeypatch):
    paths = InstallPaths(tmp_path)
    paths.libexec.mkdir(parents=True)
    (paths.libexec / "old").write_text("recoverable code")
    original_rename = Path.rename

    def rename(path, destination):
        if path.name == "previous-library":
            raise PermissionError("injected restore failure")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(RuntimeError, match="backup retained"):
        Installer(paths, Commands(paths, fail="venv"), lambda _: None).install(
            ROOT, "example-user", "/python"
        )
    backups = list(
        paths.libexec.parent.glob(".mac-app-scheduler-*/previous-library/old")
    )
    assert len(backups) == 1
    assert backups[0].read_text() == "recoverable code"


def test_install_ownership_preserves_private_state_modes(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    state.write_text("{}")
    state.chmod(0o600)
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: None)
    _secure(tmp_path)
    assert state.stat().st_mode & 0o777 == 0o600


def test_interrupted_update_restores_old_installation(tmp_path):
    paths = InstallPaths(tmp_path)
    paths.libexec.mkdir(parents=True)
    (paths.libexec / "old").write_text("old code")
    commands = Commands(paths)

    def interrupted(args, **kwargs):
        if "validate-config" in args:
            raise KeyboardInterrupt()
        return commands(args, **kwargs)

    with pytest.raises(KeyboardInterrupt):
        Installer(paths, interrupted, lambda _: None).install(
            ROOT, "example-user", "/python"
        )
    assert (paths.libexec / "old").read_text() == "old code"
