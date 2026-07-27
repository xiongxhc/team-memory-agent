"""teammem — team activity ledger CLI (M1: GitLab baseline).

  teammem collect gitlab [--since-days N] [--dry-run]
  teammem collect feishu [--since-days N] [--dry-run]
  teammem stats
  teammem reclaim [--dry-run]
"""

import argparse
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone, date

from .config import Config
from .events import Event
from .gitlab_collector import FetchJson, collect_gitlab, http_fetch_json
from .feishu_collector import collect_feishu, http_feishu_fetch
from .identity import IdentityMaps
from .reclaim import reclaim, reclaim_channel_projects
from .render import render_vault
from .slices import active_person_days, daily_person_slice, slice_hash
from .store import open_db, insert_events, stats as store_stats
from .summarize import (PROMPT_VERSION, claude_cli_llm, daily_person_journal,
                        http_llm, weekly_team_report)
from .vaultgit import ensure_repo, commit_all, push


def run_collect(cfg: Config, ids: IdentityMaps, events_fn: Callable[[], list[Event]],
                dry_run: bool) -> tuple[int, int]:
    events = events_fn()
    if dry_run:
        for e in events:
            print(f"DRY {e.ts}  {e.person:<28} {e.kind:<7} {e.project or '-':<18} {e.summary}")
        print(f"dry-run: {len(events)} events, nothing written")
        return len(events), 0
    conn = open_db(cfg.db_path)
    inserted = insert_events(conn, events)
    unmapped = sorted({e.person for e in events if e.person.startswith("_unmapped/")})
    print(f"ingested: {inserted} new / {len(events)} fetched -> {cfg.db_path}")
    if unmapped:
        print(f"UNMAPPED identities (add to roster.yaml): {', '.join(unmapped)}")
    return len(events), inserted


