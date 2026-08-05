import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from teammem.config import Config
from teammem.connectors.base import CollectionResult
from teammem.connectors.config import ConnectorSettings
from teammem.daily import StepResult, run_daily
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
        "TEAMMEM_INBOX": "",
        "TEAMMEM_ARCHIVE": "",
        "TEAMMEM_QUARANTINE": "",
        "TEAMMEM_SNAPSHOTS": "",
        "TEAMMEM_OBSIDIAN_PROJECTS": "",
        "TEAMMEM_PUSH": "false",
    }
    env.update(values)
    return Config.load(env=env)


def _run_with_failed_stage(tmp_path, monkeypatch, stage):
    values = {}
    settings = _settings()
    connectors = {}
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: None)

    if stage == "ledger":
        monkeypatch.setattr(
            "teammem.daily.open_db",
            lambda _path: (_ for _ in ()).throw(RuntimeError("ledger failure")),
        )
    elif stage in {"github", "gitlab", "slack", "feishu", "discord"}:
        settings = _settings(stage)
        connectors[stage] = FixtureConnector(
            stage, error=RuntimeError(f"{stage} failure")
        )
    elif stage == "import":
        values.update(
            TEAMMEM_INBOX=str(tmp_path / "inbox"),
            TEAMMEM_ARCHIVE=str(tmp_path / "archive"),
            TEAMMEM_QUARANTINE=str(tmp_path / "quarantine"),
        )
        monkeypatch.setattr(
            "teammem.daily.import_inbox",
            lambda *args: (_ for _ in ()).throw(RuntimeError("import failure")),
        )
    elif stage == "reclaim":
        monkeypatch.setattr(
            "teammem.daily.reclaim",
            lambda *args: (_ for _ in ()).throw(RuntimeError("reclaim failure")),
        )
    elif stage == "journal":
        monkeypatch.setattr(
            "teammem.daily.resolve_llm_backend", lambda *args: object()
        )
        monkeypatch.setattr(
            "teammem.daily.execute_journal",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("journal failure")
            ),
        )
    elif stage == "report":
        monkeypatch.setattr(
            "teammem.daily.resolve_llm_backend", lambda *args: object()
        )
        monkeypatch.setattr(
            "teammem.daily.execute_journal",
            lambda *args, **kwargs: SimpleNamespace(failed_person_days=()),
        )
        monkeypatch.setattr(
            "teammem.daily.execute_report",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("report failure")
            ),
        )
    elif stage == "docs-sync":
        values["TEAMMEM_OBSIDIAN_PROJECTS"] = str(tmp_path / "obsidian-projects")
        monkeypatch.setattr(
            "teammem.daily.run_docs_sync",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("docs-sync failure")
            ),
        )
    elif stage == "render":
        monkeypatch.setattr(
            "teammem.daily.run_render",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("render failure")
            ),
        )
    elif stage == "push":
        values["TEAMMEM_PUSH"] = "true"
        monkeypatch.setattr(
            "teammem.daily.push",
            lambda *args: (_ for _ in ()).throw(RuntimeError("push failure")),
        )
    elif stage == "snapshot":
        values["TEAMMEM_SNAPSHOTS"] = str(tmp_path / "snapshots")
        monkeypatch.setattr(
            "teammem.daily._snapshot",
            lambda *args: (_ for _ in ()).throw(RuntimeError("snapshot failure")),
        )

    cfg = _cfg(tmp_path, **values)
    return run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        settings,
        NOW,
        connectors=connectors,
    )


