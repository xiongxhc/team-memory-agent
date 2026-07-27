import sqlite3
from datetime import datetime

from memberkit import cli
from memberkit.config import Config
from memberkit.state import DraftState


def test_draft_command_records_pending_review_state(tmp_path, monkeypatch):
    db = tmp_path / "claude-mem.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE observations (project TEXT, title TEXT, subtitle TEXT,"
        " narrative TEXT, type TEXT, created_at TEXT, created_at_epoch INTEGER)"
    )
    iso = "2026-07-27T10:00:00"
    con.execute(
        "INSERT INTO observations VALUES (?,?,?,?,?,?,?)",
        ("project-alpha", "Shipped", None, None, "feature", iso,
         int(datetime.fromisoformat(iso).astimezone().timestamp())),
    )
    con.commit()
    con.close()
    cfg = Config(
        member="alex",
        db=db,
        inbox_url="git@example.test:team/inbox.git",
        workdir=tmp_path / "work",
    )
    monkeypatch.setattr(cli.config, "load", lambda: cfg)

    assert cli.main(["draft", "--date", "2026-07-27"]) == 0

    saved = DraftState(cfg.workdir / "state.json").snapshot()
    assert saved["pending"]["2026-07-27"]
