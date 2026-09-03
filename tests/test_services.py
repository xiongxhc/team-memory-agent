import json
import subprocess
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import teammem.services as services_module
from teammem.config import Config
from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.metrics import CommitCountScope, WeeklyCommitCount
from teammem.services import (
    JournalFailure,
    ReportRunResult,
    collect_connector,
    execute_journal,
    execute_report,
    run_docs_sync,
    run_journal,
    run_render,
    run_report,
    resolve_llm_backend,
)
from teammem.store import (
    SummaryRecord,
    get_summary,
    insert_events,
    open_db,
    put_summary,
    reconcile_gitlab_events,
    weekly_commit_counts,
)
from teammem.summarize import prepare_daily_journal


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


def _fake_journal_llm(calls):
    def llm(system, user):
        calls.append((system, user))
        return "generated journal"

    return llm


def test_codex_provider_wins_over_anthropic_key_and_selects_workload_effort(
    tmp_path, monkeypatch
):
    cfg = _cfg(
        tmp_path,
        TEAMMEM_LLM_PROVIDER="codex",
        TEAMMEM_CODEX_BIN="/opt/codex",
        ANTHROPIC_API_KEY="x",
    )
    calls = []

    def fake_codex(model, *, reasoning_effort, codex_bin):
        calls.append((model, reasoning_effort, codex_bin))
        return lambda _system, _user: "codex"

    monkeypatch.setattr(
        services_module,
        "codex_cli_llm",
        fake_codex,
        raising=False,
    )
    monkeypatch.setattr(
        services_module,
        "http_llm",
        lambda *_args, **_kwargs: (lambda _system, _user: "anthropic"),
    )
    monkeypatch.setattr("shutil.which", lambda binary: binary)

    daily = resolve_llm_backend(cfg, cfg.llm_daily_model, max_tokens=1024)
    report = resolve_llm_backend(cfg, cfg.llm_report_model, max_tokens=8192)

    assert daily("system", "user") == "codex"
    assert report("system", "user") == "codex"
    assert calls == [
        ("gpt-5.6-sol", "medium", "/opt/codex"),
        ("gpt-5.6-sol", "high", "/opt/codex"),
    ]


def test_codex_provider_without_configured_binary_has_no_backend(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path, TEAMMEM_LLM_PROVIDER="codex")
    monkeypatch.setattr("shutil.which", lambda _binary: None)

    assert resolve_llm_backend(cfg, cfg.llm_daily_model, 1024) is None


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
    conn = open_db(cfg.db_path)
    put_summary(conn, SummaryRecord(
        "daily-person", "alex|2026-07-14", "stale", "old", "other-model", "t"
    ))

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


def _seed_person_days(cfg, pairs):
    insert_events(open_db(cfg.db_path), [
        Event(
            person=person,
            project="project-alpha",
            ts=f"{day}T09:00:00+00:00",
            source="gitlab",
            kind="commit",
            summary=f"work for {person} on {day}",
            hash=f"{person}-{day}",
        )
        for person, day in pairs
    ])


