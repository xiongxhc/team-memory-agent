import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import teammem.cli as cli_module
import teammem.config as config_module
from teammem.cli import main, run_collect
from teammem.config import Config
from teammem.gitlab_collector import collect_gitlab
from teammem.identity import IdentityMaps
from teammem.schedule import ScheduleStatus
from teammem.store import open_db, stats

NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)
# Hermetic fixture dir: only .example files, so IdentityMaps.load's fallback is
# deterministic regardless of the operator's real config/roster.yaml.
CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"

PROJECTS = [{"id": 1, "path_with_namespace": "team/project-alpha"}]
COMMIT = {"id": "sha-abc", "author_email": "alex@example.com", "author_name": "Alex",
          "committed_date": "2026-07-14T09:00:00Z", "title": "fix: JWT refresh race",
          "web_url": "https://x/c/sha-abc"}


def fetch(path, params):
    if path == "/groups/42/projects":
        return PROJECTS if params["page"] == 1 else []
    if path == "/projects/1/repository/commits":
        return [COMMIT] if params["page"] == 1 else []
    return []


def _cfg(tmp_path):
    return Config.load(env={"TEAMMEM_DB": str(tmp_path / "ledger.db"),
                            "TEAMMEM_GITLAB_GROUP": "42"})


def test_dry_run_prints_and_writes_nothing(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    ids = IdentityMaps.load(CONFIG_DIR)
    found, inserted = run_collect(cfg, ids, lambda: collect_gitlab(cfg, ids, fetch, NOW),
                                  dry_run=True)
    assert (found, inserted) == (1, 0)
    assert "DRY" in capsys.readouterr().out
    assert not cfg.db_path.exists()


def test_live_run_inserts_then_rerun_inserts_zero(tmp_path):
    cfg = _cfg(tmp_path)
    ids = IdentityMaps.load(CONFIG_DIR)
    assert run_collect(cfg, ids, lambda: collect_gitlab(cfg, ids, fetch, NOW), dry_run=False) == (1, 1)
    assert run_collect(cfg, ids, lambda: collect_gitlab(cfg, ids, fetch, NOW), dry_run=False) == (1, 0)  # idempotent
    assert stats(open_db(cfg.db_path))["total"] == 1


def test_main_guard_exits_2_without_credentials(tmp_path, monkeypatch, capsys):
    for var in ("TEAMMEM_GITLAB_URL", "TEAMMEM_GITLAB_TOKEN", "TEAMMEM_GITLAB_GROUP"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TEAMMEM_DB", str(tmp_path / "ledger.db"))
    assert main(["collect", "gitlab", "--dry-run"]) == 2
    assert "TEAMMEM_GITLAB_URL" in capsys.readouterr().err


def test_main_feishu_guard_exits_2_without_credentials(tmp_path, monkeypatch, capsys):
    for var in ("TEAMMEM_FEISHU_APP_ID", "TEAMMEM_FEISHU_APP_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TEAMMEM_DB", str(tmp_path / "l.db"))
    assert main(["collect", "feishu", "--dry-run"]) == 2
    assert "TEAMMEM_FEISHU_APP_ID" in capsys.readouterr().err


def test_import_bundles_cli_dry_run_does_not_create_database(tmp_path, monkeypatch, capsys):
    inbox = tmp_path / "inbox"
    bundle = inbox / "alex" / "bundle-alex-2026-07-27.json"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(json.dumps({
        "schema": "teammem-bundle/v1",
        "member": "alex",
        "date": "2026-07-27",
        "events": [],
        "journal_md": "## 2026-07-27",
    }))
    db = tmp_path / "ledger.db"
    monkeypatch.setenv("TEAMMEM_DB", str(db))
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))

    assert main([
        "import-bundles",
        "--inbox", str(inbox),
        "--archive", str(tmp_path / "archive"),
        "--quarantine", str(tmp_path / "quarantine"),
        "--dry-run",
    ]) == 0

    assert not db.exists()
    assert "accepted=1" in capsys.readouterr().out


def test_main_stats_runs_without_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TEAMMEM_GITLAB_URL", raising=False)
    monkeypatch.setenv("TEAMMEM_DB", str(tmp_path / "ledger.db"))
    assert main(["stats"]) == 0
    assert "total: 0" in capsys.readouterr().out


def test_main_reclaim_runs_without_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TEAMMEM_GITLAB_URL", raising=False)
    monkeypatch.setenv("TEAMMEM_DB", str(tmp_path / "l.db"))
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))
    assert main(["reclaim", "--dry-run"]) == 0
    assert "reclaimed 0 rows" in capsys.readouterr().out


