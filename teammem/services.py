"""Callable hub services shared by the CLI and the daily workflow."""

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import partial
from pathlib import Path

from .config import (MEETING_PATH_SETTINGS, Config, _physical_path, _path_within,
                     validate_meeting_settings)
from .connectors.base import CollectionResult, Connector
from .connectors.config import ConnectorSettings
from .connectors.registry import get_connector
from .docs_sync import sync_docs
from .events import Event
from .identity import IdentityMaps, _read
from .meeting_bundles import occurrence_path_segment
from .meeting_importer import (MeetingImportResult, import_meeting_inbox,
                               sync_source_cleanup_alerts)
from .meeting_publish import PublishRefused, publish_meeting
from .meeting_retention import RetentionResult, expire_meeting_audio
from .meeting_store import list_meetings
from .queries import report_context, week_label, week_monday
from .reclaim import reclaim, reclaim_channel_projects
# `_managed_files` is the one definition of what the renderer owns and
# `_upstream` the one definition of where a vault publishes. Spelling either a
# second time here is how two answers to one question drift apart; Task 6 imports
# `render._meeting_facts` across the same boundary for the same reason.
from .render import MANAGED, render_vault_verified, verify_vault
from .render import _managed_files as managed_vault_files
from .run_lock import RunLockedError, acquire_publication_lock
from .slices import active_person_days
from .store import get_summary, insert_events, open_db, open_db_snapshot, reconcile_gitlab_events
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
from .vaultgit import (PushNotVerified, VerifiedTreeMismatch, commit_all, ensure_repo,
                       head_commit, push)
from .vaultgit import _upstream as vault_upstream


@dataclass(frozen=True)
class CollectionRun:
    fetched: int
    inserted: int
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
    if dry_run:
        if emit:
            for event in events:
                print(
                    f"DRY {event.ts}  {event.person:<28} {event.kind:<7} "
                    f"{event.project or '-':<18} {event.summary}"
                )
            print(f"dry-run: {len(events)} events, nothing written")
        inserted = 0
    else:
        connection = conn or open_db(cfg.db_path)
        reclaim(connection, ids)
        if name == "gitlab":
            inserted = reconcile_gitlab_events(connection, events, ids)
        else:
            inserted = insert_events(connection, events)
        if emit:
            print(f"ingested: {inserted} new / {len(events)} fetched -> {cfg.db_path}")
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
    try:
        with acquire_publication_lock(cfg.vault_dir):
            out = sync_docs(
                _read(cfg.config_dir, "projects"),
                cfg.obsidian_projects,
                cfg.vault_dir,
            )
    except RunLockedError as error:
        print(f"docs-sync: {error}", file=sys.stderr)
        return 1
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
    try:
        with (nullcontext() if dry_run or verify
              else acquire_publication_lock(cfg.vault_dir)):
            return _run_render(
                cfg, ids, today=today, weeks=weeks, push_requested=push_requested,
                dry_run=dry_run, verify=verify, conn=conn)
    except RunLockedError as error:
        print(f"render: {error}", file=sys.stderr)
        return 1