def test_execute_journal_counts_hits_migrations_and_calls_by_their_real_scope(
    tmp_path
):
    cfg = _cfg(tmp_path, TEAMMEM_LLM_CONCURRENCY="2")
    pairs = [
        ("alex", "2026-07-14"),
        ("alex", "2026-07-15"),
        ("sam", "2026-07-15"),
    ]
    _seed_person_days(cfg, pairs)
    conn = open_db(cfg.db_path)
    ids = IdentityMaps.load(CONFIG_DIR)
    prepared = [
        prepare_daily_journal(
            conn, person, ids.display_name(person), day, ["project-alpha"]
        )
        for person, day in pairs
    ]
    assert all(item is not None for item in prepared)
    put_summary(conn, SummaryRecord(
        "daily-person", prepared[0].key, prepared[0].input_hash,
        "current cache", cfg.llm_daily_model, "old-ts",
    ))
    put_summary(conn, SummaryRecord(
        "daily-person", prepared[1].key, prepared[1].legacy_input_hash,
        "legacy cache", cfg.llm_daily_model, "old-ts",
    ))
    calls = []
    progress = []

    result = execute_journal(
        cfg,
        ids,
        start_day="2026-07-14",
        end_day="2026-07-15",
        created_ts="2026-07-16T00:00:00",
        conn=conn,
        llm=_fake_journal_llm(calls),
        reporter=progress.append,
    )

    assert result.exit_code == 0
    assert result.failed_person_days == ()
    assert result.metrics.pairs == 3
    assert result.metrics.cached == 1
    assert result.metrics.migrated == 1
    assert result.metrics.llm_calls == 1
    assert result.metrics.concurrency == 2
    assert result.metrics.prompt_events.count == 3
    assert result.metrics.prompt_events.p50 == 1
    assert result.metrics.prompt_events.p95 == 1
    assert result.metrics.prompt_events.maximum == 1
    assert result.metrics.prompt_bytes.count == 3
    assert result.metrics.queue_wait_seconds.count == 1
    assert result.metrics.backend_seconds.count == 1
    assert len(calls) == 1
    assert get_summary(conn, "daily-person", prepared[1].key).input_hash == (
        prepared[1].input_hash
    )
    assert progress[-1].event == "journal-progress"
    assert progress[-1].stage == "journal"
    assert dict(progress[-1].fields)["prompt_events_count"] == 3
    assert dict(progress[-1].fields)["queue_wait_seconds_count"] == 1


def test_execute_journal_treats_other_model_cache_as_a_miss(tmp_path):
    cfg = _cfg(tmp_path, TEAMMEM_LLM_PROVIDER="codex")
    _seed(cfg)
    conn = open_db(cfg.db_path)
    ids = IdentityMaps.load(CONFIG_DIR)
    prepared = prepare_daily_journal(
        conn, "alex", ids.display_name("alex"), "2026-07-14", ["project-alpha"]
    )
    put_summary(conn, SummaryRecord(
        "daily-person",
        prepared.key,
        prepared.input_hash,
        "cached Claude summary",
        "claude-haiku-4-5",
        "old-ts",
    ))
    calls = []

    result = execute_journal(
        cfg,
        ids,
        start_day="2026-07-14",
        end_day="2026-07-14",
        created_ts="2026-07-16T00:00:00",
        conn=conn,
        llm=_fake_journal_llm(calls),
    )

    assert result.metrics.cached == 0
    assert result.metrics.llm_calls == 1
    assert len(calls) == 1
    assert get_summary(conn, "daily-person", prepared.key).model == "gpt-5.6-sol"
    assert result.metrics.queue_wait_seconds.count == 1
    assert result.metrics.backend_seconds.count == 1


def test_execute_journal_bounds_workers_keeps_sqlite_on_caller_and_persists_sorted(
    tmp_path
):
    cfg = _cfg(tmp_path, TEAMMEM_LLM_CONCURRENCY="2")
    pairs = [
        ("alex", "2026-07-14"),
        ("alex", "2026-07-15"),
        ("sam", "2026-07-14"),
        ("sam", "2026-07-15"),
    ]
    _seed_person_days(cfg, pairs)
    conn = open_db(cfg.db_path)
    caller_thread = threading.get_ident()
    sqlite_threads = []
    conn.set_trace_callback(
        lambda _statement: sqlite_threads.append(threading.get_ident())
    )
    lock = threading.Lock()
    release = threading.Event()
    later_day_finished = {"alex": threading.Event(), "sam": threading.Event()}
    active = 0
    maximum_active = 0

    def synchronized_llm(_system, user):
        nonlocal active, maximum_active
        person = user.split("slug: ", 1)[1].split(")", 1)[0]
        day = user.split("Date: ", 1)[1].splitlines()[0]
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if active == cfg.llm_concurrency:
                release.set()
        assert release.wait(timeout=1)
        try:
            if day.endswith("14"):
                assert later_day_finished[person].wait(timeout=1)
            else:
                later_day_finished[person].set()
            return user.splitlines()[0]
        finally:
            with lock:
                active -= 1

    result = execute_journal(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        start_day="2026-07-14",
        end_day="2026-07-15",
        created_ts="2026-07-16T00:00:00",
        conn=conn,
        llm=synchronized_llm,
    )

    assert result.exit_code == 0
    assert maximum_active == 2
    assert set(sqlite_threads) == {caller_thread}
    assert conn.execute(
        "SELECT key FROM summaries WHERE kind='daily-person' ORDER BY id"
    ).fetchall() == [(f"{person}|{day}",) for person, day in pairs]


