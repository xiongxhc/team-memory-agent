import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from memberkit import cli as memberkit_cli
from memberkit.bundle import draft
from memberkit.config import Config
from memberkit.state import DraftState, event_fingerprint
from teammem.bundles import load_bundle
from teammem.identity import IdentityMaps
from teammem.importer import import_inbox
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


def test_reviewed_evidence_first_bundle_imports_idempotently(
    tmp_path, monkeypatch,
):
    date_text = "2026-07-27"
    source_db = tmp_path / "observations.db"
    source = sqlite3.connect(source_db)
    source.execute(
        "CREATE TABLE observations (project TEXT, title TEXT, subtitle TEXT,"
        " narrative TEXT, type TEXT, created_at TEXT, created_at_epoch INTEGER)"
    )
    rows = []
    for index in range(9):
        timestamp = f"{date_text}T10:{index:02d}:00+00:00"
        rows.append(
            (
                "project-alpha",
                f"Observation {index}",
                None,
                None,
                "feature",
                timestamp,
                int(datetime.fromisoformat(timestamp).timestamp() * 1000),
            )
        )
    source.executemany(
        "INSERT INTO observations VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    source.commit()
    source.close()

    cfg = Config(
        member="alex",
        db=source_db,
        inbox_url="unused",
        workdir=tmp_path / "memberkit",
        timezone=ZoneInfo("UTC"),
    )
    monkeypatch.setattr(memberkit_cli.config, "load", lambda: cfg)
    assert memberkit_cli.main(["draft", "--date", date_text]) == 0

    local = (
        cfg.workdir / "out" / f"bundle-{cfg.member}-{date_text}.json"
    )
    payload = json.loads(local.read_text(encoding="utf-8"))
    assert len(payload["events"]) == 9
    removed = payload["events"].pop(4)
    payload["journal_md"] = "stale preview containing removed content"
    local.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    assert memberkit_cli.main(["review", "--date", date_text]) == 0
    reviewed = json.loads(local.read_text(encoding="utf-8"))
    assert len(reviewed["events"]) == 8
    assert removed not in reviewed["events"]
    state = DraftState(cfg.workdir / "state.json").snapshot()
    assert event_fingerprint(removed, date_text) in state["excluded"]

    inbox = tmp_path / "inbox"
    incoming = inbox / "alex" / f"bundle-alex-{date_text}.json"
    incoming.parent.mkdir(parents=True)
    encoded = local.read_bytes()
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
    assert first.inserted == 8
    assert second.inserted == 0
    summaries = {
        row[0]
        for row in conn.execute(
            "SELECT summary FROM events WHERE source = 'bundle:alex'"
        )
    }
    assert summaries == {
        f"Observation {index}" for index in range(9) if index != 4
    }
