import json
import subprocess
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from teammem.config import Config, validate_meeting_settings
from teammem.events import Event
from teammem.identity import IdentityMaps
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
)
from teammem.store import (
    SummaryRecord,
    get_summary,
    insert_events,
    open_db,
    put_summary,
    reconcile_gitlab_events,
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
        "current cache", "old", "old-ts",
    ))
    put_summary(conn, SummaryRecord(
        "daily-person", prepared[1].key, prepared[1].legacy_input_hash,
        "legacy cache", "old", "old-ts",
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


# --- meetings: import, publish, receipt --------------------------------------
# One `teammem process-meetings` run takes ready bundles from the meeting inbox
# to a receipt without a human in the loop. Every remote here is a local bare
# repository under tmp_path, pushed to for real.

import hashlib
import os
import shutil
import sqlite3
import stat

import pytest

from teammem import meeting_render
from teammem import render as render_module
from teammem import services as services_module
from teammem.meeting_bundles import occurrence_path_segment
from teammem.meeting_publish import PublishRefused
from teammem.meeting_store import get_meeting, list_meetings
from teammem.services import (
    MeetingRunResult,
    execute_process_meetings,
    meeting_alert_path,
    meeting_receipt_path,
    prepare_meeting_dirs,
    run_process_meetings,
)
from teammem.vaultgit import commit_all
from tests.test_meeting_bundles import write_meeting_bundle
from tests.test_meeting_publish import _git, _remote_ref, _vault
from tests.test_meeting_render import CONFIG_DIR as MEETING_CONFIG_DIR
from tests.test_meeting_render import (NOTE, SHORT_TURNS, WEEK, _payload,
                                       _short_transcript_payload)

MEETING_NOW = "2026-09-02T16:00:00Z"
MEETING_WEB_BASE = "https://git.example.com/team/team-vault"
OTHER_DAY = "2026-09-01"
# Ordinary activity, most of it older than the four-week Work Journal window.
# A hub that has been running for a while is the only realistic vault to publish
# a meeting into: its person pages link weeks the journal no longer renders, and
# a publish path tested only against a meetings-only vault never meets one.
ORDINARY = [
    Event(person="alex", ts="2026-07-14T09:00:00+04:00", source="gitlab",
          kind="commit", summary="fix: JWT refresh race", hash="o1",
          project="project-alpha"),
    Event(person="sam", ts="2026-07-01T09:00:00+04:00", source="gitlab",
          kind="commit", summary="feat: portal nav", hash="o2",
          project="project-beta"),
    Event(person="alex", ts="2026-09-01T09:00:00+04:00", source="gitlab",
          kind="mr", summary="[merged] Auth middleware fix", hash="o3",
          project="project-alpha"),
]
STALE_WEEK_LINK = "../../Work%20Journal/Week%202026-07-13-17.md"


class _Bench:
    """A configured hub with a meeting inbox and a vault with a bare remote."""

    def __init__(self, tmp_path, branch, vault, bare, cfg):
        self.tmp_path = tmp_path
        self.branch = branch
        self.vault = vault
        self.bare = bare
        self.cfg = cfg
        self.ids = IdentityMaps.load(MEETING_CONFIG_DIR)

    def drop(self, payload, **kw):
        return write_meeting_bundle(self.cfg.meeting_inbox, payload, **kw)

    def receipt(self, occurrence_id):
        return meeting_receipt_path(self.cfg, occurrence_id)

    def run(self, *, now=MEETING_NOW, dry_run=False):
        return run_process_meetings(self.cfg, self.ids, now=now, dry_run=dry_run)

    def managed(self):
        """Every managed vault file with its bytes and mode — what render owns."""
        def entry(path):
            return (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))

        found = {}
        for name in render_module.MANAGED:
            path = self.vault / name
            if path.is_dir():
                found.update({
                    child.relative_to(self.vault).as_posix(): entry(child)
                    for child in path.rglob("*") if child.is_file()
                })
            elif path.is_file():
                found[name] = entry(path)
        return found


@pytest.fixture
def bench(tmp_path):
    vault, bare, branch = _vault(tmp_path)
    inbox = tmp_path / "meeting-inbox"
    inbox.mkdir(mode=0o700)      # the producer's drop point is private, as deployed
    cfg = Config.load(env={
        "TEAMMEM_DB": str(tmp_path / "ledger.db"),
        "TEAMMEM_CONFIG_DIR": str(MEETING_CONFIG_DIR),
        "TEAMMEM_VAULT": str(vault),
        "TEAMMEM_MEETING_INBOX": str(inbox),
        "TEAMMEM_MEETING_ARCHIVE": str(tmp_path / "meeting-archive"),
        "TEAMMEM_MEETING_QUARANTINE": str(tmp_path / "meeting-quarantine"),
        "TEAMMEM_MEETING_AUDIO_STORE": str(tmp_path / "meeting-audio"),
        "TEAMMEM_MEETING_RECEIPTS": str(tmp_path / "meeting-receipts"),
        "TEAMMEM_MEETING_ALERTS": str(tmp_path / "meeting-alerts"),
        "TEAMMEM_VAULT_WEB_BASE": MEETING_WEB_BASE,
        "TEAMMEM_VAULT_REF": f"refs/heads/{branch}",
    })
    insert_events(open_db(cfg.db_path), ORDINARY)
    return _Bench(tmp_path, branch, vault, bare, cfg)


def test_process_meetings_imports_renders_and_publishes_without_approval(bench):
    payload = _payload()
    bench.drop(payload)

    assert bench.run() == 0

    receipt = json.loads(bench.receipt(payload["occurrence_id"]).read_text())
    assert receipt["status"] == "published"
    assert receipt["occurrence_id"] == payload["occurrence_id"]
    assert receipt["markdown_url"].startswith(MEETING_WEB_BASE + "/-/blob/")
    # the receipt is a claim about the remote, so ask the remote
    assert _remote_ref(bench.bare, bench.branch) == receipt["vault_commit"]
    assert subprocess.run(
        ["git", "-C", str(bench.bare), "cat-file", "-e",
         f"{receipt['vault_commit']}:{receipt['markdown_path']}"],
    ).returncode == 0
    # and the ledger and the audio store hold the imported revision
    assert list_meetings(open_db(bench.cfg.db_path))[0]["occurrence_id"] == (
        payload["occurrence_id"])
    # the vault it published into is a working hub, not a meetings-only tree:
    # ordinary pages are there, and they link weeks the journal has aged out
    person = (bench.vault / "Person" / "Alex Rivera" / "README.md").read_text()
    assert "[merged] Auth middleware fix" in person
    aged = (bench.vault / "Person" / "Alex Rivera" / "Week 2026-07-13-17.md").read_text()
    assert "fix: JWT refresh race" in aged and STALE_WEEK_LINK in aged
    assert not (bench.vault / "Work Journal" / "Week 2026-07-13-17.md").exists()
    assert list(bench.cfg.meeting_audio_store.rglob("audio.enc"))
    assert not list(bench.cfg.meeting_inbox.rglob("meeting.json"))   # consumed


def test_a_second_run_republishes_nothing_and_reissues_no_commit(
    bench, monkeypatch
):
    """A current receipt is the record of publication, so the second run does no
    work at all: `publish_meeting` re-renders and re-sweeps the whole vault, and
    that cost grows with every meeting the ledger has ever held."""
    payload = _payload()
    bench.drop(payload)
    assert bench.run() == 0
    first = bench.receipt(payload["occurrence_id"]).read_bytes()
    commits = _git(bench.vault, "rev-list", "--count", "HEAD")

    calls = []
    published = services_module.publish_meeting
    monkeypatch.setattr(services_module, "publish_meeting",
                        lambda *a, **kw: calls.append(kw["occurrence_id"])
                        or published(*a, **kw))

    assert bench.run() == 0
    assert calls == []                             # nothing was re-rendered
    assert bench.receipt(payload["occurrence_id"]).read_bytes() == first
    assert _git(bench.vault, "rev-list", "--count", "HEAD") == commits


def test_a_malformed_bundle_does_not_block_a_valid_occurrence(bench):
    good = _payload()
    bad = _payload(day=OTHER_DAY)
    bench.drop(good)
    broken = bench.drop(bad)
    (broken / "meeting.json").write_text("{not json", encoding="utf-8")

    assert bench.run() == 1                       # the run reports the bad bundle

    assert bench.receipt(good["occurrence_id"]).exists()
    assert not bench.receipt(bad["occurrence_id"]).exists()
    assert list(bench.cfg.meeting_quarantine.rglob("*.reason.json"))


def test_one_failed_receipt_leaves_it_retryable_and_publishes_the_other(bench):
    first = _payload(day=OTHER_DAY)               # earlier: published first
    second = _payload()
    bench.drop(first)
    bench.drop(second)
    # a real receipt-stage failure: the destination path is already a directory
    blocked = bench.receipt(first["occurrence_id"])
    blocked.parent.mkdir(mode=0o700)
    blocked.mkdir()

    assert bench.run() == 1
    assert blocked.is_dir()                       # no receipt for the first
    assert json.loads(bench.receipt(second["occurrence_id"]).read_text())[
        "occurrence_id"] == second["occurrence_id"]

    blocked.rmdir()
    assert bench.run() == 0                       # still retryable, no new bundle
    assert json.loads(blocked.read_text())["occurrence_id"] == first["occurrence_id"]


def test_a_refused_verify_leaves_nothing_a_later_run_could_commit(bench, monkeypatch):
    """The carried finding: publish renders before it verifies, so a refusal used
    to leave the rejected render in the working tree for the next `commit_all`."""
    published = _payload(day=OTHER_DAY)
    bench.drop(published)
    assert bench.run() == 0
    # a mode the render would not reproduce, so the restore has to carry it
    note = next((bench.vault / "Meetings").rglob("*[[]*.md"))
    note.chmod(0o600)
    before = bench.managed()
    head = _git(bench.vault, "rev-parse", "HEAD")

    refused = _payload()
    bench.drop(refused)
    # a real renderer bug against the new occurrence: its note is written without
    # the project backlinks the ledger says it must have, which the diff cannot
    # see because the re-render is wrong in exactly the same way
    real = meeting_render.project_backlinks
    monkeypatch.setattr(
        meeting_render, "project_backlinks",
        lambda notes: real([n for n in notes
                            if n.occurrence_id != refused["occurrence_id"]]))

    assert bench.run() == 1
    assert not bench.receipt(refused["occurrence_id"]).exists()
    assert bench.managed() == before               # the rejected render is gone
    assert commit_all(bench.vault, "a later daily run") is False
    assert _git(bench.vault, "rev-parse", "HEAD") == head
    assert _remote_ref(bench.bare, bench.branch) == head

    monkeypatch.undo()
    assert bench.run() == 0                        # the occurrence is still retryable
    assert bench.receipt(refused["occurrence_id"]).exists()


def test_dry_run_writes_no_database_vault_audio_receipt_or_commit(bench):
    bench.drop(_payload())
    open_db(bench.cfg.db_path).close()             # an existing ledger to compare

    def snapshot():
        conn = sqlite3.connect(bench.cfg.db_path)
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        rows = {name: conn.execute(f"SELECT * FROM {name}").fetchall()
                for name in tables}
        conn.close()
        files = {}
        for root in (bench.vault, bench.cfg.meeting_audio_store,
                     bench.cfg.meeting_receipts, bench.cfg.meeting_alerts,
                     bench.cfg.meeting_inbox, bench.cfg.meeting_archive,
                     bench.cfg.meeting_quarantine):
            for path in sorted(root.rglob("*")) if root.exists() else ():
                if path.is_file() and ".git/" not in path.as_posix():
                    files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return rows, files, _git(bench.vault, "log", "--format=%H %T"), \
            _remote_ref(bench.bare, bench.branch)

    before = snapshot()
    assert bench.run(dry_run=True) == 0
    assert snapshot() == before


def test_dry_run_legacy_wal_preserves_ledger_and_every_meeting_directory(bench, capsys):
    from tests.test_store import legacy_wal_ledger, ledger_bytes, ledger_logical

    bench.cfg.db_path = bench.tmp_path / "legacy.db"
    bench.drop(_payload())
    def files():
        return {p.relative_to(bench.tmp_path): (p.read_bytes(), p.stat().st_mode)
                for p in bench.tmp_path.rglob("*") if p.is_file()}

    with legacy_wal_ledger(bench.cfg.db_path) as caller:
        logical = ledger_logical(caller)
        before = files()
        for _ in range(2):
            assert bench.run(dry_run=True) == 0
            assert "accepted=1" in capsys.readouterr().out
            assert files() == before
            assert ledger_logical(caller) == logical

        # Caller ownership survives both successful snapshotting and refusal.
        assert run_process_meetings(bench.cfg, bench.ids, now=MEETING_NOW,
                                    dry_run=True, conn=caller) == 0
        assert files() == before
        caller.execute("BEGIN")
        bytes_before = ledger_bytes(bench.cfg.db_path)
        assert run_process_meetings(bench.cfg, bench.ids, now=MEETING_NOW,
                                    dry_run=True, conn=caller) == 2
        assert "transaction" in capsys.readouterr().err
        assert caller.in_transaction
        assert ledger_bytes(bench.cfg.db_path) == bytes_before
        caller.rollback()


def test_dry_run_closes_owned_snapshot_when_processing_fails(bench, monkeypatch):
    captured = []

    def fail_processing(*args, conn, **kwargs):
        captured.append(conn)
        raise RuntimeError("processing failed")

    monkeypatch.setattr(services_module, "execute_process_meetings", fail_processing)
    with pytest.raises(RuntimeError, match="processing failed"):
        bench.run(dry_run=True)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        captured[0].execute("SELECT 1")


def test_a_vault_publishing_to_another_ref_refuses_before_rendering(bench):
    bench.drop(_payload())
    bench.cfg.vault_ref = "refs/heads/somewhere-else"

    assert bench.run() == 2
    assert bench.cfg.meeting_receipts.is_dir()     # prepared, then refused
    assert list(bench.cfg.meeting_receipts.iterdir()) == []
    assert bench.managed() == {}                   # nothing rendered


def test_meeting_directories_are_created_private(bench):
    bench.drop(_payload())
    assert bench.run() == 0
    for path in (bench.cfg.meeting_archive, bench.cfg.meeting_quarantine,
                 bench.cfg.meeting_audio_store, bench.cfg.meeting_receipts,
                 bench.cfg.meeting_alerts):
        assert path.stat().st_mode & 0o077 == 0, path


def test_a_group_readable_meeting_directory_is_refused(bench):
    bench.drop(_payload())
    bench.cfg.meeting_audio_store.mkdir(parents=True)
    bench.cfg.meeting_audio_store.chmod(0o755)

    assert bench.run() == 2
    # the refused set leaves nothing half-made: the exposed directory is the
    # fourth of six, and the ones before it are not created either
    assert not bench.cfg.meeting_receipts.exists()
    assert not bench.cfg.meeting_archive.exists()
    assert not bench.cfg.meeting_quarantine.exists()
    assert bench.managed() == {}


def test_audio_adoption_rechecks_that_the_store_is_outside_the_vault(bench):
    safe_parent = bench.tmp_path / "safe-audio-parent"
    safe_parent.mkdir()
    alias = bench.tmp_path / "audio-parent-alias"
    alias.symlink_to(safe_parent, target_is_directory=True)
    bench.cfg.meeting_audio_store = alias / "store"
    validate_meeting_settings(bench.cfg)
    prepare_meeting_dirs(bench.cfg)

    (safe_parent / "store").rmdir()
    alias.unlink()
    alias.symlink_to(bench.vault, target_is_directory=True)
    payload = _payload()
    bench.drop(payload)
    conn = open_db(bench.cfg.db_path)

    result = execute_process_meetings(
        bench.cfg, bench.ids, now=MEETING_NOW, conn=conn)

    assert result.imported.quarantined == 1
    assert result.imported.details == (
        (payload["occurrence_id"], "quarantined", "path_escape"),
    )
    assert get_meeting(conn, payload["occurrence_id"]) is None
    assert not (bench.vault / "store").exists()
    assert result.receipts == ()


def test_the_result_names_every_occurrence_and_why_it_failed(bench, monkeypatch):
    bench.drop(_payload())
    monkeypatch.setattr(
        services_module, "publish_meeting",
        lambda *a, **kw: (_ for _ in ()).throw(
            PublishRefused("push", "the note was not published: git push exited 1")))
    conn = open_db(bench.cfg.db_path)

    result = execute_process_meetings(bench.cfg, bench.ids, now=MEETING_NOW, conn=conn)

    assert isinstance(result, MeetingRunResult)
    assert result.published == ()
    assert [(f.occurrence_id, f.stage) for f in result.refused] == [
        (_payload()["occurrence_id"], "push")]
    assert result.exit_code == 1


def test_process_meetings_is_refused_when_meetings_are_not_configured(
    tmp_path, capsys
):
    cfg = _cfg(tmp_path)
    assert cfg.meetings_enabled is False
    assert run_process_meetings(cfg, IdentityMaps.load(CONFIG_DIR),
                                now=MEETING_NOW) == 2
    assert "TEAMMEM_MEETING_INBOX" in capsys.readouterr().err


def test_the_daily_render_is_unaffected_by_the_meeting_settings(tmp_path):
    """The daily pipeline neither requires nor reads meeting configuration."""
    cfg = _cfg(tmp_path)
    _seed(cfg)
    ids = IdentityMaps.load(CONFIG_DIR)
    assert run_render(cfg, ids, today=date(2026, 7, 16)) == 0
    without = {p.relative_to(cfg.vault_dir).as_posix(): p.read_bytes()
               for p in cfg.vault_dir.rglob("*")
               if p.is_file() and ".git/" not in p.as_posix()}

    other = tmp_path / "second"
    other.mkdir()
    meeting_cfg = _cfg(other, **{
        "TEAMMEM_DB": str(cfg.db_path),
        "TEAMMEM_VAULT": str(other / "vault"),
        "TEAMMEM_MEETING_INBOX": str(other / "meeting-inbox"),
        "TEAMMEM_MEETING_ARCHIVE": str(other / "meeting-archive"),
        "TEAMMEM_MEETING_QUARANTINE": str(other / "meeting-quarantine"),
        "TEAMMEM_MEETING_AUDIO_STORE": str(other / "meeting-audio"),
        "TEAMMEM_MEETING_RECEIPTS": str(other / "meeting-receipts"),
        "TEAMMEM_MEETING_ALERTS": str(other / "meeting-alerts"),
        "TEAMMEM_VAULT_WEB_BASE": MEETING_WEB_BASE,
    })
    assert run_render(meeting_cfg, ids, today=date(2026, 7, 16)) == 0
    with_meetings = {p.relative_to(meeting_cfg.vault_dir).as_posix(): p.read_bytes()
                     for p in meeting_cfg.vault_dir.rglob("*")
                     if p.is_file() and ".git/" not in p.as_posix()}
    assert with_meetings == without


def test_the_alert_path_is_one_file_per_occurrence(bench):
    occurrence = _payload()["occurrence_id"]
    path = meeting_alert_path(bench.cfg, occurrence)
    assert path.parent == bench.cfg.meeting_alerts
    assert path.name == f"{occurrence_path_segment(occurrence)}.json"
    assert ":" not in path.name and "@" not in path.name


def test_a_refused_commit_also_leaves_nothing_a_later_run_could_commit(bench):
    """The same guarantee one stage earlier, driven by a real failing hook: the
    render is on disk, git refused it, and nothing may carry it forward."""
    published = _payload(day=OTHER_DAY)
    bench.drop(published)
    assert bench.run() == 0
    before = bench.managed()
    head = _git(bench.vault, "rev-parse", "HEAD")

    hook = bench.vault / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o700)
    bench.drop(_payload())

    assert bench.run() == 1
    hook.unlink()
    assert bench.managed() == before
    assert commit_all(bench.vault, "a later daily run") is False
    assert _git(bench.vault, "rev-parse", "HEAD") == head


