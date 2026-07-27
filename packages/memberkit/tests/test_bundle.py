import sqlite3
from datetime import datetime

from memberkit import bundle


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


def epoch(iso):
    return int(datetime.fromisoformat(iso).astimezone().timestamp())


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
    assert len(out["events"]) == 3          # yesterday's row excluded
    assert out["events"][0]["summary"] == "Shipped marketplace"
    assert out["events"][0]["kind"] == "journal-highlight"
    assert len(out["events"][2]["summary"]) <= 120   # narrative truncated
    assert "### sdk" in out["journal_md"] and "### other" in out["journal_md"]
    assert "- Shipped marketplace" in out["journal_md"]


def test_draft_empty_day(tmp_path):
    out = bundle.draft(make_db(tmp_path, []), "alex", "2026-07-24")
    assert out["events"] == [] and out["journal_md"].startswith("## 2026-07-24")


def test_draft_drops_rows_without_content(tmp_path):
    rows = [
        ("sdk", None, None, None, "feature",
         "2026-07-24T10:00:00", epoch("2026-07-24T10:00:00")),
        ("sdk", None, None, "   ", "feature",
         "2026-07-24T11:00:00", epoch("2026-07-24T11:00:00")),
    ]
    out = bundle.draft(make_db(tmp_path, rows), "alex", "2026-07-24")
    assert out["events"] == []
    assert out["journal_md"] == "## 2026-07-24"


def test_midnight_assigns_events_to_their_local_calendar_date(tmp_path):
    rows = [
        ("sdk", "Before midnight", None, None, "feature",
         "2026-07-24T23:59:59", epoch("2026-07-24T23:59:59")),
        ("sdk", "After midnight", None, None, "feature",
         "2026-07-25T00:00:00", epoch("2026-07-25T00:00:00")),
    ]
    db = make_db(tmp_path, rows)

    day_one = bundle.draft(db, "alex", "2026-07-24")
    day_two = bundle.draft(db, "alex", "2026-07-25")

    assert [event["summary"] for event in day_one["events"]] == ["Before midnight"]
    assert [event["summary"] for event in day_two["events"]] == ["After midnight"]
