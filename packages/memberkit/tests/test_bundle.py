import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

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


def valid_bundle():
    return {
        "schema": "teammem-bundle/v1",
        "member": "alex",
        "date": "2026-07-27",
        "events": [{
            "ts": "2026-07-27T10:00:00",
            "kind": "journal-highlight",
            "summary": "kept",
            "project": "project-alpha",
            "refs": None,
        }],
        "journal_md": "stale",
    }


@pytest.mark.parametrize(
    "case",
    [
        "top-level-type",
        "top-level-keys",
        "schema",
        "member",
        "date",
        "date-format",
        "events-type",
        "journal-type",
        "event-type",
        "event-keys",
        "summary",
        "kind",
        "project",
        "refs",
        "timestamp",
        "wrong-day",
    ],
)
def test_validate_bundle_rejects_non_frozen_v1_data(case):
    data = valid_bundle()
    expected_date = "2026-07-27"
    if case == "top-level-type":
        data = []
    elif case == "top-level-keys":
        data["extra"] = True
    elif case == "schema":
        data["schema"] = "teammem-bundle/v2"
    elif case == "member":
        data["member"] = "other"
    elif case == "date":
        data["date"] = "2026-07-28"
    elif case == "date-format":
        data["date"] = expected_date = "2026-7-27"
    elif case == "events-type":
        data["events"] = {}
    elif case == "journal-type":
        data["journal_md"] = None
    elif case == "event-type":
        data["events"][0] = []
    elif case == "event-keys":
        data["events"][0]["extra"] = True
    elif case == "summary":
        data["events"][0]["summary"] = "   "
    elif case == "kind":
        data["events"][0]["kind"] = "raw-observation"
    elif case == "project":
        data["events"][0]["project"] = ["project-alpha"]
    elif case == "refs":
        data["events"][0]["refs"] = ["private"]
    elif case == "timestamp":
        data["events"][0]["ts"] = "not-a-timestamp"
    else:
        data["events"][0]["ts"] = "2026-07-28T00:00:00"

    with pytest.raises(ValueError):
        bundle.validate_bundle(data, "alex", expected_date)


def test_prepare_bundle_writes_empty_events_journal(tmp_path):
    path = tmp_path / "bundle-alex-2026-07-27.json"
    data = valid_bundle()
    data["events"] = []
    path.write_text(json.dumps(data), encoding="utf-8")

    prepared = bundle.prepare_bundle(path, "alex", "2026-07-27")

    assert prepared["events"] == []
    assert prepared["journal_md"] == "## 2026-07-27"
    assert json.loads(path.read_text(encoding="utf-8")) == prepared


def test_prepare_bundle_fsyncs_same_directory_temp_before_replace(
    tmp_path, monkeypatch,
):
    path = tmp_path / "bundle-alex-2026-07-27.json"
    path.write_text(json.dumps(valid_bundle()), encoding="utf-8")
    order = []
    real_fsync = bundle.os.fsync
    real_replace = bundle.os.replace

    def record_fsync(descriptor):
        order.append("fsync")
        return real_fsync(descriptor)

    def record_replace(source, destination):
        assert Path(source).parent == path.parent
        assert Path(destination) == path
        order.append("replace")
        return real_replace(source, destination)

    monkeypatch.setattr(bundle.os, "fsync", record_fsync)
    monkeypatch.setattr(bundle.os, "replace", record_replace)

    bundle.prepare_bundle(path, "alex", "2026-07-27")

    assert order == ["fsync", "replace"]


def test_write_bundle_fsync_failure_preserves_destination_and_cleans_temp(
    tmp_path, monkeypatch,
):
    path = tmp_path / "bundle-alex-2026-07-27.json"
    original = b'{"member":"member-edited"}\n'
    path.write_bytes(original)
    monkeypatch.setattr(
        bundle.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("fsync failed")),
    )

    with pytest.raises(OSError, match="fsync failed"):
        bundle.write_bundle(path, valid_bundle())

    assert path.read_bytes() == original
    assert sorted(item.name for item in path.parent.iterdir()) == [path.name]


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


def test_draft_treats_whitespace_only_title_as_absent(tmp_path):
    rows = [
        (
            "sdk",
            "   ",
            None,
            "Useful narrative fallback",
            "change",
            "2026-07-24T10:00:00",
            epoch("2026-07-24T10:00:00"),
        ),
    ]

    out = bundle.draft(make_db(tmp_path, rows), "alex", "2026-07-24")

    assert [event["summary"] for event in out["events"]] == [
        "Useful narrative fallback"
    ]


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
