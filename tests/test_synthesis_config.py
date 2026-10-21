"""Operator synthesis setup must fail before reaching a provider."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from teammem import cli, services
from teammem.config import Config, CODEX_MODEL
from teammem.daily import run_daily
from teammem.connectors.config import load_connector_settings, ConnectorSettings
from teammem.connectors.base import CollectionResult
from teammem.events import Event
from teammem.store import open_db, stats
from teammem.identity import IdentityMaps


CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"


def config(tmp_path, **values):
    return Config.load(env={
        "TEAMMEM_DB": str(tmp_path / "ledger.db"),
        "TEAMMEM_VAULT": str(tmp_path / "vault"),
        "TEAMMEM_CONFIG_DIR": str(CONFIG_DIR),
        **values,
    }, env_file=tmp_path / "absent.env")


def test_default_models_are_unconfigured(tmp_path):
    cfg = config(tmp_path)
    assert cfg.llm_daily_model == cfg.llm_report_model == ""


@pytest.mark.parametrize("backend", ["api", "cli"])
@pytest.mark.parametrize("model", [None, "", " \t ", "daily-summary-model", "weekly-summary-model"])
@pytest.mark.parametrize("key,budget", [("TEAMMEM_LLM_DAILY_MODEL", 1024), ("TEAMMEM_LLM_REPORT_MODEL", 8192)])
def test_invalid_model_rejected_before_provider(tmp_path, monkeypatch, backend, model, key, budget):
    values = {"ANTHROPIC_API_KEY": "test-key"} if backend == "api" else {}
    if model is not None:
        values[key] = model
    cfg = config(tmp_path, **values)
    monkeypatch.setattr(services.shutil, "which", lambda _: "/bin/claude" if backend == "cli" else None)
    monkeypatch.setattr(services, "http_llm", lambda *a, **kw: pytest.fail("API must not be constructed"))
    monkeypatch.setattr(services, "claude_cli_llm", lambda *a, **kw: pytest.fail("CLI must not be constructed"))
    selected = cfg.llm_daily_model if budget == 1024 else cfg.llm_report_model
    with pytest.raises(ValueError, match="TEAMMEM_LLM_.*MODEL"):
        services.resolve_llm_backend(cfg, selected, budget)


@pytest.mark.parametrize("backend", ["api", "cli"])
def test_configured_models_reach_backend_normalized(tmp_path, monkeypatch, backend):
    cfg = config(tmp_path, ANTHROPIC_API_KEY="test-key" if backend == "api" else "",
                 TEAMMEM_LLM_DAILY_MODEL="  operator-daily  ",
                 TEAMMEM_LLM_REPORT_MODEL=" operator-weekly ")
    seen = []
    monkeypatch.setattr(services.shutil, "which", lambda _: "/bin/claude")
    monkeypatch.setattr(services, "http_llm", lambda model, *a, **kw: seen.append(("api", model)))
    monkeypatch.setattr(services, "claude_cli_llm", lambda model: seen.append(("cli", model)))
    services.resolve_llm_backend(cfg, cfg.llm_daily_model, 1024)
    services.resolve_llm_backend(cfg, cfg.llm_report_model, 8192)
    assert seen == [(backend, "operator-daily"), (backend, "operator-weekly")]
    assert cfg.llm_daily_model == "operator-daily"


def test_no_backend_skips_unconfigured_models(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    monkeypatch.setattr(services.shutil, "which", lambda _: None)
    assert services.resolve_llm_backend(cfg, cfg.llm_daily_model, 1024) is None


def test_codex_does_not_require_claude_models(tmp_path, monkeypatch):
    cfg = config(tmp_path, TEAMMEM_LLM_PROVIDER="codex", TEAMMEM_LLM_DAILY_MODEL=" ",
                 TEAMMEM_LLM_REPORT_MODEL="weekly-summary-model")
    monkeypatch.setattr(services.shutil, "which", lambda _: "/bin/codex")
    monkeypatch.setattr(services, "codex_cli_llm", lambda model, **kw: model)
    assert services.resolve_llm_backend(cfg, cfg.llm_daily_model, 1024) == CODEX_MODEL


@pytest.mark.parametrize("command", ["journal", "report"])
def test_cli_returns_actionable_configuration_error(tmp_path, monkeypatch, capsys, command):
    cfg = config(tmp_path, ANTHROPIC_API_KEY="test-key")
    monkeypatch.setattr(cli, "_load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(services, "http_llm", lambda *a, **kw: pytest.fail("No provider call"))
    assert cli.main([command]) == 2
    error = capsys.readouterr().err
    assert "TEAMMEM_LLM_" in error
    assert "test-key" not in error


@pytest.mark.parametrize("available", [False, True])
def test_daily_keeps_collection_and_render_without_models(tmp_path, monkeypatch, available):
    cfg = config(tmp_path, ANTHROPIC_API_KEY="test-key" if available else "")
    monkeypatch.setattr(services.shutil, "which", lambda _: None)
    monkeypatch.setattr(services, "http_llm", lambda *a, **kw: pytest.fail("No provider call"))
    settings = load_connector_settings(CONFIG_DIR)
    settings["github"] = ConnectorSettings("github", True, {})
    event = Event(person="alex", project="project-alpha", ts="2026-10-01T09:00:00+00:00",
                  source="github", kind="commit", summary="collected without models", hash="onboarding-test")
    connector = SimpleNamespace(name="github", validate=lambda *a: [],
                                collect=lambda *a: CollectionResult(events=(event,)))
    result = run_daily(cfg, IdentityMaps.load(CONFIG_DIR), settings,
                       datetime(2026, 10, 1, tzinfo=timezone.utc), connectors={"github": connector})
    assert result.status("github") == "ok"
    with open_db(cfg.db_path) as conn:
        assert stats(conn)["total"] == 1
    assert result.status("journal") == ("failed" if available else "skipped")
    if available:
        assert "TEAMMEM_LLM_" in result.step("journal").detail
    assert result.status("render") == "ok"
    assert cfg.db_path.exists()


def test_capture_only_never_requires_models_or_resolves_backend(tmp_path, monkeypatch):
    cfg = config(tmp_path, ANTHROPIC_API_KEY="test-key")
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *a: pytest.fail("Capture must not resolve a backend"))
    result = run_daily(cfg, IdentityMaps.load(CONFIG_DIR), load_connector_settings(CONFIG_DIR), datetime(2026, 10, 1, tzinfo=timezone.utc), capture_only=True)
    assert result.exit_code == 0
    assert result.status("journal") == result.status("render") == "skipped"


@pytest.mark.parametrize("key,budget", [("TEAMMEM_LLM_DAILY_MODEL", 1024), ("TEAMMEM_LLM_REPORT_MODEL", 8192)])
def test_only_requested_model_is_required(tmp_path, monkeypatch, key, budget):
    cfg = config(tmp_path, ANTHROPIC_API_KEY="test-key", **{key: "operator-selected"})
    monkeypatch.setattr(services, "http_llm", lambda model, *a, **kw: model)
    selected = cfg.llm_daily_model if budget == 1024 else cfg.llm_report_model
    assert services.resolve_llm_backend(cfg, selected, budget) == "operator-selected"


@pytest.mark.parametrize("command", ["journal", "report"])
def test_dry_run_does_not_require_models(tmp_path, monkeypatch, command):
    from teammem.store import open_db
    cfg = config(tmp_path, ANTHROPIC_API_KEY="test-key")
    open_db(cfg.db_path).close()
    monkeypatch.setattr(cli, "_load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(cli, "resolve_llm_backend", lambda *a, **kw: pytest.fail("Dry run must not resolve provider"))
    assert cli.main([command, "--dry-run"]) == 0