def test_a_commit_that_landed_is_never_rolled_back_by_a_later_refusal(bench):
    """The other half of the rule: once HEAD moves the content is committed, and
    restoring the working tree would stage a deletion rather than undo a write."""
    bench.drop(_payload())
    receipts = bench.cfg.meeting_receipts
    receipts.mkdir(mode=0o700)
    (receipts / f"{occurrence_path_segment(_payload()['occurrence_id'])}.json").mkdir()

    assert bench.run() == 1                        # refused at the receipt stage
    assert _git(bench.vault, "status", "--porcelain") == ""
    assert (bench.vault / "Meetings").is_dir()     # the published note is still there


def test_a_sound_occurrence_publishes_without_its_invalid_neighbour(
    bench, monkeypatch
):
    """The published commit is the verified projection, not the earlier render.

    One note is genuinely rendered without its project backlinks. The real
    checker withholds that occurrence, while the sound neighbour reaches a
    receipt whose exact local and remote trees do not carry the invalid note.
    """
    broken = _payload(day=OTHER_DAY)             # earlier: attempted first
    sound = _payload()
    bench.drop(broken)
    bench.drop(sound)

    real = meeting_render.project_backlinks
    monkeypatch.setattr(
        meeting_render, "project_backlinks",
        lambda notes: real([n for n in notes
                            if n.occurrence_id != broken["occurrence_id"]]))

    assert bench.run() == 1

    assert not bench.receipt(broken["occurrence_id"]).exists()
    receipt = json.loads(bench.receipt(sound["occurrence_id"]).read_text())
    assert receipt["occurrence_id"] == sound["occurrence_id"]
    conn = open_db(bench.cfg.db_path)
    broken_path = str(render_module._meeting_facts(
        get_meeting(conn, broken["occurrence_id"]), bench.ids)["rel"])
    sound_path = receipt["markdown_path"]
    commit = receipt["vault_commit"]

    assert _git(bench.vault, "rev-parse", "HEAD") == commit
    assert _remote_ref(bench.bare, bench.branch) == commit
    assert (bench.vault / sound_path).is_file()
    assert not (bench.vault / broken_path).exists()
    for repository in (bench.vault, bench.bare):
        assert subprocess.run(
            ["git", "-C", str(repository), "cat-file", "-e",
             f"{commit}:{sound_path}"], capture_output=True).returncode == 0
        assert subprocess.run(
            ["git", "-C", str(repository), "cat-file", "-e",
             f"{commit}:{broken_path}"], capture_output=True).returncode != 0

    monkeypatch.undo()                           # the renderer bug is fixed
    assert bench.run() == 0
    assert json.loads(bench.receipt(broken["occurrence_id"]).read_text())[
        "occurrence_id"] == broken["occurrence_id"]


