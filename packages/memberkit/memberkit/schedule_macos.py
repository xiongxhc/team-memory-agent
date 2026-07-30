"""macOS launchd scheduling backend for MemberKit."""

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .schedule import ScheduleStatus


LABEL = "org.teammem.memberkit-daily"


def _agents_dir(agents_dir: Path | None) -> Path:
    return agents_dir or Path.home() / "Library" / "LaunchAgents"


def _schedule_path(agents_dir: Path | None) -> Path:
    return _agents_dir(agents_dir) / f"{LABEL}.plist"


def install_schedule(
    config: Config,
    hour: int,
    minute: int,
    executable: str,
    *,
    agents_dir: Path | None = None,
    runner: Callable[..., Any] | None = None,
) -> Path:
    path = _schedule_path(agents_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    config.workdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [executable, "scheduled-run"],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(config.workdir / "schedule.log"),
        "StandardErrorPath": str(config.workdir / "schedule.err"),
    }
    path.write_bytes(plistlib.dumps(payload, sort_keys=True))
    if agents_dir is None:
        domain = f"gui/{os.getuid()}"
        run = runner or subprocess.run
        run(
            ["launchctl", "bootout", domain, str(path)],
            capture_output=True,
            text=True,
        )
        run(
            ["launchctl", "bootstrap", domain, str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    return path


def schedule_status(
    *,
    agents_dir: Path | None = None,
    runner: Callable[..., Any] | None = None,
) -> ScheduleStatus:
    path = _schedule_path(agents_dir)
    if not path.exists():
        return ScheduleStatus(False, path)
    try:
        interval = plistlib.loads(path.read_bytes())["StartCalendarInterval"]
        time = f"{interval['Hour']:02d}:{interval['Minute']:02d}"
    except (KeyError, ValueError, TypeError):
        time = None
    return ScheduleStatus(True, path, time)


def remove_schedule(
    *,
    agents_dir: Path | None = None,
    runner: Callable[..., Any] | None = None,
) -> bool:
    path = _schedule_path(agents_dir)
    if not path.exists():
        return False
    if agents_dir is None:
        domain = f"gui/{os.getuid()}"
        run = runner or subprocess.run
        run(
            ["launchctl", "bootout", domain, str(path)],
            capture_output=True,
            text=True,
        )
    path.unlink()
    return True


def notify_pending(
    dates: list[str], *, runner: Callable[..., Any] | None = None,
) -> None:
    if not dates:
        return
    joined = ", ".join(dates)
    script = (
        f'display notification "Review: {joined}" '
        'with title "MemberKit drafts ready"'
    )
    run = runner or subprocess.run
    run(["osascript", "-e", script], capture_output=True, text=True)
