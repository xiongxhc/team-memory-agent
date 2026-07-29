"""Portable scheduling facade for the one-shot daily hub command."""

import re
import shutil
import sys
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Callable

from .config import Config


LABEL = "org.teammem.hub-daily"
DEFAULT_TIME = "18:20"
SYSTEMD_SERVICE = "teammem-daily.service"
SYSTEMD_TIMER = "teammem-daily.timer"
_TIME = re.compile(r"([0-9]{2}):([0-9]{2})")

Runner = Callable[..., Any]


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
    if current == "win32":
        return "windows"
    raise RuntimeError(f"unsupported scheduling platform: {current}")


def _parse_time(value: str) -> tuple[int, int]:
    match = _TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("schedule time must be HH:MM")
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        raise ValueError("schedule time must be HH:MM")
    return hour, minute


def _executable(value: str | None) -> str:
    command = value or shutil.which("teammem")
    if not command:
        raise RuntimeError("teammem executable is not on PATH")
    return command


def _unix_backend():
    return import_module("teammem.schedule_unix")


def _windows_backend():
    return import_module("teammem.schedule_windows")


def install_schedule(
    cfg: Config,
    time: str = DEFAULT_TIME,
    platform: str | None = None,
    executable: str | None = None,
    agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    runner: Runner | None = None,
    windows_api: Any = None,
    windows_runner: Runner | None = None,
    windows_state_dir: Path | None = None,
    windows_task_name: str | None = None,
) -> Path:
    """Install or replace the explicit user schedule for ``run-daily``."""
    hour, minute = _parse_time(time)
    backend = _backend(platform)
    command = _executable(executable)
    if backend in {"launchd", "systemd"}:
        return _unix_backend().install_schedule(
            cfg, hour, minute, command, backend=backend, agents_dir=agents_dir,
            systemd_dir=systemd_dir, runner=runner,
        )
    return _windows_backend().install_schedule(
        cfg, hour, minute, command, api=windows_api, runner=windows_runner,
        state_dir=windows_state_dir, task_name_override=windows_task_name,
    )


def schedule_status(
    platform: str | None = None,
    agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    runner: Runner | None = None,
    windows_api: Any = None,
    windows_runner: Runner | None = None,
    windows_state_dir: Path | None = None,
    windows_task_name: str | None = None,
) -> ScheduleStatus:
    """Read status without creating artifacts or changing scheduler state."""
    backend = _backend(platform)
    if backend in {"launchd", "systemd"}:
        return _unix_backend().schedule_status(
            backend=backend, agents_dir=agents_dir, systemd_dir=systemd_dir,
            runner=runner,
        )
    return _windows_backend().schedule_status(
        api=windows_api, runner=windows_runner, state_dir=windows_state_dir,
        task_name_override=windows_task_name,
    )


def remove_schedule(
    platform: str | None = None,
    agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    runner: Runner | None = None,
    windows_api: Any = None,
    windows_runner: Runner | None = None,
    windows_state_dir: Path | None = None,
    windows_task_name: str | None = None,
) -> bool:
    """Remove an installed schedule, returning false when none exists."""
    backend = _backend(platform)
    if backend in {"launchd", "systemd"}:
        return _unix_backend().remove_schedule(
            backend=backend, agents_dir=agents_dir, systemd_dir=systemd_dir,
            runner=runner,
        )
    return _windows_backend().remove_schedule(
        api=windows_api, runner=windows_runner, state_dir=windows_state_dir,
        task_name_override=windows_task_name,
    )
