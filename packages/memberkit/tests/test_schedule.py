import json
import plistlib
import sqlite3
import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from memberkit import bundle
from memberkit import schedule
from memberkit.config import Config
from memberkit.schedule import (
    install_schedule,
    remove_schedule,
    schedule_status,
    scheduled_run,
)
from memberkit.state import DraftState


def _cfg(tmp_path, rows=()):
    db = tmp_path / "claude-mem.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE observations (project TEXT, title TEXT, subtitle TEXT,"
        " narrative TEXT, type TEXT, created_at TEXT, created_at_epoch INTEGER)"
    )
    con.executemany("INSERT INTO observations VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return Config(
        member="alex",
        db=db,
        inbox_url="git@example.test:team/inbox.git",
        workdir=tmp_path / "work",
    )


def _row(title, iso):
    return (
        "project-alpha", title, None, None, "feature", iso,
        int(datetime.fromisoformat(iso).astimezone().timestamp() * 1000),
    )


def _local_ts(iso):
    return datetime.fromtimestamp(
        _row("", iso)[-1] / 1000,
        tz=bundle._local_timezone(),
    ).isoformat(timespec="milliseconds")


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("00:00", (0, 0)),
        ("17:30", (17, 30)),
        ("23:59", (23, 59)),
    ),
)
def test_schedule_time_strictly_parses_two_digit_hour_and_minute(value, expected):
    """Catches accepting a schedule time that is not exactly HH:MM."""
    assert schedule._parse_time(value) == expected


@pytest.mark.parametrize(
    "value",
    ("7:30", "07:3", "24:00", "12:60", " 07:30", "07:30 "),
)
def test_schedule_time_strictly_rejects_non_hhmm_values(value):
    """Catches shortened, out-of-range, or whitespace-padded times."""
    with pytest.raises(ValueError, match="schedule time must be HH:MM"):
        schedule._parse_time(value)


@pytest.mark.parametrize(
    ("platform", "expected"),
    (("darwin", "macos"), ("win32", "windows")),
)
def test_platform_facade_selects_the_supported_scheduler(platform, expected):
    """Catches mapping a supported platform to the wrong backend."""
    assert schedule._backend(platform) == expected


@pytest.mark.parametrize("platform", ("linux", "freebsd"))
def test_platform_facade_rejects_unsupported_platform_before_mutation(
    tmp_path, platform,
):
    """Catches unsupported platforms creating schedule or work directories."""
    cfg = _cfg(tmp_path)
    agents = tmp_path / "LaunchAgents"

    with pytest.raises(
        ValueError,
        match=rf"unsupported scheduling platform: {platform}",
    ):
        schedule.install_schedule(
            cfg,
            agents_dir=agents,
            executable="/opt/memberkit",
            platform=platform,
        )

    assert not agents.exists()
    assert not cfg.workdir.exists()


def test_facade_rejects_explicit_empty_platform_before_backend_import(
    tmp_path, monkeypatch,
):
    """Catches an explicit empty platform falling back to the host backend."""
    cfg = _cfg(tmp_path)
    agents = tmp_path / "LaunchAgents"
    attempted_imports = []

    def no_backend_import(name, package=None):
        attempted_imports.append((name, package))
        raise AssertionError("an unsupported platform must not load a backend")

    monkeypatch.setattr(schedule, "import_module", no_backend_import)

    with pytest.raises(ValueError, match=r"unsupported scheduling platform: "):
        schedule.install_schedule(
            cfg,
            agents_dir=agents,
            executable="/opt/memberkit",
            platform="",
        )

    assert attempted_imports == []
    assert not agents.exists()
    assert not cfg.workdir.exists()


def test_facade_rejects_invalid_time_before_loading_a_backend(tmp_path, monkeypatch):
    """Catches importing a backend for an invalid schedule time."""
    cfg = _cfg(tmp_path)
    attempted_imports = []

    def no_backend_import(name, package=None):
        attempted_imports.append((name, package))
        raise AssertionError("an invalid schedule time must not load a backend")

    monkeypatch.setattr(schedule, "import_module", no_backend_import, raising=False)

    with pytest.raises(ValueError, match="schedule time must be HH:MM"):
        schedule.install_schedule(
            cfg,
            time="7:30",
            agents_dir=tmp_path / "LaunchAgents",
            executable="/opt/memberkit",
            platform="darwin",
        )

    assert attempted_imports == []