def test_main_render_dry_run(tmp_path, monkeypatch, capsys):
    from teammem.store import open_db, insert_events
    from teammem.events import Event
    db = tmp_path / "l.db"
    insert_events(open_db(db), [Event(
        person="alex", ts="2026-07-14T09:00:00+04:00", source="gitlab",
        kind="commit", summary="x", hash="h1")])
    monkeypatch.setenv("TEAMMEM_DB", str(db))
    monkeypatch.setenv("TEAMMEM_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.delenv("TEAMMEM_PUSH", raising=False)
    assert main(["render", "--today", "2026-07-16", "--dry-run"]) == 0
    assert "Week 2026-07-13-17" in capsys.readouterr().out
    assert not (tmp_path / "vault").exists()


def test_main_render_push_failure_warns_but_exits_zero(tmp_path, monkeypatch, capsys):
    # Push is best-effort delivery (VPN-only remote): a failed push must not fail
    # the run — commits are retained and the next successful push carries them.
    from teammem.store import open_db, insert_events
    from teammem.events import Event
    import subprocess
    db = tmp_path / "l.db"
    insert_events(open_db(db), [Event(
        person="alex", ts="2026-07-14T09:00:00+04:00", source="gitlab",
        kind="commit", summary="x", hash="h1")])
    monkeypatch.setenv("TEAMMEM_DB", str(db))
    monkeypatch.setenv("TEAMMEM_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.delenv("TEAMMEM_PUSH", raising=False)
    assert main(["render", "--today", "2026-07-16", "--push"]) == 0  # no remote -> push fails
    captured = capsys.readouterr()
    assert "WARN" in captured.err and "push" in captured.err
    log = subprocess.run(["git", "-C", str(tmp_path / "vault"), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "render: 2026-07-16" in log                             # render+commit still landed


def test_main_render_commits(tmp_path, monkeypatch, capsys):
    from teammem.store import open_db, insert_events
    from teammem.events import Event
    import subprocess
    db = tmp_path / "l.db"
    insert_events(open_db(db), [Event(
        person="alex", ts="2026-07-14T09:00:00+04:00", source="gitlab",
        kind="commit", summary="x", hash="h1")])
    monkeypatch.setenv("TEAMMEM_DB", str(db))
    monkeypatch.setenv("TEAMMEM_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.delenv("TEAMMEM_PUSH", raising=False)
    assert main(["render", "--today", "2026-07-16"]) == 0
    log = subprocess.run(["git", "-C", str(tmp_path / "vault"), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "render: 2026-07-16" in log
    assert main(["render", "--today", "2026-07-16"]) == 0          # idempotent -> no 2nd commit
    log2 = subprocess.run(["git", "-C", str(tmp_path / "vault"), "log", "--oneline"],
                          capture_output=True, text=True).stdout
    assert log2.count("render:") == 1


def _journal_db(tmp_path, monkeypatch):
    from teammem.store import open_db, insert_events
    from teammem.events import Event
    db = tmp_path / "l.db"
    insert_events(open_db(db), [Event(
        person="alex", ts="2026-07-14T09:00:00+04:00", source="gitlab",
        kind="commit", project="project-alpha", summary="fix: JWT race", hash="h1")])
    monkeypatch.setenv("TEAMMEM_DB", str(db))
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))
    return db


def test_journal_guard_exits_2_without_api_key(tmp_path, monkeypatch, capsys):
    _journal_db(tmp_path, monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")          # no claude CLI either
    assert main(["journal", "--today", "2026-07-16"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_journal_falls_back_to_claude_cli_without_api_key(tmp_path, monkeypatch, capsys):
    import os
    db = _journal_db(tmp_path, monkeypatch)
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "claude"
    stub.write_text("#!/bin/sh\ncat > /dev/null\nprintf -- '- **project-alpha** — **JWT race fixed**'\n")
    stub.chmod(0o755)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ['PATH']}")
    assert main(["journal", "--today", "2026-07-16"]) == 0
    assert "1 generated" in capsys.readouterr().out
    from teammem.store import open_db
    row = open_db(db).execute(
        "SELECT text FROM summaries WHERE kind = 'daily-person'").fetchone()
    assert row and "JWT race fixed" in row[0]


def test_journal_dry_run_lists_pairs_no_writes(tmp_path, monkeypatch, capsys):
    db = _journal_db(tmp_path, monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)   # dry-run needs no key
    assert main(["journal", "--today", "2026-07-16", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY" in out and "alex" in out and "2026-07-14" in out and "miss" in out
    from teammem.store import open_db
    assert open_db(db).execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0


def test_journal_live_generates_then_hits_cache(tmp_path, monkeypatch, capsys):
    db = _journal_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("TEAMMEM_LLM_DAILY_MODEL", raising=False)
    calls = []

    def fake_http_llm(model, api_key, max_tokens):
        def llm(system, user):
            calls.append(model)
            return "Alex fixed the JWT race."
        return llm

    import teammem.services as services_mod
    monkeypatch.setattr(services_mod, "http_llm", fake_http_llm)
    assert main(["journal", "--today", "2026-07-16"]) == 0
    assert "1 generated" in capsys.readouterr().out and calls == ["daily-summary-model"]
    assert main(["journal", "--today", "2026-07-16"]) == 0   # rerun: all cached
    assert "0 generated" in capsys.readouterr().out and len(calls) == 1


def test_report_without_dailies_warns_and_exits_zero(tmp_path, monkeypatch, capsys):
    _journal_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert main(["report", "--week-of", "2026-07-14"]) == 0
    assert "no daily journals" in capsys.readouterr().err


def test_report_generates_from_cached_dailies(tmp_path, monkeypatch, capsys):
    db = _journal_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("TEAMMEM_LLM_REPORT_MODEL", raising=False)
    from teammem.store import open_db
    conn = open_db(db)
    conn.execute("INSERT INTO summaries (kind, key, input_hash, text, model, created_ts)"
                 " VALUES ('daily-person', 'alex|2026-07-14', 'h', 'Alex fixed X.', 'f', 't')")
    conn.commit()
    calls = []

    def fake_http_llm(model, api_key, max_tokens):
        def llm(system, user):
            calls.append(model)
            assert "Alex fixed X." in user
            return "## Shipped\n- X"
        return llm

    import teammem.services as services_mod
    monkeypatch.setattr(services_mod, "http_llm", fake_http_llm)
    assert main(["report", "--week-of", "2026-07-14"]) == 0
    assert calls == ["weekly-summary-model"]
    assert "report: generated" in capsys.readouterr().out
    row = open_db(db).execute(
        "SELECT text FROM summaries WHERE kind='weekly-team' AND key='team|2026-07-13'").fetchone()
    assert row and "Shipped" in row[0]


def test_report_no_key_no_dailies_still_exits_zero(tmp_path, monkeypatch, capsys):
    _journal_db(tmp_path, monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["report", "--week-of", "2026-07-14"]) == 0
    assert "no daily journals" in capsys.readouterr().err


def test_report_guard_exits_2_without_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")          # no claude CLI either
    db = _journal_db(tmp_path, monkeypatch)
    from teammem.store import open_db
    conn = open_db(db)
    conn.execute("INSERT INTO summaries (kind, key, input_hash, text, model, created_ts)"
                 " VALUES ('daily-person', 'alex|2026-07-14', 'h', 'Alex fixed X.', 'f', 't')")
    conn.commit()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["report", "--week-of", "2026-07-14"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_report_dry_run_prints_state_no_llm_no_writes(tmp_path, monkeypatch, capsys):
    db = _journal_db(tmp_path, monkeypatch)
    from teammem.store import open_db
    conn = open_db(db)
    conn.execute("INSERT INTO summaries (kind, key, input_hash, text, model, created_ts)"
                 " VALUES ('daily-person', 'alex|2026-07-14', 'h', 'Alex fixed X.', 'f', 't')")
    conn.commit()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["report", "--week-of", "2026-07-14", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY report" in out and "1 dailies" in out and "miss" in out
    row = open_db(db).execute(
        "SELECT 1 FROM summaries WHERE kind='weekly-team' AND key='team|2026-07-13'").fetchone()
    assert row is None


def test_main_render_with_channel_names_file(tmp_path, monkeypatch):
    import json as _json
    from teammem.store import open_db, insert_events
    from teammem.events import Event
    db = tmp_path / "l.db"
    insert_events(open_db(db), [Event(
        person="alex", ts="2026-07-14T09:00:00+04:00", source="gitlab",
        kind="commit", summary="x", hash="h1")])
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "roster.example.yaml").write_text((CONFIG_DIR / "roster.example.yaml").read_text())
    (cfgdir / "projects.example.yaml").write_text((CONFIG_DIR / "projects.example.yaml").read_text())
    (cfgdir / "channel_names.json").write_text(_json.dumps({"oc_x": "PM. Share"}))
    monkeypatch.setenv("TEAMMEM_DB", str(db))
    monkeypatch.setenv("TEAMMEM_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(cfgdir))
    monkeypatch.delenv("TEAMMEM_PUSH", raising=False)
    assert main(["render", "--today", "2026-07-16"]) == 0     # names file must not crash render


def test_main_docs_sync_guard_exits_2_without_source(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TEAMMEM_OBSIDIAN_PROJECTS", raising=False)
    monkeypatch.setenv("TEAMMEM_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))
    assert main(["docs-sync"]) == 2


def test_main_docs_sync_copies_docs(tmp_path, monkeypatch, capsys):
    src = tmp_path / "obsidian"
    (src / "Project Alpha").mkdir(parents=True)
    (src / "Project Alpha" / "architecture.md").write_text("# arch\n")
    monkeypatch.setenv("TEAMMEM_OBSIDIAN_PROJECTS", str(src))
    monkeypatch.setenv("TEAMMEM_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))
    assert main(["docs-sync"]) == 0
    out = tmp_path / "vault" / "Docs" / "project-alpha" / "architecture.md"
    assert out.read_text() == "# arch\n"


def test_connectors_list_reports_disabled_without_credentials(monkeypatch, capsys):
    for name in (
        "TEAMMEM_GITHUB_TOKEN",
        "TEAMMEM_GITLAB_TOKEN",
        "TEAMMEM_SLACK_BOT_TOKEN",
        "TEAMMEM_FEISHU_APP_SECRET",
        "TEAMMEM_DISCORD_BOT_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))

    assert main(["connectors", "list"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "discord: disabled",
        "feishu: disabled",
        "github: disabled",
        "gitlab: disabled",
        "slack: disabled",
    ]


def test_connectors_list_reports_enabled_ok_and_missing_without_values(
    tmp_path, monkeypatch, capsys
):
    config = tmp_path / "config"
    config.mkdir()
    (config / "connectors.yaml").write_text(
        "connectors:\n"
        "  github:\n    enabled: true\n"
        "  slack:\n    enabled: true\n"
    )
    secret = "secret-value-that-must-not-print"
    for name in (
        "TEAMMEM_GITLAB_URL",
        "TEAMMEM_GITLAB_TOKEN",
        "TEAMMEM_GITLAB_GROUP",
        "TEAMMEM_SLACK_BOT_TOKEN",
        "TEAMMEM_FEISHU_APP_ID",
        "TEAMMEM_FEISHU_APP_SECRET",
        "TEAMMEM_DISCORD_BOT_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(config))
    monkeypatch.setenv("TEAMMEM_GITHUB_TOKEN", secret)

    assert main(["connectors", "list"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        "discord: disabled",
        "feishu: disabled",
        "github: enabled/ok",
        "gitlab: disabled",
        "slack: enabled/missing TEAMMEM_SLACK_BOT_TOKEN",
    ]
    assert secret not in captured.out


def test_connectors_check_reports_variable_names_not_values(
    tmp_path, monkeypatch, capsys
):
    config = tmp_path / "config"
    config.mkdir()
    (config / "connectors.yaml").write_text(
        "connectors:\n  github:\n    enabled: true\n"
    )
    secret = "secret-value-that-must-not-print"
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(config))
    monkeypatch.setenv("TEAMMEM_GITHUB_TOKEN", secret)

    assert main(["connectors", "check"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "github: ok\n"
    assert secret not in captured.out + captured.err

    monkeypatch.delenv("TEAMMEM_GITHUB_TOKEN")
    assert main(["connectors", "check"]) == 2
    captured = capsys.readouterr()
    assert "TEAMMEM_GITHUB_TOKEN" in captured.err
    assert secret not in captured.out + captured.err


def test_collect_accepts_registry_connector_name(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config"
    config.mkdir()
    for name in ("roster.example.yaml", "projects.example.yaml"):
        (config / name).write_text((CONFIG_DIR / name).read_text())
    (config / "connectors.yaml").write_text(
        "connectors:\n  github:\n    enabled: false\n"
    )
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(config))
    monkeypatch.delenv("TEAMMEM_GITHUB_TOKEN", raising=False)

    assert main(["collect", "github", "--dry-run"]) == 2
    assert "TEAMMEM_GITHUB_TOKEN" in capsys.readouterr().err


def test_collect_enabled_runs_only_enabled_connectors(
    tmp_path, monkeypatch, capsys
):
    config = tmp_path / "config"
    config.mkdir()
    for name in ("roster.example.yaml", "projects.example.yaml"):
        (config / name).write_text((CONFIG_DIR / name).read_text())
    (config / "connectors.yaml").write_text(
        "connectors:\n"
        "  github:\n    enabled: true\n"
        "  slack:\n    enabled: false\n"
    )
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(config))
    monkeypatch.delenv("TEAMMEM_GITHUB_TOKEN", raising=False)

    assert main(["collect", "--enabled", "--dry-run"]) == 2
    captured = capsys.readouterr()
    assert "github" in captured.err
    assert "slack" not in captured.err


def test_collect_runtime_failure_never_prints_secret(
    tmp_path, monkeypatch, capsys
):
    class FailingConnector:
        name = "github"

        def validate(self, cfg, settings):
            return []

        def collect(self, cfg, ids, settings, now):
            raise RuntimeError(f"request rejected for {cfg.github_token}")

    token = "ghp-never-print-this"
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setenv("TEAMMEM_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("TEAMMEM_GITHUB_TOKEN", token)
    monkeypatch.setattr("teammem.cli.get_connector", lambda name: FailingConnector())

    assert main(["collect", "github"]) == 1
    captured = capsys.readouterr()
    assert "github: collection failed" in captured.err
    assert token not in captured.out + captured.err


def test_run_daily_prints_step_warnings_and_returns_result_exit_code(
    tmp_path, monkeypatch, capsys
):
    from teammem.daily import DailyResult, StepResult

    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setattr(
        "teammem.cli.run_daily",
        lambda cfg, ids, settings, now: DailyResult(
            steps=(
                StepResult(
                    "discord",
                    "ok",
                    "0 fetched; warning: MESSAGE_CONTENT may be disabled",
                    ("MESSAGE_CONTENT may be disabled",),
                ),
                StepResult("render", "failed", "cannot write vault"),
            ),
            exit_code=1,
        ),
    )

    assert main(["run-daily"]) == 1
    captured = capsys.readouterr()
    assert "discord: ok" in captured.out
    assert "WARN discord: MESSAGE_CONTENT may be disabled" in captured.err
    assert "render: failed" in captured.err


def test_run_daily_passes_a_local_aware_clock(tmp_path, monkeypatch):
    from datetime import timedelta
    from teammem.daily import DailyResult

    local_now = datetime(
        2026, 7, 17, 18, 20, tzinfo=timezone(timedelta(hours=-7))
    )

    class FixedLocalClock:
        def astimezone(self):
            return local_now

    class FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            assert tz is None
            return FixedLocalClock()

    seen = {}
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setattr("teammem.cli.datetime", FixedDateTime)
    monkeypatch.setattr(
        "teammem.cli.run_daily",
        lambda cfg, ids, settings, now: seen.setdefault("now", now)
        and DailyResult(steps=(), exit_code=0),
    )

    assert main(["run-daily"]) == 0
    assert seen["now"].utcoffset() == timedelta(hours=-7)
    assert (seen["now"].date(), seen["now"].hour) == (local_now.date(), 18)


def test_cli_journal_uses_shared_llm_backend_resolver(
    tmp_path, monkeypatch, capsys
):
    _journal_db(tmp_path, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    import teammem.cli as cli_mod

    monkeypatch.setattr(
        cli_mod,
        "resolve_llm_backend",
        lambda cfg, model, max_tokens: (
            lambda system, user: "- **project-alpha** — fixed"
        ),
    )

    assert main(["journal", "--today", "2026-07-16"]) == 0
    assert "1 generated" in capsys.readouterr().out


def test_global_env_file_is_loaded_before_run_daily_and_process_env_wins(
    tmp_path, monkeypatch, capsys
):
    from teammem.daily import DailyResult

    env_file = tmp_path / "hub.env"
    env_file.write_text(
        "TEAMMEM_CONFIG_DIR=/file/config\n"
        "TEAMMEM_GITHUB_TOKEN=file-secret-must-not-print\n"
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setenv("TEAMMEM_GITHUB_TOKEN", "process-secret-must-not-print")
    seen = {}
    monkeypatch.setattr(
        cli_module,
        "run_daily",
        lambda cfg, ids, settings, now: (
            seen.update(
                env_file=cfg.env_file,
                config_dir=cfg.config_dir,
                credential=cfg.github_token,
            )
            or DailyResult(steps=(), exit_code=0)
        ),
    )

    assert main(["--env-file", str(env_file), "run-daily"]) == 0

    captured = capsys.readouterr()
    assert seen == {
        "env_file": env_file,
        "config_dir": CONFIG_DIR,
        "credential": "process-secret-must-not-print",
    }
    assert "file-secret-must-not-print" not in captured.out + captured.err
    assert "process-secret-must-not-print" not in captured.out + captured.err


@pytest.mark.parametrize("mode", [0o400, 0o640])
def test_global_env_file_requires_exact_0600_mode(tmp_path, mode, capsys):
    env_file = tmp_path / "hub.env"
    env_file.write_text("TEAMMEM_GITHUB_TOKEN=secret-must-not-print\n")
    env_file.chmod(mode)

    assert main(["--env-file", str(env_file), "connectors", "list"]) == 2

    captured = capsys.readouterr()
    assert "exactly 0600" in captured.err
    assert "secret-must-not-print" not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_global_env_file_rejects_symlink_without_reading_target(
    tmp_path, monkeypatch, capsys
):
    target = tmp_path / "target.env"
    target.write_text("TEAMMEM_CONFIG_DIR=/must-not-load\n")
    target.chmod(0o600)
    env_file = tmp_path / "hub.env"
    env_file.symlink_to(target)
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))

    assert main(["--env-file", str(env_file), "connectors", "list"]) == 2
    captured = capsys.readouterr()
    assert "non-symlink" in captured.err
    assert "Traceback" not in captured.err


def test_global_env_file_requires_a_regular_file(tmp_path, capsys):
    env_file = tmp_path / "hub.env"
    env_file.mkdir()
    env_file.chmod(0o600)

    assert main(["--env-file", str(env_file), "connectors", "list"]) == 2
    captured = capsys.readouterr()
    assert "regular" in captured.err
    assert "Traceback" not in captured.err


def test_global_env_file_requires_current_user_ownership(
    tmp_path, monkeypatch, capsys
):
    env_file = tmp_path / "hub.env"
    env_file.write_text("TEAMMEM_GITHUB_TOKEN=secret-must-not-print\n")
    env_file.chmod(0o600)
    different_uid = os.getuid() + 1
    monkeypatch.setattr("teammem.config.os.getuid", lambda: different_uid)

    assert main(["--env-file", str(env_file), "connectors", "list"]) == 2

    captured = capsys.readouterr()
    assert "user-owned" in captured.err
    assert "secret-must-not-print" not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_env_file_open_without_o_nofollow_keeps_descriptor_identity_checks(
    tmp_path, monkeypatch
):
    env_file = tmp_path / "hub.env"
    env_file.write_text("TEAMMEM_SINCE_DAYS=11\n")
    env_file.chmod(0o600)
    monkeypatch.delattr(config_module.os, "O_NOFOLLOW")

    cfg = Config.load(env={}, env_file=env_file)

    assert cfg.since_days == 11


def test_env_file_rejects_substitution_between_lstat_and_open(
    tmp_path, monkeypatch
):
    env_file = tmp_path / "hub.env"
    env_file.write_text("TEAMMEM_SINCE_DAYS=11\n")
    env_file.chmod(0o600)
    replacement = tmp_path / "replacement.env"
    replacement.write_text("TEAMMEM_SINCE_DAYS=22\n")
    replacement.chmod(0o600)
    original = tmp_path / "original.env"
    real_open = os.open
    swapped = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == env_file and not swapped:
            swapped = True
            env_file.rename(original)
            replacement.rename(env_file)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(config_module.os, "open", swap_before_open)

    with pytest.raises(ValueError, match="changed during validation"):
        Config.load(env={}, env_file=env_file)

    assert swapped is True


@pytest.mark.parametrize(
    "argv",
    [
        ["connectors", "list"],
        ["connectors", "check"],
        ["run-daily"],
    ],
)
def test_non_schedule_commands_never_install_a_schedule(
    tmp_path, monkeypatch, argv
):
    from teammem.daily import DailyResult

    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setattr(
        cli_module,
        "_schedule_api",
        lambda: pytest.fail("non-schedule commands must not load scheduling"),
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "run_daily",
        lambda cfg, ids, settings, now: DailyResult(steps=(), exit_code=0),
    )

    assert main(["--env-file", str(tmp_path / "missing.env"), *argv]) == 0


def test_help_never_loads_config_or_installs_a_schedule(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "_schedule_api",
        lambda: pytest.fail("help must not load scheduling"),
        raising=False,
    )
    monkeypatch.setattr(
        cli_module.Config,
        "load",
        lambda *args, **kwargs: pytest.fail("help must not load configuration"),
    )

    with pytest.raises(SystemExit, match="0"):
        main(["--help"])


def test_schedule_install_defaults_to_1820_and_prints_backend_path_time(
    tmp_path, monkeypatch, capsys
):
    env_file = tmp_path / "hub.env"
    env_file.write_text("# configured\n")
    env_file.chmod(0o600)
    seen = []
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    schedule_api = SimpleNamespace(
        install_schedule=lambda cfg, time: seen.append((cfg.env_file, time))
        or Path("/tmp/operator.plist")
    )
    monkeypatch.setattr(
        cli_module,
        "_schedule_api",
        lambda: schedule_api,
        raising=False,
    )

    assert main([
        "--env-file", str(env_file), "schedule", "install"
    ]) == 0

    assert seen == [(env_file, "18:20")]
    assert capsys.readouterr().out == (
        "installed: backend=launchd path=/tmp/operator.plist time=18:20\n"
    )


def test_schedule_status_prints_exact_backend_path_and_time(
    tmp_path, monkeypatch, capsys
):
    status = ScheduleStatus(
        installed=True,
        time="07:05",
        backend="systemd",
        path=Path("/tmp/teammem-daily.timer"),
    )
    schedule_api = SimpleNamespace(schedule_status=lambda: status)
    monkeypatch.setattr(
        cli_module, "_schedule_api", lambda: schedule_api, raising=False
    )

    assert main([
        "--env-file", str(tmp_path / "missing.env"), "schedule", "status"
    ]) == 0
    assert capsys.readouterr().out == (
        "installed: backend=systemd path=/tmp/teammem-daily.timer time=07:05\n"
    )


def test_schedule_remove_is_idempotent_and_reports_absence(
    tmp_path, monkeypatch, capsys
):
    schedule_api = SimpleNamespace(remove_schedule=lambda: False)
    monkeypatch.setattr(
        cli_module, "_schedule_api", lambda: schedule_api, raising=False
    )

    assert main([
        "--env-file", str(tmp_path / "missing.env"), "schedule", "remove"
    ]) == 0
    assert capsys.readouterr().out == "not installed\n"


def test_schedule_unsupported_platform_returns_2_with_direct_message(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    monkeypatch.setattr(
        cli_module,
        "_schedule_api",
        lambda: pytest.fail("unsupported platforms must not import scheduling"),
        raising=False,
    )

    assert main([
        "--env-file", str(tmp_path / "missing.env"), "schedule", "status"
    ]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "unsupported scheduling platform: win32\n"


def test_schedule_install_requires_existing_valid_env_before_loading_scheduler(
    tmp_path, monkeypatch, capsys
):
    scheduler_loads = []
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        cli_module,
        "_schedule_api",
        lambda: scheduler_loads.append(1),
        raising=False,
    )

    assert main([
        "--env-file", str(tmp_path / "missing.env"), "schedule", "install"
    ]) == 2

    captured = capsys.readouterr()
    assert scheduler_loads == []
    assert "environment file does not exist" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("command", ["status", "remove"])
def test_schedule_inspection_and_removal_ignore_broken_env_file(
    tmp_path, monkeypatch, capsys, command
):
    env_file = tmp_path / "hub.env"
    env_file.write_text("TEAMMEM_SINCE_DAYS=private-invalid-value\n")
    env_file.chmod(0o644)
    status = ScheduleStatus(
        installed=False,
        time=None,
        backend="launchd",
        path=Path("/tmp/operator.plist"),
    )
    calls = []
    schedule_api = SimpleNamespace(
        schedule_status=lambda: calls.append("status") or status,
        remove_schedule=lambda: calls.append("remove") or False,
    )
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        cli_module, "_schedule_api", lambda: schedule_api, raising=False
    )

    assert main([
        "--env-file", str(env_file), "schedule", command
    ]) == 0

    captured = capsys.readouterr()
    assert calls == [command]
    assert captured.err == ""
    assert (
        captured.out
        == "not installed: backend=launchd path=/tmp/operator.plist time=unknown\n"
        if command == "status"
        else captured.out == "not installed\n"
    )


def test_invalid_integer_config_is_secret_free_and_has_no_traceback(
    tmp_path, capsys
):
    sensitive_value = "private-value-9847234987234987"
    env_file = tmp_path / "hub.env"
    env_file.write_text(f"TEAMMEM_SINCE_DAYS={sensitive_value}\n")
    env_file.chmod(0o600)

    assert main([
        "--env-file", str(env_file), "connectors", "list"
    ]) == 2

    captured = capsys.readouterr()
    assert "TEAMMEM_SINCE_DAYS" in captured.err
    assert sensitive_value not in captured.out + captured.err
    assert "Traceback" not in captured.err


def test_invalid_schedule_time_returns_2_without_traceback(
    tmp_path, monkeypatch, capsys
):
    from teammem.schedule import install_schedule

    env_file = tmp_path / "hub.env"
    env_file.write_text("# configured\n")
    env_file.chmod(0o600)
    schedule_api = SimpleNamespace(install_schedule=install_schedule)
    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        cli_module, "_schedule_api", lambda: schedule_api, raising=False
    )

    assert main([
        "--env-file", str(env_file), "schedule", "install", "--time", "6:20"
    ]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: schedule time must be HH:MM\n"
    assert "Traceback" not in captured.err


def test_windows_cli_imports_without_unix_scheduler_modules(tmp_path):
    missing_env = tmp_path / "missing.env"
    script = f"""
import builtins
import os
import subprocess
import sys

real_import = builtins.__import__
sys.modules.pop("fcntl", None)

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "fcntl" or name == "teammem.schedule" or (
        name == "schedule" and level == 1
    ):
        raise AssertionError("Unix scheduler imported")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
for attribute in ("O_DIRECTORY", "O_NOFOLLOW"):
    if hasattr(os, attribute):
        delattr(os, attribute)

from teammem.cli import main

first = main(["--env-file", {str(missing_env)!r}, "connectors", "list"])
sys.platform = "win32"
second = main(["--env-file", {str(missing_env)!r}, "schedule", "status"])
if (first, second) != (0, 2):
    raise SystemExit(f"unexpected exit codes: {{first}}, {{second}}")
"""
    environment = os.environ.copy()
    environment["TEAMMEM_CONFIG_DIR"] = str(CONFIG_DIR)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "unsupported scheduling platform: win32" in result.stderr
    assert "Unix scheduler imported" not in result.stderr


def test_schedule_install_runtime_failure_returns_clean_exit_2(
    tmp_path, monkeypatch, capsys
):
    env_file = tmp_path / "hub.env"
    env_file.write_text("# configured\n")
    env_file.chmod(0o600)
    sensitive_detail = "private-runtime-detail-3287498237498"

    def fail_install(cfg, time):
        try:
            raise RuntimeError(sensitive_detail)
        except RuntimeError as cause:
            raise RuntimeError(
                "launchd schedule installation failed; previous state restored"
            ) from cause

    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        cli_module,
        "_schedule_api",
        lambda: SimpleNamespace(install_schedule=fail_install),
    )

    assert main([
        "--env-file", str(env_file), "schedule", "install"
    ]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: launchd schedule installation failed; previous state restored\n"
    )
    assert sensitive_detail not in captured.err
    assert "Traceback" not in captured.err


def test_schedule_remove_rollback_runtime_failure_returns_clean_exit_2(
    tmp_path, monkeypatch, capsys
):
    sensitive_detail = "private-rollback-detail-3287498237498"

    def fail_remove():
        try:
            raise RuntimeError(sensitive_detail)
        except RuntimeError as cause:
            raise RuntimeError(
                "systemd schedule removal failed and rollback failed"
            ) from cause

    monkeypatch.setattr(cli_module.sys, "platform", "linux")
    monkeypatch.setattr(
        cli_module,
        "_schedule_api",
        lambda: SimpleNamespace(remove_schedule=fail_remove),
    )

    assert main([
        "--env-file", str(tmp_path / "broken.env"), "schedule", "remove"
    ]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: systemd schedule removal failed and rollback failed\n"
    )
    assert sensitive_detail not in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("platform", "failure"),
    [
        (
            "darwin",
            subprocess.CalledProcessError(
                1,
                ["launchctl", "bootstrap", "private-command-detail-3287498237498"],
            ),
        ),
        (
            "linux",
            subprocess.SubprocessError(
                "systemd runner private-detail-3287498237498"
            ),
        ),
    ],
)
def test_schedule_runner_failure_returns_generic_secret_free_exit_2(
    tmp_path, monkeypatch, capsys, platform, failure
):
    from teammem.schedule import install_schedule

    env_file = tmp_path / "hub.env"
    env_file.write_text("# configured\n")
    env_file.chmod(0o600)

    def fail_runner(command, **kwargs):
        raise failure

    def install_with_failing_runner(cfg, time):
        return install_schedule(
            cfg,
            time,
            platform=platform,
            executable="/opt/pipx/bin/teammem",
            agents_dir=tmp_path / "LaunchAgents",
            systemd_dir=tmp_path / "systemd",
            runner=fail_runner,
        )

    monkeypatch.setattr(cli_module.sys, "platform", platform)
    monkeypatch.setattr(
        cli_module,
        "_schedule_api",
        lambda: SimpleNamespace(install_schedule=install_with_failing_runner),
    )

    assert main([
        "--env-file", str(env_file), "schedule", "install"
    ]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: schedule operation failed\n"
    assert "private" not in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("failure", [AssertionError("bug"), KeyboardInterrupt()])
def test_schedule_programming_and_base_exceptions_still_propagate(
    tmp_path, monkeypatch, failure
):
    def fail_status():
        raise failure

    monkeypatch.setattr(cli_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        cli_module,
        "_schedule_api",
        lambda: SimpleNamespace(schedule_status=fail_status),
    )

    with pytest.raises(type(failure), match="bug" if str(failure) else None):
        main([
            "--env-file", str(tmp_path / "broken.env"), "schedule", "status"
        ])
