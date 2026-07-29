import plistlib
import sqlite3
from datetime import datetime
from pathlib import Path

from memberkit import bundle
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
