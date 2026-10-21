import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from teammem.events import Event, event_hash
from teammem.identity import IdentityMaps
from teammem.store import insert_events, open_db, reconcile_gitlab_events, stats


PROVENANCE = {
    "evidence_cutoff", "cutoff_precision", "coverage_state",
    "source_input_hash", "effective_flags_json",
}
CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"


@contextmanager
def legacy_wal_ledger(path):
    """An unmigrated ledger with committed data found only in its live WAL."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE summaries (
            id INTEGER PRIMARY KEY, kind TEXT, key TEXT, input_hash TEXT,
            text TEXT, model TEXT, created_ts TEXT, UNIQUE(kind, key));
        CREATE TABLE marker (value TEXT);
        INSERT INTO marker VALUES ('checkpointed');
        PRAGMA user_version = 7;
        PRAGMA journal_mode = WAL;
        PRAGMA wal_autocheckpoint = 0;
    """)
    conn.execute("INSERT INTO marker VALUES ('committed WAL only')")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def legacy_persist_ledger(path, journal_mode="PERSIST"):
    """Commit a legacy ledger and close its last connection, retaining a cold journal."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA journal_mode = {journal_mode}")
        conn.executescript("""
            CREATE TABLE summaries (
                id INTEGER PRIMARY KEY, kind TEXT, key TEXT, input_hash TEXT,
                text TEXT, model TEXT, created_ts TEXT, UNIQUE(kind, key));
            CREATE TABLE marker (value TEXT);
            INSERT INTO marker VALUES ('committed persist');
            PRAGMA user_version = 7;
        """)
        return ledger_logical(conn)
    finally:
        conn.close()


def ledger_bytes(path):
    return {suffix: p.read_bytes() if p.exists() else None
            for suffix in ("", "-wal", "-shm", "-journal")
            for p in [Path(str(path) + suffix)]}


def ledger_logical(conn):
    return (list(conn.iterdump()), {
        name: conn.execute(f"PRAGMA {name}").fetchall()
        for name in ("schema_version", "user_version", "page_count", "freelist_count")
    })


def test_dry_run_snapshot_migrates_legacy_wal_without_touching_source(tmp_path):
    from teammem import store

    path = tmp_path / "ledger.db"
    with legacy_wal_ledger(path) as writer:
        logical_before = ledger_logical(writer)
        before = ledger_bytes(path)
        for _ in range(2):
            conn = store.open_db_snapshot(path)
            try:
                assert conn.execute("SELECT value FROM marker").fetchall() == [
                    ("checkpointed",), ("committed WAL only",)]
                assert PROVENANCE <= {r[1] for r in conn.execute("PRAGMA table_info(summaries)")}
                assert conn.execute("SELECT COUNT(*) FROM meetings").fetchone() == (0,)
            finally:
                conn.close()
            assert ledger_bytes(path) == before
            assert ledger_logical(writer) == logical_before


def test_dry_run_snapshot_resolves_database_alias_before_finding_wal(tmp_path):
    from teammem import store

    path = tmp_path / "ledger.db"
    alias = tmp_path / "alias.db"
    alias.symlink_to(path)
    with legacy_wal_ledger(path):
        before = ledger_bytes(path)
        conn = store.open_db_snapshot(alias)
        try:
            assert conn.execute("SELECT COUNT(*) FROM marker").fetchone() == (2,)
        finally:
            conn.close()
        assert ledger_bytes(path) == before
        assert ledger_bytes(alias)["-wal"] is None


def test_dry_run_snapshot_repairs_expiry_only_in_ephemeral_view(tmp_path):
    from teammem import store
    from tests.test_meeting_store import _legacy_fractional_expiry_db

    path = _legacy_fractional_expiry_db(tmp_path)
    before = ledger_bytes(path)
    conn = store.open_db_snapshot(path)
    try:
        assert conn.execute(
            "SELECT audio_expires_at FROM meeting_audio ORDER BY revision"
        ).fetchall() == [("2026-09-16T14:52:00.125000Z",), ("2026-09-16T14:52:00.875000Z",)]
    finally:
        conn.close()
    assert ledger_bytes(path) == before


def test_dry_run_snapshot_refuses_active_rollback_journal_without_recovery(tmp_path):
    from teammem import store

    path = tmp_path / "ledger.db"
    writer = sqlite3.connect(path)
    writer.execute("CREATE TABLE marker(value)")
    writer.commit()
    writer.execute("INSERT INTO marker VALUES (1)")
    before = ledger_bytes(path)
    try:
        with pytest.raises(ValueError, match="journal"):
            store.open_db_snapshot(path)
        assert ledger_bytes(path) == before
        assert writer.in_transaction
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("journal_mode, header", [("PERSIST", bytes(28)), ("TRUNCATE", b"")])
def test_dry_run_snapshot_migrates_idle_persist_ledger_without_touching_source(
    tmp_path, journal_mode, header,
):
    from teammem import store

    path = tmp_path / "ledger.db"
    logical = legacy_persist_ledger(path, journal_mode)
    before = ledger_bytes(path)
    assert before["-journal"][:28] == header
    for _ in range(2):
        conn = store.open_db_snapshot(path)
        try:
            assert conn.execute("SELECT value FROM marker").fetchall() == [
                ("committed persist",)]
            assert PROVENANCE <= {r[1] for r in conn.execute("PRAGMA table_info(summaries)")}
            assert conn.execute("SELECT COUNT(*) FROM meetings").fetchone() == (0,)
        finally:
            conn.close()
        assert ledger_bytes(path) == before
        source = sqlite3.connect(path.as_uri() + "?immutable=1", uri=True)
        try:
            assert ledger_logical(source) == logical
        finally:
            source.close()
        assert ledger_bytes(path) == before


def test_dry_run_snapshot_refuses_cold_journal_that_becomes_active_during_copy(tmp_path, monkeypatch):
    from teammem import store
    import shutil

    path = tmp_path / "ledger.db"
    legacy_persist_ledger(path)
    writer = sqlite3.connect(path)
    real_copy = shutil.copyfile
    changed = []

    def start_transaction(source, target):
        result = real_copy(source, target)
        writer.execute("INSERT INTO marker VALUES ('not committed')")
        changed.append(ledger_bytes(path))
        return result

    monkeypatch.setattr(shutil, "copyfile", start_transaction)
    try:
        with pytest.raises(ValueError, match="journal"):
            store.open_db_snapshot(path)
        assert writer.in_transaction
        assert ledger_bytes(path) == changed[0]
    finally:
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("continuous", [False, True])
def test_dry_run_snapshot_retries_changed_wal_and_refuses_continuous_writes(
    tmp_path, monkeypatch, continuous,
):
    from teammem import store
    import shutil

    path = tmp_path / "ledger.db"
    real_copy = shutil.copyfile
    with legacy_wal_ledger(path) as writer:
        copies = []

        def racing_copy(source, target):
            result = real_copy(source, target)
            if str(source).endswith("-wal"):
                copies.append(source)
                if continuous or len(copies) == 1:
                    writer.execute("INSERT INTO marker VALUES ('raced')")
                    writer.commit()
            return result

        monkeypatch.setattr(shutil, "copyfile", racing_copy)
        if continuous:
            with pytest.raises(ValueError, match="stable.*snapshot"):
                store.open_db_snapshot(path)
        else:
            conn = store.open_db_snapshot(path)
            try:
                assert conn.execute("SELECT COUNT(*) FROM marker").fetchone() == (3,)
            finally:
                conn.close()
        assert 1 < len(copies) <= 3


def test_dry_run_snapshot_preserves_in_memory_caller_transaction(tmp_path):
    from teammem import store

    caller = sqlite3.connect(":memory:")
    caller.execute("CREATE TABLE marker(value)")
    caller.execute("INSERT INTO marker VALUES ('uncommitted')")
    before = ledger_logical(caller)
    conn = store.open_db_snapshot(tmp_path / "absent.db", conn=caller)
    assert conn.execute("SELECT value FROM marker").fetchall() == [("uncommitted",)]
    conn.close()
    assert caller.in_transaction
    assert ledger_logical(caller) == before
    assert not (tmp_path / "absent.db").exists()
    caller.rollback()
    caller.close()


def test_dry_run_snapshot_refuses_disk_caller_transaction_without_changing_it(tmp_path):
    from teammem import store

    path = tmp_path / "ledger.db"
    with legacy_wal_ledger(path) as caller:
        caller.execute("BEGIN")
        before = ledger_bytes(path)
        with pytest.raises(ValueError, match="transaction"):
            store.open_db_snapshot(path, conn=caller)
        assert ledger_bytes(path) == before
        assert caller.in_transaction
        caller.rollback()


def test_dry_run_snapshot_does_not_acquire_wal_state_on_idle_disk_caller(tmp_path):
    from teammem import store

    path = tmp_path / "ledger.db"
    with legacy_wal_ledger(path):
        caller = sqlite3.connect(path)
        before = ledger_bytes(path)
        try:
            conn = store.open_db_snapshot(tmp_path / "wrong.db", conn=caller)
            assert conn.execute("SELECT COUNT(*) FROM marker").fetchone() == (2,)
            conn.close()
            assert ledger_bytes(path) == before
            assert not caller.in_transaction
            assert caller.execute("SELECT 1").fetchone() == (1,)
        finally:
            caller.close()


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


# The ledger holds meeting transcripts, names and summaries, so it is private
# whatever umask the scheduler or shell happens to run under.

@pytest.fixture
def open_umask():
    import os
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX modes")
def test_open_db_creates_a_private_ledger_and_parent_under_an_open_umask(tmp_path, open_umask):
    import os
    import stat as stat_module
    path = tmp_path / "state" / "nested" / "ledger.db"
    conn = open_db(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS probe (x)")
    conn.execute("INSERT INTO probe VALUES (1)")
    conn.commit()

    def mode(p):
        return stat_module.S_IMODE(os.stat(p).st_mode)

    assert mode(path) == 0o600
    for sidecar in ("-wal", "-shm"):
        assert mode(f"{path}{sidecar}") == 0o600
    assert mode(path.parent) == 0o700
    assert mode(path.parent.parent) == 0o700
    conn.close()


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX modes")
def test_open_db_tightens_an_existing_exposed_ledger(tmp_path, open_umask):
    import os
    import stat as stat_module
    path = tmp_path / "ledger.db"
    open_db(path).close()
    os.chmod(path, 0o644)
    open_db(path).close()
    assert stat_module.S_IMODE(os.stat(path).st_mode) == 0o600
