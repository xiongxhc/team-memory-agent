"""Callable hub services shared by the CLI and the daily workflow."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Config
from .connectors.base import CollectionResult, Connector
from .connectors.config import ConnectorSettings
from .connectors.registry import get_connector
from .docs_sync import sync_docs
from .events import Event
from .identity import IdentityMaps, _read
from .queries import report_context, week_label, week_monday
from .reclaim import reclaim, reclaim_channel_projects
from .render import render_vault, verify_vault
from .slices import active_person_days
from .store import (
    get_summary,
    insert_events,
    open_db,
    reconcile_gitlab_events,
    replace_weekly_commit_counts,
)
from .summarize import (
    DAILY_SYSTEM,
    LLM,
    DailySummaryInput,
    PreparedDailyJournal,
    claude_cli_llm,
    daily_cache_status,
    http_llm,
    prepare_daily_journal,
    put_daily_journal,
    weekly_team_report,
)
from .telemetry import (
    Distribution,
    ProgressEvent,
    Reporter,
    distribution,
    noop_reporter,
)
from .vaultgit import commit_all, ensure_repo, push


@dataclass(frozen=True)
class CollectionRun:
    fetched: int
    inserted: int
    aggregate_rows: int
    aggregate_changes: int
    channel_names: dict[str, str]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class JournalFailure:
    person: str
    day: str
    detail: str


@dataclass(frozen=True)
class JournalMetrics:
    pairs: int
    cached: int
    migrated: int
    llm_calls: int
    concurrency: int
    prompt_events: Distribution
    prompt_bytes: Distribution
    queue_wait_seconds: Distribution
    backend_seconds: Distribution
    elapsed_seconds: float


@dataclass(frozen=True)
class JournalRunResult:
    metrics: JournalMetrics
    failures: tuple[JournalFailure, ...]

    @property
    def failed_person_days(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((failure.person, failure.day) for failure in self.failures))

    @property
    def exit_code(self) -> int:
        return int(bool(self.failures))


@dataclass(frozen=True)
class ReportRunResult:
    target_monday: date
    status: str
    detail: str
    elapsed_seconds: float


@dataclass(frozen=True)
class _JournalCallResult:
    prepared: PreparedDailyJournal
    text: str | None
    error: Exception | None
    queue_wait_seconds: float
    backend_seconds: float


def redact_secrets(detail: object, cfg: Config) -> str:
    """Remove configured credential values from operator-visible details."""
    text = str(detail)
    for secret in (
        cfg.gitlab_token,
        cfg.feishu_app_secret,
        cfg.github_token,
        cfg.slack_bot_token,
        cfg.discord_bot_token,
        cfg.anthropic_api_key,
    ):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def resolve_llm_backend(cfg: Config, model: str, max_tokens: int):
    """Resolve the optional synthesis backend without exposing credentials."""
    if cfg.anthropic_api_key:
        return http_llm(model, cfg.anthropic_api_key, max_tokens=max_tokens)
    import shutil

    if shutil.which("claude"):
        return claude_cli_llm(model)
    return None


def run_collect(
    cfg: Config,
    ids: IdentityMaps,
    events_fn: Callable[[], list[Event]],
    dry_run: bool,
) -> tuple[int, int]:
    """Legacy event-list collection helper retained for import compatibility."""
    events = events_fn()
    if dry_run:
        for event in events:
            print(
                f"DRY {event.ts}  {event.person:<28} {event.kind:<7} "
                f"{event.project or '-':<18} {event.summary}"
            )
        print(f"dry-run: {len(events)} events, nothing written")
        return len(events), 0
    conn = open_db(cfg.db_path)
    inserted = insert_events(conn, events)
    unmapped = sorted({
        event.person for event in events if event.person.startswith("_unmapped/")
    })
    print(f"ingested: {inserted} new / {len(events)} fetched -> {cfg.db_path}")
    if unmapped:
        print(f"UNMAPPED identities (add to roster.yaml): {', '.join(unmapped)}")
    return len(events), inserted


def collect_connector(
    name: str,
    cfg: Config,
    ids: IdentityMaps,
    settings: ConnectorSettings,
    now: datetime,
    dry_run: bool = False,
    *,
    connector: Connector | None = None,
    conn: sqlite3.Connection | None = None,
    emit: bool = True,
) -> CollectionRun:
    """Collect one registered provider and optionally write its normalized events."""
    selected = connector or get_connector(name)
    result: CollectionResult = selected.collect(cfg, ids, settings, now)
    events = list(result.events)
    commit_counts = tuple(result.commit_counts)
    if dry_run:
        if emit:
            for event in events:
                print(
                    f"DRY {event.ts}  {event.person:<28} {event.kind:<7} "
                    f"{event.project or '-':<18} {event.summary}"
                )
            for count in commit_counts:
                print(
                    f"DRY {count.project} {count.week_start} "
                    f"{count.person} {count.commit_count}"
                )
            print(
                f"dry-run: {len(events)} events; "
                f"{len(commit_counts)} aggregate rows, nothing written"
            )
        inserted = 0
        aggregate_changes = 0
    else:
        connection = conn or open_db(cfg.db_path)
        reclaim(connection, ids)
        if name == "gitlab":
            inserted = reconcile_gitlab_events(connection, events, ids)
        else:
            inserted = insert_events(connection, events)
        aggregate_changes = replace_weekly_commit_counts(
            connection,
            result.commit_count_scopes,
            commit_counts,
        )
        if emit:
            print(
                f"ingested: {inserted} new / {len(events)} fetched; "
                f"{len(commit_counts)} aggregate rows / "
                f"{aggregate_changes} changed -> {cfg.db_path}"
            )
            unmapped = sorted({
                event.person
                for event in events
                if event.person.startswith("_unmapped/")
            })
            if unmapped:
                print(
                    "UNMAPPED identities (add to roster.yaml): "
                    + ", ".join(unmapped)
                )
    if emit:
        for warning in result.warnings:
            print(f"WARN {name}: {warning}", file=sys.stderr)
    if not dry_run:
        persist_channel_names(cfg.config_dir, result.channel_names)
    return CollectionRun(
        fetched=len(events),
        inserted=inserted,
        aggregate_rows=len(commit_counts),
        aggregate_changes=aggregate_changes,
        channel_names=dict(result.channel_names),
        warnings=tuple(result.warnings),
    )


def persist_channel_names(config_dir: Path, additions: dict[str, str]) -> None:
    """Merge display metadata via atomic replacement."""
    if not additions:
        return
    path = config_dir / "channel_names.json"
    existing = json.loads(path.read_text()) if path.exists() else {}
    existing.update(additions)
    config_dir.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_dir,
            prefix=".channel_names.json.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(existing, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_docs_sync(cfg: Config) -> int:
    if not cfg.obsidian_projects:
        print("set TEAMMEM_OBSIDIAN_PROJECTS", file=sys.stderr)
        return 2
    out = sync_docs(
        _read(cfg.config_dir, "projects"),
        cfg.obsidian_projects,
        cfg.vault_dir,
    )
    print(
        f"docs-sync: {out['copied']} files updated"
        f" across {out['projects']} projects -> {cfg.vault_dir / 'Docs'}"
    )
    return 0


def run_render(
    cfg: Config,
    ids: IdentityMaps,
    *,
    today: date,
    weeks: int = 4,
    push_requested: bool = False,
    dry_run: bool = False,
    verify: bool = False,
    conn: sqlite3.Connection | None = None,
) -> int:
    if verify and (push_requested or dry_run):
        print("render --verify cannot combine with --push or --dry-run",
              file=sys.stderr)
        return 2
    if dry_run:
        print(
            f"DRY render -> {cfg.vault_dir} ({week_label(week_monday(today))},"
            f" weeks={weeks})"
        )
        return 0
    connection = conn or open_db(cfg.db_path)
    names_file = cfg.config_dir / "channel_names.json"
    channel_names = json.loads(names_file.read_text()) if names_file.exists() else {}
    if verify:
        out = verify_vault(
            connection, ids, cfg.vault_dir, today,
            weeks=weeks, channel_names=channel_names,
        )
        drift = [(label, path)
                 for label, paths in (("MISSING", out["missing"]),
                                      ("UNEXPECTED", out["unexpected"]),
                                      ("DIFFERS", out["differing"]))
                 for path in paths]
        for label, path in drift:
            print(f"verify-render: {label} {path}")
        if drift:
            print(f"verify-render: {len(drift)} managed path(s) drifted from "
                  f"ledger render -> re-run `teammem render` to republish")
            return 1
        print("verify-render: vault matches ledger render")
        return 0
    ensure_repo(cfg.vault_dir)
    out = render_vault(
        connection,
        ids,
        cfg.vault_dir,
        today,
        weeks=weeks,
        channel_names=channel_names,
    )
    committed = commit_all(
        cfg.vault_dir,
        f"render: {today.isoformat()} {out['week_label']} ({out['files']} files)",
    )
    print(
        f"rendered {out['files']} files -> {cfg.vault_dir}"
        f" ({'committed' if committed else 'no changes'})"
    )
    if push_requested or cfg.push:
        try:
            push(cfg.vault_dir)
            print("pushed")
        except subprocess.CalledProcessError as error:
            lines = (error.stderr or "").strip().splitlines()
            detail = redact_secrets(lines[-1] if lines else error, cfg)
            print(
                f"WARN: vault push failed ({detail}); "
                "commits retained for next push",
                file=sys.stderr,
            )
    return 0


def _invoke_daily_llm(
    prepared: PreparedDailyJournal,
    llm: LLM,
    submitted_at: float,
    monotonic: Callable[[], float],
) -> _JournalCallResult:
    started_at = monotonic()
    text = None
    error = None
    try:
        text = llm(DAILY_SYSTEM, prepared.user_prompt)
    except Exception as failure:
        error = failure
    finished_at = monotonic()
    return _JournalCallResult(
        prepared=prepared,
        text=text,
        error=error,
        queue_wait_seconds=started_at - submitted_at,
        backend_seconds=finished_at - started_at,
    )


def _distribution_fields(name: str, value: Distribution) -> list[tuple[str, object]]:
    fields: list[tuple[str, object]] = [(f"{name}_count", value.count)]
    if value.count:
        fields.extend((
            (f"{name}_p50", value.p50),
            (f"{name}_p95", value.p95),
            (f"{name}_max", value.maximum),
        ))
    return fields


def _journal_metric_fields(metrics: JournalMetrics) -> tuple[tuple[str, object], ...]:
    fields: list[tuple[str, object]] = [
        ("pairs", metrics.pairs),
        ("cached", metrics.cached),
        ("migrated", metrics.migrated),
        ("llm_calls", metrics.llm_calls),
        ("concurrency", metrics.concurrency),
    ]
    fields.extend(_distribution_fields("prompt_events", metrics.prompt_events))
    fields.extend(_distribution_fields("prompt_bytes", metrics.prompt_bytes))
    fields.extend(
        _distribution_fields("queue_wait_seconds", metrics.queue_wait_seconds)
    )
    fields.extend(_distribution_fields("backend_seconds", metrics.backend_seconds))
    fields.append(("elapsed_seconds", metrics.elapsed_seconds))
    return tuple(fields)


def execute_journal(
    cfg: Config,
    ids: IdentityMaps,
    *,
    start_day: str,
    end_day: str,
    created_ts: str,
    conn: sqlite3.Connection,
    llm: LLM,
    reporter: Reporter = noop_reporter,
    monotonic: Callable[[], float] = time.monotonic,
) -> JournalRunResult:
    run_started_at = monotonic()
    pairs = active_person_days(conn, start_day, end_day)
    global_projects = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT project FROM events"
            " WHERE project IS NOT NULL ORDER BY project"
        )
    ]
    prepared_journals: list[PreparedDailyJournal] = []
    misses: list[PreparedDailyJournal] = []
    cached = migrated = 0
    for person, day in pairs:
        prepared = prepare_daily_journal(
            conn,
            person,
            ids.display_name(person),
            day,
            global_projects,
        )
        if prepared is None:
            continue
        prepared_journals.append(prepared)
        status, _text = daily_cache_status(conn, prepared)
        if status == "cached":
            cached += 1
        elif status == "migrated":
            migrated += 1
        else:
            misses.append(prepared)

    call_results: list[_JournalCallResult] = []
    if misses:
        with ThreadPoolExecutor(max_workers=cfg.llm_concurrency) as executor:
            futures = {
                executor.submit(
                    _invoke_daily_llm,
                    prepared,
                    llm,
                    monotonic(),
                    monotonic,
                ): prepared
                for prepared in misses
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                call_results.append(future.result())
                reporter(ProgressEvent(
                    "journal-progress",
                    stage="journal",
                    fields=(("completed", completed), ("total", len(misses))),
                ))

    successful = sorted(
        (result for result in call_results if result.error is None),
        key=lambda result: (result.prepared.person, result.prepared.day),
    )
    for result in successful:
        put_daily_journal(
            conn,
            result.prepared,
            result.text,
            cfg.llm_daily_model,
            created_ts,
        )

    failures = tuple(sorted(
        (
            JournalFailure(
                person=result.prepared.person,
                day=result.prepared.day,
                detail=redact_secrets(result.error, cfg),
            )
            for result in call_results
            if result.error is not None
        ),
        key=lambda failure: (failure.person, failure.day),
    ))
    metrics = JournalMetrics(
        pairs=len(prepared_journals),
        cached=cached,
        migrated=migrated,
        llm_calls=len(misses),
        concurrency=cfg.llm_concurrency,
        prompt_events=distribution([
            prepared.event_count for prepared in prepared_journals
        ]),
        prompt_bytes=distribution([
            prepared.prompt_bytes for prepared in prepared_journals
        ]),
        queue_wait_seconds=distribution([
            result.queue_wait_seconds for result in call_results
        ]),
        backend_seconds=distribution([
            result.backend_seconds for result in call_results
        ]),
        elapsed_seconds=monotonic() - run_started_at,
    )
    reporter(ProgressEvent(
        "journal-progress",
        stage="journal",
        fields=_journal_metric_fields(metrics),
    ))
    return JournalRunResult(metrics=metrics, failures=failures)


def run_journal(
    cfg: Config,
    ids: IdentityMaps,
    *,
    today: date,
    day: str | None = None,
    since_days: int = 7,
    dry_run: bool = False,
    conn: sqlite3.Connection | None = None,
    llm=None,
) -> int:
    connection = conn or open_db(cfg.db_path)
    if day:
        start_day = end_day = day
    else:
        start_day = (today - timedelta(days=since_days - 1)).isoformat()
        end_day = today.isoformat()
    if dry_run:
        pairs = active_person_days(connection, start_day, end_day)
        for person, pair_day in pairs:
            cached = connection.execute(
                "SELECT input_hash FROM summaries "
                "WHERE kind = 'daily-person' AND key = ?",
                (f"{person}|{pair_day}",),
            ).fetchone()
            state = "hit" if cached else "miss"
            print(f"DRY journal {person:<28} {pair_day}  {state}")
        print(
            f"dry-run: {len(pairs)} (person, day) pairs, "
            "no LLM calls, nothing written"
        )
        return 0
    backend = llm or resolve_llm_backend(
        cfg, cfg.llm_daily_model, max_tokens=1024
    )
    if backend is None:
        print(
            "no LLM backend: set ANTHROPIC_API_KEY or install the claude CLI",
            file=sys.stderr,
        )
        return 2
    result = execute_journal(
        cfg,
        ids,
        start_day=start_day,
        end_day=end_day,
        created_ts=f"{today.isoformat()}T00:00:00",
        conn=connection,
        llm=backend,
    )
    generated = result.metrics.llm_calls - len(result.failures)
    migration_detail = (
        f", {result.metrics.migrated} migrated"
        if result.metrics.migrated
        else ""
    )
    failure_detail = (
        f", {len(result.failures)} failed" if result.failures else ""
    )
    print(
        f"journals: {generated} generated, {result.metrics.cached} cached"
        f"{migration_detail}{failure_detail}"
        f" ({result.metrics.pairs} pairs, model {cfg.llm_daily_model})"
    )
    for failure in result.failures:
        print(
            f"WARN journal {failure.person} {failure.day}: {failure.detail}",
            file=sys.stderr,
        )
    return result.exit_code


def _report_dailies(
    conn: sqlite3.Connection,
    monday: date,
) -> list[DailySummaryInput]:
    days = {
        (monday + timedelta(days=index)).isoformat()
        for index in range(7)
    }
    dailies = []
    for key, input_hash, text in conn.execute(
        "SELECT key, input_hash, text FROM summaries "
        "WHERE kind = 'daily-person' ORDER BY key"
    ):
        person, day = key.split("|", 1)
        if day in days:
            dailies.append(DailySummaryInput(person, day, input_hash, text))
    return dailies


def execute_report(
    cfg: Config,
    ids: IdentityMaps,
    *,
    target_week: date,
    operator_date: date,
    conn: sqlite3.Connection,
    llm: LLM,
    monotonic: Callable[[], float] = time.monotonic,
) -> ReportRunResult:
    started_at = monotonic()
    monday = week_monday(target_week)
    dailies = _report_dailies(conn, monday)
    if not dailies:
        return ReportRunResult(
            monday,
            "skipped",
            "no daily journals",
            monotonic() - started_at,
        )

    try:
        context = report_context(
            conn,
            monday,
            operator_date,
            ids,
            {(daily.person, daily.day) for daily in dailies},
        )
        key = f"team|{monday.isoformat()}"
        existing = get_summary(conn, "weekly-team", key)
        record = weekly_team_report(
            conn,
            monday_iso=monday.isoformat(),
            dailies=dailies,
            context=context,
            llm=llm,
            model=cfg.llm_report_model,
            created_ts=f"{operator_date.isoformat()}T00:00:00",
        )
        status = (
            "cached"
            if existing is not None and existing.input_hash == record.input_hash
            else "generated"
        )
        detail = f"{len(dailies)} dailies"
    except Exception as failure:
        status = "failed"
        detail = redact_secrets(failure, cfg)
    return ReportRunResult(
        monday,
        status,
        detail,
        monotonic() - started_at,
    )


def run_report(
    cfg: Config,
    ids: IdentityMaps,
    *,
    base: date,
    dry_run: bool = False,
    conn: sqlite3.Connection | None = None,
    llm=None,
) -> int:
    connection = conn or open_db(cfg.db_path)
    monday = week_monday(base)
    dailies = _report_dailies(connection, monday)
    if not dailies:
        print(
            f"no daily journals cached for {week_label(monday)};"
            " run `teammem journal` first",
            file=sys.stderr,
        )
        return 0
    if dry_run:
        cached = connection.execute(
            "SELECT 1 FROM summaries WHERE kind='weekly-team' AND key=?",
            (f"team|{monday.isoformat()}",),
        ).fetchone()
        print(
            f"DRY report {week_label(monday)}: {len(dailies)} dailies,"
            f" {'hit' if cached else 'miss'}"
        )
        return 0
    backend = llm or resolve_llm_backend(
        cfg, cfg.llm_report_model, max_tokens=8192
    )
    if backend is None:
        print(
            "no LLM backend: set ANTHROPIC_API_KEY or install the claude CLI",
            file=sys.stderr,
        )
        return 2
    result = execute_report(
        cfg,
        ids,
        target_week=base,
        operator_date=date.today(),
        conn=connection,
        llm=backend,
    )
    if result.status == "failed":
        print(
            f"report: failed {week_label(result.target_monday)} — {result.detail}",
            file=sys.stderr,
        )
        return 1
    print(
        f"report: {result.status} {week_label(monday)}"
        f" from {len(dailies)} dailies (model {cfg.llm_report_model})"
    )
    return 0
