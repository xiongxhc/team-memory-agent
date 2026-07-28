"""Explicit user-level scheduling for the one-shot daily hub command."""

import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .config import Config


LABEL = "org.teammem.hub-daily"
DEFAULT_TIME = "18:20"
SYSTEMD_SERVICE = "teammem-daily.service"
SYSTEMD_TIMER = "teammem-daily.timer"
_TIME = re.compile(r"([0-9]{2}):([0-9]{2})")

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ScheduleStatus:
    installed: bool
    time: str | None
    backend: str
    path: Path


def _backend(platform: str | None) -> str:
    current = sys.platform if platform is None else platform
    if current == "darwin":
        return "launchd"
    if current.startswith("linux"):
        return "systemd"
    raise RuntimeError(f"unsupported scheduling platform: {current}")


def _parse_time(value: str) -> tuple[int, int]:
    match = _TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("schedule time must be HH:MM")
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        raise ValueError("schedule time must be HH:MM")
    return hour, minute


def _agents_dir(path: Path | None) -> Path:
    return Path.home() / "Library" / "LaunchAgents" if path is None else Path(path)


def _systemd_dir(path: Path | None) -> Path:
    return (
        Path.home() / ".config" / "systemd" / "user"
        if path is None
        else Path(path)
    )


def _launchd_path(agents_dir: Path | None) -> Path:
    return _agents_dir(agents_dir) / f"{LABEL}.plist"


def _systemd_paths(systemd_dir: Path | None) -> tuple[Path, Path]:
    directory = _systemd_dir(systemd_dir)
    return directory / SYSTEMD_SERVICE, directory / SYSTEMD_TIMER


