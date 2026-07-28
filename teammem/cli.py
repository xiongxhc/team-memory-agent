"""teammem — local-first team activity ledger and reporting CLI."""

import argparse
import sqlite3
import sys
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from .config import Config
from .connectors.config import load_connector_settings
from .connectors.registry import connector_names, get_connector
from .daily import run_daily
from .identity import IdentityMaps
from .reclaim import reclaim, reclaim_channel_projects
from .services import (
    collect_connector,
    resolve_llm_backend,
    run_collect,
    run_docs_sync,
    run_journal,
    run_render,
    run_report,
)
from .store import open_db, stats as store_stats


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="teammem", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_connectors = sub.add_parser(
        "connectors", help="list or validate built-in connectors"
    )
    connector_sub = p_connectors.add_subparsers(
        dest="connectors_cmd", required=True
    )
    connector_sub.add_parser("list", help="list built-in connector names")
    connector_sub.add_parser("check", help="validate enabled connectors")

    p_collect = sub.add_parser("collect", help="run a central collector")
    p_collect.add_argument("collector", nargs="?", choices=connector_names())
    p_collect.add_argument("--enabled", action="store_true")
    p_collect.add_argument("--since-days", type=int, default=None)
    p_collect.add_argument("--dry-run", action="store_true")

    sub.add_parser("run-daily", help="run configured hub stages once")
    sub.add_parser("stats", help="ledger row counts")

    p_reclaim = sub.add_parser(
        "reclaim", help="re-attribute _unmapped rows via roster"
    )
    p_reclaim.add_argument("--dry-run", action="store_true")

    p_render = sub.add_parser("render", help="render vault from ledger")
    p_render.add_argument("--weeks", type=int, default=4)
    p_render.add_argument("--today", type=str, default=None)
    p_render.add_argument("--push", action="store_true")
    p_render.add_argument("--dry-run", action="store_true")

    p_journal = sub.add_parser(
        "journal", help="generate cached daily per-person journals"
    )
    p_journal.add_argument("--day", type=str, default=None)
    p_journal.add_argument("--since-days", type=int, default=7)
    p_journal.add_argument("--today", type=str, default=None)
    p_journal.add_argument("--dry-run", action="store_true")

    sub.add_parser(
        "docs-sync",
        help="copy Obsidian project docs (architecture/summary) into vault Docs/",
    )

    p_report = sub.add_parser(
        "report", help="generate the cached weekly team report"
    )
    p_report.add_argument("--week-of", type=str, default=None)
    p_report.add_argument("--dry-run", action="store_true")

    p_import = sub.add_parser(
        "import-bundles", help="validate and import reviewed member bundles"
    )
    p_import.add_argument("--inbox", required=True)
    p_import.add_argument("--archive", required=True)
    p_import.add_argument("--quarantine", required=True)
    p_import.add_argument("--dry-run", action="store_true")
    return parser


def _check_enabled_connectors(
    cfg: Config,
    settings,
) -> tuple[int, list[str]]:
    invalid: list[str] = []
    enabled = [
        (name, connector_settings)
        for name, connector_settings in settings.items()
        if connector_settings.enabled
    ]
    if not enabled:
        print("no connectors enabled")
        return 0, []
    for name, connector_settings in enabled:
        missing = get_connector(name).validate(cfg, connector_settings)
        if missing:
            print(
                f"{name}: missing {', '.join(missing)}",
                file=sys.stderr,
            )
            invalid.append(name)
        else:
            print(f"{name}: ok")
    return (2 if invalid else 0), invalid


def _list_connectors(cfg: Config, settings) -> None:
    for name in connector_names():
        connector_settings = settings[name]
        if not connector_settings.enabled:
            print(f"{name}: disabled")
            continue
        missing = get_connector(name).validate(cfg, connector_settings)
        state = (
            f"enabled/missing {', '.join(missing)}"
            if missing
            else "enabled/ok"
        )
        print(f"{name}: {state}")


