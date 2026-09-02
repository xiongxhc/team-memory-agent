import json
import sqlite3
from pathlib import Path

import pytest

from teammem.events import Event, event_hash
from teammem.identity import IdentityMaps
from teammem.metrics import CommitCountScope, WeeklyCommitCount
from teammem.store import (
    insert_events,
    open_db,
    reconcile_gitlab_events,
    replace_weekly_commit_counts,
    stats,
    weekly_commit_counts,
)


PROVENANCE = {
    "evidence_cutoff", "cutoff_precision", "coverage_state",
    "source_input_hash", "effective_flags_json",
}
CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"


def _ev(**kw):
    base = dict(person="alex", ts="2026-07-14T09:00:00Z", source="gitlab",
                kind="commit", summary="fix auth", hash="sha-1")
    base.update(kw)
    return Event(**base)


def test_insert_twice_is_idempotent(tmp_path):
    conn = open_db(tmp_path / "ledger.db")
    assert insert_events(conn, [_ev()]) == 1
    assert insert_events(conn, [_ev()]) == 0          # spec success criterion #3
    assert stats(conn)["total"] == 1


def test_same_hash_different_source_is_two_rows(tmp_path):
    conn = open_db(tmp_path / "ledger.db")
    assert insert_events(conn, [_ev(), _ev(source="bundle:alex")]) == 2


def test_gitlab_commit_reconciliation_upgrades_legacy_bare_sha_without_duplicate(
    tmp_path,
):
    conn = open_db(tmp_path / "ledger.db")
    refs = json.dumps({"sha": "shared-sha", "url": "https://gitlab.test/a/commit/shared-sha"})
    legacy = _ev(project="project-alpha", hash="shared-sha", refs=refs)
    current = _ev(
        project="project-alpha",
        summary="current commit title",
        hash=event_hash("commit", "1", "shared-sha"),
        refs=refs,
    )
    assert insert_events(conn, [legacy]) == 1

    inserted = reconcile_gitlab_events(
        conn,
        [current],
        IdentityMaps.load(CONFIG_DIR),
    )

    assert inserted == 0
    assert conn.execute(
        "SELECT project, summary, hash FROM events"
    ).fetchall() == [(
        "project-alpha",
        "current commit title",
        event_hash("commit", "1", "shared-sha"),
    )]


def test_gitlab_commit_reconciliation_maps_null_legacy_project_without_duplicate(
    tmp_path,
):
    conn = open_db(tmp_path / "ledger.db")
    refs = json.dumps({"sha": "shared-sha", "url": "https://gitlab.test/a/commit/shared-sha"})
    legacy = _ev(project=None, hash="shared-sha", refs=refs)
    current = _ev(
        project="project-alpha",
        summary="current commit title",
        hash=event_hash("commit", "1", "shared-sha"),
        refs=refs,
    )
    assert insert_events(conn, [legacy]) == 1

    inserted = reconcile_gitlab_events(
        conn,
        [current],
        IdentityMaps.load(CONFIG_DIR),
    )

    assert inserted == 0
    assert conn.execute(
        "SELECT project, summary, hash FROM events"
    ).fetchall() == [(
        "project-alpha",
        "current commit title",
        event_hash("commit", "1", "shared-sha"),
    )]


def test_gitlab_commit_reconciliation_preserves_same_sha_across_projects(
    tmp_path,
):
    conn = open_db(tmp_path / "ledger.db")
    alpha_refs = json.dumps({
        "sha": "shared-sha",
        "url": "https://gitlab.test/a/commit/shared-sha",
    })
    alpha = _ev(
        project="project-alpha",
        hash=event_hash("commit", "1", "shared-sha"),
        refs=alpha_refs,
    )
    beta = _ev(
        project="project-beta",
        hash=event_hash("commit", "2", "shared-sha"),
        refs=json.dumps({
            "sha": "shared-sha",
            "url": "https://gitlab.test/b/commit/shared-sha",
        }),
    )
    assert insert_events(conn, [_ev(
        project="project-alpha",
        hash="shared-sha",
        refs=alpha_refs,
    )]) == 1

    inserted = reconcile_gitlab_events(
        conn,
        [alpha, beta],
        IdentityMaps.load(CONFIG_DIR),
    )

    assert inserted == 1
    assert conn.execute(
        "SELECT project, hash FROM events ORDER BY project"
    ).fetchall() == [
        ("project-alpha", event_hash("commit", "1", "shared-sha")),
        ("project-beta", event_hash("commit", "2", "shared-sha")),
    ]


