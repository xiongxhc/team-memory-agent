import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from memberkit.bundle import draft
from teammem.bundles import load_bundle
from teammem.identity import IdentityMaps
from teammem.importer import import_inbox
from teammem.render import render_vault
from teammem.store import open_db


CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"


@pytest.mark.parametrize("all_observations", [False, True])
def test_memberkit_utc_timestamp_bundle_passes_validator(
    tmp_path, monkeypatch, all_observations,
):
    monkeypatch.setenv("TZ", "America/New_York")
    source_db = tmp_path / "observations.db"
    source = sqlite3.connect(source_db)
    source.execute(
        "CREATE TABLE observations (project TEXT, title TEXT, subtitle TEXT,"
        " narrative TEXT, type TEXT, created_at TEXT, created_at_epoch INTEGER)"
    )
    timestamp = "2026-03-09T03:30:00Z"
    source.execute(
        "INSERT INTO observations VALUES (?,?,?,?,?,?,?)",
        (
            "project-alpha",
            "Shipped local-date normalization",
            None,
            None,
            "change",
            timestamp,
            int(datetime.fromisoformat(timestamp).timestamp() * 1000),
        ),
    )
    source.commit()
    source.close()

    payload = draft(
        source_db,
        "alex",
        "2026-03-08",
        all_observations=all_observations,
    )
    inbox = tmp_path / "inbox"
    incoming = inbox / "alex" / "bundle-alex-2026-03-08.json"
    incoming.parent.mkdir(parents=True)
    incoming.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_bundle(incoming, inbox)

    assert loaded.events[0]["ts"] == "2026-03-08T23:30:00.000-04:00"


def test_memberkit_bundle_imports_idempotently_and_renders(tmp_path):
    source_db = tmp_path / "observations.db"
    source = sqlite3.connect(source_db)
    source.execute(
        "CREATE TABLE observations (project TEXT, title TEXT, subtitle TEXT,"
        " narrative TEXT, type TEXT, created_at TEXT, created_at_epoch INTEGER)"
    )
    timestamp = "2026-07-27T10:00:00"
    source.execute(
        "INSERT INTO observations VALUES (?,?,?,?,?,?,?)",
        ("project-alpha", "Shipped retry fix", None, None, "feature", timestamp,
         int(datetime.fromisoformat(timestamp).astimezone().timestamp() * 1000)),
    )
    source.commit()
    source.close()

    payload = draft(source_db, "alex", "2026-07-27")
    inbox = tmp_path / "inbox"
    incoming = inbox / "alex" / "bundle-alex-2026-07-27.json"
    incoming.parent.mkdir(parents=True)
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    incoming.write_bytes(encoded)

    conn = open_db(tmp_path / "ledger.db")
    ids = IdentityMaps.load(CONFIG_DIR)
    first = import_inbox(
        conn, ids, inbox, tmp_path / "archive", tmp_path / "quarantine"
    )
    incoming.parent.mkdir(parents=True, exist_ok=True)
    incoming.write_bytes(encoded)
    second = import_inbox(
        conn, ids, inbox, tmp_path / "archive", tmp_path / "quarantine"
    )
    render_vault(conn, ids, tmp_path / "vault", date(2026, 7, 27))

    assert first.inserted == 1
    assert second.inserted == 0
    person_page = tmp_path / "vault" / "Person" / "Alex Rivera.md"
    assert "Shipped retry fix" in person_page.read_text(encoding="utf-8")
