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
from memberkit.state import DraftState, event_fingerprint


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


def test_draft_and_all_write_the_same_evidence_bundle(tmp_path, monkeypatch):
    cfg = _setup_cfg(tmp_path)
    con = sqlite3.connect(cfg.db)
    con.execute(
        "CREATE TABLE observations (project TEXT, title TEXT, subtitle TEXT,"
        " narrative TEXT, type TEXT, created_at TEXT, created_at_epoch INTEGER)"
    )
    con.executemany(
        "INSERT INTO observations VALUES (?,?,?,?,?,?,?)",
        [
            (
                "project-alpha", f"Observation {index}", None, None, "change",
                f"2026-07-27T{index + 8:02d}:00:00",
                int(datetime.fromisoformat(
                    f"2026-07-27T{index + 8:02d}:00:00"
                ).astimezone().timestamp() * 1000),
            )
            for index in range(8)
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(cli.config, "load", lambda: cfg)

    assert cli.main(["draft", "--date", "2026-07-27"]) == 0
    out = cfg.workdir / "out" / "bundle-alex-2026-07-27.json"
    default = json.loads(out.read_text(encoding="utf-8"))
    assert cli.main(["draft", "--date", "2026-07-27", "--all", "--force"]) == 0
    compat = json.loads(out.read_text(encoding="utf-8"))

    assert len(default["events"]) == 8
    assert default["events"] == compat["events"]


def test_draft_preserves_exact_duplicate_observations(tmp_path, monkeypatch):
    cfg = _setup_cfg(tmp_path)
    con = sqlite3.connect(cfg.db)
    con.execute(
        "CREATE TABLE observations (project TEXT, title TEXT, subtitle TEXT,"
        " narrative TEXT, type TEXT, created_at TEXT, created_at_epoch INTEGER)"
    )
    iso = "2026-07-27T10:00:00"
    row = (
        "project-alpha", "Same observation", None, None, "change", iso,
        int(datetime.fromisoformat(iso).astimezone().timestamp() * 1000),
    )
    con.executemany("INSERT INTO observations VALUES (?,?,?,?,?,?,?)", [row, row])
    con.commit()
    con.close()
    monkeypatch.setattr(cli.config, "load", lambda: cfg)

    assert cli.main(["draft", "--date", "2026-07-27"]) == 0
    out = cfg.workdir / "out" / "bundle-alex-2026-07-27.json"
    first = json.loads(out.read_text(encoding="utf-8"))
    assert cli.main(["draft", "--date", "2026-07-27", "--force"]) == 0
    repeated = json.loads(out.read_text(encoding="utf-8"))

    assert len(first["events"]) == 2
    assert first["events"] == repeated["events"]


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


def test_force_draft_validates_generated_bundle_before_overwriting(
    tmp_path, monkeypatch,
):
    cfg = _setup_cfg(tmp_path)
    cfg.db.touch()
    out = cfg.workdir / "out" / "bundle-alex-2026-07-27.json"
    out.parent.mkdir(parents=True)
    original = b'{"events": [member edit in progress'
    out.write_bytes(original)
    invalid = {
        "schema": cli.bundle.SCHEMA,
        "member": cfg.member,
        "date": "2026-07-27",
        "events": [{
            "ts": "2026-07-27T10:00:00",
            "kind": "journal-highlight",
            "summary": "generated",
            "project": "project-alpha",
            "refs": ["private"],
        }],
        "journal_md": "stale",
    }
    monkeypatch.setattr(cli.config, "load", lambda: cfg)
    monkeypatch.setattr(cli.bundle, "draft", lambda *args, **kwargs: invalid)

    with pytest.raises(ValueError, match="refs must be null"):
        cli.main(["draft", "--date", "2026-07-27", "--force"])

    assert out.read_bytes() == original
    assert sorted(item.name for item in out.parent.iterdir()) == [out.name]
    assert DraftState(cfg.workdir / "state.json").snapshot() == {
        "approved": [],
        "excluded": [],
        "pending": {},
    }


def test_force_draft_replace_failure_preserves_existing_bytes_and_cleans_temp(
    tmp_path, monkeypatch,
):
    cfg = _setup_cfg(tmp_path)
    cfg.db.touch()
    out = cfg.workdir / "out" / "bundle-alex-2026-07-27.json"
    out.parent.mkdir(parents=True)
    original = b'{"events": [member edit in progress'
    out.write_bytes(original)
    replacement = {
        "schema": cli.bundle.SCHEMA,
        "member": cfg.member,
        "date": "2026-07-27",
        "events": [],
        "journal_md": "## 2026-07-27",
    }
    monkeypatch.setattr(cli.config, "load", lambda: cfg)
    monkeypatch.setattr(
        cli.bundle,
        "draft",
        lambda *args, **kwargs: replacement,
    )
    monkeypatch.setattr(
        cli.bundle.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        cli.main(["draft", "--date", "2026-07-27", "--force"])

    assert out.read_bytes() == original
    assert sorted(item.name for item in out.parent.iterdir()) == [out.name]


def _setup_cfg(tmp_path, *, timezone=None):
    return Config(
        member="alex",
        db=tmp_path / "claude-mem.db",
        inbox_url="git@example.test:team/inbox.git",
        workdir=tmp_path / "work",
        timezone=timezone,
    )


def _review_event(summary):
    return {
        "ts": "2026-07-27T10:00:00",
        "kind": "journal-highlight",
        "summary": summary,
        "project": "project-alpha",
        "refs": None,
    }


def _write_review_bundle(cfg, events, journal="stale"):
    path = cfg.workdir / "out" / "bundle-alex-2026-07-27.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema": "teammem-bundle/v1",
        "member": "alex",
        "date": "2026-07-27",
        "events": events,
        "journal_md": journal,
    }), encoding="utf-8")
    return path


