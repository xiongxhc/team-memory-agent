import json
import sqlite3

import pytest

from teammem.chat.retrieval import open_ledger_readonly, search_evidence
from teammem.events import Event
from teammem.store import insert_events, open_db, replace_weekly_commit_counts
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
