"""Callable hub services shared by the CLI and the daily workflow."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
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
from .queries import flags as week_flags
from .queries import week_label, week_monday
from .reclaim import reclaim, reclaim_channel_projects
from .render import render_vault
from .slices import active_person_days
from .store import insert_events, open_db, reconcile_gitlab_events
from .summarize import (
    claude_cli_llm,
    daily_person_journal,
    http_llm,
    weekly_team_report,
)
from .vaultgit import commit_all, ensure_repo, push


@dataclass(frozen=True)
class CollectionRun:
    fetched: int
    inserted: int
    channel_names: dict[str, str]
    warnings: tuple[str, ...]


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
    conn: sqlite3.Connection | None = None,
) -> int:
    if dry_run:
        print(
            f"DRY render -> {cfg.vault_dir} ({week_label(week_monday(today))},"
            f" weeks={weeks})"
        )
        return 0
    connection = conn or open_db(cfg.db_path)
    ensure_repo(cfg.vault_dir)
    names_file = cfg.config_dir / "channel_names.json"
    channel_names = json.loads(names_file.read_text()) if names_file.exists() else {}
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
    pairs = active_person_days(connection, start_day, end_day)
    projects = [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT project FROM events "
            "WHERE project IS NOT NULL ORDER BY project"
        )
    ]
    if dry_run:
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
    generated = cached_n = 0
    for person, pair_day in pairs:
        before = connection.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
        pre = connection.execute(
            "SELECT input_hash FROM summaries "
            "WHERE kind='daily-person' AND key=?",
            (f"{person}|{pair_day}",),
        ).fetchone()
        text = daily_person_journal(
            connection,
            person,
            ids.display_name(person),
            pair_day,
            projects,
            backend,
            cfg.llm_daily_model,
            created_ts=f"{today.isoformat()}T00:00:00",
        )
        post = connection.execute(
            "SELECT input_hash FROM summaries "
            "WHERE kind='daily-person' AND key=?",
            (f"{person}|{pair_day}",),
        ).fetchone()
        if text is None:
            continue
        if (
            pre == post
            and before
            == connection.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
            and pre is not None
        ):
            cached_n += 1
        else:
            generated += 1
    print(
        f"journals: {generated} generated, {cached_n} cached"
        f" ({len(pairs)} pairs, model {cfg.llm_daily_model})"
    )
    return 0


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
    days = [(monday + timedelta(days=index)).isoformat() for index in range(7)]
    rows = connection.execute(
        "SELECT key, text FROM summaries WHERE kind = 'daily-person'"
    ).fetchall()
    dailies = [
        {
            "person": key.split("|", 1)[0],
            "day": key.split("|", 1)[1],
            "text": text,
        }
        for key, text in rows
        if key.split("|", 1)[1] in days
    ]
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
    weekly_team_report(
        connection,
        monday.isoformat(),
        dailies,
        week_flags(connection, monday, ids),
        backend,
        cfg.llm_report_model,
        created_ts=f"{base.isoformat()}T00:00:00",
    )
    print(
        f"report: generated {week_label(monday)}"
        f" from {len(dailies)} dailies (model {cfg.llm_report_model})"
    )
    return 0
