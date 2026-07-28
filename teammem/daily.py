"""Portable, run-once orchestration for an operator-controlled hub."""

import io
import os
import sqlite3
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .connectors.base import Connector
from .connectors.config import ConnectorSettings
from .connectors.registry import get_connector
from .identity import IdentityMaps
from .importer import import_inbox
from .reclaim import reclaim, reclaim_channel_projects
from .services import (
    collect_connector,
    redact_secrets,
    resolve_llm_backend,
    run_docs_sync,
    run_journal,
    run_render,
    run_report,
)
from .store import open_db
from .vaultgit import push


@dataclass(frozen=True)
class StepResult:
    name: str
    status: str
    detail: str = ""
    warnings: tuple[str, ...] = ()


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


def _call_service(function, *args, **kwargs) -> tuple[int, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = function(*args, **kwargs)
    detail = "\n".join(
        part.strip() for part in (stdout.getvalue(), stderr.getvalue()) if part.strip()
    )
    return code, detail


def _service_step(
    steps: list[StepResult],
    name: str,
    cfg: Config,
    function,
    *args,
    **kwargs,
) -> bool:
    try:
        code, detail = _call_service(function, *args, **kwargs)
    except Exception as error:
        steps.append(StepResult(name, "failed", redact_secrets(error, cfg)))
        return False
    ok = code == 0
    steps.append(
        StepResult(
            name,
            "ok" if ok else "failed",
            redact_secrets(detail or f"exit {code}", cfg),
        )
    )
    return ok


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


def run_daily(
    cfg: Config,
    ids: IdentityMaps,
    settings: dict[str, ConnectorSettings],
    now: datetime,
    *,
    connectors: dict[str, Connector] | None = None,
) -> DailyResult:
    """Run enabled collection and local projection stages exactly once."""
    steps: list[StepResult] = []
    local_day = now.date()
    collection_now = now.astimezone(timezone.utc)
    enabled = [
        (name, connector_settings)
        for name, connector_settings in settings.items()
        if connector_settings.enabled
    ]

    try:
        conn = open_db(cfg.db_path)
    except Exception as error:
        steps.append(StepResult("ledger", "failed", redact_secrets(error, cfg)))
        for name, _connector_settings in enabled:
            steps.append(StepResult(name, "skipped", "ledger unavailable"))
        steps.extend(
            StepResult(name, "skipped", "ledger unavailable")
            for name in _LOCAL_STAGES
        )
        return DailyResult(tuple(steps), 1)

    steps.append(StepResult("ledger", "ok", str(cfg.db_path)))

    for name, connector_settings in enabled:
        connector = (
            connectors[name]
            if connectors is not None and name in connectors
            else get_connector(name)
        )
        missing = connector.validate(cfg, connector_settings)
        if missing:
            steps.append(
                StepResult(
                    name,
                    "failed",
                    "missing configuration: " + ", ".join(missing),
                )
            )
            continue
        try:
            result = collect_connector(
                name,
                cfg,
                ids,
                connector_settings,
                collection_now,
                connector=connector,
                conn=conn,
                emit=False,
            )
            detail = f"{result.inserted} new / {result.fetched} fetched"
            if result.warnings:
                detail += "; " + "; ".join(
                    f"warning: {warning}" for warning in result.warnings
                )
            steps.append(
                StepResult(name, "ok", detail, warnings=result.warnings)
            )
        except Exception as error:
            steps.append(
                StepResult(name, "failed", redact_secrets(error, cfg))
            )

    paths = (cfg.inbox, cfg.archive, cfg.quarantine)
    if all(path is not None for path in paths):
        try:
            imported = import_inbox(
                conn,
                ids,
                cfg.inbox,
                cfg.archive,
                cfg.quarantine,
            )
            steps.append(
                StepResult(
                    "import",
                    "ok",
                    f"accepted={imported.accepted} "
                    f"quarantined={imported.quarantined} "
                    f"events={imported.events} inserted={imported.inserted}",
                )
            )
        except Exception as error:
            steps.append(
                StepResult("import", "failed", redact_secrets(error, cfg))
            )
    else:
        steps.append(
            StepResult(
                "import",
                "skipped",
                "TEAMMEM_INBOX, TEAMMEM_ARCHIVE, and TEAMMEM_QUARANTINE not all set",
            )
        )

    try:
        identities = reclaim(conn, ids)
        channels = reclaim_channel_projects(conn, ids)
        steps.append(
            StepResult(
                "reclaim",
                "ok",
                f"{sum(item[2] for item in identities)} identity rows; "
                f"{sum(item[2] for item in channels)} channel rows",
            )
        )
        reclaim_ok = True
    except Exception as error:
        steps.append(
            StepResult("reclaim", "failed", redact_secrets(error, cfg))
        )
        reclaim_ok = False

    if not reclaim_ok:
        for name in ("journal", "report", "docs-sync", "render", "push"):
            steps.append(StepResult(name, "skipped", "reclaim failed"))
    else:
        daily_llm = resolve_llm_backend(cfg, cfg.llm_daily_model, 1024)
        if daily_llm is None:
            steps.append(StepResult("journal", "skipped", "no LLM backend"))
        else:
            _service_step(
                steps,
                "journal",
                cfg,
                run_journal,
                cfg,
                ids,
                today=local_day,
                since_days=cfg.since_days,
                conn=conn,
                llm=daily_llm,
            )

        if local_day.weekday() != 4:
            steps.append(StepResult("report", "skipped", "not Friday"))
        else:
            report_llm = resolve_llm_backend(cfg, cfg.llm_report_model, 8192)
            if report_llm is None:
                steps.append(StepResult("report", "skipped", "no LLM backend"))
            else:
                _service_step(
                    steps,
                    "report",
                    cfg,
                    run_report,
                    cfg,
                    ids,
                    base=local_day,
                    conn=conn,
                    llm=report_llm,
                )

        if cfg.obsidian_projects is None:
            steps.append(
                StepResult("docs-sync", "skipped", "TEAMMEM_OBSIDIAN_PROJECTS not set")
            )
        else:
            _service_step(steps, "docs-sync", cfg, run_docs_sync, cfg)

        render_ok = _service_step(
            steps,
            "render",
            cfg,
            run_render,
            replace(cfg, push=False),
            ids,
            today=local_day,
            conn=conn,
        )

        if not cfg.push:
            steps.append(StepResult("push", "skipped", "TEAMMEM_PUSH not enabled"))
        elif not render_ok:
            steps.append(StepResult("push", "skipped", "render failed"))
        else:
            try:
                push(cfg.vault_dir)
                steps.append(StepResult("push", "ok", "pushed"))
            except Exception as error:
                steps.append(
                    StepResult("push", "failed", redact_secrets(error, cfg))
                )

    if cfg.snapshots is None:
        steps.append(
            StepResult("snapshot", "skipped", "TEAMMEM_SNAPSHOTS not set")
        )
    else:
        try:
            destination = _snapshot(conn, cfg.snapshots, local_day.isoformat())
            steps.append(StepResult("snapshot", "ok", str(destination)))
        except Exception as error:
            steps.append(
                StepResult("snapshot", "failed", redact_secrets(error, cfg))
            )

    exit_code = 1 if any(step.status == "failed" for step in steps) else 0
    return DailyResult(tuple(steps), exit_code)
