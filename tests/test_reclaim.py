from pathlib import Path

from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.reclaim import reclaim
from teammem.store import open_db, insert_events, stats

CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"


def _ev(person, h):
    return Event(person=person, ts="2026-07-14T09:00:00Z", source="gitlab",
                 kind="commit", summary="x", hash=h)


def _seed(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        _ev("_unmapped/alex@example.com", "s1"),   # claimable via email
        _ev("_unmapped/alexdev", "s2"),              # claimable via gitlab username
        _ev("_unmapped/ghost@nowhere.com", "s3"),    # stays
        _ev("sam", "s4"),                           # untouched
    ])
    return conn


def test_reclaim_updates_claimable_rows(tmp_path):
    conn = _seed(tmp_path)
    got = reclaim(conn, IdentityMaps.load(CONFIG_DIR))
    assert ("alex@example.com", "alex", 1) in got
    assert ("alexdev", "alex", 1) in got
    s = stats(conn)
    assert s["by_person"]["alex"] == 2
    assert s["unmapped"] == ["_unmapped/ghost@nowhere.com"]


def test_reclaim_dry_run_writes_nothing(tmp_path):
    conn = _seed(tmp_path)
    got = reclaim(conn, IdentityMaps.load(CONFIG_DIR), dry_run=True)
    assert len(got) == 2
    assert len(stats(conn)["unmapped"]) == 3


def test_reclaim_is_idempotent(tmp_path):
    conn = _seed(tmp_path)
    reclaim(conn, IdentityMaps.load(CONFIG_DIR))
    assert reclaim(conn, IdentityMaps.load(CONFIG_DIR)) == []


def test_reclaim_conflict_is_reported_and_untouched(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [_ev("_unmapped/dup", "s1")])
    ids = IdentityMaps({"members": {
        "a": {"emails": ["dup"]},
        "b": {"gitlab": ["dup"]},
    }}, {})
    got = reclaim(conn, ids)
    assert got == [("dup", "!conflict:a|b", 0)]
    assert stats(conn)["unmapped"] == ["_unmapped/dup"]


def test_reclaim_channel_projects_updates_only_unmapped_rows(tmp_path):
    import json as _json
    from teammem.reclaim import reclaim_channel_projects
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        Event(person="alex", ts="2026-07-14T09:00:00+00:00", source="feishu-channel",
              kind="message", summary="hi", refs=_json.dumps({"chat_id": "oc_new"}),
              hash="m1"),
        Event(person="alex", ts="2026-07-14T09:01:00+00:00", source="feishu-channel",
              kind="message", summary="hi2", project="already",
              refs=_json.dumps({"chat_id": "oc_new"}), hash="m2"),
    ])
    ids = IdentityMaps({"members": {}},
                       {"projects": {"project-alpha": {"feishu_channels": ["oc_new"]}}})
    dry = reclaim_channel_projects(conn, ids, dry_run=True)
    assert dry == [("oc_new", "project-alpha", 1)]
    assert conn.execute("SELECT COUNT(*) FROM events WHERE project='project-alpha'").fetchone()[0] == 0
    live = reclaim_channel_projects(conn, ids)
    assert live == [("oc_new", "project-alpha", 1)]
    assert conn.execute("SELECT project FROM events WHERE hash='m1'").fetchone()[0] == "project-alpha"
    assert conn.execute("SELECT project FROM events WHERE hash='m2'").fetchone()[0] == "already"
