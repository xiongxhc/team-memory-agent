"""Portable, run-once orchestration for an operator-controlled hub."""

import io
import os
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TypeAlias

from .config import Config
from .connectors.base import Connector
from .connectors.config import ConnectorSettings
from .connectors.registry import get_connector
from .identity import IdentityMaps
from .importer import import_inbox
from .reclaim import (
    reclaim,
    reclaim_channel_projects,
    reclaim_repository_projects,
)
from .run_lock import acquire_run_lock
from .services import (
    collect_connector,
    execute_journal,
    execute_report,
    redact_secrets,
    resolve_llm_backend,
    run_docs_sync,
    run_render,
)
from .store import open_db
from .telemetry import ProgressEvent, Reporter, noop_reporter
from .vaultgit import push


@dataclass(frozen=True)
class StepResult:
    name: str
    status: str
    detail: str = ""
    warnings: tuple[str, ...] = ()
    subresults: tuple["StepResult", ...] = ()


@dataclass(frozen=True)
class DailyResult:
    steps: tuple[StepResult, ...]
    exit_code: int

    def step(self, name: str) -> StepResult:
        for step in self.steps:
            if step.name == name:
                return step
        raise KeyError(name)

    def status(self, name: str) -> str:
        return self.step(name).status


_LOCAL_STAGES = (
    "import",
    "reclaim",
    "journal",
    "report",
    "docs-sync",
    "render",
    "push",
    "snapshot",
)
_FATAL_STAGES = frozenset({"lock", "ledger", "reclaim", "render", "snapshot"})
StageFields: TypeAlias = tuple[tuple[str, object], ...]
StageOutcome: TypeAlias = StepResult | tuple[StepResult, StageFields]


@dataclass
class _Run:
    steps: list[StepResult]
    reporter: Reporter
    monotonic: Callable[[], float]

    def start(self, name: str) -> float:
        started_at = self.monotonic()
        self.reporter(ProgressEvent("stage-start", stage=name))
        return started_at

    def finish(
        self,
        started_at: float,
        step: StepResult,
        fields: StageFields = (),
    ) -> StepResult:
        self.steps.append(step)
        self.reporter(ProgressEvent(
            "stage-end",
            stage=step.name,
            fields=(
                ("status", step.status),
                *fields,
                ("elapsed_seconds", self.monotonic() - started_at),
            ),
        ))
        return step

    def timed(self, name: str, action: Callable[[], StageOutcome]) -> StepResult:
        started_at = self.start(name)
        outcome = action()
        step, fields = outcome if isinstance(outcome, tuple) else (outcome, ())
        if step.name != name:
            raise ValueError("stage result name mismatch")
        return self.finish(started_at, step, fields)

    def skip(self, names: Iterable[str], detail: str) -> None:
        for name in names:
            self.timed(
                name,
                lambda name=name: StepResult(name, "skipped", detail),
            )


def _daily_result(
    steps: list[StepResult], *, capture_only: bool,
    source_stages: frozenset[str],
) -> DailyResult:
    fatal_stages = _FATAL_STAGES
    if capture_only:
        fatal_stages = fatal_stages | source_stages | {"import"}
    exit_code = int(any(
        step.name in fatal_stages and step.status == "failed" for step in steps
    ))
    return DailyResult(tuple(steps), exit_code)


def _service_result(
    name: str,
    cfg: Config,
    function,
    *args,
    **kwargs,
) -> StepResult:
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = function(*args, **kwargs)
    except Exception as error:
        return StepResult(name, "failed", redact_secrets(error, cfg))
    detail = "\n".join(
        part.strip() for part in (stdout.getvalue(), stderr.getvalue())
        if part.strip()
    )
    return StepResult(
        name,
        "ok" if code == 0 else "failed",
        redact_secrets(detail or f"exit {code}", cfg),
    )


