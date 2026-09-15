import json
import sqlite3
import time

import pytest

from teammem.chat.retrieval import RetrievalTimeoutError, open_ledger_readonly, search_evidence
from teammem.events import Event
from teammem.store import (
    SummaryRecord,
    insert_events,
    open_db,
    put_summary,
    replace_weekly_commit_counts,
)
from teammem.metrics import CommitCountScope, WeeklyCommitCount


def _ledger(tmp_path):
    path = tmp_path / "ledger.db"
    conn = open_db(path)
    insert_events(conn, [
        Event(
            person="alice", project="detail", ts="2026-09-01T10:00:00Z",
            source="gitlab", kind="commit", summary="Ship lexical search endpoint",
            refs=json.dumps({"url": "https://gitlab.example/detail/commit/1"}),
            raw='{"private":"detail must not be read by chat"}', hash="detail-1",
        ),
        Event(
            person="alice", project="other", ts="2026-09-01T11:00:00Z",
            source="gitlab", kind="commit", summary="Ship other project endpoint",
            refs=json.dumps({"url": "https://gitlab.example/other/commit/1"}),
            raw='{"private":"other detail"}', hash="other-1",
        ),
        Event(
            person="alice", project="counts", ts="2026-09-01T12:00:00Z",
            source="memberkit", kind="journal-highlight",
            summary="MemberKit private note: secret launch plan", refs=None,
            raw='{"private":"MemberKit raw detail"}', hash="count-detail-1",
        ),
        Event(
            person="alice", project=None, ts="2026-09-01T13:00:00Z",
            source="feishu-channel", kind="message", summary="no project evidence",
            refs=json.dumps({"message_id": "om_missing_url"}), raw=None,
            hash="none-1",
        ),
        Event(
            person="alice", project="hidden", ts="2026-09-01T14:00:00Z",
            source="gitlab", kind="commit", summary="hidden launch plan",
            refs=None, raw=None, hash="hidden-1",
        ),
        Event(
            person="alice", project="detail", ts="2026-09-02T10:00:00Z",
            source="feishu-channel", kind="message", summary="Chinese 搜索 已完成",
            refs=json.dumps({"message_id": "om_no_url"}), raw=None,
            hash="detail-no-url",
        ),
    ])
    replace_weekly_commit_counts(
        conn,
        (CommitCountScope("counts", "2026-09-01"),),
        (WeeklyCommitCount("counts", "2026-09-01", "alice", 7),),
    )
    conn.close()
    return path


def _query(text, **filters):
    return {"text": text, **filters}


def test_search_filters_sql_results_to_authorized_detailed_project(tmp_path):
    """Moving the project condition after fetch would load another project's evidence."""
    path = _ledger(tmp_path)

    found = search_evidence(
        path, {"detail": "detail", "other": "detail", "counts": "count_only", "hidden": "hidden"},
        frozenset({"detail"}), _query("endpoint"),
    )

    assert [(e.project, e.text, e.url) for e in found] == [(
        "detail", "Ship lexical search endpoint", "https://gitlab.example/detail/commit/1",
    )]


def test_search_is_read_only_and_preserves_source_fields(tmp_path):
    """Opening normally or mutating the ledger would break concurrent collector safety."""
    path = _ledger(tmp_path)
    before = path.read_bytes()

    found = search_evidence(
        path, {"detail": "detail"}, frozenset({"detail"}), _query("搜索"),
    )

    assert len(found) == 1
    assert found[0].timestamp == "2026-09-02T10:00:00Z"
    assert found[0].url is None
    assert found[0].id.isdigit()
    assert "feishu" not in found[0].text.lower()
    assert path.read_bytes() == before
    with open_ledger_readonly(path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO events(person, project, ts, source, kind, summary, hash) "
                         "VALUES ('x', 'detail', '2026-09-03T00:00:00Z', 'x', 'x', 'x', 'x')")


def test_count_only_project_returns_aggregate_without_memberkit_detail(tmp_path):
    """Reading events for count-only projects would expose their MemberKit narrative."""
    path = _ledger(tmp_path)

    found = search_evidence(
        path, {"counts": "count_only"}, frozenset({"counts"}), _query("commit"),
    )

    assert len(found) == 1
    assert found[0].project == "counts"
    assert "7" in found[0].text
    assert "MemberKit" not in found[0].text
    assert found[0].url is None


