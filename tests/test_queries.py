import json
from datetime import date
from pathlib import Path

from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.queries import (week_monday, week_label, week_range,
                             events_between, by_key, flags)
from teammem.store import open_db, insert_events

CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"


def test_week_math():
    assert week_monday(date(2026, 7, 16)) == date(2026, 7, 13)
    assert week_label(date(2026, 7, 13)) == "Week 2026-07-13-17"
    assert week_label(date(2026, 6, 29)) == "Week 2026-06-29-03"   # cross-month
    assert week_range(date(2026, 7, 13)) == ("2026-07-13T00:00:00", "2026-07-20T00:00:00")


def _ev(person, h, ts, project=None, kind="commit"):
    return Event(person=person, ts=ts, source="gitlab", kind=kind,
                 summary=f"work {h}", hash=h, project=project,
                 refs='{"url": "https://x/" + "%s"}' % h)


def _seed(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        _ev("alex", "a1", "2026-07-14T09:00:00+04:00", "project-alpha"),
        _ev("alex", "a2", "2026-07-15T09:00:00+04:00", "project-alpha"),
        _ev("sam", "b1", "2026-07-01T09:00:00+04:00", "project-beta"),  # prior weeks only
        _ev("_unmapped/x@y.z", "c1", "2026-07-14T10:00:00+04:00"),
    ])
    return conn


def test_events_between_and_grouping(tmp_path):
    conn = _seed(tmp_path)
    rows = events_between(conn, "2026-07-13T00:00:00", "2026-07-20T00:00:00")
    assert [r["hash"] for r in rows] == ["a2", "c1", "a1"]        # ts DESC (a2: 07-15, c1: 07-14 10:00, a1: 07-14 09:00)
    assert set(by_key(rows, "person")) == {"alex", "_unmapped/x@y.z"}
    assert set(by_key(rows, "project")) == {"project-alpha", "(no project)"}


def test_flags_gap_unmapped_concentration(tmp_path):
    conn = _seed(tmp_path)
    # concentration fixture: 10 events on one project, 9 by alex
    insert_events(conn, [
        _ev("alex", f"k{i}", "2026-07-14T11:00:00+04:00", "hse") for i in range(9)
    ] + [_ev("sam", "k9", "2026-07-14T12:00:00+04:00", "hse")])
    f = flags(conn, date(2026, 7, 13), IdentityMaps.load(CONFIG_DIR))
    # fix expectation: sam has k9 in-window, so no gap:
    assert f["gaps"] == []
    assert f["unmapped"] == [("_unmapped/x@y.z", 1)]
    assert ("hse", "alex", 0.9) in f["concentration"]


def test_flags_detects_real_gap(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [_ev("sam", "old1", "2026-07-01T09:00:00+04:00")])
    f = flags(conn, date(2026, 7, 13), IdentityMaps.load(CONFIG_DIR))
    assert f["gaps"] == ["sam"]


def _feishu_ev(person, h, ts, chat_id, project=None):
    return Event(person=person, ts=ts, source="feishu-channel", kind="message",
                 summary=f"msg {h}", hash=h, project=project,
                 refs=json.dumps({"chat_id": chat_id, "message_id": f"om_{h}"}))


def test_flags_unmapped_channel(tmp_path):
    conn = _seed(tmp_path)
    insert_events(conn, [
        _feishu_ev("alex", "f1", "2026-07-14T13:00:00+04:00", "oc_9"),
        _feishu_ev("alex", "f2", "2026-07-14T13:05:00+04:00", "oc_9"),
    ])
    # a gitlab, project=None event whose refs happen to carry a chat_id-shaped
    # key must NOT be counted — only feishu-channel rows qualify.
    insert_events(conn, [
        Event(person="sam", ts="2026-07-14T14:00:00+04:00", source="gitlab",
              kind="commit", summary="x", hash="g1", project=None,
              refs='{"chat_id": "oc_should_not_count"}'),
    ])
    f = flags(conn, date(2026, 7, 13), IdentityMaps.load(CONFIG_DIR))
    assert f["unmapped_channels"] == [("oc_9", 2)]


def test_flags_accepts_provider_neutral_channel_id(tmp_path):
    conn = _seed(tmp_path)
    insert_events(conn, [
        Event(
            person="alex",
            ts="2026-07-14T13:00:00+04:00",
            source="slack-channel",
            kind="message",
            summary="msg s1",
            hash="s1",
            refs=json.dumps({"channel_id": "C9"}),
        ),
        Event(
            person="alex",
            ts="2026-07-14T13:05:00+04:00",
            source="discord-channel",
            kind="message",
            summary="msg d1",
            hash="d1",
            refs=json.dumps({"channel_id": "D9"}),
        ),
    ])

    result = flags(conn, date(2026, 7, 13), IdentityMaps.load(CONFIG_DIR))

    assert result["unmapped_channels"] == [("C9", 1), ("D9", 1)]