def test_review_persists_authoritative_journal_and_removed_exclusion(
    tmp_path, monkeypatch, capsys,
):
    cfg = _setup_cfg(tmp_path)
    kept, removed = _review_event("kept"), _review_event("removed private event")
    state_path = cfg.workdir / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({
        "approved": [event_fingerprint(kept, "2026-07-27")],
        "excluded": [],
        "pending": {
            "2026-07-27": [event_fingerprint(removed, "2026-07-27")]
        },
    }), encoding="utf-8")
    path = _write_review_bundle(cfg, [kept], "private stale journal")
    monkeypatch.setattr(cli.config, "load", lambda: cfg)

    assert cli.main(["review", "--date", "2026-07-27"]) == 0

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["journal_md"] == (
        "## 2026-07-27\n\n### project-alpha\n- kept"
    )
    state = DraftState(state_path).snapshot()
    assert event_fingerprint(removed, "2026-07-27") in state["excluded"]
    output = capsys.readouterr().out
    assert "kept" in output
    assert "removed private event" not in output


def test_review_invalid_json_preserves_bundle_and_state(tmp_path, monkeypatch):
    cfg = _setup_cfg(tmp_path)
    path = cfg.workdir / "out" / "bundle-alex-2026-07-27.json"
    path.parent.mkdir(parents=True)
    original = b'{"events": [member edit in progress'
    path.write_bytes(original)
    state_path = cfg.workdir / "state.json"
    DraftState(state_path).refresh(
        "2026-07-27", [_review_event("pending")], current=None
    )
    before = DraftState(state_path).snapshot()
    monkeypatch.setattr(cli.config, "load", lambda: cfg)

    with pytest.raises(ValueError):
        cli.main(["review", "--date", "2026-07-27"])

    assert path.read_bytes() == original
    assert DraftState(state_path).snapshot() == before