def _llm_backend(cfg: Config, model: str, max_tokens: int):
    """ANTHROPIC_API_KEY => direct Messages API; else headless `claude -p` on
    the operator's subscription; None when neither is available."""
    if cfg.anthropic_api_key:
        return http_llm(model, cfg.anthropic_api_key, max_tokens=max_tokens)
    import shutil
    if shutil.which("claude"):
        return claude_cli_llm(model)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="teammem", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_collect = sub.add_parser("collect", help="run a central collector")
    p_collect.add_argument("collector", choices=["gitlab", "feishu"])
    p_collect.add_argument("--since-days", type=int, default=None)
    p_collect.add_argument("--dry-run", action="store_true")
    sub.add_parser("stats", help="ledger row counts")
    p_reclaim = sub.add_parser("reclaim", help="re-attribute _unmapped rows via roster")
    p_reclaim.add_argument("--dry-run", action="store_true")
    p_render = sub.add_parser("render", help="render vault from ledger")
    p_render.add_argument("--weeks", type=int, default=4)
    p_render.add_argument("--today", type=str, default=None)
    p_render.add_argument("--push", action="store_true")
    p_render.add_argument("--dry-run", action="store_true")
    p_journal = sub.add_parser("journal", help="generate cached daily per-person journals")
    p_journal.add_argument("--day", type=str, default=None)
    p_journal.add_argument("--since-days", type=int, default=7)
    p_journal.add_argument("--today", type=str, default=None)
    p_journal.add_argument("--dry-run", action="store_true")
    sub.add_parser("docs-sync",
                   help="copy Obsidian project docs (architecture/summary) into vault Docs/")
    p_report = sub.add_parser("report", help="generate the cached weekly team report")
    p_report.add_argument("--week-of", type=str, default=None)
    p_report.add_argument("--dry-run", action="store_true")
    p_import = sub.add_parser("import-bundles",
                              help="validate and import reviewed member bundles")
    p_import.add_argument("--inbox", required=True)
    p_import.add_argument("--archive", required=True)
    p_import.add_argument("--quarantine", required=True)
    p_import.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cfg = Config.load()
    if args.cmd == "import-bundles":
        from pathlib import Path
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

    if args.cmd == "stats":
        s = store_stats(open_db(cfg.db_path))
        print(f"total: {s['total']}")
        for section in ("by_source", "by_kind", "by_person"):
            print(f"{section}: {s[section]}")
        if s["unmapped"]:
            print(f"unmapped: {s['unmapped']}")
        return 0

    if args.cmd == "reclaim":
        ids = IdentityMaps.load(cfg.config_dir)
        conn = open_db(cfg.db_path)
        results = reclaim(conn, ids, dry_run=args.dry_run)
        prefix = "DRY " if args.dry_run else ""
        for raw, slug, n in results:
            print(f"{prefix}reclaim {raw} -> {slug} ({n} rows)")
        print(f"{prefix}reclaimed {sum(n for _, _, n in results)} rows"
              f" across {len(results)} identities")
        ch = reclaim_channel_projects(conn, ids, dry_run=args.dry_run)
        for chat_id, project, n in ch:
            print(f"{prefix}reclaim channel {chat_id} -> {project} ({n} rows)")
        print(f"{prefix}reclaimed {sum(n for _, _, n in ch)} channel rows"
              f" across {len(ch)} channels")
        return 0

    if args.cmd == "docs-sync":
        if not cfg.obsidian_projects:
            print("set TEAMMEM_OBSIDIAN_PROJECTS", file=sys.stderr)
            return 2
        from .docs_sync import sync_docs
        from .identity import _read
        out = sync_docs(_read(cfg.config_dir, "projects"),
                        cfg.obsidian_projects, cfg.vault_dir)
        print(f"docs-sync: {out['copied']} files updated"
              f" across {out['projects']} projects -> {cfg.vault_dir / 'Docs'}")
        return 0

    if args.cmd == "render":
        from .queries import week_label, week_monday
        ids = IdentityMaps.load(cfg.config_dir)
        conn = open_db(cfg.db_path)
        today = date.fromisoformat(args.today) if args.today else date.today()
        if args.dry_run:
            print(f"DRY render -> {cfg.vault_dir} ({week_label(week_monday(today))},"
                  f" weeks={args.weeks})")
            return 0
        ensure_repo(cfg.vault_dir)
        names_file = cfg.config_dir / "channel_names.json"
        channel_names = (json.loads(names_file.read_text())
                         if names_file.exists() else {})
        out = render_vault(conn, ids, cfg.vault_dir, today, weeks=args.weeks,
                           channel_names=channel_names)
        committed = commit_all(cfg.vault_dir,
                               f"render: {today.isoformat()} {out['week_label']}"
                               f" ({out['files']} files)")
        print(f"rendered {out['files']} files -> {cfg.vault_dir}"
              f" ({'committed' if committed else 'no changes'})")
        if args.push or cfg.push:
            # Best-effort: remote is VPN-only; commits are retained and the next
            # successful push carries them, so an unreachable remote must not fail the run.
            try:
                push(cfg.vault_dir)
                print("pushed")
            except subprocess.CalledProcessError as e:
                detail = (e.stderr or "").strip().splitlines()
                print(f"WARN: vault push failed ({detail[-1] if detail else e}); "
                      f"commits retained for next push", file=sys.stderr)
        return 0

    if args.cmd == "journal":
        from datetime import timedelta
        ids = IdentityMaps.load(cfg.config_dir)
        conn = open_db(cfg.db_path)
        today = date.fromisoformat(args.today) if args.today else date.today()
        if args.day:
            start_day = end_day = args.day
        else:
            start_day = (today - timedelta(days=args.since_days - 1)).isoformat()
            end_day = today.isoformat()
        pairs = active_person_days(conn, start_day, end_day)
        projects = [r[0] for r in conn.execute(
            "SELECT DISTINCT project FROM events WHERE project IS NOT NULL ORDER BY project")]
        if args.dry_run:
            for person, day in pairs:
                user = daily_person_slice(conn, person, day)
                h = slice_hash(PROMPT_VERSION + "\n" + user) if user else ""
                cached = conn.execute(
                    "SELECT input_hash FROM summaries WHERE kind = 'daily-person' AND key = ?",
                    (f"{person}|{day}",)).fetchone()
                # NOTE: the stored hash covers the full prompt (name+slugs+slice);
                # dry-run only distinguishes "row exists" vs "no row" — report
                # 'hit' when a row exists, 'miss' otherwise. Cheap and honest.
                state = "hit" if cached else "miss"
                print(f"DRY journal {person:<28} {day}  {state}")
            print(f"dry-run: {len(pairs)} (person, day) pairs, no LLM calls, nothing written")
            return 0
        llm = _llm_backend(cfg, cfg.llm_daily_model, max_tokens=1024)
        if llm is None:
            print("no LLM backend: set ANTHROPIC_API_KEY or install the claude CLI",
                  file=sys.stderr)
            return 2
        generated = cached_n = 0
        for person, day in pairs:
            before = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
            pre = conn.execute(
                "SELECT input_hash FROM summaries WHERE kind='daily-person' AND key=?",
                (f"{person}|{day}",)).fetchone()
            text = daily_person_journal(conn, person, ids.display_name(person), day,
                                        projects, llm, cfg.llm_daily_model,
                                        created_ts=f"{today.isoformat()}T00:00:00")
            post = conn.execute(
                "SELECT input_hash FROM summaries WHERE kind='daily-person' AND key=?",
                (f"{person}|{day}",)).fetchone()
            if text is None:
                continue
            if pre == post and before == conn.execute(
                    "SELECT COUNT(*) FROM summaries").fetchone()[0] and pre is not None:
                cached_n += 1
            else:
                generated += 1
        print(f"journals: {generated} generated, {cached_n} cached"
              f" ({len(pairs)} pairs, model {cfg.llm_daily_model})")
        return 0

    if args.cmd == "report":
        from datetime import timedelta
        from .queries import flags as week_flags, week_label, week_monday
        ids = IdentityMaps.load(cfg.config_dir)
        conn = open_db(cfg.db_path)
        base = date.fromisoformat(args.week_of) if args.week_of else date.today()
        monday = week_monday(base)
        days = [(monday + timedelta(days=i)).isoformat() for i in range(7)]
        rows = conn.execute(
            "SELECT key, text FROM summaries WHERE kind = 'daily-person'").fetchall()
        dailies = [{"person": k.split("|", 1)[0], "day": k.split("|", 1)[1], "text": t}
                   for k, t in rows if k.split("|", 1)[1] in days]
        if not dailies:
            print(f"no daily journals cached for {week_label(monday)};"
                  f" run `teammem journal` first", file=sys.stderr)
            return 0
        if args.dry_run:
            cached = conn.execute(
                "SELECT 1 FROM summaries WHERE kind='weekly-team' AND key=?",
                (f"team|{monday.isoformat()}",)).fetchone()
            print(f"DRY report {week_label(monday)}: {len(dailies)} dailies,"
                  f" {'hit' if cached else 'miss'}")
            return 0
        llm = _llm_backend(cfg, cfg.llm_report_model, max_tokens=8192)
        if llm is None:
            print("no LLM backend: set ANTHROPIC_API_KEY or install the claude CLI",
                  file=sys.stderr)
            return 2
        weekly_team_report(conn, monday.isoformat(), dailies,
                           week_flags(conn, monday, ids), llm, cfg.llm_report_model,
                           created_ts=f"{base.isoformat()}T00:00:00")
        print(f"report: generated {week_label(monday)}"
              f" from {len(dailies)} dailies (model {cfg.llm_report_model})")
        return 0

    if args.cmd == "collect":
        now = datetime.now(timezone.utc)
        if args.since_days is not None:
            cfg = replace(cfg, since_days=args.since_days)
        ids = IdentityMaps.load(cfg.config_dir)

        if args.collector == "gitlab":
            if not (cfg.gitlab_url and cfg.gitlab_token and cfg.gitlab_group):
                print("set TEAMMEM_GITLAB_URL, TEAMMEM_GITLAB_TOKEN, TEAMMEM_GITLAB_GROUP",
                      file=sys.stderr)
                return 2
            run_collect(cfg, ids, lambda: collect_gitlab(cfg, ids, http_fetch_json(cfg), now),
                        dry_run=args.dry_run)
        elif args.collector == "feishu":
            if not (cfg.feishu_app_id and cfg.feishu_app_secret):
                print("set TEAMMEM_FEISHU_APP_ID, TEAMMEM_FEISHU_APP_SECRET",
                      file=sys.stderr)
                return 2
            run_collect(cfg, ids, lambda: collect_feishu(cfg, ids, http_feishu_fetch(cfg), now),
                        dry_run=args.dry_run)
        return 0


if __name__ == "__main__":
    sys.exit(main())