def test_sql_injection_text_is_a_literal_and_cannot_bypass_scope(tmp_path):
    """Interpolating query text would make this return every accessible event."""
    path = _ledger(tmp_path)

    assert search_evidence(
        path, {"detail": "detail", "other": "detail"}, frozenset({"detail"}),
        _query("x' OR 1=1 --"),
    ) == []


@pytest.mark.parametrize("query", [
    _query("endpoint", start="not-a-date"),
    _query("endpoint", end="2026-09-01T00:00:00Z", start="2026-09-02T00:00:00Z"),
    _query("x" * 401),
])
def test_invalid_query_bounds_are_rejected_before_reading(tmp_path, query):
    """Accepting malformed or reversed bounds makes the reader's resource scope unbounded."""
    path = _ledger(tmp_path)

    with pytest.raises(ValueError):
        search_evidence(path, {"detail": "detail", "other": "detail"}, frozenset({"detail"}), query)


def test_hidden_unknown_and_empty_scopes_return_no_evidence(tmp_path):
    """Treating policy gaps as detailed makes unclassified evidence visible."""
    path = _ledger(tmp_path)

    assert search_evidence(path, {"hidden": "hidden"}, frozenset({"hidden"}), _query("launch")) == []
    assert search_evidence(path, {}, frozenset({"detail"}), _query("endpoint")) == []
    assert search_evidence(path, {"detail": "detail"}, frozenset(), _query("endpoint")) == []


def test_natural_question_matches_topic_date_wording_and_returns_full_message(tmp_path):
    path = _ledger(tmp_path)
    message = (
        "Agent Assistant rollout coordination: the planned deployment remains on track "
        "for September 22 after the final access review and staging checks."
    )
    conn = open_db(path)
    insert_events(conn, [Event(
        person="alice", project="agent-assistant", ts="2026-09-18T08:00:00Z",
        source="feishu-channel", kind="message", summary=message[:100],
        refs=json.dumps({"message_id": "om_plan"}),
        raw=json.dumps({
            "msg_type": "text",
            "body": {"content": json.dumps({"text": message})},
        }),
        hash="agent-plan",
    )])
    conn.close()

    found = search_evidence(
        path, {"agent-assistant": "detail"}, frozenset({"agent-assistant"}),
        _query("Tell me about are we doing ok on the planned deployment on 22th sept "
               "for agent assistant"),
    )

    assert len(found) == 1
    assert found[0].text == message


def test_long_message_returns_bounded_window_containing_late_topic(tmp_path):
    path = _ledger(tmp_path)
    message = (
        "General coordination notes. " * 50
        + "The rollback rehearsal for the assistant deployment passed all checks."
    )
    conn = open_db(path)
    insert_events(conn, [Event(
        person="alice", project="agent-assistant", ts="2026-09-18T09:00:00Z",
        source="feishu-channel", kind="message", summary=message[:100], refs=None,
        raw=json.dumps({
            "msg_type": "text",
            "body": {"content": json.dumps({"text": message})},
        }),
        hash="long-agent-plan",
    )])
    conn.close()

    found = search_evidence(
        path, {"agent-assistant": "detail"}, frozenset({"agent-assistant"}),
        _query("rollback rehearsal deployment"),
    )

    assert len(found) == 1
    assert "rollback rehearsal" in found[0].text
    assert len(found[0].text) <= 800


def test_more_relevant_older_event_ranks_before_recent_weak_match(tmp_path):
    path = _ledger(tmp_path)
    conn = open_db(path)
    insert_events(conn, [
        Event(
            person="alice", project="agent-assistant", ts="2026-09-10T08:00:00Z",
            source="gitlab", kind="commit",
            summary="Planned deployment readiness for September 22",
            refs=None, raw=None, hash="strong",
        ),
        Event(
            person="alice", project="agent-assistant", ts="2026-09-19T08:00:00Z",
            source="gitlab", kind="commit", summary="Routine assistant update",
            refs=None, raw=None, hash="weak",
        ),
    ])
    conn.close()

    found = search_evidence(
        path, {"agent-assistant": "detail"}, frozenset({"agent-assistant"}),
        _query("agent assistant planned deployment on 22nd September"),
    )

    assert [item.text for item in found[:2]] == [
        "Planned deployment readiness for September 22",
        "Routine assistant update",
    ]