def test_review_replace_failure_preserves_bundle_and_state(
    tmp_path, monkeypatch,
):
    cfg = _setup_cfg(tmp_path)
    kept, removed = _review_event("kept"), _review_event("removed")
    state_path = cfg.workdir / "state.json"
    DraftState(state_path).refresh(
        "2026-07-27", [kept, removed], current=None
    )
    path = _write_review_bundle(cfg, [kept], "private stale journal")
    original = path.read_bytes()
    before = DraftState(state_path).snapshot()
    monkeypatch.setattr(cli.config, "load", lambda: cfg)
    monkeypatch.setattr(
        cli.bundle.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        cli.main(["review", "--date", "2026-07-27"])

    assert path.read_bytes() == original
    assert DraftState(state_path).snapshot() == before
    assert sorted(item.name for item in path.parent.iterdir()) == [path.name]


def test_setup_uses_platform_config_api_then_installs_default_schedule(
    tmp_path,
    monkeypatch,
    capsys,
):
    cfg = _setup_cfg(tmp_path)
    prompts = []
    calls = []
    config_path = tmp_path / "platform" / "memberkit.env"

    def write_config(values):
        calls.append(("write", values))
        return config_path

    def load(*, config_file):
        calls.append(("load", config_file))
        return cfg

    def install(config, time):
        calls.append(("install", config, time))
        return tmp_path / "scheduler-artifact"

    monkeypatch.setattr(cli.config, "write_config", write_config)
    monkeypatch.setattr(cli.config, "load", load)
    monkeypatch.setattr(cli, "install_schedule", install)
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

    assert calls == [
        (
            "write",
            {
                "MEMBERKIT_MEMBER": "alex",
                "MEMBERKIT_INBOX_URL": "git@example.test:team/inbox.git",
                "MEMBERKIT_DB": str(cfg.db),
                "MEMBERKIT_WORKDIR": str(cfg.workdir),
            },
        ),
        ("load", config_path),
        ("install", cfg, "17:30"),
    ]
    assert any("17:30" in prompt for prompt in prompts)
    assert str(config_path) in capsys.readouterr().out


@pytest.mark.parametrize(
    "setup_args",
    [
        [],
        ["--no-schedule"],
    ],
)
def test_setup_can_skip_schedule_after_saving_config(
    tmp_path,
    monkeypatch,
    capsys,
    setup_args,
):
    cfg = _setup_cfg(tmp_path)
    installed = []
    path = tmp_path / "platform" / "memberkit.env"
    monkeypatch.setattr(cli.config, "write_config", lambda _values: path)
    monkeypatch.setattr(
        cli.config,
        "load",
        lambda *, config_file: cfg if config_file == path else None,
    )
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
        *setup_args,
    ]) == 0

    assert installed == []
    assert str(path) in capsys.readouterr().out


def test_setup_invalid_timezone_never_calls_config_writer(tmp_path, monkeypatch):
    path = tmp_path / "memberkit.env"
    original = b"MEMBERKIT_TIMEZONE=Asia/Dubai\n"
    path.write_bytes(original)
    monkeypatch.setattr(
        cli.config,
        "write_config",
        lambda _values: (_ for _ in ()).throw(
            AssertionError("invalid timezone must fail before configuration write")
        ),
    )

    with pytest.raises(SystemExit, match="invalid MEMBERKIT_TIMEZONE"):
        cli.main([
            "setup",
            "--member", "alex",
            "--inbox-url", "git@example.test:team/inbox.git",
            "--timezone", "Mars/Olympus_Mons",
            "--no-schedule",
        ])

    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "argv",
    [
        ["setup", "--help"],
        ["schedule", "install", "--help"],
    ],
)
def test_setup_and_schedule_help_name_host_local_timezone(argv, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(argv)

    assert error.value.code == 0
    assert "host's local timezone" in capsys.readouterr().out


def test_setup_schedule_failure_preserves_config_and_names_retry_command(
    tmp_path,
    monkeypatch,
):
    cfg = _setup_cfg(tmp_path)
    config_path = tmp_path / "platform" / "memberkit.env"
    scheduler_artifact = tmp_path / "scheduler-artifact"

    def write_config(_values):
        config_path.parent.mkdir(parents=True)
        config_path.write_text("saved\n", encoding="utf-8")
        return config_path

    monkeypatch.setattr(cli.config, "write_config", write_config)
    monkeypatch.setattr(
        cli.config,
        "load",
        lambda *, config_file: cfg if config_file == config_path else None,
    )
    monkeypatch.setattr(
        cli,
        "install_schedule",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("unsupported scheduling platform: linux secret-token")
        ),
    )

    with pytest.raises(SystemExit) as error:
        cli.main([
            "setup",
            "--member", cfg.member,
            "--inbox-url", cfg.inbox_url,
            "--time", "17:30",
        ])

    message = str(error.value)
    assert str(config_path) in message
    assert "memberkit schedule install" in message
    assert "secret-token" not in message
    assert config_path.read_text(encoding="utf-8") == "saved\n"
    assert not scheduler_artifact.exists()


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