def test_an_unattributable_finding_blocks_every_occurrence(bench):
    """Leaked content could have come from any revision, so nothing publishes
    while it is in the vault — and the file is never read to say what leaked."""
    first = _payload(day=OTHER_DAY)
    second = _payload()
    bench.drop(first)
    bench.drop(second)
    sentence = first["transcript"][0]["text"]
    (bench.vault / "Docs" / "jottings.md").write_text(
        f"# jottings\n\n{sentence}\n", encoding="utf-8")
    before = _remote_ref(bench.bare, bench.branch)

    assert bench.run() == 1

    assert not any(bench.cfg.meeting_receipts.iterdir())
    assert _remote_ref(bench.bare, bench.branch) == before


def test_an_all_short_transcript_copied_into_the_summary_is_not_published(
    bench, capsys
):
    payload = _short_transcript_payload()
    payload["summary"] = "Status:\n" + "\n".join(SHORT_TURNS)
    bench.drop(payload)
    before = _remote_ref(bench.bare, bench.branch)

    assert bench.run() == 1

    captured = capsys.readouterr()
    assert "meeting.leakage.forbidden-content" in captured.err
    assert all(turn not in captured.err for turn in SHORT_TURNS)
    assert not bench.receipt(payload["occurrence_id"]).exists()
    assert _remote_ref(bench.bare, bench.branch) == before