def test_topic_phrase_outranks_incidental_date_and_version_tokens(tmp_path):
    path = _ledger(tmp_path)
    conn = open_db(path)
    insert_events(conn, [
        Event(
            person="alice", project="agent-assistant", ts="2026-09-10T08:00:00Z",
            source="gitlab", kind="commit", summary="Agent Assistant deployment update",
            refs=None, raw=None, hash="topic",
        ),
        Event(
            person="alice", project="awards", ts="2026-09-22T08:00:00Z",
            source="gitlab", kind="commit", summary="Upgrade deployment image to node:22",
            refs=None, raw=None, hash="incidental-date",
        ),
    ])
    conn.close()

    found = search_evidence(
        path, {"agent-assistant": "detail", "awards": "detail"},
        frozenset({"agent-assistant", "awards"}),
        _query("Tell me about the planned deployment on 22th Sept for agent assistant"),
    )

    assert found[0].project == "agent-assistant"


@pytest.mark.parametrize("text", ["%", "_", r"\\%_", "x' OR 1=1 --"])
def test_sql_wildcards_and_metacharacters_are_literal(tmp_path, text):
    path = _ledger(tmp_path)

    assert search_evidence(
        path, {"detail": "detail", "other": "detail"},
        frozenset({"detail"}), _query(text),
    ) == []


def test_multilingual_question_matches_shared_chinese_topic(tmp_path):
    path = _ledger(tmp_path)
    conn = open_db(path)
    insert_events(conn, [Event(
        person="alice", project="agent-assistant", ts="2026-09-12T08:00:00Z",
        source="feishu-channel", kind="message",
        summary="智能助手九月部署进展顺利，访问审查已经完成",
        refs=None, raw=None, hash="zh-progress",
    )])
    conn.close()

    found = search_evidence(
        path, {"agent-assistant": "detail"}, frozenset({"agent-assistant"}),
        _query("请告诉我智能助手的部署进展怎么样"),
    )

    assert found[0].text == "智能助手九月部署进展顺利，访问审查已经完成"


def test_multilingual_query_matches_json_escaped_rich_commit_body(tmp_path):
    path = _ledger(tmp_path)
    message = "General coordination\n\n" + "普通记录。" * 20 + "部署回滚验证完成"
    conn = open_db(path)
    insert_events(conn, [Event(
        person="alice", project="agent-assistant", ts="2026-09-12T09:00:00Z",
        source="gitlab", kind="commit", summary="General coordination",
        refs=None, raw=json.dumps({"title": "General coordination", "message": message}),
        hash="zh-escaped-progress",
    )])
    conn.close()

    found = search_evidence(
        path, {"agent-assistant": "detail"}, frozenset({"agent-assistant"}),
        _query("部署回滚"),
    )

    assert len(found) == 1
    assert "部署回滚验证完成" in found[0].text


def test_nested_json_escaped_feishu_body_matches_chinese_topic(tmp_path):
    path = _ledger(tmp_path)
    message = "General coordination notes. " * 5 + "部署回滚验证完成"
    conn = open_db(path)
    insert_events(conn, [Event(
        person="alice", project="agent-assistant", ts="2026-09-12T10:00:00Z",
        source="feishu-channel", kind="message", summary=message[:100],
        refs=None,
        raw=json.dumps({
            "msg_type": "text",
            "body": {"content": json.dumps({"text": message}, ensure_ascii=True)},
        }, ensure_ascii=True),
        hash="zh-nested-progress",
    )])
    conn.close()

    found = search_evidence(
        path, {"agent-assistant": "detail"}, frozenset({"agent-assistant"}),
        _query("部署回滚"),
    )

    assert len(found) == 1
    assert "部署回滚验证完成" in found[0].text


def test_unproven_mixed_project_summaries_are_never_relabelled_as_authorized(tmp_path):
    path = _ledger(tmp_path)
    conn = open_db(path)
    put_summary(conn, SummaryRecord(
        kind="daily-person", key="alice|2026-09-01", input_hash="mixed-input",
        text="Detail status. Hidden launch phrase. Count-only private narrative.",
        model="test-model", created_ts="2026-09-02T00:00:00Z",
    ))
    put_summary(conn, SummaryRecord(
        kind="weekly-team", key="team|2026-09-01", input_hash="mixed-week",
        text="Mixed weekly report contains hidden launch phrase.",
        model="test-model", created_ts="2026-09-05T00:00:00Z",
        evidence_cutoff="2026-09-05T00:00:00Z", cutoff_precision="instant",
        coverage_state="friday-checkpoint", source_input_hash="mixed-source",
        effective_flags_json="{}",
    ))
    conn.close()
    before = path.read_bytes()

    found = search_evidence(
        path,
        {"detail": "detail", "hidden": "hidden", "counts": "count_only"},
        frozenset({"detail"}), _query("hidden launch phrase"),
    )

    assert found == []
    assert path.read_bytes() == before