@pytest.mark.parametrize(
    ("stage", "expected_exit_code"),
    [
        ("ledger", 1),
        ("reclaim", 1),
        ("render", 1),
        ("snapshot", 1),
        ("github", 0),
        ("gitlab", 0),
        ("slack", 0),
        ("feishu", 0),
        ("discord", 0),
        ("import", 0),
        ("journal", 0),
        ("report", 0),
        ("docs-sync", 0),
        ("push", 0),
    ],
)
def test_daily_exit_policy_preserves_visible_stage_failures(
    tmp_path, monkeypatch, stage, expected_exit_code
):
    result = _run_with_failed_stage(tmp_path, monkeypatch, stage)

    assert result.exit_code == expected_exit_code
    assert result.status(stage) == "failed"
    assert f"{stage} failure" in result.step(stage).detail


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

    assert result.exit_code == 0
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


def test_capture_only_holds_lock_closes_ledger_and_skips_publication(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path, TEAMMEM_SNAPSHOTS=str(tmp_path / "snapshots"))
    events = []
    real_open_db = open_db

    class TrackingConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def close(self):
            events.append("close")
            self.connection.close()

    @contextmanager
    def lock_factory(path, *, wait_seconds, on_wait, monotonic):
        events.append(("lock", path, wait_seconds))
        yield
        events.append("release")

    def tracked_open(path):
        events.append("open")
        return TrackingConnection(real_open_db(path))

    monkeypatch.setattr("teammem.daily.open_db", tracked_open)
    monkeypatch.setattr(
        "teammem.daily.resolve_llm_backend",
        lambda *args: pytest.fail("capture-only must not resolve an LLM"),
    )
    monkeypatch.setattr(
        "teammem.daily.run_render",
        lambda *args, **kwargs: pytest.fail("capture-only must not render"),
    )
    monkeypatch.setattr(
        "teammem.daily.run_docs_sync",
        lambda *args, **kwargs: pytest.fail("capture-only must not sync docs"),
    )
    monkeypatch.setattr(
        "teammem.daily.push",
        lambda *args, **kwargs: pytest.fail("capture-only must not push"),
    )

    result = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings("feishu"),
        NOW,
        connectors={
            "feishu": FixtureConnector(
                "feishu", CollectionResult(events=(EVENT,))
            )
        },
        capture_only=True,
        lock_factory=lock_factory,
    )

    assert result.exit_code == 0
    assert events[:2] == [("lock", cfg.db_path, 0), "open"]
    assert events[-2:] == ["close", "release"]
    assert stats(real_open_db(cfg.db_path))["total"] == 1
    assert (tmp_path / "snapshots" / "ledger-2026-07-17.db").exists()
    for name in ("journal", "report", "docs-sync", "render", "push"):
        assert result.step(name) == StepResult(name, "skipped", "capture-only")


def test_reporter_failure_after_open_closes_ledger_before_lock_release(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path)
    events = []
    failure = OSError("stderr closed")
    connection = open_db(cfg.db_path)

    class TrackingConnection:
        def close(self):
            events.append("close")
            connection.close()

    @contextmanager
    def lock_factory(*args, **kwargs):
        events.append("acquire")
        try:
            yield
        finally:
            events.append("release")

    def tracked_open(path):
        events.append("open")
        return TrackingConnection()

    def reporter(event):
        if event.event == "stage-end" and event.stage == "ledger":
            events.append("ledger stage-end")
            raise failure

    monkeypatch.setattr("teammem.daily.open_db", tracked_open)

    with pytest.raises(OSError) as raised:
        run_daily(
            cfg,
            IdentityMaps.load(CONFIG_DIR),
            _settings(),
            NOW,
            connectors={},
            lock_factory=lock_factory,
            reporter=reporter,
        )

    assert raised.value is failure
    assert events == [
        "acquire",
        "open",
        "ledger stage-end",
        "close",
        "release",
    ]


def test_capture_only_source_failure_is_fatal_but_snapshot_still_runs(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path, TEAMMEM_SNAPSHOTS=str(tmp_path / "snapshots"))

    result = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings("github"),
        NOW,
        connectors={
            "github": FixtureConnector("github", error=RuntimeError("offline"))
        },
        capture_only=True,
    )

    assert result.exit_code == 1
    assert result.status("github") == "failed"
    assert result.status("snapshot") == "ok"
    assert (tmp_path / "snapshots" / "ledger-2026-07-17.db").exists()