# --- the render path is gated too --------------------------------------------
# `MANAGED` carries `Meetings`, so `teammem render` and the daily pipeline render
# meeting notes, commit them and push them. The publish gate sits on
# `process-meetings` alone, so without a gate here the remote can carry meeting
# content no check has ever read.

MEETING_TODAY = date(2026, 9, 2)


def _render(bench, capsys, **kw):
    code = run_render(bench.cfg, bench.ids, today=MEETING_TODAY,
                      push_requested=True, **kw)
    return code, capsys.readouterr()


def _remote_holds(bench, path: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(bench.bare), "cat-file", "-e",
         f"{_remote_ref(bench.bare, bench.branch)}:{path}"],
    ).returncode == 0


def _leaking(text: str) -> dict:
    """A bundle whose summary quotes a transcript line: legitimate free text in
    the one note field that must carry it, holding what must never be published."""
    payload = _payload()
    payload["summary"] = f"{payload['summary']}\n\n{text}"
    return payload


def test_the_daily_render_withholds_meeting_content_that_fails_the_checks(
    bench, capsys
):
    leak = _payload()["transcript"][0]["text"]
    payload = _leaking(leak)
    bench.drop(payload)
    assert bench.run() == 1                     # publication refuses, as it did

    code, captured = _render(bench, capsys)

    assert code == 0                            # the daily run is not stopped
    assert payload["occurrence_id"] in captured.err
    assert "meeting.leakage.forbidden-content" in captured.err
    assert leak not in captured.err             # never quote what leaked
    assert not list((bench.vault / "Meetings").rglob("*.md"))[1:]
    assert leak not in "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted(bench.vault.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(bench.vault).parts)
    assert not _remote_holds(bench, f"Meetings/{WEEK}/{NOTE}")
    # and everything the vault always published still went out
    assert _remote_holds(bench, "Person/Alex Rivera/README.md")
    assert _remote_holds(bench, "Work Journal/Week 2026-08-31-04.md")