def test_unvalidated_raw_payload_cannot_supply_retrieval_text(tmp_path):
    path = _ledger(tmp_path)
    conn = open_db(path)
    insert_events(conn, [Event(
        person="alice", project="detail", ts="2026-09-03T00:00:00Z",
        source="feishu-channel", kind="message", summary="Public coordination note",
        refs=None,
        raw=json.dumps({
            "msg_type": "text",
            "body": {"content": json.dumps({"text": "private transport sentinel"})},
        }),
        hash="projection-mismatch",
    )])
    conn.close()

    found = search_evidence(
        path, {"detail": "detail"}, frozenset({"detail"}),
        _query("private transport sentinel"),
    )

    assert found == []


def test_natural_count_question_returns_only_authorized_aggregate(tmp_path):
    path = _ledger(tmp_path)

    found = search_evidence(
        path, {"counts": "count_only"}, frozenset({"counts"}),
        _query("How many commits did Alice make for counts this week?"),
    )

    assert [item.text for item in found] == [
        "7 commits by alice for week starting 2026-09-01",
    ]


def test_search_has_an_independent_sql_deadline(tmp_path, monkeypatch):
    path = _ledger(tmp_path)
    calls = 0

    def expired():
        nonlocal calls
        calls += 1
        return 0.0 if calls == 1 else 10.0

    monkeypatch.setattr(time, "monotonic", expired)

    with pytest.raises(RetrievalTimeoutError, match="took too long"):
        search_evidence(
            path, {"detail": "detail"}, frozenset({"detail"}),
            _query("endpoint"),
        )


def test_latest_authorized_thread_update_is_not_dropped_by_older_topic_match(tmp_path):
    path = _ledger(tmp_path)
    parent = "https://gitlab.example/group/project/-/issues/330"
    conn = open_db(path)
    insert_events(conn, [
        Event(
            person="alice", project="agent-assistant", ts="2026-09-07T08:00:00Z",
            source="gitlab", kind="comment",
            summary="[#330] Assistant deployment still reproduces the failure",
            refs=json.dumps({"url": parent + "#note_100"}), raw=None, hash="old-thread",
        ),
        Event(
            person="alice", project="agent-assistant", ts="2026-09-15T20:03:00Z",
            source="gitlab", kind="comment",
            summary="[#330] Verified fixed in staging; rollout may proceed",
            refs=json.dumps({"url": parent + "#note_200"}), raw=None, hash="new-thread",
        ),
    ])
    conn.close()

    found = search_evidence(
        path, {"agent-assistant": "detail"}, frozenset({"agent-assistant"}),
        _query("assistant deployment failure"), limit=1,
    )

    assert len(found) == 1
    assert found[0].text == "[#330] Verified fixed in staging; rollout may proceed"


def test_thread_freshness_lookup_respects_end_and_person_filters(tmp_path):
    path = _ledger(tmp_path)
    parent = "https://gitlab.example/group/project/-/issues/330"
    conn = open_db(path)
    insert_events(conn, [
        Event(
            person="alice", project="agent-assistant", ts="2026-09-07T08:00:00Z",
            source="gitlab", kind="comment",
            summary="[#330] Assistant deployment failure under review",
            refs=json.dumps({"url": parent + "#note_100"}), raw=None, hash="old-filtered",
        ),
        Event(
            person="bob", project="agent-assistant", ts="2026-09-15T20:03:00Z",
            source="gitlab", kind="comment",
            summary="[#330] Verified fixed in staging",
            refs=json.dumps({"url": parent + "#note_200"}), raw=None, hash="new-filtered",
        ),
    ])
    conn.close()

    found = search_evidence(
        path, {"agent-assistant": "detail"}, frozenset({"agent-assistant"}),
        _query("assistant deployment failure", person="alice", end="2026-09-10T00:00:00Z"),
    )

    assert [item.text for item in found] == [
        "[#330] Assistant deployment failure under review",
    ]


