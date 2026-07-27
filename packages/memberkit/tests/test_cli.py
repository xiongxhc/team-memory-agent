import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

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


def _setup_cfg(tmp_path):
    return Config(
        member="alex",
        db=tmp_path / "claude-mem.db",
        inbox_url="git@example.test:team/inbox.git",
        workdir=tmp_path / "work",
    )


def test_setup_prompts_to_accept_default_schedule(tmp_path, monkeypatch):
    cfg = _setup_cfg(tmp_path)
    prompts = []
    installed = []
    monkeypatch.setattr(cli.config, "CONFIG_FILE", tmp_path / "memberkit.env")
    monkeypatch.setattr(cli.config, "load", lambda env: cfg)
    monkeypatch.setattr(
        cli,
        "install_schedule",
        lambda config, time: installed.append(time) or tmp_path / "agent.plist",
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: prompts.append(prompt) or "",
    )

    assert cli.main([
        "setup",
        "--member", cfg.member,
        "--inbox-url", cfg.inbox_url,
        "--db", str(cfg.db),
        "--workdir", str(cfg.workdir),
    ]) == 0

    assert installed == ["17:30"]
    assert any("17:30" in prompt for prompt in prompts)


def test_setup_can_decline_schedule_interactively(tmp_path, monkeypatch):
    cfg = _setup_cfg(tmp_path)
    installed = []
    monkeypatch.setattr(cli.config, "CONFIG_FILE", tmp_path / "memberkit.env")
    monkeypatch.setattr(cli.config, "load", lambda env: cfg)
    monkeypatch.setattr(
        cli,
        "install_schedule",
        lambda config, time: installed.append(time) or tmp_path / "agent.plist",
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "no")

    assert cli.main([
        "setup",
        "--member", cfg.member,
        "--inbox-url", cfg.inbox_url,
        "--db", str(cfg.db),
        "--workdir", str(cfg.workdir),
    ]) == 0

    assert installed == []


def test_dismiss_excludes_pending_date_without_transmitting(tmp_path, monkeypatch):
    cfg = _setup_cfg(tmp_path)
    event = {
        "ts": "2026-07-27T10:00:00",
        "kind": "journal-highlight",
        "summary": "Do not share",
        "project": "project-alpha",
        "refs": None,
    }
    state = DraftState(cfg.workdir / "state.json")
    state.refresh("2026-07-27", [event], current=None)
    monkeypatch.setattr(cli.config, "load", lambda: cfg)

    assert cli.main(["dismiss", "--date", "2026-07-27"]) == 0

    saved = DraftState(cfg.workdir / "state.json").snapshot()
    assert "2026-07-27" not in saved["pending"]
    assert saved["excluded"]
    assert not (cfg.workdir / "inbox").exists()


def test_importing_cli_does_not_import_push_module():
    package_root = Path(__file__).parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(package_root)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import memberkit.cli; "
            "print('memberkit.push' in sys.modules)",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.stdout.strip() == "False"
