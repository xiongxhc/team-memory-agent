"""Draft a teammem-bundle/v1 from the local claude-mem observations db (read-only)."""

import os
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = "teammem-bundle/v1"


def render_journal(events: list[dict], date: str) -> str:
    by_project: dict[str, list[str]] = {}
    for e in events:
        by_project.setdefault(e["project"] or "general", []).append(e["summary"])
    lines = [f"## {date}"]
    for project in sorted(by_project):
        lines.append(f"\n### {project}")
        lines.extend(f"- {s}" for s in by_project[project])
    return "\n".join(lines)


def _local_timezone():
    configured = os.environ.get("MEMBERKIT_TIMEZONE") or os.environ.get("TZ")
    if configured:
        try:
            return ZoneInfo(configured.removeprefix(":"))
        except ZoneInfoNotFoundError:
            pass

    try:
        resolved = Path("/etc/localtime").resolve()
        parts = resolved.parts
        marker = parts.index("zoneinfo")
        return ZoneInfo("/".join(parts[marker + 1:]))
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return datetime.now().astimezone().tzinfo


def _day_epochs(date: str) -> tuple[int, int]:
    day = datetime.strptime(date, "%Y-%m-%d").date()
    zone = _local_timezone()
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return int(start.timestamp()), int(end.timestamp())


def draft(db_path: Path, member: str, date: str) -> dict:
    lo, hi = _day_epochs(date)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT project, title, narrative, created_at FROM observations"
            " WHERE created_at_epoch >= ? AND created_at_epoch < ?"
            " ORDER BY created_at_epoch",
            (lo, hi),
        ).fetchall()
    finally:
        con.close()

    events = [
        {
            "ts": r["created_at"],
            "kind": "journal-highlight",
            "summary": (r["title"] or (r["narrative"] or "").strip()[:120]),
            "project": r["project"],
            "refs": None,
        }
        for r in rows
        if r["title"] or (r["narrative"] or "").strip()
    ]

    return {"schema": SCHEMA, "member": member, "date": date,
            "events": events, "journal_md": render_journal(events, date)}