def _run(
    runner: Runner | None,
    command: Sequence[str],
    *,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    execute = subprocess.run if runner is None else runner
    return execute(
        list(command),
        check=check,
        capture_output=True,
        text=True,
    )


def _launchctl(
    runner: Runner | None,
    *arguments: str,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    return _run(runner, ["launchctl", *arguments], check=check)


def _systemctl(
    runner: Runner | None,
    *arguments: str,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    return _run(runner, ["systemctl", "--user", *arguments], check=check)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise PermissionError(f"schedule state directory is not user-owned: {path}")
    path.chmod(0o700)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            temporary.chmod(0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _unlink_durable(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    _fsync_directory(path.parent)
    return True


def _executable(value: str | None) -> str:
    command = value or shutil.which("teammem")
    if not command:
        raise RuntimeError("teammem executable is not on PATH")
    return command


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchd_target() -> str:
    return f"{_launchd_domain()}/{LABEL}"


def _launchd_loaded(runner: Runner | None) -> bool:
    result = _launchctl(runner, "print", _launchd_target(), check=False)
    return result.returncode == 0


def _install_launchd(
    cfg: Config,
    hour: int,
    minute: int,
    executable: str,
    agents_dir: Path | None,
    runner: Runner | None,
) -> Path:
    path = _launchd_path(agents_dir)
    previous = path.read_bytes() if path.exists() else None
    was_loaded = _launchd_loaded(runner)
    if was_loaded and previous is None:
        raise RuntimeError(
            "cannot replace loaded launchd schedule without its definition"
        )
    if was_loaded:
        _launchctl(runner, "bootout", _launchd_target(), check=True)

    state_dir = Path.home() / ".local" / "state" / "teammem"
    if agents_dir is None:
        _ensure_private_directory(state_dir)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            executable,
            "--env-file",
            str(cfg.env_file),
            "run-daily",
        ],
        "RunAtLoad": False,
        "StandardErrorPath": str(state_dir / "schedule.err"),
        "StandardOutPath": str(state_dir / "schedule.log"),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
    }
    try:
        _write_atomic(path, plistlib.dumps(payload, sort_keys=True))
        _launchctl(
            runner, "bootstrap", _launchd_domain(), str(path), check=True
        )
    except Exception as failure:
        try:
            if previous is None:
                _unlink_durable(path)
            else:
                _write_atomic(path, previous)
            if was_loaded:
                _launchctl(
                    runner,
                    "bootstrap",
                    _launchd_domain(),
                    str(path),
                    check=True,
                )
        except Exception as rollback_failure:
            raise RuntimeError(
                "launchd schedule installation failed and rollback failed"
            ) from rollback_failure
        restored = (
            "previous schedule restored"
            if previous is not None
            else "previous state restored"
        )
        raise RuntimeError(
            f"launchd schedule installation failed; {restored}"
        ) from failure
    return path


def _systemd_exec(arguments: Sequence[str]) -> str:
    return shlex.join([argument.replace("%", "%%") for argument in arguments])


def _systemd_enabled(runner: Runner | None) -> bool:
    result = _systemctl(runner, "is-enabled", SYSTEMD_TIMER, check=False)
    return result.returncode == 0


def _set_systemd_enabled(runner: Runner | None, enabled: bool) -> None:
    action = "enable" if enabled else "disable"
    _systemctl(runner, action, "--now", SYSTEMD_TIMER, check=True)


def _reload_systemd(runner: Runner | None) -> None:
    _systemctl(runner, "daemon-reload", check=True)


def _snapshot_files(paths: Sequence[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.exists() else None for path in paths}


def _restore_files(snapshot: dict[Path, bytes | None]) -> None:
    for path, previous in snapshot.items():
        if previous is None:
            _unlink_durable(path)
        else:
            _write_atomic(path, previous)


def _install_systemd(
    cfg: Config,
    hour: int,
    minute: int,
    executable: str,
    systemd_dir: Path | None,
    runner: Runner | None,
) -> Path:
    service_path, timer_path = _systemd_paths(systemd_dir)
    previous = _snapshot_files([service_path, timer_path])
    was_enabled = (
        _systemd_enabled(runner) if previous[timer_path] is not None else False
    )
    service = (
        "[Unit]\n"
        "Description=Run Team Memory Agent daily workflow\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={_systemd_exec([executable, '--env-file', str(cfg.env_file), 'run-daily'])}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Run Team Memory Agent daily\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {hour:02d}:{minute:02d}:00\n"
        "Persistent=true\n"
        f"Unit={SYSTEMD_SERVICE}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    enable_attempted = False
    try:
        _write_atomic(service_path, service.encode())
        _write_atomic(timer_path, timer.encode())
        _reload_systemd(runner)
        enable_attempted = True
        _set_systemd_enabled(runner, True)
    except Exception as failure:
        try:
            _restore_files(previous)
            _reload_systemd(runner)
            if was_enabled:
                _set_systemd_enabled(runner, True)
            elif enable_attempted:
                _set_systemd_enabled(runner, False)
        except Exception as rollback_failure:
            raise RuntimeError(
                "systemd schedule installation failed and rollback failed"
            ) from rollback_failure
        raise RuntimeError(
            "systemd schedule installation failed; previous state restored"
        ) from failure
    return timer_path


def install_schedule(
    cfg: Config,
    time: str = DEFAULT_TIME,
    platform: str | None = None,
    executable: str | None = None,
    agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    runner: Runner | None = None,
) -> Path:
    """Install or replace the explicit user schedule for ``run-daily``."""
    hour, minute = _parse_time(time)
    backend = _backend(platform)
    command = _executable(executable)
    if backend == "launchd":
        return _install_launchd(
            cfg, hour, minute, command, agents_dir, runner
        )
    return _install_systemd(
        cfg, hour, minute, command, systemd_dir, runner
    )


def _launchd_time(path: Path) -> str | None:
    try:
        interval = plistlib.loads(path.read_bytes())["StartCalendarInterval"]
        hour = interval["Hour"]
        minute = interval["Minute"]
        if (
            type(hour) is not int
            or type(minute) is not int
            or not 0 <= hour <= 23
            or not 0 <= minute <= 59
        ):
            return None
        return f"{hour:02d}:{minute:02d}"
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _launchd_definition_is_valid(path: Path) -> bool:
    try:
        data = plistlib.loads(path.read_bytes())
        arguments = data["ProgramArguments"]
        return (
            data["Label"] == LABEL
            and data["RunAtLoad"] is False
            and _launchd_time(path) is not None
            and isinstance(arguments, list)
            and len(arguments) == 4
            and isinstance(arguments[0], str)
            and bool(arguments[0])
            and arguments[1] == "--env-file"
            and isinstance(arguments[2], str)
            and bool(arguments[2])
            and arguments[3] == "run-daily"
            and isinstance(data["StandardOutPath"], str)
            and isinstance(data["StandardErrorPath"], str)
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _systemd_time(path: Path) -> str | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    match = re.search(
        r"^OnCalendar=\*-\*-\* ([0-9]{2}):([0-9]{2}):00$",
        text,
        re.MULTILINE,
    )
    if match is None:
        return None
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _systemd_definition_is_valid(
    service_path: Path,
    timer_path: Path,
) -> bool:
    try:
        service = service_path.read_text()
        timer = timer_path.read_text()
        exec_lines = [
            line.removeprefix("ExecStart=")
            for line in service.splitlines()
            if line.startswith("ExecStart=")
        ]
        arguments = shlex.split(exec_lines[0]) if len(exec_lines) == 1 else []
    except (OSError, ValueError):
        return False
    return (
        "[Service]" in service.splitlines()
        and "Type=oneshot" in service.splitlines()
        and len(arguments) == 4
        and bool(arguments[0])
        and arguments[1] == "--env-file"
        and bool(arguments[2])
        and arguments[3] == "run-daily"
        and "[Timer]" in timer.splitlines()
        and _systemd_time(timer_path) is not None
        and "Persistent=true" in timer.splitlines()
        and f"Unit={SYSTEMD_SERVICE}" in timer.splitlines()
        and "WantedBy=timers.target" in timer.splitlines()
    )


def schedule_status(
    platform: str | None = None,
    agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    runner: Runner | None = None,
) -> ScheduleStatus:
    """Read valid definition and active user-scheduler state without mutation."""
    backend = _backend(platform)
    if backend == "launchd":
        path = _launchd_path(agents_dir)
        exists = path.exists()
        time = _launchd_time(path) if exists else None
        valid = exists and _launchd_definition_is_valid(path)
        return ScheduleStatus(
            valid and _launchd_loaded(runner),
            time,
            backend,
            path,
        )

    service_path, timer_path = _systemd_paths(systemd_dir)
    time = _systemd_time(timer_path) if timer_path.exists() else None
    valid = (
        service_path.exists()
        and timer_path.exists()
        and _systemd_definition_is_valid(service_path, timer_path)
    )
    return ScheduleStatus(
        valid and _systemd_enabled(runner),
        time,
        backend,
        timer_path,
    )


def remove_schedule(
    platform: str | None = None,
    agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    runner: Runner | None = None,
) -> bool:
    """Remove an installed schedule, returning false when none exists."""
    backend = _backend(platform)
    if backend == "launchd":
        path = _launchd_path(agents_dir)
        if not path.exists():
            return False
        if _launchd_loaded(runner):
            _launchctl(runner, "bootout", _launchd_target(), check=True)
        _unlink_durable(path)
        return True

    service_path, timer_path = _systemd_paths(systemd_dir)
    if not service_path.exists() and not timer_path.exists():
        return False
    if timer_path.exists() and _systemd_enabled(runner):
        _set_systemd_enabled(runner, False)
    _unlink_durable(timer_path)
    _unlink_durable(service_path)
    _reload_systemd(runner)
    return True