def _run_render(
    cfg: Config,
    ids: IdentityMaps,
    *,
    today: date,
    weeks: int,
    push_requested: bool,
    dry_run: bool,
    verify: bool,
    conn: sqlite3.Connection | None,
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
    # Verified, not bare: this path commits and pushes what it renders, and no
    # meeting content may reach the remote that the meeting invariants have not
    # read. A meeting that fails them is withheld and reported; the rest of the
    # vault renders, commits and pushes exactly as it always did.
    out = render_vault_verified(
        connection,
        ids,
        cfg.vault_dir,
        today,
        weeks=weeks,
        channel_names=channel_names,
    )
    for occurrence_id, reason in sorted(out["withheld"].items()):
        print(f"WARN: meeting {occurrence_id} withheld from the vault: {reason}",
              file=sys.stderr)
    if out["findings"]:
        # Named by invariant and path only, never by content: reporting a leak
        # must not copy it into the operator's log.
        for finding in out["findings"]:
            print(f"WARN: {finding['invariant']} on {finding['path']}",
                  file=sys.stderr)
        print(f"render: {len(out['findings'])} meeting invariant finding(s) no "
              "meeting could be withheld for; nothing committed or pushed",
              file=sys.stderr)
        return 1
    try:
        committed = commit_all(
            cfg.vault_dir,
            f"render: {today.isoformat()} {out['week_label']} ({out['files']} files)",
            verified_manifest=out["verified_manifest"],
        )
    except VerifiedTreeMismatch as error:
        print(f"render: {error}; nothing pushed", file=sys.stderr)
        return 1
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


# --- meetings ----------------------------------------------------------------
# `process-meetings` is the unattended path: ready bundles in, receipts out, no
# approval anywhere in it. Every occurrence is processed on its own, so one bad
# bundle and one refused publication cost exactly one meeting.

@dataclass(frozen=True)
class MeetingFailure:
    occurrence_id: str
    stage: str
    detail: str


@dataclass(frozen=True)
class MeetingRunResult:
    imported: MeetingImportResult
    receipts: tuple[dict, ...] = ()
    refused: tuple[MeetingFailure, ...] = ()
    pending: tuple[str, ...] = ()      # dry run: what a real run would publish

    @property
    def published(self) -> tuple[str, ...]:
        return tuple(receipt["occurrence_id"] for receipt in self.receipts)

    @property
    def exit_code(self) -> int:
        """A quarantined bundle is a failure too: the producer must see it."""
        return int(bool(self.refused or self.imported.quarantined or self.imported.cleanup_pending))


def meeting_receipt_path(cfg: Config, occurrence_id: str) -> Path:
    """The one receipt file for an occurrence. Its presence is the publication."""
    return cfg.meeting_receipts / f"{occurrence_path_segment(occurrence_id)}.json"


def meeting_alert_path(cfg: Config, occurrence_id: str) -> Path:
    """The one operator-alert file for an occurrence, for retention warnings."""
    return cfg.meeting_alerts / f"{occurrence_path_segment(occurrence_id)}.json"


def prepare_meeting_dirs(cfg: Config, *, create: bool = True, fields=MEETING_PATH_SETTINGS) -> None:
    """Create the meeting directories privately, or refuse an exposed one.

    Meeting bundles carry transcripts and the audio store carries recordings, so
    none of these may be group- or world-accessible. They are created here at
    0700 rather than left to the importer, which would otherwise make them with
    the process umask and fail this check on the next run.

    Every path is judged before any is created, so a set that is refused leaves
    nothing half-made behind.
    """
    absent = []
    for field, key in fields:
        path = getattr(cfg, field)
        if path is None:
            raise ValueError(f"{key} is not set")
        if path.is_symlink():
            raise ValueError(f"{key} must not be a symlink: {path}")
        if not path.exists():
            absent.append(path)
            continue
        if not path.is_dir():
            raise ValueError(f"{key} must be a directory: {path}")
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError(
                f"{key} must not be group- or world-accessible: {path}"
            )
    if not create:
        return
    for path in absent:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)


def _check_publication_target(cfg: Config) -> None:
    """The vault must publish to the remote and ref the receipts will name.

    A receipt says "this commit is on the Team Vault". A vault quietly tracking
    some other remote would make that sentence false while every git command
    still succeeded, so the declared target is checked once per run.
    """
    try:
        remote, ref, _branch = vault_upstream(cfg.vault_dir)
    except (PushNotVerified, subprocess.CalledProcessError) as failure:
        raise ValueError(
            f"the vault at {cfg.vault_dir} has no publication target: {failure}"
        ) from failure
    if (remote, ref) != (cfg.vault_remote, cfg.vault_ref):
        raise ValueError(
            f"the vault publishes to {remote} {ref}, but TEAMMEM_VAULT_REMOTE and "
            f"TEAMMEM_VAULT_REF declare {cfg.vault_remote} {cfg.vault_ref}"
        )


def _utc_day(now: str) -> date:
    moment = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("--now must carry a UTC offset, e.g. 2026-09-02T16:00:00Z")
    return moment.astimezone(timezone.utc).date()


def _receipt_is_current(path: Path, meeting: dict) -> bool:
    """Has this exact revision already been published? Anything unreadable,
    unparseable or stale is republished rather than trusted."""
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(receipt, dict)
        and receipt.get("status") == "published"
        and receipt.get("revision") == meeting["current_revision"]
        and receipt.get("artifact_hash") == meeting["artifact_hash"]
    )