def test_execute_journal_drains_failures_retains_successes_and_redacts_details(
    tmp_path
):
    cfg = _cfg(
        tmp_path,
        TEAMMEM_LLM_CONCURRENCY="2",
        ANTHROPIC_API_KEY="provider-secret",
    )
    pairs = [
        ("alex", "2026-07-14"),
        ("alex", "2026-07-15"),
        ("sam", "2026-07-15"),
    ]
    _seed_person_days(cfg, pairs)
    conn = open_db(cfg.db_path)
    called = []

    def partly_failing_llm(_system, user):
        person = user.splitlines()[0]
        day = user.splitlines()[1]
        called.append((person, day))
        if "alex" in person:
            raise ValueError(f"provider rejected provider-secret for {user[:20]}")
        return "sam succeeded"

    result = execute_journal(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        start_day="2026-07-14",
        end_day="2026-07-15",
        created_ts="2026-07-16T00:00:00",
        conn=conn,
        llm=partly_failing_llm,
    )

    assert len(called) == 3
    assert result.exit_code == 1
    assert result.failed_person_days == (
        ("alex", "2026-07-14"),
        ("alex", "2026-07-15"),
    )
    assert result.failures == tuple(sorted(result.failures, key=lambda failure: (
        failure.person, failure.day
    )))
    assert all(isinstance(failure, JournalFailure) for failure in result.failures)
    assert all("provider-secret" not in failure.detail for failure in result.failures)
    assert all("[REDACTED]" in failure.detail for failure in result.failures)
    assert result.metrics.backend_seconds.count == 3
    assert get_summary(conn, "daily-person", "sam|2026-07-15").text == "sam succeeded"
    assert get_summary(conn, "daily-person", "alex|2026-07-14") is None
    assert get_summary(conn, "daily-person", "alex|2026-07-15") is None


def test_run_journal_returns_failure_exit_and_prints_redacted_pair_details(
    tmp_path, capsys
):
    cfg = _cfg(tmp_path, ANTHROPIC_API_KEY="provider-secret")
    _seed(cfg)

    def failing_llm(_system, _user):
        raise ValueError("backend refused provider-secret")

    assert run_journal(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        today=date(2026, 7, 16),
        since_days=7,
        llm=failing_llm,
    ) == 1

    captured = capsys.readouterr()
    assert "journals: 0 generated, 0 cached" in captured.out
    assert "alex 2026-07-14" in captured.err
    assert "[REDACTED]" in captured.err
    assert "provider-secret" not in captured.out + captured.err