def test_capture_only_import_failure_is_fatal_but_full_import_failure_is_not(
    tmp_path, monkeypatch
):
    cfg = _cfg(
        tmp_path,
        TEAMMEM_INBOX=str(tmp_path / "inbox"),
        TEAMMEM_ARCHIVE=str(tmp_path / "archive"),
        TEAMMEM_QUARANTINE=str(tmp_path / "quarantine"),
        TEAMMEM_SNAPSHOTS=str(tmp_path / "snapshots"),
    )
    monkeypatch.setattr(
        "teammem.daily.import_inbox",
        lambda *args: (_ for _ in ()).throw(RuntimeError("bad bundle")),
    )
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: None)

    capture = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings(),
        NOW,
        connectors={},
        capture_only=True,
    )
    full = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings(),
        NOW,
        connectors={},
    )

    assert capture.exit_code == 1
    assert full.exit_code == 0
    assert capture.status("snapshot") == full.status("snapshot") == "ok"


def test_lock_collision_fails_before_opening_the_ledger(tmp_path, monkeypatch):
    from teammem.run_lock import RunLockedError

    cfg = _cfg(tmp_path)

    @contextmanager
    def busy_lock(*args, **kwargs):
        raise RunLockedError("another run is active")
        yield

    monkeypatch.setattr(
        "teammem.daily.open_db",
        lambda path: pytest.fail("ledger must not open before lock acquisition"),
    )

    result = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings(),
        NOW,
        lock_factory=busy_lock,
    )

    assert result.exit_code == 1
    assert result.step("lock") == StepResult(
        "lock", "failed", "another run is active"
    )
    assert result.status("ledger") == "skipped"


def test_full_run_requests_thirty_minute_lock_wait(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    seen = {}

    @contextmanager
    def lock_factory(path, *, wait_seconds, on_wait, monotonic):
        seen["wait_seconds"] = wait_seconds
        yield

    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: None)

    result = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings(),
        NOW,
        connectors={},
        lock_factory=lock_factory,
    )

    assert result.exit_code == 0
    assert seen["wait_seconds"] == 1800


def test_monday_full_run_reconciles_previous_then_current_across_year_boundary(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path)
    journal_calls = []
    report_calls = []
    backends = [object(), object()]
    monkeypatch.setattr(
        "teammem.daily.resolve_llm_backend", lambda *args: backends.pop(0)
    )
    monkeypatch.setattr(
        "teammem.daily.execute_journal",
        lambda *args, **kwargs: journal_calls.append(kwargs)
        or SimpleNamespace(failed_person_days=()),
    )
    def report(*args, **kwargs):
        report_calls.append(kwargs)
        return SimpleNamespace(
            target_monday=kwargs["target_week"],
            status="generated",
            detail="2 dailies",
            elapsed_seconds=0.25,
        )

    monkeypatch.setattr("teammem.daily.execute_report", report)

    local_monday = datetime(
        2027, 1, 4, 1, 30, tzinfo=timezone(timedelta(hours=14))
    )
    result = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings(),
        local_monday,
        connectors={},
    )

    assert journal_calls[0]["start_day"] == "2026-12-28"
    assert journal_calls[0]["end_day"] == "2027-01-04"
    assert [call["target_week"].isoformat() for call in report_calls] == [
        "2026-12-28",
        "2027-01-04",
    ]
    report_step = result.step("report")
    assert report_step.status == "ok"
    assert [(item.name, item.status) for item in report_step.subresults] == [
        ("previous", "generated"),
        ("current", "generated"),
    ]