def _vault_head(vault_dir: Path) -> str | None:
    """HEAD, or None for a repository that has never committed."""
    try:
        return head_commit(vault_dir)
    except subprocess.CalledProcessError:
        return None


def _managed_snapshot(vault_dir: Path) -> dict[str, tuple[bytes, int]]:
    """Bytes and mode, because a re-render writes at the process umask and an
    operator who tightened a managed file must get that file back as it was."""
    snapshot = {}
    for rel in managed_vault_files(vault_dir):
        path = vault_dir / rel
        snapshot[rel] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    return snapshot


def _restore_managed(vault_dir: Path, snapshot: dict[str, tuple[bytes, int]]) -> None:
    """Put the vault's managed projection back exactly as it was.

    `publish_meeting` renders before it verifies, so a refusal at the verify gate
    leaves the rejected content in the working tree, where the next `commit_all`
    — this command's or the daily run's — would commit and push exactly what the
    gate rejected. Restoring the snapshot is the same wipe-and-rewrite
    `render_vault` performs, so it touches only what the renderer owns and leaves
    `Docs/` and every other operator input alone.

    The caller restores only when HEAD did not move, so this never runs against
    content a refused attempt had already committed, where deleting the working
    copy would stage a deletion instead of undoing a write.
    """
    for name in MANAGED:
        path = vault_dir / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    for rel, (blob, mode) in snapshot.items():
        path = vault_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        os.chmod(path, mode)


def execute_process_meetings(
    cfg: Config,
    ids: IdentityMaps,
    *,
    now: str,
    conn: sqlite3.Connection,
    dry_run: bool = False,
    weeks: int = 4,
    channel_names: dict | None = None,
) -> MeetingRunResult:
    """Import every ready bundle, then publish every occurrence without a receipt.

    Each occurrence is independent: a refusal is recorded and the loop continues,
    so one malformed bundle or one unreachable remote cannot keep another
    meeting's note out of the vault.
    """
    today = _utc_day(now)
    imported = import_meeting_inbox(
        conn, cfg.meeting_inbox, cfg.meeting_archive, cfg.meeting_quarantine,
        cfg.meeting_audio_store, vault_dir=cfg.vault_dir, now=now,
        dry_run=dry_run,
    )
    receipts: list[dict] = []
    refused = [] if dry_run else [MeetingFailure(occurrence_id, "source_cleanup_alert",
                              "source cleanup alert update failed; retry required")
               for occurrence_id, _ in sync_source_cleanup_alerts(
                   conn, alert_path=partial(meeting_alert_path, cfg), now=now)]
    try:
        with (nullcontext() if dry_run else acquire_publication_lock(cfg.vault_dir)):
            # Refresh after acquiring: another publisher may have issued receipts
            # while this process imported or waited for the shared vault.
            unpublished = [
                meeting["occurrence_id"] for meeting in list_meetings(conn)
                if not _receipt_is_current(
                    meeting_receipt_path(cfg, meeting["occurrence_id"]), meeting)
            ]
            if dry_run:
                return MeetingRunResult(imported=imported, pending=tuple(unpublished))
            for occurrence_id in unpublished:
                head = _vault_head(cfg.vault_dir)
                snapshot = _managed_snapshot(cfg.vault_dir)
                try:
                    receipts.append(publish_meeting(
                        conn, ids, cfg.vault_dir, today,
                        occurrence_id=occurrence_id,
                        receipt_path=meeting_receipt_path(cfg, occurrence_id),
                        web_base=cfg.vault_web_base, weeks=weeks,
                        channel_names=channel_names,
                    ))
                except PublishRefused as refusal:
                    # The snapshot and this restore share the publication lock:
                    # refusal cannot roll back another publisher's valid render.
                    if _vault_head(cfg.vault_dir) == head:
                        _restore_managed(cfg.vault_dir, snapshot)
                    refused.append(MeetingFailure(
                        occurrence_id, refusal.stage, redact_secrets(refusal, cfg)))
    except RunLockedError as error:
        refused.append(MeetingFailure("", "lock", str(error)))
    return MeetingRunResult(
        imported=imported, receipts=tuple(receipts), refused=tuple(refused))


