"""Opt-in local scheduling for draft preparation.

The scheduled command never imports the push module and never transmits data.
"""

import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Literal

from . import bundle
from .config import Config
from .state import DraftState


DEFAULT_TIME = "17:30"


@dataclass(frozen=True)
class ScheduleStatus:
    installed: bool
    path: Path
    time: str | None = None


def _backend(platform: str | None) -> Literal["macos", "windows"]:
    selected = platform or sys.platform
    if selected == "darwin":
        return "macos"
    if selected == "win32":
        return "windows"
    raise ValueError(f"unsupported scheduling platform: {selected}")


def _parse_time(value: str) -> tuple[int, int]:
    if (
        not isinstance(value, str)
        or len(value) != 5
        or value[2] != ":"
        or not all("0" <= digit <= "9" for digit in value[:2] + value[3:])
    ):
        raise ValueError("schedule time must be HH:MM")
    hour, minute = int(value[:2]), int(value[3:])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("schedule time must be HH:MM")
    return hour, minute


def _executable(value: str | None) -> str:
    command = value or shutil.which("memberkit")
    if not command:
        raise RuntimeError("memberkit executable is not on PATH")
    return command


def _load_backend(backend: Literal["macos", "windows"]) -> Any:
    return import_module(f".schedule_{backend}", package=__package__)


def install_schedule(
    config: Config,
    time: str = DEFAULT_TIME,
    agents_dir: Path | None = None,
    executable: str | None = None,
    platform: str | None = None,
    runner: Callable[..., Any] | None = None,
    windows_api: Any = None,
    windows_runner: Callable[..., Any] | None = None,
    windows_state_dir: Path | None = None,
    windows_task_name: str | None = None,
) -> Path:
    backend = _backend(platform)
    hour, minute = _parse_time(time)
    command = _executable(executable)
    module = _load_backend(backend)
    if backend == "macos":
        return module.install_schedule(
            config,
            hour,
            minute,
            command,
            agents_dir=agents_dir,
            runner=runner,
        )
    return module.install_schedule(
        hour,
        minute,
        command,
        api=windows_api,
        runner=windows_runner,
        state_dir=windows_state_dir,
        task_name_override=windows_task_name,
    )


def schedule_status(
    agents_dir: Path | None = None,
    platform: str | None = None,
    runner: Callable[..., Any] | None = None,
    windows_api: Any = None,
    windows_runner: Callable[..., Any] | None = None,
    windows_state_dir: Path | None = None,
    windows_task_name: str | None = None,
    windows_executable: str | None = None,
) -> ScheduleStatus:
    backend = _backend(platform)
    module = _load_backend(backend)
    if backend == "macos":
        return module.schedule_status(agents_dir=agents_dir, runner=runner)
    return module.schedule_status(
        api=windows_api,
        runner=windows_runner,
        state_dir=windows_state_dir,
        task_name_override=windows_task_name,
        executable=_executable(windows_executable),
    )


def remove_schedule(
    agents_dir: Path | None = None,
    platform: str | None = None,
    runner: Callable[..., Any] | None = None,
    windows_api: Any = None,
    windows_runner: Callable[..., Any] | None = None,
    windows_state_dir: Path | None = None,
    windows_task_name: str | None = None,
    windows_executable: str | None = None,
) -> bool:
    backend = _backend(platform)
    module = _load_backend(backend)
    if backend == "macos":
        return module.remove_schedule(agents_dir=agents_dir, runner=runner)
    return module.remove_schedule(
        api=windows_api,
        runner=windows_runner,
        state_dir=windows_state_dir,
        task_name_override=windows_task_name,
        executable=_executable(windows_executable),
    )


def _notify_pending(dates: list[str]) -> None:
    _load_backend("macos").notify_pending(dates)


def _valid_existing_draft(data: object, config: Config, date: str) -> bool:
    try:
        bundle.validate_bundle(data, config.member, date)
    except ValueError:
        return False
    return True


def scheduled_run(config: Config, now: datetime | None = None,
                  notify: bool = True, timezone=None) -> list[str]:
    timezone = timezone or config.timezone or bundle._local_timezone()
    if now is None:
        now = datetime.now(timezone)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone)
    else:
        now = now.astimezone(timezone)
    state = DraftState(config.workdir / "state.json")
    output_dir = config.workdir / "out"
    pending_dates: list[str] = []

    for day in ((now.date() - timedelta(days=1)), now.date()):
        date_text = day.isoformat()
        path = output_dir / f"bundle-{config.member}-{date_text}.json"
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pending_dates.append(date_text)
                continue
            if not _valid_existing_draft(current, config, date_text):
                pending_dates.append(date_text)
                continue
            if state.refresh(date_text, discovered=[], current=current):
                pending_dates.append(date_text)
            continue
        discovered = bundle.draft(
            config.db,
            config.member,
            date_text,
            timezone=timezone,
        )
        bundle.validate_bundle(discovered, config.member, date_text)
        events = state.refresh(date_text, discovered["events"], current=None)
        if not events:
            continue
        data = {
            "schema": bundle.SCHEMA,
            "member": config.member,
            "date": date_text,
            "events": events,
            "journal_md": bundle.render_journal(events, date_text),
        }
        bundle.validate_bundle(data, config.member, date_text)
        output_dir.mkdir(parents=True, exist_ok=True)
        bundle.write_bundle(path, data)
        pending_dates.append(date_text)

    pending_dates = sorted(set(pending_dates) | set(state.pending_dates()))
    if notify:
        _notify_pending(pending_dates)
    return pending_dates
