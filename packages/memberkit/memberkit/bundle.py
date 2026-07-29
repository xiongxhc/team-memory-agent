"""Draft a teammem-bundle/v1 from the local claude-mem observations db (read-only)."""

import json
import os
import sqlite3
import tempfile
from datetime import date as calendar_date
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = "teammem-bundle/v1"
SUMMARY_LIMIT = 120
_TOP_KEYS = {"schema", "member", "date", "events", "journal_md"}
_EVENT_KEYS = {"ts", "kind", "summary", "project", "refs"}


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


def validate_bundle(data: object, member: str, date: str) -> dict:
    if not isinstance(data, dict) or set(data) != _TOP_KEYS:
        raise ValueError("bundle must have the exact frozen-v1 fields")
    if data["schema"] != SCHEMA:
        raise ValueError(f"bundle schema must be {SCHEMA}")
    if data["member"] != member:
        raise ValueError("bundle member does not match configured member")
    if data["date"] != date:
        raise ValueError("bundle date does not match requested date")
    try:
        if calendar_date.fromisoformat(date).isoformat() != date:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("bundle date must be YYYY-MM-DD") from exc
    if not isinstance(data["events"], list):
        raise ValueError("bundle events must be an array")
    if not isinstance(data["journal_md"], str):
        raise ValueError("bundle journal_md must be a string")

    for index, event in enumerate(data["events"]):
        prefix = f"bundle event {index}"
        if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
            raise ValueError(f"{prefix} must have the exact frozen-v1 fields")
        if not isinstance(event["ts"], str):
            raise ValueError(f"{prefix} timestamp must be a string")
        try:
            event_date = datetime.fromisoformat(
                event["ts"].replace("Z", "+00:00")
            ).date().isoformat()
        except ValueError as exc:
            raise ValueError(f"{prefix} timestamp is invalid") from exc
        if event_date != date:
            raise ValueError(f"{prefix} timestamp is outside bundle date")
        if event["kind"] != "journal-highlight":
            raise ValueError(f"{prefix} kind must be journal-highlight")
        if not isinstance(event["summary"], str) or not event["summary"].strip():
            raise ValueError(f"{prefix} summary must be non-empty")
        if event["project"] is not None and not isinstance(event["project"], str):
            raise ValueError(f"{prefix} project must be a string or null")
        if event["refs"] is not None:
            raise ValueError(f"{prefix} refs must be null")
    return data


def write_bundle(path: Path, data: dict) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def prepare_bundle(path: Path, member: str, date: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid bundle JSON: {exc}") from exc
    validated = validate_bundle(data, member, date)
    validated["journal_md"] = render_journal(validated["events"], date)
    write_bundle(path, validated)
    return validated


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
