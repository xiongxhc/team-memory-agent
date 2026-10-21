import json

import pytest

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


def _post_slice(tmp_path, content):
    conn = open_db(tmp_path / "posts.db")
    insert_events(conn, [Event(
        person="alex", ts="2026-07-14T10:12:03+00:00", source="feishu-channel",
        kind="message", project="test", summary="[post]", hash="post",
        raw=json.dumps({"msg_type": "post", "body": {"content": content}}),
    )])
    return daily_person_slice(conn, "alex", "2026-07-14")


def test_post_slice_preserves_readable_evidence_without_duplicate_versions(tmp_path):
    rows = [
        [{"tag": "at", "user_name": "Alex", "user_id": "@_user_1"},
         {"tag": "text", "text": " KPI changes are documented; some remain unfinished."}],
        [{"tag": "a", "text": "Plan", "href": "https://example.com/plan"}],
        [{"tag": "img", "image_key": "private-image-key"}],
        [{"tag": "code_block", "text": "activity_unique_id\n  not id"}],
        [{"tag": "md", "text": '> "Could you confirm the date?"\n  Preserve this quotation.'}],
    ]
    text = _post_slice(tmp_path, json.dumps({
        "title": "KPI status", "content": rows, "content_v2": rows,
    }))
    assert "KPI status\n@Alex KPI changes are documented; some remain unfinished." in text
    assert "Plan (https://example.com/plan)" in text
    assert "activity_unique_id\n  not id" in text
    assert '> "Could you confirm the date?"\n  Preserve this quotation.' in text
    assert text.count("KPI changes") == 1
    assert "private-image-key" not in text


def test_post_slice_prefers_newer_content_and_supports_localized_posts(tmp_path):
    text = _post_slice(tmp_path, json.dumps({
        "zh_cn": {"title": "进展", "content": [[{"tag": "text", "text": "尚未完成"}]]},
        "en_us": {"title": "Progress", "content": [[{"tag": "text", "text": "old text"}]],
                  "content_v2": [[{"tag": "text", "text": "Still unfinished"}]]},
    }))
    assert "Progress\nStill unfinished\n进展\n尚未完成" in text
    assert "old text" not in text


def test_post_slice_uses_legacy_content_when_newer_content_is_malformed(tmp_path):
    text = _post_slice(tmp_path, json.dumps({
        "content_v2": "invalid", "content": [[
            {"tag": "text", "text": "Use the HSE sender address."},
            {"tag": "a", "href": "https://example.com/matrix"},
        ]],
    }))
    assert "Use the HSE sender address.https://example.com/matrix" in text


@pytest.mark.parametrize("content", [
    "This message was recalled", "null", "[]", "42",
    json.dumps({"content": [[{"tag": "img", "image_key": "image-only"}]]}),
    json.dumps({"title": 42, "content": [None, [None, {"tag": "text", "text": []}]]}),
])
def test_post_slice_malformed_or_nontext_content_keeps_summary(tmp_path, content):
    assert _post_slice(tmp_path, content).endswith("message  test  [post]")
