"""memberkit CLI: draft -> review -> push. Push only after the member reviews."""

import argparse
import json
from datetime import date as _date
from pathlib import Path

from . import bundle, config
from .schedule import (DEFAULT_TIME, install_schedule, remove_schedule,
                       schedule_status, scheduled_run)
from .state import DraftState


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memberkit",
                                     description="Review and share local activity with team memory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("draft", "review", "push", "dismiss"):
        p = sub.add_parser(name)
        p.add_argument("--date", default=_date.today().isoformat(),
                       help="YYYY-MM-DD (default: today)")
    sub.choices["draft"].add_argument("--force", action="store_true",
                                      help="overwrite an existing bundle")
    sub.choices["draft"].add_argument(
        "--all",
        action="store_true",
        help="include every observation instead of the curated 3–7 per project",
    )
    p_setup = sub.add_parser("setup", help="configure MemberKit and its local reminder")
    p_setup.add_argument("--member")
    p_setup.add_argument("--inbox-url")
    p_setup.add_argument("--time")
    p_setup.add_argument("--no-schedule", action="store_true")
    p_setup.add_argument("--db", default="~/.claude-mem/claude-mem.db")
    p_setup.add_argument("--workdir", default="~/.memberkit")
    p_schedule = sub.add_parser("schedule", help="manage the local draft reminder")
    schedule_sub = p_schedule.add_subparsers(dest="schedule_cmd", required=True)
    p_install = schedule_sub.add_parser("install")
    p_install.add_argument("--time", default=DEFAULT_TIME)
    schedule_sub.add_parser("status")
    schedule_sub.add_parser("remove")
    sub.add_parser("scheduled-run", help="prepare local drafts for the scheduler")
    args = parser.parse_args(argv)

    if args.cmd == "setup":
        member = args.member or input("Member slug: ").strip()
        inbox_url = args.inbox_url or input("Inbox Git URL: ").strip()
        if not member or not inbox_url:
            raise SystemExit("member slug and inbox URL are required")
        config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.CONFIG_FILE.write_text(
            "\n".join([
                f"MEMBERKIT_MEMBER={member}",
                f"MEMBERKIT_INBOX_URL={inbox_url}",
                f"MEMBERKIT_DB={Path(args.db).expanduser()}",
                f"MEMBERKIT_WORKDIR={Path(args.workdir).expanduser()}",
                "",
            ]),
            encoding="utf-8",
        )
        config.CONFIG_FILE.chmod(0o600)
        cfg = config.load({})
        schedule_time = args.time
        if not args.no_schedule and schedule_time is None:
            choice = input(
                f"Daily reminder time [{DEFAULT_TIME}], or 'no' to decline: "
            ).strip()
            if choice.lower() in {"n", "no", "none", "off", "decline"}:
                args.no_schedule = True
            else:
                schedule_time = choice or DEFAULT_TIME
        if args.no_schedule:
            print(f"configured {config.CONFIG_FILE}; schedule disabled")
        else:
            path = install_schedule(cfg, time=schedule_time)
            print(
                f"configured {config.CONFIG_FILE}; daily reminder "
                f"{schedule_time} -> {path}"
            )
        return 0

    cfg = config.load()
    if args.cmd == "schedule":
        if args.schedule_cmd == "install":
            path = install_schedule(cfg, time=args.time)
            print(f"installed {args.time} -> {path}")
        elif args.schedule_cmd == "status":
            status = schedule_status()
            print(f"{'installed' if status.installed else 'not installed'}"
                  f"{f' at {status.time}' if status.time else ''}")
        else:
            print("removed" if remove_schedule() else "not installed")
        return 0
    if args.cmd == "scheduled-run":
        dates = scheduled_run(cfg)
        print(f"drafts ready: {', '.join(dates) if dates else 'none'}")
        return 0

    out = cfg.workdir / "out" / f"bundle-{cfg.member}-{args.date}.json"

    if args.cmd == "dismiss":
        DraftState(cfg.workdir / "state.json").dismiss(args.date)
        print(f"dismissed {args.date}; pending events excluded")
    elif args.cmd == "draft":
        if not cfg.db.exists():
            raise SystemExit(f"no claude-mem db at {cfg.db} — is claude-mem installed?")
        if out.exists() and not args.force:
            raise SystemExit(f"{out} exists (possibly member-edited) — use --force to overwrite")
        data = bundle.draft(
            cfg.db,
            cfg.member,
            args.date,
            all_observations=args.all,
        )
        data["events"] = DraftState(cfg.workdir / "state.json").refresh(
            args.date, data["events"], current=None
        )
        data["journal_md"] = bundle.render_journal(data["events"], args.date)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"drafted {len(data['events'])} events -> {out}")
        print("review before pushing: memberkit review")
    elif args.cmd == "review":
        if not out.exists():
            raise SystemExit(f"no bundle at {out} — run `memberkit draft` first")
        data = json.loads(out.read_text(encoding="utf-8"))
        print(bundle.render_journal(data["events"], data["date"]))
        print()
        for e in data["events"]:
            print(f"  [{e['ts']}] {e['project'] or 'general'}: {e['summary']}")
        print(f"\n[{len(data['events'])} events — remove private items by deleting them from"
              f" the 'events' list in {out}; journal_md is regenerated from events at push]")
    else:
        from . import push as push_mod

        dest = push_mod.push(cfg, args.date)
        print(f"pushed -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