def test_gitlab_commit_reconciliation_does_not_claim_other_repo_legacy_row(
    tmp_path,
):
    conn = open_db(tmp_path / "ledger.db")
    legacy_refs = json.dumps({
        "sha": "shared-sha",
        "url": "https://gitlab.test/a/commit/shared-sha",
    })
    current_refs = json.dumps({
        "sha": "shared-sha",
        "url": "https://gitlab.test/b/commit/shared-sha",
    })
    assert insert_events(conn, [_ev(
        project="shared-product",
        hash="shared-sha",
        refs=legacy_refs,
    )]) == 1

    inserted = reconcile_gitlab_events(
        conn,
        [_ev(
            project="shared-product",
            hash=event_hash("commit", "2", "shared-sha"),
            refs=current_refs,
        )],
        IdentityMaps.load(CONFIG_DIR),
    )

    assert inserted == 1
    assert conn.execute(
        "SELECT refs, hash FROM events ORDER BY refs"
    ).fetchall() == [
        (legacy_refs, "shared-sha"),
        (current_refs, event_hash("commit", "2", "shared-sha")),
    ]


def test_open_db_twice_is_safe(tmp_path):
    p = tmp_path / "ledger.db"
    open_db(p).close()
    conn = open_db(p)                                  # migrate must be idempotent
    assert stats(conn)["total"] == 0