def test_a_withheld_meeting_publishes_once_its_next_revision_is_clean(bench, capsys):
    leak = _payload()["transcript"][0]["text"]
    bench.drop(_leaking(leak))
    assert bench.run() == 1
    assert _render(bench, capsys)[0] == 0
    assert not _remote_holds(bench, f"Meetings/{WEEK}/{NOTE}")

    corrected = _payload()
    corrected["revision"] = corrected["revision"] + 1
    bench.drop(corrected)
    assert bench.run() == 0

    code, captured = _render(bench, capsys)
    assert code == 0
    assert corrected["occurrence_id"] not in captured.err
    assert (bench.vault / "Meetings" / WEEK / NOTE).is_file()
    assert _remote_holds(bench, f"Meetings/{WEEK}/{NOTE}")


def test_a_finding_no_meeting_owns_stops_the_commit_and_the_push(bench, capsys):
    """A transcript line in an unmanaged file is a leak no render can withdraw,
    so the vault does not publish at all until an operator clears it."""
    bench.drop(_payload())
    assert bench.run() == 0
    before = _remote_ref(bench.bare, bench.branch)
    (bench.vault / "Docs").mkdir(exist_ok=True)
    (bench.vault / "Docs" / "jottings.md").write_text(
        _payload()["transcript"][0]["text"] + "\n", encoding="utf-8")

    code, captured = _render(bench, capsys)

    assert code == 1
    assert "nothing committed or pushed" in captured.err
    assert _remote_ref(bench.bare, bench.branch) == before
