import sqlite3

import pytest

from teammem.events import Event
from teammem.store import insert_events, open_db, stats


PROVENANCE = {
    "evidence_cutoff", "cutoff_precision", "coverage_state",
    "source_input_hash", "effective_flags_json",
}


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


def test_open_db_twice_is_safe(tmp_path):
    p = tmp_path / "ledger.db"
    open_db(p).close()
    conn = open_db(p)                                  # migrate must be idempotent
    assert stats(conn)["total"] == 0


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