def test_failed_person_day_blocks_only_its_report_week(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    calls = []
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: object())
    monkeypatch.setattr(
        "teammem.daily.execute_journal",
        lambda *args, **kwargs: SimpleNamespace(
            failed_person_days=(("alex", "2026-07-14"),)
        ),
    )
    monkeypatch.setattr(
        "teammem.daily.execute_report",
        lambda *args, **kwargs: calls.append(kwargs["target_week"])
        or SimpleNamespace(
            target_monday=kwargs["target_week"],
            status="cached",
            detail="cached",
            elapsed_seconds=0.0,
        ),
    )

    result = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings(),
        datetime(2026, 7, 20, 18, 20, tzinfo=timezone.utc),
        connectors={},
    )

    report = result.step("report")
    assert calls == [datetime(2026, 7, 20).date()]
    assert report.subresults[0] == StepResult(
        "previous",
        "skipped",
        "2026-07-13: journal failed for target week",
    )
    assert report.subresults[1].status == "cached"


def test_report_week_exceptions_are_redacted_and_attempts_are_independent(
    tmp_path, monkeypatch
):
    secret = "sk-do-not-print"
    cfg = _cfg(tmp_path, ANTHROPIC_API_KEY=secret)
    calls = []
    monkeypatch.setattr("teammem.daily.resolve_llm_backend", lambda *args: object())
    monkeypatch.setattr(
        "teammem.daily.execute_journal",
        lambda *args, **kwargs: SimpleNamespace(failed_person_days=()),
    )

    def report(*args, **kwargs):
        calls.append(kwargs["target_week"])
        if len(calls) == 1:
            raise RuntimeError(f"provider rejected {secret}")
        return SimpleNamespace(
            target_monday=kwargs["target_week"],
            status="generated",
            detail="1 daily",
            elapsed_seconds=0.1,
        )

    monkeypatch.setattr("teammem.daily.execute_report", report)

    result = run_daily(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        _settings(),
        datetime(2026, 7, 20, 18, 20, tzinfo=timezone.utc),
        connectors={},
    )

    assert len(calls) == 2
    report_step = result.step("report")
    assert report_step.status == "failed"
    assert report_step.subresults[0].status == "failed"
    assert report_step.subresults[0].detail == (
        "2026-07-13: provider rejected [REDACTED]"
    )
    assert report_step.subresults[1].status == "generated"
    assert result.exit_code == 0


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
        "teammem.daily.execute_journal",
        lambda *args, **kwargs: seen.setdefault(
            "journal_day", kwargs["end_day"]
        )
        and SimpleNamespace(failed_person_days=()),
    )
    monkeypatch.setattr(
        "teammem.daily.execute_report",
        lambda *args, **kwargs: seen.setdefault(
            "report_days", []
        ).append(kwargs["target_week"])
        or SimpleNamespace(
            status="generated", detail="generated", elapsed_seconds=0.0
        ),
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
    assert seen["journal_day"] == "2026-07-17"
    assert [day.isoformat() for day in seen["report_days"]] == [
        "2026-07-06",
        "2026-07-13",
    ]
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
    assert [item.status for item in result.step("report").subresults] == [
        "skipped",
        "skipped",
    ]
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
        "teammem.daily.execute_journal",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("llm offline")),
    )
    monkeypatch.setattr(
        "teammem.daily.execute_report",
        lambda *args, **kwargs: calls.append("report"),
    )
    monkeypatch.setattr(
        "teammem.daily.run_docs_sync",
        lambda *args, **kwargs: calls.append("docs-sync") or 0,
    )

    result = run_daily(
        cfg, IdentityMaps.load(CONFIG_DIR), _settings(), NOW, connectors={}
    )

    assert result.exit_code == 0
    assert result.status("journal") == "failed"
    assert result.status("report") == "skipped"
    assert calls == ["docs-sync"]
    assert result.status("docs-sync") == "ok"
    assert result.status("render") == "ok"
    assert "Cached journal remains available." in (
        cfg.vault_dir / "Person" / "Alex Rivera" / "README.md"
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