def _print_daily(result) -> None:
    for step in result.steps:
        line = f"{step.name}: {step.status}"
        if step.detail:
            line += f" — {step.detail}"
        print(line, file=sys.stderr if step.status == "failed" else sys.stdout)
        for warning in step.warnings:
            print(f"WARN {step.name}: {warning}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    cfg = Config.load()

    if args.cmd == "import-bundles":
        from .importer import import_inbox

        ids = IdentityMaps.load(cfg.config_dir)
        conn = sqlite3.connect(":memory:") if args.dry_run else open_db(cfg.db_path)
        result = import_inbox(
            conn,
            ids,
            Path(args.inbox),
            Path(args.archive),
            Path(args.quarantine),
            dry_run=args.dry_run,
        )
        prefix = "dry-run: " if args.dry_run else ""
        print(
            f"{prefix}accepted={result.accepted} quarantined={result.quarantined} "
            f"events={result.events} inserted={result.inserted}"
        )
        return 0

    if args.cmd == "connectors":
        settings = load_connector_settings(cfg.config_dir)
        if args.connectors_cmd == "list":
            _list_connectors(cfg, settings)
            return 0
        code, _invalid = _check_enabled_connectors(cfg, settings)
        return code

    if args.cmd == "stats":
        values = store_stats(open_db(cfg.db_path))
        print(f"total: {values['total']}")
        for section in ("by_source", "by_kind", "by_person"):
            print(f"{section}: {values[section]}")
        if values["unmapped"]:
            print(f"unmapped: {values['unmapped']}")
        return 0

    if args.cmd == "reclaim":
        ids = IdentityMaps.load(cfg.config_dir)
        conn = open_db(cfg.db_path)
        results = reclaim(conn, ids, dry_run=args.dry_run)
        prefix = "DRY " if args.dry_run else ""
        for raw, slug, count in results:
            print(f"{prefix}reclaim {raw} -> {slug} ({count} rows)")
        print(
            f"{prefix}reclaimed {sum(count for _, _, count in results)} rows"
            f" across {len(results)} identities"
        )
        channels = reclaim_channel_projects(conn, ids, dry_run=args.dry_run)
        for channel_id, project, count in channels:
            print(
                f"{prefix}reclaim channel {channel_id} -> {project} ({count} rows)"
            )
        print(
            f"{prefix}reclaimed {sum(count for _, _, count in channels)} channel rows"
            f" across {len(channels)} channels"
        )
        return 0

    if args.cmd == "docs-sync":
        return run_docs_sync(cfg)

    if args.cmd == "render":
        ids = IdentityMaps.load(cfg.config_dir)
        today = date.fromisoformat(args.today) if args.today else date.today()
        return run_render(
            cfg,
            ids,
            today=today,
            weeks=args.weeks,
            push_requested=args.push,
            dry_run=args.dry_run,
        )

    if args.cmd == "journal":
        ids = IdentityMaps.load(cfg.config_dir)
        today = date.fromisoformat(args.today) if args.today else date.today()
        llm = (
            None
            if args.dry_run
            else resolve_llm_backend(
                cfg, cfg.llm_daily_model, max_tokens=1024
            )
        )
        return run_journal(
            cfg,
            ids,
            today=today,
            day=args.day,
            since_days=args.since_days,
            dry_run=args.dry_run,
            llm=llm,
        )

    if args.cmd == "report":
        ids = IdentityMaps.load(cfg.config_dir)
        base = date.fromisoformat(args.week_of) if args.week_of else date.today()
        llm = (
            None
            if args.dry_run
            else resolve_llm_backend(
                cfg, cfg.llm_report_model, max_tokens=8192
            )
        )
        return run_report(
            cfg,
            ids,
            base=base,
            dry_run=args.dry_run,
            llm=llm,
        )

    if args.cmd == "collect":
        if bool(args.collector) == bool(args.enabled):
            parser.error("choose one connector or --enabled")
        if args.since_days is not None:
            cfg = replace(cfg, since_days=args.since_days)
        ids = IdentityMaps.load(cfg.config_dir)
        settings = load_connector_settings(cfg.config_dir)
        names = (
            [args.collector]
            if args.collector
            else [
                name
                for name, connector_settings in settings.items()
                if connector_settings.enabled
            ]
        )
        if not names:
            print("no connectors enabled")
            return 0
        invalid = failed = False
        for name in names:
            connector = get_connector(name)
            connector_settings = settings[name]
            missing = connector.validate(cfg, connector_settings)
            if missing:
                print(
                    f"{name}: missing {', '.join(missing)}",
                    file=sys.stderr,
                )
                invalid = True
                continue
            try:
                collect_connector(
                    name,
                    cfg,
                    ids,
                    connector_settings,
                    datetime.now(timezone.utc),
                    dry_run=args.dry_run,
                    connector=connector,
                )
            except Exception:
                print(f"{name}: collection failed", file=sys.stderr)
                failed = True
        return 2 if invalid else (1 if failed else 0)

    if args.cmd == "run-daily":
        ids = IdentityMaps.load(cfg.config_dir)
        settings = load_connector_settings(cfg.config_dir)
        result = run_daily(cfg, ids, settings, datetime.now().astimezone())
        _print_daily(result)
        return result.exit_code

    raise AssertionError(f"unhandled command: {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
