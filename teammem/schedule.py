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
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
    result = _run(
        runner,
        ["launchctl", "print", _launchd_target()],
        check=False,
    )
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
    if _launchd_loaded(runner):
        _run(
            runner,
            ["launchctl", "bootout", _launchd_target()],
            check=True,
        )

    state_dir = Path.home() / ".local" / "state" / "teammem"
    if agents_dir is None:
        state_dir.mkdir(parents=True, exist_ok=True)
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
    _write_atomic(path, plistlib.dumps(payload, sort_keys=True))
    _run(
        runner,
        ["launchctl", "bootstrap", _launchd_domain(), str(path)],
        check=True,
    )
    return path


def _systemd_exec(arguments: Sequence[str]) -> str:
    return shlex.join([argument.replace("%", "%%") for argument in arguments])


def _install_systemd(
    cfg: Config,
    hour: int,
    minute: int,
    executable: str,
    systemd_dir: Path | None,
    runner: Runner | None,
) -> Path:
    service_path, timer_path = _systemd_paths(systemd_dir)
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
    _write_atomic(service_path, service.encode())
    _write_atomic(timer_path, timer.encode())
    _run(
        runner,
        ["systemctl", "--user", "daemon-reload"],
        check=True,
    )
    _run(
        runner,
        ["systemctl", "--user", "enable", "--now", SYSTEMD_TIMER],
        check=True,
    )
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


def schedule_status(
    platform: str | None = None,
    agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    runner: Runner | None = None,
) -> ScheduleStatus:
    """Read the installed definition without changing scheduler state."""
    del runner
    backend = _backend(platform)
    if backend == "launchd":
        path = _launchd_path(agents_dir)
        return ScheduleStatus(
            path.exists(),
            _launchd_time(path) if path.exists() else None,
            backend,
            path,
        )

    service_path, timer_path = _systemd_paths(systemd_dir)
    installed = service_path.exists() and timer_path.exists()
    return ScheduleStatus(
        installed,
        _systemd_time(timer_path) if installed else None,
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
            _run(
                runner,
                ["launchctl", "bootout", _launchd_target()],
                check=True,
            )
        path.unlink()
        return True

    service_path, timer_path = _systemd_paths(systemd_dir)
    if not service_path.exists() and not timer_path.exists():
        return False
    _run(
        runner,
        ["systemctl", "--user", "disable", "--now", SYSTEMD_TIMER],
        check=True,
    )
    timer_path.unlink(missing_ok=True)
    service_path.unlink(missing_ok=True)
    _run(
        runner,
        ["systemctl", "--user", "daemon-reload"],
        check=True,
    )
    return True