def test_facade_win32_does_not_import_the_macos_backend(tmp_path, monkeypatch):
    """Catches Windows dispatch importing its unselected macOS backend."""
    observed = {}
    windows = types.ModuleType("memberkit.schedule_windows")

    def windows_status(**kwargs):
        observed.update(kwargs)
        return schedule.ScheduleStatus(True, tmp_path / "windows-task", "17:30")

    windows.schedule_status = windows_status
    monkeypatch.setitem(sys.modules, "memberkit.schedule_windows", windows)
    monkeypatch.setitem(sys.modules, "memberkit.schedule_macos", None)

    result = schedule.schedule_status(
        platform="win32",
        windows_api="api",
        windows_runner="runner",
        windows_state_dir=tmp_path / "state",
        windows_task_name="task",
        windows_executable="C:/memberkit.exe",
    )

    assert result == schedule.ScheduleStatus(
        True, tmp_path / "windows-task", "17:30"
    )
    assert observed == {
        "api": "api",
        "runner": "runner",
        "state_dir": tmp_path / "state",
        "task_name_override": "task",
        "executable": "C:/memberkit.exe",
    }


def test_facade_darwin_does_not_import_the_windows_backend(tmp_path, monkeypatch):
    """Catches macOS dispatch importing its unselected Windows backend."""
    agents = tmp_path / "LaunchAgents"
    monkeypatch.setitem(sys.modules, "memberkit.schedule_windows", None)

    path = schedule.install_schedule(
        _cfg(tmp_path),
        agents_dir=agents,
        executable="/opt/example/memberkit",
        platform="darwin",
    )

    assert schedule.schedule_status(agents, platform="darwin") == (
        schedule.ScheduleStatus(True, path, "17:30")
    )


def test_install_defaults_to_1730_and_calls_only_scheduled_run(tmp_path):
    agents = tmp_path / "LaunchAgents"
    path = install_schedule(
        _cfg(tmp_path),
        agents_dir=agents,
        executable="/opt/example/memberkit",
    )

    data = plistlib.loads(path.read_bytes())
    assert data["StartCalendarInterval"] == {"Hour": 17, "Minute": 30}
    assert data["ProgramArguments"] == [
        "/opt/example/memberkit", "scheduled-run"
    ]
    assert "push" not in " ".join(data["ProgramArguments"])


def test_status_and_remove_are_idempotent(tmp_path):
    agents = tmp_path / "LaunchAgents"
    cfg = _cfg(tmp_path)
    assert not schedule_status(agents_dir=agents).installed
    assert remove_schedule(agents_dir=agents) is False

    install_schedule(cfg, time="08:15", agents_dir=agents, executable="memberkit")
    status = schedule_status(agents_dir=agents)
    assert status.installed and status.time == "08:15"
    assert remove_schedule(agents_dir=agents) is True
    assert remove_schedule(agents_dir=agents) is False


def test_scheduled_run_catches_up_original_date_without_transmitting(tmp_path):
    rows = [
        _row("Monday early", "2026-07-27T10:00:00"),
        _row("Monday late", "2026-07-27T22:00:00"),
        _row("Tuesday", "2026-07-28T09:00:00"),
    ]
    cfg = _cfg(tmp_path, rows)
    state = DraftState(cfg.workdir / "state.json")
    early = {
        "ts": _local_ts("2026-07-27T10:00:00"),
        "kind": "journal-highlight",
        "summary": "Monday early",
        "project": "project-alpha",
        "refs": None,
    }
    state.refresh("2026-07-27", [early], current=None)
    state.record_push("2026-07-27", [early])

    pending = scheduled_run(
        cfg, datetime.fromisoformat("2026-07-28T17:30:00"), notify=False
    )

    assert pending == ["2026-07-27", "2026-07-28"]
    monday = (cfg.workdir / "out" / "bundle-alex-2026-07-27.json").read_text()
    tuesday = (cfg.workdir / "out" / "bundle-alex-2026-07-28.json").read_text()
    assert "Monday late" in monday and "Monday early" not in monday
    assert "Tuesday" in tuesday
    assert not (cfg.workdir / "inbox").exists()


