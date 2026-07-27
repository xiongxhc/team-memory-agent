import json

from teammem.identity import IdentityMaps
from teammem.importer import import_inbox
from teammem.store import open_db


def _bundle(member="alex", date="2026-07-27", summaries=("one", "two")):
    return {
        "schema": "teammem-bundle/v1",
        "member": member,
        "date": date,
        "events": [
            {
                "ts": f"{date}T10:0{i}:00",
                "kind": "journal-highlight",
                "summary": summary,
                "project": "project-alpha",
                "refs": None,
            }
            for i, summary in enumerate(summaries)
        ],
        "journal_md": f"## {date}",
    }


def _write(inbox, data, filename=None):
    member, date = data["member"], data["date"]
    path = inbox / member / (filename or f"bundle-{member}-{date}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _run(tmp_path, inbox, dry_run=False):
    conn = open_db(tmp_path / "ledger.db")
    ids = IdentityMaps(
        {"members": {"alex": {"name": "Alex Rivera"}}},
        {"projects": {"project-alpha": {}}},
    )
    result = import_inbox(
        conn,
        ids,
        inbox,
        tmp_path / "archive",
        tmp_path / "quarantine",
        dry_run=dry_run,
    )
    return conn, result


def test_imports_bundle_transactionally_and_archives_by_content(tmp_path):
    inbox = tmp_path / "inbox"
    _write(inbox, _bundle())

    conn, result = _run(tmp_path, inbox)

    assert (result.accepted, result.events, result.inserted) == (1, 2, 2)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    archived = list((tmp_path / "archive" / "alex").glob("bundle-*.json/*.json"))
    assert len(archived) == 1
    assert not list(inbox.rglob("*.json"))


def test_identical_retry_inserts_zero_and_changed_revision_adds_one(tmp_path):
    inbox = tmp_path / "inbox"
    original = _bundle(summaries=("one",))
    _write(inbox, original)
    conn, first = _run(tmp_path, inbox)
    assert first.inserted == 1

    _write(inbox, original)
    _, duplicate = _run(tmp_path, inbox)
    assert duplicate.inserted == 0

    _write(inbox, _bundle(summaries=("one", "late")))
    _, changed = _run(tmp_path, inbox)
    assert changed.inserted == 1
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    assert len(list((tmp_path / "archive" / "alex").glob("bundle-*.json/*.json"))) == 2


def test_invalid_and_unknown_member_quarantine_without_stopping_valid(tmp_path):
    inbox = tmp_path / "inbox"
    _write(inbox, _bundle(member="sam"))
    broken = _write(inbox, _bundle(), filename="bundle-alex-wrong.json")
    broken.write_text("{")
    _write(inbox, _bundle(summaries=("valid",)))

    conn, result = _run(tmp_path, inbox)

    assert (result.accepted, result.quarantined, result.inserted) == (1, 2, 1)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    reasons = list((tmp_path / "quarantine").rglob("*.reason.json"))
    assert len(reasons) == 2


def test_empty_bundle_archives_and_dry_run_writes_nothing(tmp_path):
    inbox = tmp_path / "inbox"
    _write(inbox, _bundle(summaries=()))

    conn, dry = _run(tmp_path, inbox, dry_run=True)

    assert (dry.accepted, dry.events, dry.inserted) == (1, 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert list(inbox.rglob("*.json"))
    assert not (tmp_path / "archive").exists()
    assert not (tmp_path / "quarantine").exists()
