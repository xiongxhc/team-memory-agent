import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teammem.config import Config
from teammem.connectors.base import CollectionResult
from teammem.connectors.config import ConnectorSettings
from teammem.daily import run_daily
from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.store import insert_events, open_db, stats


CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"
NOW = datetime(2026, 7, 17, 18, 20, tzinfo=timezone.utc)  # Friday
EVENT = Event(
    person="alex",
    project="project-alpha",
    ts="2026-07-17T10:00:00+00:00",
    source="feishu-channel",
    kind="message",
    summary="release is ready",
    refs=json.dumps({"chat_id": "oc_example_alpha"}),
    hash="m1",
)


class FixtureConnector:
    def __init__(self, name, result=None, error=None):
        self.name = name
        self.result = result or CollectionResult()
        self.error = error

    def validate(self, cfg, settings):
        return []

    def collect(self, cfg, ids, settings, now):
        if self.error:
            raise self.error
        return self.result


class RecordingConnector(FixtureConnector):
    def __init__(self, name, seen):
        super().__init__(name)
        self.seen = seen

    def collect(self, cfg, ids, settings, now):
        self.seen["connector_now"] = now
        return super().collect(cfg, ids, settings, now)


def _settings(*enabled):
    return {
        name: ConnectorSettings(name, name in enabled, {})
        for name in ("github", "gitlab", "slack", "feishu", "discord")
    }


def _cfg(tmp_path, **values):
    env = {
        "TEAMMEM_DB": str(tmp_path / "ledger.db"),
        "TEAMMEM_CONFIG_DIR": str(CONFIG_DIR),
        "TEAMMEM_VAULT": str(tmp_path / "vault"),
    }
    env.update(values)
    return Config.load(env=env)


def test_daily_continues_after_one_network_connector_fails(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: None)
    result = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings("github", "feishu"),
        NOW,
        connectors={
            "github": FixtureConnector("github", error=RuntimeError("timeout")),
            "feishu": FixtureConnector(
                "feishu", CollectionResult(events=(EVENT,))
            ),
        },
    )

    assert result.exit_code == 1
    assert result.status("github") == "failed"
    assert result.status("feishu") == "ok"
    assert result.status("render") == "ok"
    assert stats(open_db(cfg.db_path))["total"] == 1


def test_daily_exposes_connector_warnings_in_result(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: None)
    result = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings("discord"),
        NOW,
        connectors={
            "discord": FixtureConnector(
                "discord",
                CollectionResult(warnings=("MESSAGE_CONTENT may be disabled",)),
            )
        },
    )

    step = result.step("discord")
    assert step.status == "ok"
    assert step.warnings == ("MESSAGE_CONTENT may be disabled",)
    assert "MESSAGE_CONTENT may be disabled" in step.detail


def test_daily_redacts_secrets_from_connector_failures(tmp_path, monkeypatch):
    token = "ghp-never-print-this"
    cfg = _cfg(tmp_path, TEAMMEM_GITHUB_TOKEN=token)
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: None)
    result = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings("github"),
        NOW,
        connectors={
            "github": FixtureConnector(
                "github", error=RuntimeError(f"request rejected for {token}")
            )
        },
    )

    assert token not in result.step("github").detail
    assert "[REDACTED]" in result.step("github").detail


def test_daily_ledger_open_failure_skips_dependent_stages(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    def fail_open(_path):
        raise sqlite3.OperationalError("cannot open ledger")

    monkeypatch.setattr("teammem.daily.open_db", fail_open)
    result = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings("github"),
        NOW,
        connectors={"github": FixtureConnector("github")},
    )

    assert result.exit_code == 1
    assert result.status("ledger") == "failed"
    for name in ("github", "import", "reclaim", "journal", "report", "docs-sync", "render"):
        assert result.status(name) == "skipped"


def test_daily_runs_weekly_report_only_on_friday(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    calls = []
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: object())
    monkeypatch.setattr("teammem.daily.run_journal", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        "teammem.daily.run_report",
        lambda *args, **kwargs: calls.append(kwargs["base"]) or 0,
    )

    friday = run_daily(
        cfg, IdentityMaps.load(CONFIG_DIR), _settings(), NOW, connectors={}
    )
    monday = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings(),
        datetime(2026, 7, 20, 18, 20, tzinfo=timezone.utc),
        connectors={},
    )

    assert friday.status("report") == "ok"
    assert monday.status("report") == "skipped"
    assert calls == [NOW.date()]


def test_daily_uses_operator_friday_but_collects_with_utc_clock(
    tmp_path, monkeypatch
):
    local_now = datetime(
        2026, 7, 17, 18, 20, tzinfo=timezone(timedelta(hours=-7))
    )
    snapshots = tmp_path / "snapshots"
    cfg = _cfg(tmp_path, TEAMMEM_SNAPSHOTS=str(snapshots))
    seen = {}
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: object())
    monkeypatch.setattr(
        "teammem.daily.run_journal",
        lambda *args, **kwargs: seen.setdefault("journal_day", kwargs["today"]) and 0,
    )
    monkeypatch.setattr(
        "teammem.daily.run_report",
        lambda *args, **kwargs: seen.setdefault("report_day", kwargs["base"]) and 0,
    )

    result = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings("github"),
        local_now,
        connectors={"github": RecordingConnector("github", seen)},
    )

    assert result.status("report") == "ok"
    assert seen["connector_now"].tzinfo == timezone.utc
    assert seen["connector_now"] == datetime(2026, 7, 18, 1, 20, tzinfo=timezone.utc)
    assert seen["journal_day"].isoformat() == "2026-07-17"
    assert seen["report_day"].isoformat() == "2026-07-17"
    assert "generated: 2026-07-17" in (cfg.vault_dir / "README.md").read_text()
    assert (snapshots / "ledger-2026-07-17.db").exists()