def run_process_meetings(
    cfg: Config,
    ids: IdentityMaps,
    *,
    now: str,
    dry_run: bool = False,
    conn: sqlite3.Connection | None = None,
) -> int:
    """CLI entry: 0 clean, 1 something failed and is retryable, 2 misconfigured."""
    try:
        _utc_day(now)
        validate_meeting_settings(cfg)
        if not cfg.meetings_enabled:
            raise ValueError(
                "TEAMMEM_MEETING_INBOX is not set; this hub processes no meetings"
            )
        prepare_meeting_dirs(cfg, create=not dry_run)
        with (nullcontext() if dry_run else acquire_publication_lock(cfg.vault_dir)):
            if not dry_run:
                ensure_repo(cfg.vault_dir)
            if (cfg.vault_dir / ".git").is_dir():
                _check_publication_target(cfg)
    except RunLockedError as failure:
        print(f"error: {failure}", file=sys.stderr)
        return 1
    except ValueError as failure:
        print(f"error: {redact_secrets(failure, cfg)}", file=sys.stderr)
        return 2

    prefix = "dry-run: " if dry_run else ""
    try:
        connection = (open_db_snapshot(cfg.db_path, conn=conn) if dry_run
                      else conn if conn is not None else open_db(cfg.db_path))
    except ValueError as failure:
        print(f"error: {redact_secrets(failure, cfg)}", file=sys.stderr)
        return 2
    try:
        result = execute_process_meetings(
            cfg, ids, now=now, conn=connection, dry_run=dry_run)
    finally:
        if dry_run or conn is None:
            connection.close()

    imported = result.imported
    print(
        f"{prefix}meetings: accepted={imported.accepted} "
        f"unchanged={imported.unchanged} activated={imported.activated} "
        f"quarantined={imported.quarantined}"
    )
    for name, outcome, reason in imported.details:
        if outcome == "quarantined":
            print(f"{prefix}quarantined {name}: {reason}", file=sys.stderr)
    for occurrence_id, revision in imported.cleanup_pending:
        print(f"{prefix}source_cleanup_pending {occurrence_id} revision {revision}: "
              "retry required; retention is not extended", file=sys.stderr)
    for occurrence_id in result.pending:
        print(f"{prefix}would publish {occurrence_id}")
    for receipt in result.receipts:
        print(f"published {receipt['occurrence_id']} -> {receipt['markdown_url']}")
    for failure in result.refused:
        print(
            f"REFUSED {failure.occurrence_id} at {failure.stage}: {failure.detail}",
            file=sys.stderr,
        )
    return result.exit_code


# --- meeting audio retention -------------------------------------------------
# The deletion deadline cannot wait on anything: this command needs no vault, no
# remote and no network, only the ledger and the audio store.


def execute_expire_meeting_audio(
    cfg: Config,
    *,
    now: str,
    conn: sqlite3.Connection,
    dry_run: bool = False,
    alerts_enabled: bool = True,
) -> RetentionResult:
    """Delete every expired recording and warn before an incomplete one is lost."""
    return expire_meeting_audio(
        conn,
        cfg.meeting_audio_store,
        now=now,
        alert_path=partial(meeting_alert_path, cfg) if alerts_enabled else None,
        warn_days=cfg.meeting_warn_days,
        dry_run=dry_run,
        quarantine=cfg.meeting_quarantine,
    )


def run_expire_meeting_audio(
    cfg: Config,
    *,
    now: str,
    dry_run: bool = False,
    conn: sqlite3.Connection | None = None,
) -> int:
    """CLI entry: 0 clean, 1 an alert or a retryable failure, 2 misconfigured."""
    return _run_expire_meeting_audio(cfg, now=now, dry_run=dry_run, conn=conn)[1]


