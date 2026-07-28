import json
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

from teammem.config import Config
from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.services import (
    collect_connector,
    run_docs_sync,
    run_journal,
    run_render,
    run_report,
)
from teammem.store import insert_events, open_db


CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"
NOW = datetime(2026, 7, 16, tzinfo=timezone.utc)


def _cfg(tmp_path, **overrides):
    values = {
        "TEAMMEM_DB": str(tmp_path / "ledger.db"),
        "TEAMMEM_CONFIG_DIR": str(CONFIG_DIR),
        "TEAMMEM_VAULT": str(tmp_path / "vault"),
    }
    values.update(overrides)
    return Config.load(env=values)


def _seed(cfg):
    insert_events(open_db(cfg.db_path), [Event(
        person="alex",
        project="project-alpha",
        ts="2026-07-14T09:00:00+00:00",
        source="gitlab",
        kind="commit",
        summary="fix: JWT race",
        hash="h1",
    )])


def test_render_service_preserves_existing_dry_run_output(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seed(cfg)

    assert run_render(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        today=date(2026, 7, 16),
        weeks=4,
        dry_run=True,
    ) == 0

    assert capsys.readouterr().out == (
        f"DRY render -> {cfg.vault_dir} (Week 2026-07-13-17, weeks=4)\n"
    )


def test_journal_service_preserves_existing_dry_run_output(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seed(cfg)

    assert run_journal(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        today=date(2026, 7, 16),
        since_days=7,
        dry_run=True,
    ) == 0

    assert capsys.readouterr().out == (
        "DRY journal alex                         2026-07-14  miss\n"
        "dry-run: 1 (person, day) pairs, no LLM calls, nothing written\n"
    )


def test_report_service_preserves_existing_dry_run_output(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    conn = open_db(cfg.db_path)
    conn.execute(
        "INSERT INTO summaries (kind, key, input_hash, text, model, created_ts)"
        " VALUES ('daily-person', 'alex|2026-07-14', 'h', 'Alex fixed X.', 'f', 't')"
    )
    conn.commit()

    assert run_report(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        base=date(2026, 7, 14),
        dry_run=True,
    ) == 0

    assert capsys.readouterr().out == (
        "DRY report Week 2026-07-13-17: 1 dailies, miss\n"
    )


def test_docs_sync_service_preserves_existing_output(tmp_path, capsys):
    source = tmp_path / "obsidian"
    (source / "Project Alpha").mkdir(parents=True)
    (source / "Project Alpha" / "architecture.md").write_text("# arch\n")
    cfg = _cfg(tmp_path, TEAMMEM_OBSIDIAN_PROJECTS=str(source))

    assert run_docs_sync(cfg) == 0

    assert capsys.readouterr().out == (
        f"docs-sync: 1 files updated across 1 projects -> {cfg.vault_dir / 'Docs'}\n"
    )


class _WarningConnector:
    name = "discord"

    def validate(self, cfg, settings):
        return []

    def collect(self, cfg, ids, settings, now):
        from teammem.connectors.base import CollectionResult

        return CollectionResult(
            events=(Event(
                person="alex",
                project="project-alpha",
                ts=now.isoformat(),
                source="discord-channel",
                kind="message",
                summary="hello",
                refs=json.dumps({"channel_id": "123"}),
                hash="m1",
            ),),
            warnings=("history may be incomplete",),
        )


def test_collect_connector_returns_provider_warnings(tmp_path):
    from teammem.connectors.config import ConnectorSettings

    cfg = _cfg(tmp_path)
    result = collect_connector(
        "discord",
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        ConnectorSettings("discord", True, {}),
        NOW,
        connector=_WarningConnector(),
    )

    assert result.fetched == 1
    assert result.inserted == 1
    assert result.warnings == ("history may be incomplete",)


def test_render_push_warning_keeps_sanitized_final_git_stderr_line(
    tmp_path, monkeypatch, capsys
):
    cfg = _cfg(tmp_path, TEAMMEM_GITHUB_TOKEN="secret-token")
    _seed(cfg)

    def fail_push(_path):
        raise subprocess.CalledProcessError(
            128,
            ["git", "push"],
            stderr="remote: authentication rejected\n"
            "fatal: push failed for secret-token",
        )

    monkeypatch.setattr("teammem.services.push", fail_push)

    assert run_render(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        today=date(2026, 7, 16),
        push_requested=True,
    ) == 0

    captured = capsys.readouterr()
    assert (
        "WARN: vault push failed (fatal: push failed for [REDACTED]); "
        "commits retained for next push"
    ) in captured.err
    assert "secret-token" not in captured.out + captured.err
