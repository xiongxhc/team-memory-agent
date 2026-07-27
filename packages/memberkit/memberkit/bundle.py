"""Draft a teammem-bundle/v1 from the local claude-mem observations db (read-only)."""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

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


def _day_epochs(date: str) -> tuple[int, int]:
    start = datetime.fromisoformat(date).astimezone()
    # fixed-offset window: on DST-transition days (not UAE) the boundary hour may mis-bucket
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


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
