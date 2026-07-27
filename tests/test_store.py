from teammem.events import Event
from teammem.store import open_db, insert_events, stats


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