def test_thread_freshness_never_crosses_project_scope(tmp_path):
    path = _ledger(tmp_path)
    parent = "https://gitlab.example/group/project/-/issues/330"
    conn = open_db(path)
    insert_events(conn, [
        Event(
            person="alice", project="agent-assistant", ts="2026-09-07T08:00:00Z",
            source="gitlab", kind="comment",
            summary="[#330] Assistant deployment failure under review",
            refs=json.dumps({"url": parent + "#note_100"}), raw=None,
            hash="old-authorized-thread",
        ),
        Event(
            person="alice", project="hidden", ts="2026-09-15T20:03:00Z",
            source="gitlab", kind="comment",
            summary="[#330] Hidden project resolution",
            refs=json.dumps({"url": parent + "#note_200"}), raw=None,
            hash="hidden-thread-update",
        ),
    ])
    conn.close()

    found = search_evidence(
        path, {"agent-assistant": "detail", "hidden": "hidden"},
        frozenset({"agent-assistant"}), _query("assistant deployment failure"),
    )

    assert [item.text for item in found] == [
        "[#330] Assistant deployment failure under review",
    ]


def test_opened_issue_promotes_latest_comment_on_same_resource(tmp_path):
    path = _ledger(tmp_path)
    parent = "https://gitlab.example/group/project/-/issues/324"
    conn = open_db(path)
    insert_events(conn, [
        Event(
            person="alice", project="agent-assistant", ts="2026-09-02T08:00:00Z",
            source="gitlab", kind="issue",
            summary="[opened] Assistant deployment fails readiness",
            refs=json.dumps({"url": parent}), raw=None, hash="opened-resource",
        ),
        Event(
            person="alice", project="agent-assistant", ts="2026-09-15T20:03:00Z",
            source="gitlab", kind="comment",
            summary="[#324] Verified fixed in staging",
            refs=json.dumps({"url": parent + "#note_200"}), raw=None,
            hash="fixed-resource-comment",
        ),
    ])
    conn.close()

    found = search_evidence(
        path, {"agent-assistant": "detail"}, frozenset({"agent-assistant"}),
        _query("assistant deployment readiness failure"), limit=1,
    )

    assert found[0].text == "[#324] Verified fixed in staging"


def test_old_comment_promotes_latest_closed_issue_on_same_resource(tmp_path):
    path = _ledger(tmp_path)
    parent = "https://gitlab.example/group/project/-/issues/325"
    conn = open_db(path)
    insert_events(conn, [
        Event(
            person="alice", project="agent-assistant", ts="2026-09-02T08:00:00Z",
            source="gitlab", kind="comment",
            summary="[#325] Assistant deployment failure under review",
            refs=json.dumps({"url": parent + "#note_100"}), raw=None,
            hash="old-resource-comment",
        ),
        Event(
            person="alice", project="agent-assistant", ts="2026-09-15T20:03:00Z",
            source="gitlab", kind="issue",
            summary="[closed] Assistant deployment failure",
            refs=json.dumps({"url": parent}), raw=None, hash="closed-resource",
        ),
    ])
    conn.close()

    found = search_evidence(
        path, {"agent-assistant": "detail"}, frozenset({"agent-assistant"}),
        _query("assistant deployment failure under review"), limit=1,
    )

    assert found[0].text == "[closed] Assistant deployment failure"


def test_resource_freshness_normalizes_optional_trailing_slash(tmp_path):
    path = _ledger(tmp_path)
    parent = "https://gitlab.example/group/project/-/issues/326"
    conn = open_db(path)
    insert_events(conn, [
        Event(
            person="alice", project="agent-assistant", ts="2026-09-02T08:00:00Z",
            source="gitlab", kind="issue",
            summary="[opened] Assistant deployment readiness failure",
            refs=json.dumps({"url": parent + "/"}), raw=None,
            hash="opened-trailing-slash",
        ),
        Event(
            person="alice", project="agent-assistant", ts="2026-09-15T20:03:00Z",
            source="gitlab", kind="comment",
            summary="[#326] Verified fixed in staging",
            refs=json.dumps({"url": parent + "#note_200"}), raw=None,
            hash="fixed-no-trailing-slash",
        ),
    ])
    conn.close()

    found = search_evidence(
        path, {"agent-assistant": "detail"}, frozenset({"agent-assistant"}),
        _query("assistant deployment readiness failure"), limit=1,
    )

    assert found[0].text == "[#326] Verified fixed in staging"
