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
    return int(datetime.fromisoformat(iso).astimezone().timestamp() * 1000)


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


def rich_row(project, session, title, iso, *, subtitle=None, narrative=None,
             facts=None, kind="discovery"):
    return (
        project, session, title, subtitle, narrative, facts, kind, iso, epoch(iso)
    )


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


def test_day_window_follows_dst_aware_local_midnights(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")

    spring_start, spring_end = bundle._day_epochs("2026-03-08")
    fall_start, fall_end = bundle._day_epochs("2026-11-01")

    assert spring_end - spring_start == 23 * 60 * 60 * 1000
    assert fall_end - fall_start == 25 * 60 * 60 * 1000


def test_all_observations_preserves_legacy_projection_and_order(tmp_path):
    rows = [
        ("sdk", None, None, "  raw narrative  ", "discovery",
         "2026-07-24T11:00:00", epoch("2026-07-24T11:00:00")),
        ("sdk", "Earlier title", "ignored subtitle", "ignored narrative", "change",
         "2026-07-24T10:00:00", epoch("2026-07-24T10:00:00")),
    ]

    out = bundle.draft(
        make_db(tmp_path, rows), "alex", "2026-07-24",
        all_observations=True,
    )

    assert [event["summary"] for event in out["events"]] == [
        "Earlier title", "raw narrative"
    ]
    assert out["events"][0] == {
        "ts": "2026-07-24T10:00:00",
        "kind": "journal-highlight",
        "summary": "Earlier title",
        "project": "sdk",
        "refs": None,
    }


def test_curated_draft_uses_one_best_outcome_per_session_and_safe_v1_shape(tmp_path):
    rows = [
        rich_row(
            "sdk", "session-a", "Task 2", "2026-07-24T09:00:00",
            subtitle="Schedule lifecycle — added install, status, and remove",
            narrative="Internal path /home/example/private was inspected.",
            facts='["/home/example/private/secret.txt"]',
            kind="change",
        ),
        rich_row(
            "sdk", "session-a", "Tests passed", "2026-07-24T10:00:00",
            narrative="Ran the focused suite.", kind="discovery",
        ),
        rich_row(
            "sdk", "session-b", "Progress", "2026-07-24T11:00:00",
            narrative="Privacy checks now reject direct-message ingestion. Follow-up detail.",
            kind="decision",
        ),
        rich_row(
            "sdk", "session-c", "Work", "2026-07-24T12:00:00",
            narrative="Internal file /home/example/private.txt was inspected.",
            facts='["Windows scheduling remains an unresolved blocker."]',
            kind="discovery",
        ),
    ]

    out = bundle.draft(make_rich_db(tmp_path, rows), "alex", "2026-07-24")

    assert [event["summary"] for event in out["events"]] == [
        "Schedule lifecycle — added install, status, and remove",
        "Privacy checks now reject direct-message ingestion.",
        "Windows scheduling remains an unresolved blocker.",
    ]
    assert all(
        set(event) == {"ts", "kind", "summary", "project", "refs"}
        for event in out["events"]
    )
    assert all(len(event["summary"]) <= 120 for event in out["events"])
    encoded = repr(out)
    assert "session-a" not in encoded
    assert "/home/example/private" not in encoded
    assert "secret.txt" not in encoded


def test_curated_draft_deduplicates_normalized_summaries_keeping_earliest(tmp_path):
    rows = [
        rich_row("sdk", "session-a", "Shipped   retry fix",
                 "2026-07-24T09:00:00", kind="change"),
        rich_row("sdk", "session-b", "  shipped retry FIX  ",
                 "2026-07-24T10:00:00", kind="decision"),
    ]

    out = bundle.draft(make_rich_db(tmp_path, rows), "alex", "2026-07-24")

    assert [(event["ts"], event["summary"]) for event in out["events"]] == [
        ("2026-07-24T09:00:00", "Shipped retry fix")
    ]


def test_curated_draft_caps_each_project_at_seven_and_renders_chronologically(
    tmp_path,
):
    rows = [
        rich_row(
            "sdk", f"session-{index}", title,
            f"2026-07-24T{index + 8:02d}:00:00", kind=kind,
        )
        for index, (title, kind) in enumerate([
            ("Progress update one", "discovery"),
            ("Progress update two", "discovery"),
            ("Progress update three", "discovery"),
            ("Progress update four", "discovery"),
            ("Privacy boundary secured", "discovery"),
            ("Architecture decision approved", "decision"),
            ("Unresolved release blocker", "discovery"),
            ("Published package release", "change"),
            ("Implemented connector validation", "change"),
        ])
    ]
    rows.extend([
        rich_row("other", "other-a", "Shipped other one",
                 "2026-07-24T10:30:00", kind="change"),
        rich_row("other", "other-b", "Shipped other two",
                 "2026-07-24T11:30:00", kind="change"),
    ])

    out = bundle.draft(make_rich_db(tmp_path, rows), "alex", "2026-07-24")
    sdk = [event for event in out["events"] if event["project"] == "sdk"]
    other = [event for event in out["events"] if event["project"] == "other"]

    assert len(sdk) == 7
    assert len(other) == 2
    assert [event["ts"] for event in sdk] == sorted(event["ts"] for event in sdk)
    assert "Progress update three" not in [event["summary"] for event in sdk]
    assert "Progress update four" not in [event["summary"] for event in sdk]
    assert {
        "Privacy boundary secured",
        "Architecture decision approved",
        "Unresolved release blocker",
        "Published package release",
        "Implemented connector validation",
    } <= {event["summary"] for event in sdk}