def _report_result(
    run: _Run,
    cfg: Config,
    ids: IdentityMaps,
    conn: sqlite3.Connection,
    local_day: date,
    previous_monday: date,
    current_monday: date,
    report_llm,
    backend_error: Exception | None,
    failed_person_days: tuple[tuple[str, str], ...],
    journal_unavailable: bool,
) -> StepResult:
    failed_weeks = {
        parsed - timedelta(days=parsed.weekday())
        for _person, day in failed_person_days
        for parsed in (date.fromisoformat(day),)
    }
    subresults: list[StepResult] = []
    for label, target in (
        ("previous", previous_monday), ("current", current_monday)
    ):
        target_iso = target.isoformat()
        started_at = run.monotonic()
        if backend_error is not None:
            detail = redact_secrets(backend_error, cfg)
            subresult = StepResult(label, "failed", detail)
        elif report_llm is None:
            subresult = StepResult(label, "skipped", "no LLM backend")
        elif journal_unavailable:
            subresult = StepResult(label, "skipped", "journal failed")
        elif target in failed_weeks:
            subresult = StepResult(
                label, "skipped", "journal failed for target week"
            )
        else:
            try:
                result = execute_report(
                    cfg,
                    ids,
                    target_week=target,
                    operator_date=local_day,
                    conn=conn,
                    llm=report_llm,
                    monotonic=run.monotonic,
                )
                subresult = StepResult(label, result.status, result.detail)
            except Exception as error:
                subresult = StepResult(label, "failed", redact_secrets(error, cfg))
        subresult = replace(
            subresult,
            detail=f"{target_iso}: {subresult.detail}"
            if subresult.detail
            else target_iso,
        )
        subresults.append(subresult)
        run.reporter(ProgressEvent(
            "report-progress", stage="report", fields=(
                ("target_week", target_iso), ("status", subresult.status),
                ("elapsed_seconds", run.monotonic() - started_at),
            )
        ))

    status = (
        "failed"
        if any(result.status == "failed" for result in subresults)
        else "skipped"
        if all(result.status == "skipped" for result in subresults)
        else "ok"
    )
    detail = "; ".join(
        f"{result.name}={result.status}: {result.detail}" for result in subresults
    )
    return StepResult("report", status, detail, subresults=tuple(subresults))