def _run_expire_meeting_audio(
    cfg: Config, *, now: str, dry_run: bool = False,
    conn: sqlite3.Connection | None = None,
) -> tuple[RetentionResult | None, int]:
    """One diagnosed retention pass, shared by one-shot CLI and Unix worker."""
    try:
        _utc_day(now)
        if cfg.meeting_audio_store is None:
            raise ValueError(
                "TEAMMEM_MEETING_AUDIO_STORE is not set; this hub processes no meetings"
            )
        store = _physical_path(cfg.meeting_audio_store)
        vault = _physical_path(cfg.vault_dir)
        if (not cfg.meeting_audio_store.is_absolute()
                or _path_within(store, vault) or _path_within(vault, store)):
            raise ValueError("TEAMMEM_MEETING_AUDIO_STORE must be absolute and outside the vault")
    except (ValueError, OSError, RuntimeError) as failure:
        print(f"error: {redact_secrets(failure, cfg)}", file=sys.stderr)
        return None, 2

    # Publication settings and metadata spools cannot veto governed deletion.
    # Diagnose them visibly, then enable alert writes only after their own
    # private-path and containment checks have succeeded.
    configuration_errors = []
    try:
        validate_meeting_settings(cfg)
    except (ValueError, OSError) as failure:
        configuration_errors.append(str(failure))
    for field, key in MEETING_PATH_SETTINGS:
        try:
            prepare_meeting_dirs(cfg, create=False, fields=((field, key),))
        except (ValueError, OSError) as failure:
            configuration_errors.append(str(failure))
    alerts_enabled = False
    try:
        path = cfg.meeting_alerts
        if path is None or not path.is_absolute() or ".." in path.parts:
            raise ValueError("TEAMMEM_MEETING_ALERTS must be an absolute path")
        alerts = _physical_path(path)
        for other in [cfg.vault_dir] + [getattr(cfg, field) for field, _ in MEETING_PATH_SETTINGS
                                       if field != "meeting_alerts"]:
            if other is not None:
                physical = _physical_path(other)
                if _path_within(alerts, physical) or _path_within(physical, alerts):
                    raise ValueError("TEAMMEM_MEETING_ALERTS overlaps another configured directory")
        prepare_meeting_dirs(cfg, create=not dry_run,
                             fields=(("meeting_alerts", "TEAMMEM_MEETING_ALERTS"),))
        alerts_enabled = True
    except (ValueError, OSError, RuntimeError) as failure:
        configuration_errors.append(str(failure))
    for failure in dict.fromkeys(configuration_errors):
        print(f"error: {redact_secrets(failure, cfg)}", file=sys.stderr)

    prefix = "dry-run: " if dry_run else ""
    try:
        connection = (open_db_snapshot(cfg.db_path, conn=conn) if dry_run
                      else conn if conn is not None else open_db(cfg.db_path))
    except ValueError as failure:
        print(f"error: {redact_secrets(failure, cfg)}", file=sys.stderr)
        return None, 2
    try:
        result = execute_expire_meeting_audio(cfg, now=now, conn=connection, dry_run=dry_run,
                                             alerts_enabled=alerts_enabled)
    finally:
        if dry_run or conn is None:
            connection.close()

    print(
        f"{prefix}meeting audio: deleted={len(result.deleted)} "
        f"swept={len(result.swept)} alerted={len(result.alerted)} "
        f"cleared={len(result.cleared)} failed={len(result.failed)}"
    )
    deleted, swept = ("would delete", "would sweep") if dry_run else ("deleted", "swept")
    for occurrence_id, revision in result.deleted:
        print(f"{prefix}{deleted} {occurrence_id} revision {revision}")
    for occurrence_id, revision in result.swept:
        print(f"{prefix}{swept} {occurrence_id} revision {revision}")
    for label in result.quarantine_deleted:
        print(f"{prefix}{deleted} quarantined copy {label}")
    for occurrence_id, revision in result.mismatched:
        print(
            f"MISMATCH {occurrence_id} revision {revision}: the deleted audio was "
            "not the bytes this revision recorded",
            file=sys.stderr,
        )
    for occurrence_id in result.cleared:
        print(f"{prefix}{'would clear' if dry_run else 'cleared'} the alert for "
              f"{occurrence_id}")
    for occurrence_id in result.alerted:
        print(
            f"{prefix}ALERT {occurrence_id}: the transcript is incomplete and its "
            "audio expires soon",
            file=sys.stderr,
        )
    for failure in result.failed:
        print(
            f"FAILED {failure.occurrence_id} revision {failure.revision} at "
            f"{failure.stage}: {redact_secrets(failure.detail, cfg)}",
            file=sys.stderr,
        )
    for occurrence_id, revision in result.source_cleanup_pending:
        print(f"{prefix}source_cleanup_pending {occurrence_id} revision {revision}: "
              "retry required; retention is not extended", file=sys.stderr)
    return result, 2 if configuration_errors else result.exit_code
