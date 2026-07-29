import os
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

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
         int(datetime.fromisoformat(iso).astimezone().timestamp() * 1000)),
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


def test_draft_all_requests_legacy_mode(tmp_path, monkeypatch):
    cfg = _setup_cfg(tmp_path)
    cfg.db.touch()
    calls = []
    monkeypatch.setattr(cli.config, "load", lambda: cfg)
    monkeypatch.setattr(
        cli.bundle,
        "draft",
        lambda db, member, date, *, all_observations=False, timezone=None: (
            calls.append((all_observations, timezone))
            or {
                "schema": cli.bundle.SCHEMA,
                "member": member,
                "date": date,
                "events": [],
                "journal_md": f"## {date}",
            }
        ),
    )

    assert cli.main(["draft", "--date", "2026-07-27", "--all"]) == 0
    assert calls == [(True, cli.bundle._local_timezone())]


def test_direct_draft_uses_configured_member_timezone_not_host_timezone(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    cfg = _setup_cfg(tmp_path, timezone=ZoneInfo("America/Los_Angeles"))
    con = sqlite3.connect(cfg.db)
    con.execute(
        "CREATE TABLE observations (project TEXT, title TEXT, subtitle TEXT,"
        " narrative TEXT, type TEXT, created_at TEXT, created_at_epoch INTEGER)"
    )
    timestamp = "2026-07-28T06:00:00Z"
    con.execute(
        "INSERT INTO observations VALUES (?,?,?,?,?,?,?)",
        (
            "project-alpha", "Shipped timezone boundary", None, None,
            "feature", timestamp,
            int(datetime.fromisoformat(timestamp).timestamp() * 1000),
        ),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(cli.config, "load", lambda: cfg)

    assert cli.main(["draft", "--date", "2026-07-27"]) == 0

    out = cfg.workdir / "out" / "bundle-alex-2026-07-27.json"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["events"][0]["ts"] == "2026-07-27T23:00:00.000-07:00"


def test_draft_preserves_existing_bytes_unless_force_is_explicit(
    tmp_path, monkeypatch,
):
    cfg = _setup_cfg(tmp_path)
    cfg.db.touch()
    monkeypatch.setattr(cli.config, "load", lambda: cfg)
    out = cfg.workdir / "out" / "bundle-alex-2026-07-27.json"
    out.parent.mkdir(parents=True)
    original = b'{"events": [member edit in progress'
    out.write_bytes(original)

    try:
        cli.main(["draft", "--date", "2026-07-27"])
    except SystemExit as exc:
        assert "use --force" in str(exc)
    else:
        raise AssertionError("draft should refuse to overwrite an existing file")
    assert out.read_bytes() == original

    replacement = {
        "schema": cli.bundle.SCHEMA,
        "member": cfg.member,
        "date": "2026-07-27",
        "events": [],
        "journal_md": "## 2026-07-27",
    }
    monkeypatch.setattr(
        cli.bundle,
        "draft",
        lambda *args, **kwargs: replacement,
    )

    assert cli.main(["draft", "--date", "2026-07-27", "--force"]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == replacement


def _setup_cfg(tmp_path, *, timezone=None):
    return Config(
        member="alex",
        db=tmp_path / "claude-mem.db",
        inbox_url="git@example.test:team/inbox.git",
        workdir=tmp_path / "work",
        timezone=timezone,
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


def test_setup_invalid_timezone_preserves_existing_config(tmp_path, monkeypatch):
    path = tmp_path / "memberkit.env"
    original = (
        b"MEMBERKIT_MEMBER=alex\n"
        b"MEMBERKIT_INBOX_URL=git@example.test:team/inbox.git\n"
        b"MEMBERKIT_TIMEZONE=Asia/Dubai\n"
    )
    path.write_bytes(original)
    monkeypatch.setattr(cli.config, "CONFIG_FILE", path)

    with pytest.raises(SystemExit, match="invalid MEMBERKIT_TIMEZONE"):
        cli.main([
            "setup",
            "--member", "alex",
            "--inbox-url", "git@example.test:team/inbox.git",
            "--timezone", "Mars/Olympus_Mons",
            "--no-schedule",
        ])

    assert path.read_bytes() == original


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