def _snapshot(
    conn: sqlite3.Connection,
    directory: Path,
    day: str,
    retain: int = 14,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"ledger-{day}.db"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory,
            prefix=f".{destination.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        backup = sqlite3.connect(temporary)
        try:
            conn.backup(backup)
        finally:
            backup.close()
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    snapshots = sorted(directory.glob("ledger-*.db"), reverse=True)
    for stale in snapshots[retain:]:
        stale.unlink()
    return destination


def _run_full_stages(
    run: _Run,
    cfg: Config,
    ids: IdentityMaps,
    conn: sqlite3.Connection,
    now: datetime,
) -> None:
    local_day = now.date()
    current_monday = local_day - timedelta(days=local_day.weekday())
    previous_monday = current_monday - timedelta(days=7)
    daily_llm = None
    failed_person_days: tuple[tuple[str, str], ...] = ()
    journal_unavailable = False

    def journal_action() -> StepResult:
        nonlocal daily_llm, failed_person_days, journal_unavailable
        try:
            daily_llm = resolve_llm_backend(cfg, cfg.llm_daily_model, 1024)
            if daily_llm is None:
                journal_unavailable = True
                return StepResult("journal", "skipped", "no LLM backend")
            result = execute_journal(
                cfg,
                ids,
                start_day=previous_monday.isoformat(),
                end_day=local_day.isoformat(),
                created_ts=now.isoformat(),
                conn=conn,
                llm=daily_llm,
                reporter=run.reporter,
                monotonic=run.monotonic,
            )
        except Exception as error:
            journal_unavailable = True
            return StepResult("journal", "failed", redact_secrets(error, cfg))
        failed_person_days = result.failed_person_days
        if failed_person_days:
            return StepResult(
                "journal",
                "failed",
                f"{len(failed_person_days)} person-days failed",
            )
        return StepResult("journal", "ok")

    run.timed("journal", journal_action)

    def report_action() -> StepResult:
        backend_error = None
        report_llm = None
        if daily_llm is not None:
            try:
                report_llm = resolve_llm_backend(
                    cfg, cfg.llm_report_model, 8192
                )
            except Exception as error:
                backend_error = error
        return _report_result(
            run,
            cfg,
            ids,
            conn,
            local_day,
            previous_monday,
            current_monday,
            report_llm,
            backend_error,
            failed_person_days,
            journal_unavailable,
        )

    run.timed("report", report_action)
    if cfg.obsidian_projects is None:
        run.skip(("docs-sync",), "TEAMMEM_OBSIDIAN_PROJECTS not set")
    else:
        run.timed(
            "docs-sync",
            lambda: _service_result("docs-sync", cfg, run_docs_sync, cfg),
        )

    render_step = run.timed(
        "render",
        lambda: _service_result(
            "render",
            cfg,
            run_render,
            replace(cfg, push=False),
            ids,
            today=local_day,
            conn=conn,
        ),
    )

    def push_action() -> StepResult:
        if not cfg.push:
            return StepResult("push", "skipped", "TEAMMEM_PUSH not enabled")
        if render_step.status != "ok":
            return StepResult("push", "skipped", "render failed")
        try:
            push(cfg.vault_dir)
        except Exception as error:
            return StepResult("push", "failed", redact_secrets(error, cfg))
        return StepResult("push", "ok", "pushed")

    run.timed("push", push_action)


def _run_locked_stages(
    run: _Run,
    cfg: Config,
    ids: IdentityMaps,
    enabled: list[tuple[str, ConnectorSettings]],
    now: datetime,
    conn: sqlite3.Connection,
    connectors: dict[str, Connector] | None,
    capture_only: bool,
    gitlab_reclaim_origins: Iterable[str],
) -> None:
    collection_now = now.astimezone(timezone.utc)

    def connector_action(
        name: str, settings: ConnectorSettings
    ) -> StageOutcome:
        try:
            connector = (
                connectors[name]
                if connectors is not None and name in connectors
                else get_connector(name)
            )
            missing = connector.validate(cfg, settings)
            if missing:
                return StepResult(
                    name,
                    "failed",
                    "missing configuration: " + ", ".join(missing),
                )
            result = collect_connector(
                name,
                cfg,
                ids,
                settings,
                collection_now,
                connector=connector,
                conn=conn,
                emit=False,
            )
        except Exception as error:
            return StepResult(name, "failed", redact_secrets(error, cfg))
        detail = (
            f"{result.inserted} new / {result.fetched} fetched; "
            f"{result.aggregate_rows} aggregate rows / "
            f"{result.aggregate_changes} changed"
        )
        if result.warnings:
            detail += "; " + "; ".join(
                f"warning: {warning}" for warning in result.warnings
            )
        return (
            StepResult(name, "ok", detail, warnings=result.warnings),
            (
                ("fetched", result.fetched),
                ("inserted", result.inserted),
                ("aggregate_rows", result.aggregate_rows),
                ("aggregate_changes", result.aggregate_changes),
                ("warning_count", len(result.warnings)),
            ),
        )

    for name, settings in enabled:
        run.timed(
            name,
            lambda name=name, settings=settings: connector_action(
                name, settings
            ),
        )

    def import_action() -> StageOutcome:
        if not all(
            path is not None
            for path in (cfg.inbox, cfg.archive, cfg.quarantine)
        ):
            return StepResult(
                "import",
                "skipped",
                "TEAMMEM_INBOX, TEAMMEM_ARCHIVE, and "
                "TEAMMEM_QUARANTINE not all set",
            )
        try:
            result = import_inbox(
                conn, ids, cfg.inbox, cfg.archive, cfg.quarantine
            )
        except Exception as error:
            return StepResult(
                "import", "failed", redact_secrets(error, cfg)
            )
        return (
            StepResult(
                "import",
                "ok",
                f"accepted={result.accepted} "
                f"quarantined={result.quarantined} "
                f"events={result.events} inserted={result.inserted}",
            ),
            (
                ("accepted", result.accepted),
                ("quarantined", result.quarantined),
                ("events", result.events),
                ("inserted", result.inserted),
            ),
        )

    run.timed("import", import_action)

    def reclaim_action() -> StageOutcome:
        try:
            identities = reclaim(conn, ids)
            channels = reclaim_channel_projects(conn, ids)
            repositories = reclaim_repository_projects(
                conn,
                ids,
                gitlab_url=cfg.gitlab_url,
                reclaim_origins=gitlab_reclaim_origins,
            )
        except Exception as error:
            return StepResult(
                "reclaim", "failed", redact_secrets(error, cfg)
            )
        identity_rows = sum(item[2] for item in identities)
        channel_rows = sum(item[2] for item in channels)
        repository_rows = sum(item[2] for item in repositories)
        return (
            StepResult(
                "reclaim",
                "ok",
                f"{identity_rows} identity rows; "
                f"{channel_rows} channel rows; "
                f"{repository_rows} repository rows",
            ),
            (
                ("identity_rows", identity_rows),
                ("channel_rows", channel_rows),
                ("repository_rows", repository_rows),
            ),
        )

    reclaim_step = run.timed("reclaim", reclaim_action)
    skip_reason = (
        "capture-only"
        if capture_only
        else "reclaim failed"
        if reclaim_step.status == "failed"
        else None
    )
    if skip_reason is None:
        _run_full_stages(run, cfg, ids, conn, now)
    else:
        run.skip(
            ("journal", "report", "docs-sync", "render", "push"),
            skip_reason,
        )

    def snapshot_action() -> StepResult:
        if cfg.snapshots is None:
            return StepResult(
                "snapshot", "skipped", "TEAMMEM_SNAPSHOTS not set"
            )
        try:
            destination = _snapshot(
                conn, cfg.snapshots, now.date().isoformat()
            )
        except Exception as error:
            return StepResult(
                "snapshot", "failed", redact_secrets(error, cfg)
            )
        return StepResult("snapshot", "ok", str(destination))

    run.timed("snapshot", snapshot_action)


def _skip_unavailable(
    run: _Run,
    enabled: list[tuple[str, ConnectorSettings]],
    detail: str,
    *,
    include_ledger: bool,
) -> None:
    names = (
        *(("ledger",) if include_ledger else ()),
        *(name for name, _settings in enabled),
        *_LOCAL_STAGES,
    )
    run.skip(names, detail)


def run_daily(
    cfg: Config,
    ids: IdentityMaps,
    settings: dict[str, ConnectorSettings],
    now: datetime,
    *,
    connectors: dict[str, Connector] | None = None,
    capture_only: bool = False,
    lock_factory=acquire_run_lock,
    reporter: Reporter = noop_reporter,
    monotonic: Callable[[], float] = time.monotonic,
) -> DailyResult:
    """Run enabled collection and local projection stages exactly once."""
    run_started_at = monotonic()
    mode = "capture-only" if capture_only else "full"
    enabled = [
        (name, connector_settings)
        for name, connector_settings in settings.items()
        if connector_settings.enabled
    ]
    source_stages = frozenset(name for name, _settings in enabled)
    run = _Run([], reporter, monotonic)
    reporter(ProgressEvent(
        "run-start",
        stage="run",
        fields=(("mode", mode), ("local_start", now.isoformat())),
    ))

    lock_started_at = run.start("lock")
    acquired = False

    def on_lock_wait(_message: str) -> None:
        reporter(ProgressEvent(
            "lock-wait",
            stage="lock",
            fields=(
                ("status", "waiting"),
                ("elapsed_seconds", monotonic() - lock_started_at),
            ),
        ))

    try:
        with lock_factory(
            cfg.db_path,
            wait_seconds=0 if capture_only else 1800,
            on_wait=on_lock_wait,
            monotonic=monotonic,
        ):
            acquired = True
            run.finish(lock_started_at, StepResult("lock", "ok"))

            ledger_started_at = run.start("ledger")
            try:
                conn = open_db(cfg.db_path)
            except Exception as error:
                run.finish(
                    ledger_started_at,
                    StepResult(
                        "ledger", "failed", redact_secrets(error, cfg)
                    ),
                )
                _skip_unavailable(
                    run, enabled, "ledger unavailable", include_ledger=False
                )
            else:
                try:
                    run.finish(
                        ledger_started_at,
                        StepResult("ledger", "ok", str(cfg.db_path)),
                    )
                    _run_locked_stages(
                        run,
                        cfg,
                        ids,
                        enabled,
                        now,
                        conn,
                        connectors,
                        capture_only,
                        settings["gitlab"].options.get("reclaim_origins", ()),
                    )
                finally:
                    conn.close()
    except Exception as error:
        if acquired:
            raise
        run.finish(
            lock_started_at,
            StepResult("lock", "failed", redact_secrets(error, cfg)),
        )
        _skip_unavailable(
            run, enabled, "lock unavailable", include_ledger=True
        )

    result = _daily_result(
        run.steps,
        capture_only=capture_only,
        source_stages=source_stages,
    )
    reporter(ProgressEvent(
        "run-end",
        stage="run",
        fields=(
            ("mode", mode),
            ("local_start", now.isoformat()),
            ("ok", result.exit_code == 0),
            ("exit_code", result.exit_code),
            ("elapsed_seconds", monotonic() - run_started_at),
        ),
    ))
    return result