def test_scheduled_run_writes_every_eligible_observation(tmp_path):
    rows = [
        _row(f"Observation {index}", f"2026-07-27T{index + 8:02d}:00:00")
        for index in range(8)
    ]
    cfg = _cfg(tmp_path, rows)

    pending = scheduled_run(
        cfg, datetime.fromisoformat("2026-07-28T17:30:00"), notify=False
    )

    assert pending == ["2026-07-27"]
    payload = json.loads(
        (cfg.workdir / "out" / "bundle-alex-2026-07-27.json").read_text(
            encoding="utf-8"
        )
    )
    assert [event["summary"] for event in payload["events"]] == [
        f"Observation {index}" for index in range(8)
    ]


def test_scheduled_run_preserves_exact_duplicate_observations(tmp_path):
    row = _row("Same observation", "2026-07-27T10:00:00")
    cfg = _cfg(tmp_path, [row, row])
    now = datetime.fromisoformat("2026-07-28T17:30:00")

    assert scheduled_run(cfg, now, notify=False) == ["2026-07-27"]
    path = cfg.workdir / "out" / "bundle-alex-2026-07-27.json"
    first = json.loads(path.read_text(encoding="utf-8"))
    assert scheduled_run(cfg, now, notify=False) == ["2026-07-27"]
    repeated = json.loads(path.read_text(encoding="utf-8"))

    assert len(first["events"]) == 2
    assert first["events"] == repeated["events"]


