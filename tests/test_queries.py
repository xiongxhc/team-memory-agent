import json
from datetime import date
from pathlib import Path

from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.queries import (week_monday, week_label, week_range,
                             events_between, by_key, flags, report_context)
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


def test_flags_orders_equal_count_unmapped_people_by_person(tmp_path):
    events = [
        _ev("_unmapped/b", "b", "2026-07-14T09:00:00+04:00"),
        _ev("_unmapped/a", "a", "2026-07-14T09:00:00+04:00"),
    ]
    results = []
    for name, ordered in (("forward", events), ("reverse", events[::-1])):
        conn = open_db(tmp_path / f"{name}.db")
        insert_events(conn, ordered)
        results.append(
            flags(conn, date(2026, 7, 13), IdentityMaps.load(CONFIG_DIR))["unmapped"]
        )

    assert results == [
        [("_unmapped/a", 1), ("_unmapped/b", 1)],
        [("_unmapped/a", 1), ("_unmapped/b", 1)],
    ]


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


def _report_fixture(tmp_path):
    conn = open_db(tmp_path / "report.db")
    insert_events(conn, [
        _ev("sam", "prior", "2026-07-20T09:00:00+04:00"),
        _ev("alex", "current", "2026-08-04T09:00:00+04:00"),
        _ev("_unmapped/x@y.z", "unmapped", "2026-08-04T10:00:00+04:00"),
        _feishu_ev("alex", "channel-1", "2026-08-04T11:00:00+04:00", "oc_9"),
        _feishu_ev("alex", "channel-2", "2026-08-04T12:00:00+04:00", "oc_9"),
        *[
            _ev("alex", f"concentrated-{i}", "2026-08-04T13:00:00+04:00", "hse")
            for i in range(9)
        ],
        _ev("_unmapped/x@y.z", "concentrated-other", "2026-08-04T14:00:00+04:00", "hse"),
    ])
    return conn


def test_report_context_suppresses_only_deferred_flags_before_friday(tmp_path):
    conn = _report_fixture(tmp_path)

    context = report_context(
        conn,
        date(2026, 8, 3),
        date(2026, 8, 4),
        IdentityMaps.load(CONFIG_DIR),
        {("alex", "2026-08-04")},
    )

    assert context.state.coverage_state == "provisional"
    assert context.effective_flags == {
        "unmapped": [("_unmapped/x@y.z", 2)],
        "unmapped_channels": [("oc_9", 2)],
    }


def test_report_context_keeps_full_flags_at_friday_checkpoint_and_for_previous_week(tmp_path):
    conn = _report_fixture(tmp_path)
    ids = IdentityMaps.load(CONFIG_DIR)
    included = {("alex", "2026-08-04")}

    friday = report_context(conn, date(2026, 8, 3), date(2026, 8, 7), ids, included)
    saturday = report_context(conn, date(2026, 8, 3), date(2026, 8, 8), ids, included)
    sunday = report_context(conn, date(2026, 8, 3), date(2026, 8, 9), ids, included)
    previous = report_context(conn, date(2026, 8, 3), date(2026, 8, 10), ids, included)

    expected = {
        "gaps": ["sam"],
        "unmapped": [("_unmapped/x@y.z", 2)],
        "unmapped_channels": [("oc_9", 2)],
        "concentration": [("hse", "alex", 0.9)],
    }
    for context in (friday, saturday, sunday, previous):
        assert context.state.coverage_state == "friday-checkpoint"
        assert context.effective_flags == expected


def test_report_context_uses_chronological_aware_cutoff_from_included_person_days(tmp_path):
    conn = open_db(tmp_path / "report.db")
    insert_events(conn, [
        _ev("alex", "late", "2026-08-04T23:30:00-04:00"),
        _ev("sam", "earlier", "2026-08-05T01:00:00+04:00"),
        _ev("sam", "excluded", "2026-08-06T12:00:00Z"),
    ])

    context = report_context(
        conn,
        date(2026, 8, 3),
        date(2026, 8, 7),
        IdentityMaps.load(CONFIG_DIR),
        {("alex", "2026-08-04"), ("sam", "2026-08-05")},
    )

    assert context.state.evidence_cutoff == "2026-08-04T23:30:00-04:00"
    assert context.state.cutoff_precision == "instant"
    assert context.state.cutoff_note is None


def test_report_context_uses_date_precision_when_latest_day_has_naive_timestamps(tmp_path):
    conn = open_db(tmp_path / "report.db")
    insert_events(conn, [
        _ev("alex", "aware", "2026-08-05T23:00:00+04:00"),
        _ev("sam", "naive", "2026-08-05T22:00:00"),
    ])

    context = report_context(
        conn,
        date(2026, 8, 3),
        date(2026, 8, 7),
        IdentityMaps.load(CONFIG_DIR),
        {("alex", "2026-08-05"), ("sam", "2026-08-05")},
    )

    assert context.state.evidence_cutoff == "2026-08-05"
    assert context.state.cutoff_precision == "date"
    assert context.state.cutoff_note == "some source timestamps omit offsets"


def test_report_context_degrades_when_an_earlier_included_day_is_naive(tmp_path):
    conn = open_db(tmp_path / "report.db")
    insert_events(conn, [
        _ev("alex", "naive-earlier", "2026-08-04T23:00:00"),
        _ev("sam", "aware-later", "2026-08-05T01:00:00+04:00"),
    ])

    context = report_context(
        conn,
        date(2026, 8, 3),
        date(2026, 8, 7),
        IdentityMaps.load(CONFIG_DIR),
        {("alex", "2026-08-04"), ("sam", "2026-08-05")},
    )

    assert context.state.evidence_cutoff == "2026-08-05"
    assert context.state.cutoff_precision == "date"
    assert context.state.cutoff_note == "some source timestamps omit offsets"


def test_report_context_has_no_cutoff_without_included_person_days(tmp_path):
    context = report_context(
        _report_fixture(tmp_path),
        date(2026, 8, 3),
        date(2026, 8, 7),
        IdentityMaps.load(CONFIG_DIR),
        set(),
    )

    assert context.state.evidence_cutoff is None
    assert context.state.cutoff_precision == "none"
    assert context.state.cutoff_note is None


def test_report_context_uses_current_week_across_year_boundary(tmp_path):
    context = report_context(
        _report_fixture(tmp_path),
        date(2026, 12, 28),
        date(2026, 12, 31),
        IdentityMaps.load(CONFIG_DIR),
        set(),
    )

    assert context.state.target_monday == date(2026, 12, 28)
    assert context.state.coverage_state == "provisional"
