"""Draft a teammem-bundle/v1 from the local claude-mem observations db (read-only)."""

import os
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = "teammem-bundle/v1"
SUMMARY_LIMIT = 120


def render_journal(events: list[dict], date: str) -> str:
    by_project: dict[str, list[str]] = {}
    for event in events:
        by_project.setdefault(event["project"] or "general", []).append(
            event["summary"]
        )
    lines = [f"## {date}"]
    for project in sorted(by_project):
        lines.append(f"\n### {project}")
        lines.extend(f"- {summary}" for summary in by_project[project])
    return "\n".join(lines)


def _local_timezone():
    configured = os.environ.get("MEMBERKIT_TIMEZONE") or os.environ.get("TZ")
    if configured:
        try:
            return ZoneInfo(configured.removeprefix(":"))
        except ZoneInfoNotFoundError as exc:
            if os.environ.get("MEMBERKIT_TIMEZONE"):
                raise ValueError(
                    f"invalid MEMBERKIT_TIMEZONE {configured!r}: "
                    "use an IANA timezone such as Asia/Dubai"
                ) from exc

    try:
        resolved = Path("/etc/localtime").resolve()
        parts = resolved.parts
        marker = parts.index("zoneinfo")
        return ZoneInfo("/".join(parts[marker + 1:]))
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return datetime.now().astimezone().tzinfo


def _day_epochs(date: str, zone=None) -> tuple[int, int]:
    day = datetime.strptime(date, "%Y-%m-%d").date()
    zone = zone or _local_timezone()
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _event_timestamp(row: sqlite3.Row, zone) -> str:
    return datetime.fromtimestamp(
        row["created_at_epoch"] / 1000,
        tz=zone,
    ).isoformat(timespec="milliseconds")


def _legacy_events(rows: list[sqlite3.Row], zone) -> list[dict]:
    return [
        {
            "ts": _event_timestamp(row, zone),
            "kind": "journal-highlight",
            "summary": (
                row["title"] or (row["narrative"] or "").strip()[:SUMMARY_LIMIT]
            ),
            "project": row["project"],
            "refs": None,
        }
        for row in rows
        if row["title"] or (row["narrative"] or "").strip()
    ]


def draft(
    db_path: Path,
    member: str,
    date: str,
    *,
    all_observations: bool = False,
    timezone=None,
) -> dict:
    """Project every eligible observation in chronological order.

    ``all_observations`` is retained as a compatibility alias: default drafts
    already include every eligible legacy-v1 observation.
    """
    zone = timezone or _local_timezone()
    lo, hi = _day_epochs(date, zone)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        columns = {
            row["name"] for row in con.execute("PRAGMA table_info(observations)")
        }
        order_by = "created_at_epoch, id" if "id" in columns else "created_at_epoch, rowid"
        rows = con.execute(
            "SELECT project, title, narrative, created_at_epoch FROM observations"
            " WHERE created_at_epoch >= ? AND created_at_epoch < ?"
            f" ORDER BY {order_by}",
            (lo, hi),
        ).fetchall()
    finally:
        con.close()

    events = _legacy_events(rows, zone)
    return {
        "schema": SCHEMA,
        "member": member,
        "date": date,
        "events": events,
        "journal_md": render_journal(events, date),
    }