def test_daily_marks_unconfigured_optional_stages_skipped(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: None)
    result = run_daily(
        cfg, IdentityMaps.load(CONFIG_DIR), _settings(), NOW, connectors={}
    )

    for name in ("import", "journal", "report", "docs-sync", "snapshot"):
        assert result.status(name) == "skipped"
    assert result.status("render") == "ok"


def test_daily_imports_only_from_configured_disposable_staging_directory(
    tmp_path, monkeypatch
):
    staging = tmp_path / "exported-staging"
    bundle = staging / "alex" / "bundle-alex-2026-07-17.json"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(json.dumps({
        "schema": "teammem-bundle/v1",
        "member": "alex",
        "date": "2026-07-17",
        "events": [],
        "journal_md": "## 2026-07-17",
    }))
    cfg = _cfg(
        tmp_path,
        TEAMMEM_INBOX=str(staging),
        TEAMMEM_ARCHIVE=str(tmp_path / "archive"),
        TEAMMEM_QUARANTINE=str(tmp_path / "quarantine"),
    )
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: None)

    result = run_daily(
        cfg, IdentityMaps.load(CONFIG_DIR), _settings(), NOW, connectors={}
    )

    assert result.status("import") == "ok"
    assert "accepted=1" in result.step("import").detail
    assert not bundle.exists()


def test_daily_persists_channel_metadata_atomically_after_collection(
    tmp_path, monkeypatch
):
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    for name in ("roster.example.yaml", "projects.example.yaml"):
        (cfgdir / name).write_text((CONFIG_DIR / name).read_text())
    (cfgdir / "channel_names.json").write_text('{"old": "Existing"}')
    cfg = _cfg(tmp_path, TEAMMEM_CONFIG_DIR=str(cfgdir))
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: None)
    result = run_daily(
        cfg,
        IdentityMaps.load(cfgdir),
        _settings("feishu"),
        NOW,
        connectors={
            "feishu": FixtureConnector(
                "feishu",
                CollectionResult(channel_names={"oc_example_alpha": "Alpha"}),
            )
        },
    )

    assert result.status("feishu") == "ok"
    assert json.loads((cfgdir / "channel_names.json").read_text()) == {
        "old": "Existing",
        "oc_example_alpha": "Alpha",
    }
    assert not list(cfgdir.glob(".channel_names.json.*"))


def test_daily_journal_failure_skips_friday_report_but_keeps_local_projections(
    tmp_path, monkeypatch
):
    cfg = _cfg(
        tmp_path,
        TEAMMEM_OBSIDIAN_PROJECTS=str(tmp_path / "obsidian-projects"),
    )
    conn = open_db(cfg.db_path)
    insert_events(conn, [EVENT])
    conn.execute(
        "INSERT INTO summaries (kind, key, input_hash, text, model, created_ts)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            "daily-person",
            "alex|2026-07-17",
            "cached",
            "Cached journal remains available.",
            "fake",
            "2026-07-17T00:00:00",
        ),
    )
    conn.commit()
    calls = []
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: object())
    monkeypatch.setattr(
        "teammem.daily.run_journal",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("llm offline")),
    )
    monkeypatch.setattr(
        "teammem.daily.run_report",
        lambda *args, **kwargs: calls.append("report") or 0,
    )
    monkeypatch.setattr(
        "teammem.daily.run_docs_sync",
        lambda *args, **kwargs: calls.append("docs-sync") or 0,
    )

    result = run_daily(
        cfg, IdentityMaps.load(CONFIG_DIR), _settings(), NOW, connectors={}
    )

    assert result.exit_code == 1
    assert result.status("journal") == "failed"
    assert result.status("report") == "skipped"
    assert result.step("report").detail == "journal failed"
    assert calls == ["docs-sync"]
    assert result.status("docs-sync") == "ok"
    assert result.status("render") == "ok"
    assert "Cached journal remains available." in (
        cfg.vault_dir / "Person" / "Alex Rivera.md"
    ).read_text()


def test_daily_snapshot_uses_sqlite_backup_and_retains_fourteen(tmp_path, monkeypatch):
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    for day in range(1, 16):
        (snapshots / f"ledger-2026-06-{day:02d}.db").write_text("old")
    cfg = _cfg(tmp_path, TEAMMEM_SNAPSHOTS=str(snapshots))
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: None)

    result = run_daily(
        cfg, IdentityMaps.load(CONFIG_DIR), _settings(), NOW, connectors={}
    )

    assert result.status("snapshot") == "ok"
    assert (snapshots / "ledger-2026-07-17.db").exists()
    assert len(list(snapshots.glob("ledger-*.db"))) == 14
    assert open_db(snapshots / "ledger-2026-07-17.db").execute(
        "SELECT COUNT(*) FROM events"
    ).fetchone()[0] == 0
