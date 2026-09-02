import json

import pytest

from teammem.bundles import BundleError, bundle_events, load_bundle
from teammem.store import insert_events, open_db


def _event(summary="Shipped café support"):
    return {
        "ts": "2026-07-27T10:00:00",
        "kind": "journal-highlight",
        "summary": summary,
        "project": "project-alpha",
        "refs": None,
    }


def _write(inbox, member="alex", date="2026-07-27", **changes):
    path = inbox / member / f"bundle-{member}-{date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema": "teammem-bundle/v1",
        "member": member,
        "date": date,
        "events": [_event()],
        "journal_md": f"## {date}",
    }
    data.update(changes)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_valid_bundle_preserves_non_ascii_and_converts_event(tmp_path):
    inbox = tmp_path / "inbox"
    bundle = load_bundle(_write(inbox), inbox)

    events = bundle_events(bundle, "alex")

    assert bundle.events[0]["summary"] == "Shipped café support"
    assert events[0].person == "alex"
    assert events[0].source == "bundle:alex"
    assert "café" in events[0].raw


def test_memberkit_event_preserves_project_when_inserted_into_ledger(tmp_path):
    """Bundle conversion remains independent of project projection mode."""
    inbox = tmp_path / "inbox"
    path = _write(
        inbox,
        events=[
            {
                **_event("MemberKit evidence remains recordable"),
                "project": "team-memory-agent",
            }
        ],
    )

    bundle = load_bundle(path, inbox)
    events = bundle_events(bundle, "alex")
    conn = open_db(tmp_path / "ledger.db")

    assert insert_events(conn, events) == 1
    assert conn.execute(
        "SELECT source, summary, project FROM events"
    ).fetchone() == (
        "bundle:alex",
        "MemberKit evidence remains recordable",
        "team-memory-agent",
    )


def test_empty_event_list_is_valid(tmp_path):
    inbox = tmp_path / "inbox"
    bundle = load_bundle(_write(inbox, events=[]), inbox)
    assert bundle.events == ()


def test_accepts_neighbouring_utc_calendar_dates_for_review_day(tmp_path):
    inbox = tmp_path / "inbox"
    for timestamp in ("2026-07-27T20:30:00Z", "2026-07-27T23:30:00"):
        bundle = load_bundle(
            _write(
                inbox,
                date="2026-07-28",
                events=[{**_event(), "ts": timestamp}],
            ),
            inbox,
        )
        assert bundle.events[0]["ts"] == timestamp


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema": "wrong/v9"}, "schema"),
        ({"events": "wrong"}, "events"),
        ({"journal_md": None}, "journal_md"),
        ({"extra": True}, "top-level"),
    ],
)
def test_rejects_invalid_top_level_data(tmp_path, change, message):
    inbox = tmp_path / "inbox"
    with pytest.raises(BundleError, match=message):
        load_bundle(_write(inbox, **change), inbox)


def test_rejects_json_member_mismatch_and_invalid_json_date(tmp_path):
    inbox = tmp_path / "inbox"
    path = _write(inbox)
    data = json.loads(path.read_text())
    data["member"] = "other"
    path.write_text(json.dumps(data))
    with pytest.raises(BundleError, match="member"):
        load_bundle(path, inbox)

    data["member"] = "alex"
    data["date"] = "not-a-date"
    path.write_text(json.dumps(data))
    with pytest.raises(BundleError, match="date"):
        load_bundle(path, inbox)


def test_rejects_filename_date_mismatch(tmp_path):
    inbox = tmp_path / "inbox"
    path = _write(inbox)
    data = json.loads(path.read_text())
    data["date"] = "2026-07-26"
    path.write_text(json.dumps(data))
    with pytest.raises(BundleError, match="filename"):
        load_bundle(path, inbox)


@pytest.mark.parametrize(
    "event",
    [
        {**_event(), "summary": ""},
        {**_event(), "kind": "commit"},
        {**_event(), "project": 7},
        {**_event(), "refs": {"url": "https://example.test"}},
        {**_event(), "extra": True},
        {**_event(), "ts": "bad"},
    ],
)
def test_rejects_invalid_event(tmp_path, event):
    inbox = tmp_path / "inbox"
    with pytest.raises(BundleError, match="event"):
        load_bundle(_write(inbox, events=[event]), inbox)


def test_rejects_invalid_member_slug_and_symlink(tmp_path):
    inbox = tmp_path / "inbox"
    path = _write(inbox, member="Bad Member")
    with pytest.raises(BundleError, match="member"):
        load_bundle(path, inbox)

    target = _write(inbox, member="alex")
    link = inbox / "sam" / "bundle-sam-2026-07-27.json"
    link.parent.mkdir()
    link.symlink_to(target)
    with pytest.raises(BundleError, match="symlink"):
        load_bundle(link, inbox)