def test_open_db_adds_aggregate_table_without_rewriting_legacy_rows(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE events (
          id INTEGER PRIMARY KEY,
          person TEXT NOT NULL,
          project TEXT,
          ts TEXT NOT NULL,
          source TEXT NOT NULL,
          kind TEXT NOT NULL,
          summary TEXT NOT NULL,
          refs TEXT,
          raw TEXT,
          hash TEXT NOT NULL,
          UNIQUE(person, source, hash)
        );
        CREATE TABLE summaries (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL,
          key TEXT NOT NULL,
          input_hash TEXT NOT NULL,
          text TEXT NOT NULL,
          model TEXT NOT NULL,
          created_ts TEXT NOT NULL,
          UNIQUE(kind, key)
        );
        INSERT INTO events(person, project, ts, source, kind, summary, hash)
        VALUES ('alex', 'project-alpha', '2026-08-31T10:00:00Z', 'gitlab',
                'commit', 'legacy commit', 'legacy-sha');
        INSERT INTO summaries(kind, key, input_hash, text, model, created_ts)
        VALUES ('weekly-team', 'team|2026-08-31', 'input', 'legacy summary',
                'model', '2026-09-01T10:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    upgraded = open_db(path)
    assert upgraded.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert upgraded.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 1
    assert upgraded.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name = 'weekly_commit_counts'"
    ).fetchone() == ("weekly_commit_counts",)
    upgraded.close()


def test_replace_weekly_commit_counts_is_transactional_and_idempotent(tmp_path):
    conn = open_db(tmp_path / "ledger.db")
    scopes = (CommitCountScope("team-memory-agent", "2026-08-31"),)
    first = (
        WeeklyCommitCount("team-memory-agent", "2026-08-31", "cx", 9),
        WeeklyCommitCount("team-memory-agent", "2026-08-31", "sam", 3),
    )

    assert replace_weekly_commit_counts(conn, scopes, first) == 2
    assert replace_weekly_commit_counts(conn, scopes, first) == 0
    assert replace_weekly_commit_counts(conn, scopes, ()) == 2
    assert weekly_commit_counts(conn, "team-memory-agent", "2026-08-31") == []


def test_weekly_commit_counts_sort_by_count_then_person(tmp_path):
    conn = open_db(tmp_path / "ledger.db")
    scope = CommitCountScope("project-alpha", "2026-08-31")
    counts = (
        WeeklyCommitCount("project-alpha", "2026-08-31", "zoe", 4),
        WeeklyCommitCount("project-alpha", "2026-08-31", "alex", 4),
        WeeklyCommitCount("project-alpha", "2026-08-31", "sam", 9),
    )
    assert replace_weekly_commit_counts(conn, (scope,), counts) == 3
    assert weekly_commit_counts(conn, "project-alpha", "2026-08-31") == [
        counts[2], counts[1], counts[0]
    ]


def test_replace_weekly_commit_counts_counts_an_updated_row_once(tmp_path):
    conn = open_db(tmp_path / "ledger.db")
    scope = CommitCountScope("project-alpha", "2026-08-31")
    original = WeeklyCommitCount("project-alpha", "2026-08-31", "cx", 9)
    replacement = WeeklyCommitCount("project-alpha", "2026-08-31", "cx", 10)
    assert replace_weekly_commit_counts(conn, (scope,), (original,)) == 1
    assert replace_weekly_commit_counts(conn, (scope,), (replacement,)) == 1


def test_replace_weekly_commit_counts_rolls_back_when_insert_fails(tmp_path):
    conn = open_db(tmp_path / "ledger.db")
    scope = CommitCountScope("project-alpha", "2026-08-31")
    original = WeeklyCommitCount("project-alpha", "2026-08-31", "cx", 9)
    replacement = WeeklyCommitCount("project-alpha", "2026-08-31", "cx", 10)
    assert replace_weekly_commit_counts(conn, (scope,), (original,)) == 1
    conn.execute(
        "CREATE TRIGGER reject_weekly_count_insert "
        "BEFORE INSERT ON weekly_commit_counts BEGIN "
        "SELECT RAISE(ABORT, 'blocked'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="blocked"):
        replace_weekly_commit_counts(conn, (scope,), (replacement,))

    assert weekly_commit_counts(conn, "project-alpha", "2026-08-31") == [original]


def test_replace_weekly_commit_counts_reads_old_rows_inside_transaction(tmp_path):
    conn = open_db(tmp_path / "ledger.db")
    scope = CommitCountScope("project-alpha", "2026-08-31")
    original = WeeklyCommitCount("project-alpha", "2026-08-31", "cx", 9)
    replacement = WeeklyCommitCount("project-alpha", "2026-08-31", "cx", 10)
    assert replace_weekly_commit_counts(conn, (scope,), (original,)) == 1
    transaction_state = []

    def trace(sql):
        if "SELECT project, week_start, person, commit_count" in sql:
            transaction_state.append(conn.in_transaction)

    conn.set_trace_callback(trace)
    try:
        assert replace_weekly_commit_counts(conn, (scope,), (replacement,)) == 1
    finally:
        conn.set_trace_callback(None)
    assert transaction_state == [True]


def test_replace_weekly_commit_counts_rejects_out_of_scope_counts(tmp_path):
    conn = open_db(tmp_path / "ledger.db")
    scope = CommitCountScope("project-alpha", "2026-08-31")
    with pytest.raises(ValueError, match="scope"):
        replace_weekly_commit_counts(
            conn,
            (scope,),
            (WeeklyCommitCount("project-beta", "2026-08-31", "cx", 1),),
        )
    assert weekly_commit_counts(conn, "project-alpha", "2026-08-31") == []


@pytest.mark.parametrize("commit_count", [0, -1, True])
def test_replace_weekly_commit_counts_rejects_non_positive_counts(tmp_path, commit_count):
    conn = open_db(tmp_path / "ledger.db")
    with pytest.raises(ValueError, match="positive"):
        replace_weekly_commit_counts(
            conn,
            (CommitCountScope("project-alpha", "2026-08-31"),),
            (WeeklyCommitCount("project-alpha", "2026-08-31", "cx", commit_count),),
        )


def test_open_db_migrates_legacy_summaries_once(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE summaries (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, "
        "key TEXT NOT NULL, input_hash TEXT NOT NULL, text TEXT NOT NULL, "
        "model TEXT NOT NULL, created_ts TEXT NOT NULL, UNIQUE(kind,key))"
    )
    conn.execute(
        "INSERT INTO summaries(kind,key,input_hash,text,model,created_ts) "
        "VALUES('weekly-team','team|2026-07-27','h','old','m','t')"
    )
    conn.commit()
    conn.close()

    from teammem.store import get_summary

    upgraded = open_db(path)
    columns = {row[1] for row in upgraded.execute("PRAGMA table_info(summaries)")}
    assert PROVENANCE <= columns
    record = get_summary(upgraded, "weekly-team", "team|2026-07-27")
    assert record.text == "old"
    assert record.evidence_cutoff is None
    upgraded.close()
    open_db(path).close()


def test_open_db_rolls_back_every_column_when_legacy_migration_fails(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE summaries (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, "
        "key TEXT NOT NULL, input_hash TEXT NOT NULL, text TEXT NOT NULL, "
        "model TEXT NOT NULL, created_ts TEXT NOT NULL, UNIQUE(kind,key))"
    )
    conn.execute(
        "INSERT INTO summaries(kind,key,input_hash,text,model,created_ts) "
        "VALUES('weekly-team','team|2026-07-27','h','old','m','t')"
    )
    conn.commit()
    conn.close()

    from teammem import store

    monkeypatch.setattr(
        store,
        "_SUMMARY_PROVENANCE_COLUMNS",
        {**store._SUMMARY_PROVENANCE_COLUMNS, "must_fail": "TEXT NOT NULL"},
    )
    with pytest.raises(sqlite3.OperationalError):
        store.open_db(path)

    inspected = sqlite3.connect(path)
    columns = {row[1] for row in inspected.execute("PRAGMA table_info(summaries)")}
    inspected.close()
    assert not (PROVENANCE & columns)


def test_put_summary_round_trips_every_record_field_atomically(tmp_path):
    from teammem.store import SummaryRecord, get_summary, put_summary

    conn = open_db(tmp_path / "ledger.db")
    record = SummaryRecord(
        kind="weekly-team",
        key="team|2026-07-27",
        input_hash="input-hash",
        text="first narrative",
        model="test-model",
        created_ts="2026-08-05T10:00:00Z",
        evidence_cutoff="2026-08-05T09:59:59Z",
        cutoff_precision="second",
        coverage_state="complete",
        source_input_hash="source-hash",
        effective_flags_json='{"include_prs":true}',
    )
    put_summary(conn, record)
    assert get_summary(conn, record.kind, record.key) == record

    replacement = SummaryRecord(
        kind=record.kind,
        key=record.key,
        input_hash="replacement-hash",
        text="replacement narrative",
        model="replacement-model",
        created_ts="2026-08-05T11:00:00Z",
        evidence_cutoff="2026-08-05",
        cutoff_precision="day",
        coverage_state="partial",
        source_input_hash="replacement-source-hash",
        effective_flags_json='{"include_prs":false}',
    )
    put_summary(conn, replacement)
    assert get_summary(conn, replacement.kind, replacement.key) == replacement
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 1


def test_stats_groups_and_surfaces_unmapped(tmp_path):
    conn = open_db(tmp_path / "ledger.db")
    insert_events(conn, [_ev(), _ev(person="_unmapped/x@y.z", hash="sha-2", kind="mr")])
    s = stats(conn)
    assert s["by_kind"] == {"commit": 1, "mr": 1}
    assert s["by_person"]["alex"] == 1
    assert s["unmapped"] == ["_unmapped/x@y.z"]


def test_get_or_make_miss_hit_and_regenerate(tmp_path):
    from teammem.store import get_or_make
    conn = open_db(tmp_path / "l.db")
    calls = []

    def make():
        calls.append(1)
        return f"narrative v{len(calls)}", "fake-model"

    # miss -> generates
    t1 = get_or_make(conn, "daily-person", "alex|2026-07-14", "hashA", make, "2026-07-14T18:20:00")
    assert t1 == "narrative v1" and len(calls) == 1
    # hit on same hash -> no regeneration
    t2 = get_or_make(conn, "daily-person", "alex|2026-07-14", "hashA", make, "2026-07-15T18:20:00")
    assert t2 == "narrative v1" and len(calls) == 1
    # changed hash -> regenerates, overwrites the single (kind, key) row
    t3 = get_or_make(conn, "daily-person", "alex|2026-07-14", "hashB", make, "2026-07-15T18:20:00")
    assert t3 == "narrative v2" and len(calls) == 2
    rows = conn.execute("SELECT input_hash, text, model FROM summaries").fetchall()
    assert rows == [("hashB", "narrative v2", "fake-model")]


def test_get_or_make_keys_are_independent(tmp_path):
    from teammem.store import get_or_make
    conn = open_db(tmp_path / "l.db")
    get_or_make(conn, "daily-person", "a|2026-07-14", "h1", lambda: ("A", "m"), "t")
    get_or_make(conn, "weekly-team", "team|2026-07-13", "h1", lambda: ("W", "m"), "t")
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 2