def test_scheduled_run_uses_member_timezone_for_dates_bounds_and_timestamps(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    rows = [
        _row("Member Sunday", "2026-07-27T06:00:00Z"),
        _row("Member Tuesday", "2026-07-28T08:00:00Z"),
    ]
    base = _cfg(tmp_path, rows)
    cfg = Config(
        member=base.member,
        db=base.db,
        inbox_url=base.inbox_url,
        workdir=base.workdir,
        timezone=ZoneInfo("America/Los_Angeles"),
    )

    pending = scheduled_run(
        cfg,
        datetime.fromisoformat("2026-07-28T01:00:00+00:00"),
        notify=False,
    )

    assert pending == ["2026-07-26"]
    sunday = (
        cfg.workdir / "out" / "bundle-alex-2026-07-26.json"
    ).read_text(encoding="utf-8")
    assert "Member Sunday" in sunday
    assert "2026-07-26T23:00:00.000-07:00" in sunday
    assert not (
        cfg.workdir / "out" / "bundle-alex-2026-07-28.json"
    ).exists()


def test_scheduled_run_keeps_reminding_for_older_pending_date(tmp_path):
    cfg = _cfg(tmp_path)
    old_date = "2026-07-20"
    event = {
        "ts": f"{old_date}T10:00:00",
        "kind": "journal-highlight",
        "summary": "Still needs review",
        "project": "project-alpha",
        "refs": None,
    }
    DraftState(cfg.workdir / "state.json").refresh(
        old_date, [event], current=None
    )

    pending = scheduled_run(
        cfg, datetime.fromisoformat("2026-07-28T17:30:00"), notify=False
    )

    assert pending == [old_date]


def test_scheduled_run_never_overwrites_invalid_member_edited_draft(tmp_path):
    cfg = _cfg(
        tmp_path,
        [_row("Discovered", "2026-07-27T10:00:00")],
    )
    path = cfg.workdir / "out" / "bundle-alex-2026-07-27.json"
    path.parent.mkdir(parents=True)
    edited = b'{"events": [member edit in progress'
    path.write_bytes(edited)

    pending = scheduled_run(
        cfg, datetime.fromisoformat("2026-07-28T17:30:00"), notify=False
    )

    assert "2026-07-27" in pending
    assert path.read_bytes() == edited


def test_scheduled_run_preserves_non_utf8_draft_and_processes_other_date(
    tmp_path,
):
    cfg = _cfg(
        tmp_path,
        [_row("Tuesday eligible", "2026-07-28T09:00:00")],
    )
    malformed = cfg.workdir / "out" / "bundle-alex-2026-07-27.json"
    malformed.parent.mkdir(parents=True)
    original = b"\xff\xfe\x00member edit in progress"
    malformed.write_bytes(original)

    pending = scheduled_run(
        cfg, datetime.fromisoformat("2026-07-28T17:30:00"), notify=False
    )

    assert pending == ["2026-07-27", "2026-07-28"]
    assert malformed.read_bytes() == original
    created = (
        cfg.workdir / "out" / "bundle-alex-2026-07-28.json"
    ).read_text(encoding="utf-8")
    assert "Tuesday eligible" in created


def test_scheduled_run_never_overwrites_valid_member_edited_draft(tmp_path):
    cfg = _cfg(
        tmp_path,
        [_row("Newly discovered", "2026-07-27T10:00:00")],
    )
    path = cfg.workdir / "out" / "bundle-alex-2026-07-27.json"
    path.parent.mkdir(parents=True)
    edited = (
        b'{"schema":"teammem-bundle/v1","member":"alex",'
        b'"date":"2026-07-27","events":[{"ts":"2026-07-27T09:00:00",'
        b'"kind":"journal-highlight","summary":"Manual decision",'
        b'"project":"project-alpha","refs":null}],"journal_md":"manual"}\n'
    )
    path.write_bytes(edited)

    pending = scheduled_run(
        cfg, datetime.fromisoformat("2026-07-28T17:30:00"), notify=False
    )

    assert "2026-07-27" in pending
    assert path.read_bytes() == edited


@pytest.mark.parametrize("invalid_event", ["extra-key", "wrong-kind"])
def test_scheduled_run_preserves_and_reports_non_frozen_existing_draft(
    tmp_path, invalid_event,
):
    cfg = _cfg(tmp_path)
    path = cfg.workdir / "out" / "bundle-alex-2026-07-27.json"
    path.parent.mkdir(parents=True)
    event = {
        "ts": "2026-07-27T09:00:00",
        "kind": "journal-highlight",
        "summary": "Manual decision",
        "project": "project-alpha",
        "refs": None,
    }
    if invalid_event == "extra-key":
        event["source"] = "private"
    else:
        event["kind"] = "raw-observation"
    edited = (json.dumps({
        "schema": bundle.SCHEMA,
        "member": cfg.member,
        "date": "2026-07-27",
        "events": [event],
        "journal_md": "manual",
    }, separators=(",", ":")) + "\n").encode()
    path.write_bytes(edited)

    pending = scheduled_run(
        cfg, datetime.fromisoformat("2026-07-28T17:30:00"), notify=False
    )

    assert "2026-07-27" in pending
    assert path.read_bytes() == edited
    assert DraftState(cfg.workdir / "state.json").snapshot() == {
        "approved": [],
        "excluded": [],
        "pending": {},
    }


def test_scheduled_run_validates_generated_bundle_before_creating_file(
    tmp_path, monkeypatch,
):
    cfg = _cfg(tmp_path)
    invalid = {
        "schema": bundle.SCHEMA,
        "member": cfg.member,
        "date": "2026-07-27",
        "events": [{
            "ts": "2026-07-27T10:00:00",
            "kind": "journal-highlight",
            "summary": "generated",
            "project": "project-alpha",
            "refs": ["private"],
        }],
        "journal_md": "stale",
    }
    monkeypatch.setattr(bundle, "draft", lambda *args, **kwargs: invalid)

    with pytest.raises(ValueError, match="refs must be null"):
        scheduled_run(
            cfg, datetime.fromisoformat("2026-07-28T17:30:00"), notify=False
        )

    output_dir = cfg.workdir / "out"
    assert not output_dir.exists() or list(output_dir.iterdir()) == []
    assert DraftState(cfg.workdir / "state.json").snapshot() == {
        "approved": [],
        "excluded": [],
        "pending": {},
    }


def test_scheduled_run_replace_failure_leaves_no_bundle_or_temp(
    tmp_path, monkeypatch,
):
    cfg = _cfg(
        tmp_path,
        [_row("Generated", "2026-07-27T10:00:00")],
    )
    monkeypatch.setattr(
        bundle.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        scheduled_run(
            cfg, datetime.fromisoformat("2026-07-28T17:30:00"), notify=False
        )

    output_dir = cfg.workdir / "out"
    assert output_dir.exists()
    assert list(output_dir.iterdir()) == []
