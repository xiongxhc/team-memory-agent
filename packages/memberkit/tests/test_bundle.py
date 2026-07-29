import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from memberkit import bundle


def epoch(iso):
    return int(datetime.fromisoformat(iso).astimezone().timestamp() * 1000)


def make_db(tmp_path, rows):
    db = tmp_path / "claude-mem.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE observations (project TEXT, title TEXT, subtitle TEXT,"
        " narrative TEXT, type TEXT, created_at TEXT, created_at_epoch INTEGER)"
    )
    con.executemany("INSERT INTO observations VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return db


def make_rich_db(tmp_path, rows):
    db = tmp_path / "claude-mem-rich.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE observations (project TEXT, memory_session_id TEXT,"
        " title TEXT, subtitle TEXT, narrative TEXT, facts TEXT, type TEXT,"
        " created_at TEXT, created_at_epoch INTEGER)"
    )
    con.executemany("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return db


def rich_row(project, session, title, iso):
    return (project, session, title, None, None, None, "change", iso, epoch(iso))


def test_draft_selects_only_that_day_and_groups_journal(tmp_path):
    rows = [
        ("sdk", "Shipped marketplace", None, None, "feature",
         "2026-07-24T10:00:00", epoch("2026-07-24T10:00:00")),
        ("sdk", "Fixed sync script", None, None, "bugfix",
         "2026-07-24T15:00:00", epoch("2026-07-24T15:00:00")),
        ("other", None, None, "long narrative " * 20, "discovery",
         "2026-07-24T16:00:00", epoch("2026-07-24T16:00:00")),
        ("sdk", "Yesterday's work", None, None, "feature",
         "2026-07-23T10:00:00", epoch("2026-07-23T10:00:00")),
    ]

    out = bundle.draft(make_db(tmp_path, rows), "alex", "2026-07-24")

    assert out["schema"] == bundle.SCHEMA == "teammem-bundle/v1"
    assert out["member"] == "alex" and out["date"] == "2026-07-24"
    assert [event["summary"] for event in out["events"]] == [
        "Shipped marketplace", "Fixed sync script", "long narrative " * 8,
    ]
    assert "### sdk" in out["journal_md"] and "### other" in out["journal_md"]


def test_draft_drops_rows_without_legacy_content(tmp_path):
    rows = [
        ("sdk", None, None, None, "feature",
         "2026-07-24T10:00:00", epoch("2026-07-24T10:00:00")),
        ("sdk", None, None, "   ", "feature",
         "2026-07-24T11:00:00", epoch("2026-07-24T11:00:00")),
    ]

    out = bundle.draft(make_db(tmp_path, rows), "alex", "2026-07-24")

    assert out["events"] == []
    assert out["journal_md"] == "## 2026-07-24"


def test_default_and_all_preserve_every_eligible_observation(tmp_path):
    rows = [
        rich_row("sdk", "session-a", "same", "2026-07-24T09:00:00"),
        rich_row("sdk", "session-a", "same", "2026-07-24T09:01:00"),
        rich_row("sdk", "session-b", "two", "2026-07-24T09:02:00"),
        rich_row("sdk", "session-c", "three", "2026-07-24T09:03:00"),
        rich_row("sdk", "session-d", "four", "2026-07-24T09:04:00"),
        rich_row("sdk", "session-e", "five", "2026-07-24T09:05:00"),
        rich_row("sdk", "session-f", "six", "2026-07-24T09:06:00"),
        rich_row("sdk", "session-g", "seven", "2026-07-24T09:07:00"),
    ]
    db = make_rich_db(tmp_path, rows)
    zone = ZoneInfo("Asia/Dubai")

    default = bundle.draft(db, "alex", "2026-07-24", timezone=zone)
    compat = bundle.draft(
        db, "alex", "2026-07-24", all_observations=True, timezone=zone,
    )

    assert len(default["events"]) == 8
    assert default["events"] == compat["events"]
    assert [event["summary"] for event in default["events"]].count("same") == 2


def test_draft_uses_only_frozen_v1_fields_from_rich_observation_rows(tmp_path):
    row = (
        "sdk", "SESSION_SENTINEL", "Visible title", "SUBTITLE_SENTINEL",
        "NARRATIVE_SENTINEL", "FACTS_SENTINEL", "TYPE_SENTINEL",
        "2026-07-24T09:00:00", epoch("2026-07-24T09:00:00"),
    )

    out = bundle.draft(
        make_rich_db(tmp_path, [row]), "alex", "2026-07-24",
        timezone=ZoneInfo("Asia/Dubai"),
    )

    assert out["events"] == [{
        "ts": "2026-07-24T09:00:00.000+04:00",
        "kind": "journal-highlight",
        "summary": "Visible title",
        "project": "sdk",
        "refs": None,
    }]
    assert set(out["events"][0]) == {"ts", "kind", "summary", "project", "refs"}
    assert "SENTINEL" not in repr(out)


def test_draft_uses_member_timezone_for_day_bounds_and_event_timestamps(tmp_path):
    zone = ZoneInfo("America/Los_Angeles")
    rows = [
        ("sdk", "Before local midnight", None, None, "feature",
         "2026-07-28T06:30:00Z", epoch("2026-07-28T06:30:00Z")),
        ("sdk", "After local midnight", None, None, "feature",
         "2026-07-28T07:30:00Z", epoch("2026-07-28T07:30:00Z")),
    ]
    db = make_db(tmp_path, rows)

    july_27 = bundle.draft(db, "alex", "2026-07-27", timezone=zone)
    july_28 = bundle.draft(db, "alex", "2026-07-28", timezone=zone)

    assert july_27["events"] == [{
        "ts": "2026-07-27T23:30:00.000-07:00",
        "kind": "journal-highlight",
        "summary": "Before local midnight",
        "project": "sdk",
        "refs": None,
    }]
    assert [event["summary"] for event in july_28["events"]] == [
        "After local midnight"
    ]


def test_day_window_follows_dst_aware_local_midnights(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")

    spring_start, spring_end = bundle._day_epochs("2026-03-08")
    fall_start, fall_end = bundle._day_epochs("2026-11-01")

    assert spring_end - spring_start == 23 * 60 * 60 * 1000
    assert fall_end - fall_start == 25 * 60 * 60 * 1000


def test_equal_timestamps_use_stable_id_tiebreaker_for_both_modes(tmp_path):
    db = tmp_path / "claude-mem-with-id.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE observations (id TEXT, project TEXT, title TEXT,"
        " narrative TEXT, created_at TEXT, created_at_epoch INTEGER)"
    )
    iso = "2026-07-24T10:00:00"
    con.executemany(
        "INSERT INTO observations VALUES (?,?,?,?,?,?)",
        [
            ("b", "sdk", "Second by identifier", None, iso, epoch(iso)),
            ("a", "sdk", "First by identifier", None, iso, epoch(iso)),
        ],
    )
    con.commit()
    con.close()

    default = bundle.draft(db, "alex", "2026-07-24")
    compat = bundle.draft(db, "alex", "2026-07-24", all_observations=True)

    assert [event["summary"] for event in default["events"]] == [
        "First by identifier", "Second by identifier"
    ]
    assert default["events"] == compat["events"]


def test_equal_timestamps_without_id_preserve_rowid_insertion_order(tmp_path):
    rows = [
        ("sdk", "Inserted first", None, None, "change",
         "2026-07-24T10:00:00", epoch("2026-07-24T10:00:00")),
        ("sdk", "Inserted second", None, None, "change",
         "2026-07-24T10:00:00", epoch("2026-07-24T10:00:00")),
    ]

    out = bundle.draft(make_db(tmp_path, rows), "alex", "2026-07-24")

    assert [event["summary"] for event in out["events"]] == [
        "Inserted first", "Inserted second"
    ]
