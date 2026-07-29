import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from memberkit.bundle import draft
from teammem.identity import IdentityMaps
from teammem.importer import import_inbox
from teammem.render import render_vault
from teammem.store import open_db


CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"


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
