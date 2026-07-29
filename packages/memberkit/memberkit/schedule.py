"""Opt-in local scheduling for draft preparation.

The scheduled command never imports the push module and never transmits data.
"""

import json
import plistlib
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import bundle
from .config import Config
from .state import DraftState


LABEL = "org.teammem.memberkit-daily"
DEFAULT_TIME = "17:30"


@dataclass(frozen=True)
class ScheduleStatus:
    installed: bool
    path: Path
    time: str | None = None


def _agents_dir(agents_dir: Path | None) -> Path:
    return agents_dir or Path.home() / "Library" / "LaunchAgents"


def _schedule_path(agents_dir: Path | None) -> Path:
    return _agents_dir(agents_dir) / f"{LABEL}.plist"


def _parse_time(value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError) as exc:
        raise ValueError("schedule time must be HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("schedule time must be HH:MM")
    return hour, minute


def install_schedule(config: Config, time: str = DEFAULT_TIME,
                     agents_dir: Path | None = None,
                     executable: str | None = None) -> Path:
    hour, minute = _parse_time(time)
    path = _schedule_path(agents_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    config.workdir.mkdir(parents=True, exist_ok=True)
    command = executable or shutil.which("memberkit")
    if not command:
        raise RuntimeError("memberkit executable is not on PATH")
    payload = {
        "Label": LABEL,
        "ProgramArguments": [command, "scheduled-run"],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(config.workdir / "schedule.log"),
        "StandardErrorPath": str(config.workdir / "schedule.err"),
    }
    path.write_bytes(plistlib.dumps(payload, sort_keys=True))
    if agents_dir is None:
        domain = f"gui/{__import__('os').getuid()}"
        subprocess.run(
            ["launchctl", "bootout", domain, str(path)],
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["launchctl", "bootstrap", domain, str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    return path


def schedule_status(agents_dir: Path | None = None) -> ScheduleStatus:
    path = _schedule_path(agents_dir)
    if not path.exists():
        return ScheduleStatus(False, path)
    try:
        interval = plistlib.loads(path.read_bytes())["StartCalendarInterval"]
        time = f"{interval['Hour']:02d}:{interval['Minute']:02d}"
    except (KeyError, ValueError, TypeError):
        time = None
    return ScheduleStatus(True, path, time)


def remove_schedule(agents_dir: Path | None = None) -> bool:
    path = _schedule_path(agents_dir)
    if not path.exists():
        return False
    if agents_dir is None:
        domain = f"gui/{__import__('os').getuid()}"
        subprocess.run(
            ["launchctl", "bootout", domain, str(path)],
            capture_output=True,
            text=True,
        )
    path.unlink()
    return True


def _notify_pending(dates: list[str]) -> None:
    if not dates:
        return
    joined = ", ".join(dates)
    script = (
        f'display notification "Review: {joined}" '
        'with title "MemberKit drafts ready"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True, text=True)


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
