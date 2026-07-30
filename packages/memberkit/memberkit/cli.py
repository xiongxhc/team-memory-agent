"""memberkit CLI: draft -> review -> push. Push only after the member reviews."""

import argparse
from datetime import datetime
from pathlib import Path

from . import bundle, config
from .schedule import (
    DEFAULT_TIME,
    UnsupportedSchedulingPlatformError,
    install_schedule,
    remove_schedule,
    schedule_status,
    scheduled_run,
)
from .state import DraftState


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memberkit",
                                     description="Review and share local activity with team memory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("draft", "review", "push", "dismiss"):
        p = sub.add_parser(name)
        p.add_argument(
            "--date",
            help="YYYY-MM-DD (default: today in the member timezone)",
        )
    sub.choices["draft"].add_argument("--force", action="store_true",
                                      help="overwrite an existing bundle")
    sub.choices["draft"].add_argument(
        "--all",
        action="store_true",
        help="compatibility alias; drafts already include every eligible observation",
    )
    p_setup = sub.add_parser("setup", help="configure MemberKit and its local reminder")
    p_setup.add_argument("--member")
    p_setup.add_argument("--inbox-url")
    p_setup.add_argument(
        "--time",
        help="daily HH:MM in the host's local timezone",
    )
    p_setup.add_argument("--no-schedule", action="store_true")
    p_setup.add_argument("--db", default="~/.claude-mem/claude-mem.db")
    p_setup.add_argument("--workdir", default="~/.memberkit")
    p_setup.add_argument("--timezone")
    p_schedule = sub.add_parser("schedule", help="manage the local draft reminder")
    schedule_sub = p_schedule.add_subparsers(dest="schedule_cmd", required=True)
    p_install = schedule_sub.add_parser("install")
    p_install.add_argument(
        "--time",
        default=DEFAULT_TIME,
        help="daily HH:MM in the host's local timezone",
    )
    schedule_sub.add_parser("status")
    schedule_sub.add_parser("remove")
    sub.add_parser("scheduled-run", help="prepare local drafts for the scheduler")
    args = parser.parse_args(argv)

    if args.cmd == "setup":
        member = args.member or input("Member slug: ").strip()
        inbox_url = args.inbox_url or input("Inbox Git URL: ").strip()
        if not member or not inbox_url:
            raise SystemExit("member slug and inbox URL are required")
        config.resolve_timezone(args.timezone)
        values = {
            "MEMBERKIT_MEMBER": member,
            "MEMBERKIT_INBOX_URL": inbox_url,
            "MEMBERKIT_DB": str(Path(args.db).expanduser()),
            "MEMBERKIT_WORKDIR": str(Path(args.workdir).expanduser()),
        }
        if args.timezone:
            values["MEMBERKIT_TIMEZONE"] = args.timezone
        config_path = config.write_config(values)
        cfg = config.load(config_file=config_path)
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
            print(f"configured {config_path}; schedule disabled")
        else:
            schedule_failed = False
            schedule_unsupported = False
            try:
                path = install_schedule(cfg, time=schedule_time)
            except UnsupportedSchedulingPlatformError:
                schedule_unsupported = True
            except Exception:
                schedule_failed = True
            if schedule_unsupported:
                raise SystemExit(
                    f"configuration saved at {config_path}; automatic scheduling "
                    "is unavailable on this platform; rerun setup with "
                    "`--no-schedule` or configure a scheduler to invoke "
                    "`memberkit scheduled-run`"
                )
            if schedule_failed:
                raise SystemExit(
                    f"configuration saved at {config_path}; scheduling failed; "
                    "retry with `memberkit schedule install`"
                )
            print(
                f"configured {config_path}; daily reminder "
                f"{schedule_time} -> {path}"
            )
        return 0

    cfg = config.load()
    timezone = cfg.timezone or bundle._local_timezone()
    date_text = (
        args.date
        if hasattr(args, "date") and args.date
        else datetime.now(timezone).date().isoformat()
    )
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
        dates = scheduled_run(cfg, timezone=timezone)
        print(f"drafts ready: {', '.join(dates) if dates else 'none'}")
        return 0

    out = cfg.workdir / "out" / f"bundle-{cfg.member}-{date_text}.json"

    if args.cmd == "dismiss":
        DraftState(cfg.workdir / "state.json").dismiss(date_text)
        print(f"dismissed {date_text}; pending events excluded")
    elif args.cmd == "draft":
        if not cfg.db.exists():
            raise SystemExit(f"no claude-mem db at {cfg.db} — is claude-mem installed?")
        if out.exists() and not args.force:
            raise SystemExit(f"{out} exists (possibly member-edited) — use --force to overwrite")
        data = bundle.draft(
            cfg.db,
            cfg.member,
            date_text,
            all_observations=args.all,
            timezone=timezone,
        )
        bundle.validate_bundle(data, cfg.member, date_text)
        data["events"] = DraftState(cfg.workdir / "state.json").refresh(
            date_text, data["events"], current=None
        )
        data["journal_md"] = bundle.render_journal(data["events"], date_text)
        bundle.validate_bundle(data, cfg.member, date_text)
        out.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_bundle(out, data)
        print(f"drafted {len(data['events'])} events -> {out}")
        print("review before pushing: memberkit review")
    elif args.cmd == "review":
        if not out.exists():
            raise SystemExit(f"no bundle at {out} — run `memberkit draft` first")
        data = bundle.prepare_bundle(out, cfg.member, date_text)
        DraftState(cfg.workdir / "state.json").refresh(
            date_text, discovered=[], current=data
        )
        print(data["journal_md"])
        print()
        for e in data["events"]:
            print(f"  [{e['ts']}] {e['project'] or 'general'}: {e['summary']}")
        print(f"\n[{len(data['events'])} events — remove private items by deleting them from"
              f" the 'events' list in {out}; journal_md is regenerated during review]")
    else:
        from . import push as push_mod

        dest = push_mod.push(cfg, date_text)
        print(f"pushed -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
