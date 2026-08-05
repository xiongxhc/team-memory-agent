import json

from teammem.events import Event
from teammem.slices import (active_person_days, daily_person_projects,
                            daily_person_slice, slice_hash, weekly_team_input)
from teammem.store import insert_events, open_db


def _feishu_raw(text):
    return json.dumps({"msg_type": "text",
                       "body": {"content": json.dumps({"text": text})}})


def _db(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        Event(person="alex", ts="2026-07-14T09:00:00+04:00", source="gitlab",
              kind="commit", project="project-alpha", summary="fix: JWT refresh race", hash="h1"),
        Event(person="alex", ts="2026-07-14T10:12:03+00:00", source="feishu-channel",
              kind="message", project="project-alpha", summary="Deployed the fix to st",
              raw=_feishu_raw("Deployed the fix to staging, please verify"), hash="h2"),
        Event(person="alex", ts="2026-07-15T09:00:00+04:00", source="gitlab",
              kind="commit", project="project-alpha", summary="next day", hash="h3"),
        Event(person="_unmapped/x@y.z", ts="2026-07-14T09:30:00+04:00", source="gitlab",
              kind="commit", summary="stray", hash="h4"),
    ])
    return conn


def test_daily_slice_orders_and_extracts_message_text(tmp_path):
    s = daily_person_slice(_db(tmp_path), "alex", "2026-07-14")
    lines = s.splitlines()
    assert len(lines) == 2
    assert "fix: JWT refresh race" in lines[0]                       # ts order
    assert "Deployed the fix to staging, please verify" in lines[1]  # full raw text
    assert "next day" not in s                                       # day boundary
    assert daily_person_slice(_db(tmp_path), "alex", "2026-07-16") == ""


def test_slice_hash_tracks_content(tmp_path):
    conn = _db(tmp_path)
    h1 = slice_hash(daily_person_slice(conn, "alex", "2026-07-14"))
    insert_events(conn, [Event(person="alex", ts="2026-07-14T11:00:00+04:00",
                               source="gitlab", kind="commit", project="project-alpha",
                               summary="late fix", hash="h5")])
    h2 = slice_hash(daily_person_slice(conn, "alex", "2026-07-14"))
    assert h1 != h2 and len(h1) == 64


def test_active_person_days_and_weekly_input(tmp_path):
    pairs = active_person_days(_db(tmp_path), "2026-07-13", "2026-07-19")
    assert pairs == [("alex", "2026-07-14"), ("alex", "2026-07-15")]
    text = weekly_team_input([
        {"person": "sam", "day": "2026-07-14", "text": "B"},
        {"person": "alex", "day": "2026-07-14", "text": "A"},
    ])
    assert text.index("alex") < text.index("sam")   # deterministic order
    assert "## alex — 2026-07-14\nA" in text


def test_daily_person_projects_are_local_distinct_and_sorted(tmp_path):
    conn = _db(tmp_path)
    insert_events(conn, [
        Event(person="alex", ts="2026-07-14T11:00:00+04:00", source="gitlab",
              kind="commit", project="project-beta", summary="beta work", hash="h5"),
        Event(person="sam", ts="2026-07-14T11:00:00+04:00", source="gitlab",
              kind="commit", project="project-other", summary="other work", hash="h6"),
        Event(person="alex", ts="2026-07-15T11:00:00+04:00", source="gitlab",
              kind="commit", project="project-next", summary="next work", hash="h7"),
        Event(person="alex", ts="2026-07-14T12:00:00+04:00", source="gitlab",
              kind="commit", summary="no project", hash="h8"),
    ])

    assert daily_person_projects(conn, "alex", "2026-07-14") == [
        "project-alpha",
        "project-beta",
    ]


def test_message_text_handles_non_dict_json_content(tmp_path):
    """Test that _message_text falls back to summary when body.content is not a dict."""
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        Event(person="alex", ts="2026-07-14T10:12:03+00:00", source="feishu-channel",
              kind="message", project="test", summary="Array content fallback",
              raw=json.dumps({"msg_type": "text",
                             "body": {"content": json.dumps([1, 2, 3])}}), hash="h_array"),
    ])
    s = daily_person_slice(conn, "alex", "2026-07-14")
    # Should fallback to summary instead of raising AttributeError
    assert "Array content fallback" in s