def test_report_service_preserves_existing_dry_run_output(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    conn = open_db(cfg.db_path)
    conn.execute(
        "INSERT INTO summaries (kind, key, input_hash, text, model, created_ts)"
        " VALUES ('daily-person', 'alex|2026-07-14', 'h', 'Alex fixed X.', 'f', 't')"
    )
    put_summary(conn, SummaryRecord(
        "weekly-team", "team|2026-07-13", "stale", "old", "other-model", "t"
    ))
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


def test_execute_report_normalizes_week_and_stores_exact_daily_provenance(tmp_path):
    cfg = _cfg(tmp_path)
    conn = open_db(cfg.db_path)
    insert_events(conn, [
        Event(
            person="alex",
            project="project-alpha",
            ts="2026-08-04T18:22:36+04:00",
            source="gitlab",
            kind="commit",
            summary="included work",
            hash="included",
        ),
        Event(
            person="sam",
            project="project-beta",
            ts="2026-08-05T23:59:59+04:00",
            source="gitlab",
            kind="commit",
            summary="not yet summarized",
            hash="excluded",
        ),
    ])
    daily = SummaryRecord(
        "daily-person",
        "alex|2026-08-04",
        "canonical-daily-hash",
        "Alex shipped the included work.",
        "daily-model",
        "daily-created",
    )
    put_summary(conn, daily)
    calls = []

    result = execute_report(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        target_week=date(2026, 8, 6),
        operator_date=date(2026, 8, 6),
        conn=conn,
        llm=_fake_journal_llm(calls),
        monotonic=iter([10.0, 12.5]).__next__,
    )

    assert result == ReportRunResult(
        target_monday=date(2026, 8, 3),
        status="generated",
        detail="1 dailies",
        elapsed_seconds=2.5,
    )
    assert len(calls) == 1
    assert "Alex shipped the included work." in calls[0][1]
    stored = get_summary(conn, "weekly-team", "team|2026-08-03")
    assert stored.coverage_state == "provisional"
    assert stored.evidence_cutoff == "2026-08-04T18:22:36+04:00"
    assert stored.source_input_hash
    assert get_summary(conn, "daily-person", daily.key) == daily


def test_execute_report_cache_uses_daily_canonical_hash_even_when_text_is_same(
    tmp_path,
):
    cfg = _cfg(tmp_path)
    conn = open_db(cfg.db_path)
    insert_events(conn, [Event(
        person="alex",
        project="project-alpha",
        ts="2026-08-04T09:00:00+04:00",
        source="gitlab",
        kind="commit",
        summary="work",
        hash="work",
    )])
    daily = SummaryRecord(
        "daily-person", "alex|2026-08-04", "daily-hash-1", "same daily text",
        "daily-model", "daily-created",
    )
    put_summary(conn, daily)
    calls = []
    arguments = dict(
        cfg=cfg,
        ids=IdentityMaps.load(CONFIG_DIR),
        target_week=date(2026, 8, 4),
        operator_date=date(2026, 8, 4),
        conn=conn,
        llm=_fake_journal_llm(calls),
    )

    first = execute_report(**arguments)
    cached = execute_report(**arguments)
    put_summary(conn, SummaryRecord(
        "daily-person", daily.key, "daily-hash-2", daily.text,
        daily.model, daily.created_ts,
    ))
    regenerated = execute_report(**arguments)

    assert (first.status, cached.status, regenerated.status) == (
        "generated", "cached", "generated"
    )
    assert len(calls) == 2


def test_execute_report_reports_generation_when_model_changes(tmp_path):
    claude_cfg = _cfg(tmp_path)
    codex_cfg = _cfg(tmp_path, TEAMMEM_LLM_PROVIDER="codex")
    conn = open_db(claude_cfg.db_path)
    insert_events(conn, [Event(
        person="alex",
        project="project-alpha",
        ts="2026-08-04T09:00:00+04:00",
        source="gitlab",
        kind="commit",
        summary="work",
        hash="work",
    )])
    put_summary(conn, SummaryRecord(
        "daily-person",
        "alex|2026-08-04",
        "daily-hash",
        "same daily text",
        "daily-model",
        "daily-created",
    ))
    calls = []
    common = dict(
        ids=IdentityMaps.load(CONFIG_DIR),
        target_week=date(2026, 8, 4),
        operator_date=date(2026, 8, 4),
        conn=conn,
        llm=_fake_journal_llm(calls),
    )

    first = execute_report(claude_cfg, **common)
    switched = execute_report(codex_cfg, **common)

    assert (first.status, switched.status) == ("generated", "generated")
    assert len(calls) == 2
    assert get_summary(conn, "weekly-team", "team|2026-08-03").model == (
        "gpt-5.6-sol"
    )


def test_execute_report_cache_is_stable_across_equal_flag_reconciliation_order(
    tmp_path,
):
    cfg = _cfg(tmp_path)
    conn = open_db(cfg.db_path)
    ids = IdentityMaps.load(CONFIG_DIR)
    insert_events(conn, [Event(
        person="alex",
        project="project-alpha",
        ts="2026-08-04T09:00:00+04:00",
        source="gitlab",
        kind="commit",
        summary="daily evidence",
        hash="daily-evidence",
    )])
    put_summary(conn, SummaryRecord(
        "daily-person", "alex|2026-08-04", "daily-hash", "same daily text",
        "daily-model", "daily-created",
    ))
    equal_flags = [
        Event(
            person="_unmapped/b",
            project=None,
            ts="2026-08-04T10:00:00+04:00",
            source="gitlab",
            kind="issue",
            summary="same fact b",
            hash="unmapped-b",
        ),
        Event(
            person="_unmapped/a",
            project=None,
            ts="2026-08-04T10:00:00+04:00",
            source="gitlab",
            kind="issue",
            summary="same fact a",
            hash="unmapped-a",
        ),
    ]
    reconcile_gitlab_events(conn, equal_flags, ids)
    calls = []
    arguments = dict(
        cfg=cfg,
        ids=ids,
        target_week=date(2026, 8, 4),
        operator_date=date(2026, 8, 4),
        conn=conn,
        llm=_fake_journal_llm(calls),
    )

    first_result = execute_report(**arguments)
    first = get_summary(conn, "weekly-team", "team|2026-08-03")
    reconcile_gitlab_events(conn, equal_flags[::-1], ids)
    second_result = execute_report(**arguments)
    second = get_summary(conn, "weekly-team", "team|2026-08-03")

    expected_flags = json.dumps(
        {
            "unmapped": [["_unmapped/a", 1], ["_unmapped/b", 1]],
            "unmapped_channels": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert (first_result.status, second_result.status) == ("generated", "cached")
    assert first.effective_flags_json == second.effective_flags_json == expected_flags
    assert first.source_input_hash == second.source_input_hash
    assert first.input_hash == second.input_hash
    assert len(calls) == 1


def test_execute_report_without_dailies_skips_and_writes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    conn = open_db(cfg.db_path)

    result = execute_report(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        target_week=date(2026, 8, 5),
        operator_date=date(2026, 8, 7),
        conn=conn,
        llm=lambda _system, _user: (_ for _ in ()).throw(AssertionError("LLM called")),
        monotonic=iter([4.0, 4.25]).__next__,
    )

    assert result == ReportRunResult(
        target_monday=date(2026, 8, 3),
        status="skipped",
        detail="no daily journals",
        elapsed_seconds=0.25,
    )
    assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] == 0


def test_execute_report_returns_redacted_failure_without_replacing_cache(tmp_path):
    cfg = _cfg(tmp_path, ANTHROPIC_API_KEY="provider-secret")
    conn = open_db(cfg.db_path)
    put_summary(conn, SummaryRecord(
        "daily-person", "alex|2026-08-04", "daily-hash", "daily text",
        "daily-model", "daily-created",
    ))
    previous = SummaryRecord(
        "weekly-team", "team|2026-08-03", "legacy-hash", "legacy report",
        "old-model", "old-created",
    )
    put_summary(conn, previous)

    result = execute_report(
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        target_week=date(2026, 8, 3),
        operator_date=date(2026, 8, 4),
        conn=conn,
        llm=lambda _system, _user: (_ for _ in ()).throw(
            ValueError("backend rejected provider-secret")
        ),
    )

    assert result.status == "failed"
    assert result.detail == "backend rejected [REDACTED]"
    assert get_summary(conn, "weekly-team", previous.key) == previous


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


class _AggregateConnector:
    name = "github"
    provider_commit_detail = "fix: provider payload must stay private"

    def validate(self, cfg, settings):
        return []

    def collect(self, cfg, ids, settings, now):
        from teammem.connectors.base import CollectionResult

        return CollectionResult(
            commit_counts=(
                WeeklyCommitCount("project-alpha", "2026-07-13", "alex", 3),
                WeeklyCommitCount("project-alpha", "2026-07-13", "sam", 1),
            ),
            commit_count_scopes=(
                CommitCountScope("project-alpha", "2026-07-13"),
            ),
        )


def test_collect_connector_persists_aggregate_snapshot_idempotently(tmp_path):
    from teammem.connectors.config import ConnectorSettings

    cfg = _cfg(tmp_path)
    ids = IdentityMaps.load(CONFIG_DIR)
    settings = ConnectorSettings("github", True, {})
    connector = _AggregateConnector()

    first = collect_connector(
        "github", cfg, ids, settings, NOW, connector=connector, emit=False
    )
    second = collect_connector(
        "github", cfg, ids, settings, NOW, connector=connector, emit=False
    )

    assert (first.fetched, first.inserted) == (0, 0)
    assert (first.aggregate_rows, first.aggregate_changes) == (2, 2)
    assert (second.aggregate_rows, second.aggregate_changes) == (2, 0)
    assert weekly_commit_counts(
        open_db(cfg.db_path), "project-alpha", "2026-07-13"
    ) == [
        WeeklyCommitCount("project-alpha", "2026-07-13", "alex", 3),
        WeeklyCommitCount("project-alpha", "2026-07-13", "sam", 1),
    ]


def test_collect_connector_dry_run_lists_counts_without_writing_or_payload(
    tmp_path, capsys
):
    from teammem.connectors.config import ConnectorSettings

    cfg = _cfg(tmp_path)
    conn = open_db(cfg.db_path)
    result = collect_connector(
        "github",
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        ConnectorSettings("github", True, {}),
        NOW,
        dry_run=True,
        connector=_AggregateConnector(),
        conn=conn,
    )

    assert (result.aggregate_rows, result.aggregate_changes) == (2, 0)
    assert capsys.readouterr().out == (
        "DRY project-alpha 2026-07-13 alex 3\n"
        "DRY project-alpha 2026-07-13 sam 1\n"
        "dry-run: 0 events; 2 aggregate rows, nothing written\n"
    )
    assert weekly_commit_counts(conn, "project-alpha", "2026-07-13") == []


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
    assert result.aggregate_rows == 0
    assert result.aggregate_changes == 0
    assert result.warnings == ("history may be incomplete",)


def test_collect_connector_reclaims_roster_mapping_before_inserting(tmp_path):
    from teammem.connectors.config import ConnectorSettings

    cfg = _cfg(tmp_path)
    conn = open_db(cfg.db_path)
    insert_events(conn, [Event(
        person="_unmapped/1234567890",
        project="project-alpha",
        ts=NOW.isoformat(),
        source="discord-channel",
        kind="message",
        summary="hello",
        refs=json.dumps({"channel_id": "123"}),
        hash="m1",
    )])

    result = collect_connector(
        "discord",
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        ConnectorSettings("discord", True, {}),
        NOW,
        connector=_WarningConnector(),
        conn=conn,
        emit=False,
    )

    assert result.fetched == 1
    assert result.inserted == 0
    assert conn.execute(
        "SELECT person, source, hash FROM events"
    ).fetchall() == [("alex", "discord-channel", "m1")]


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


def test_render_service_verify_reports_clean_then_drift(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    ids = IdentityMaps.load(CONFIG_DIR)
    assert run_render(cfg, ids, today=date(2026, 7, 16)) == 0
    capsys.readouterr()

    assert run_render(cfg, ids, today=date(2026, 7, 16), verify=True) == 0
    assert "verify-render: vault matches ledger render" in capsys.readouterr().out

    (cfg.vault_dir / "README.md").write_text("tampered")
    assert run_render(cfg, ids, today=date(2026, 7, 16), verify=True) == 1
    out = capsys.readouterr().out
    assert "DIFFERS README.md" in out


def test_render_service_verify_rejects_push_and_dry_run(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    ids = IdentityMaps.load(CONFIG_DIR)
    assert run_render(cfg, ids, today=date(2026, 7, 16), verify=True,
                      push_requested=True) == 2
    assert run_render(cfg, ids, today=date(2026, 7, 16), verify=True,
                      dry_run=True) == 2
