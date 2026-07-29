"""Draft a teammem-bundle/v1 from the local claude-mem observations db (read-only)."""

import json
import os
import re
import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA = "teammem-bundle/v1"
SUMMARY_LIMIT = 120
PROJECT_LIMIT = 7

_GENERIC_TITLE = re.compile(
    r"^(?:task(?:\s+\d+)?|update|progress|work|notes?|review|implementation|"
    r"status|summary|tests?\s+passed|red\s+phase|green\s+phase)$",
    re.IGNORECASE,
)
_PRIVATE_PATH = re.compile(
    r"(?:/Users/|/home/|file://|~/(?:\.|Library/)|[A-Za-z]:\\)"
)
_SIGNALS = (
    (500, ("security", "privacy", "credential", "secret", "direct message", "dm ")),
    (400, ("decision", "decided", "approved", "architecture", "contract", "design")),
    (300, ("blocker", "blocked", "risk", "unresolved", "unsupported", "failure", "defect")),
    (200, ("release", "released", "publish", "published", "shipped", "deployed", "merged")),
    (100, ("added", "fixed", "completed", "resolved", "implemented", "verified", "prevent")),
)
_MECHANICS = (
    "progress", "test passed", "tests passed", "review dispatched", "staged",
    "commit", "worktree", "red phase", "green phase",
)


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
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _legacy_events(rows: list[sqlite3.Row]) -> list[dict]:
    return [
        {
            "ts": row["created_at"],
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


def _normalize(text: str | None) -> str:
    return " ".join((text or "").split())


def _is_generic(text: str) -> bool:
    return bool(_GENERIC_TITLE.fullmatch(text.strip(" .:;-")))


def _meaningful(text: str | None) -> str:
    normalized = _normalize(text)
    if not normalized or _is_generic(normalized):
        return ""
    return normalized


def _first_sentence(text: str | None) -> str:
    normalized = _normalize(text)
    if not normalized:
        return ""
    match = re.match(r"^(.+?[.!?])(?:\s|$)", normalized)
    return match.group(1) if match else normalized


def _fact_text(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        value = raw
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = [item for item in value if isinstance(item, str)]
    elif isinstance(value, dict):
        values = [item for item in value.values() if isinstance(item, str)]
    else:
        values = []
    for value in values:
        sentence = _first_sentence(value)
        if sentence and not _PRIVATE_PATH.search(sentence):
            return sentence
    return ""


def _summary(row: sqlite3.Row) -> str:
    choices = (
        _meaningful(row["title"]),
        _meaningful(row["subtitle"]),
        _meaningful(_first_sentence(row["narrative"])),
        _meaningful(_fact_text(row["facts"])),
    )
    for selected in choices:
        if selected and not _PRIVATE_PATH.search(selected):
            return selected[:SUMMARY_LIMIT]
    return ""


def _score(row: sqlite3.Row, summary: str) -> int:
    text = " ".join([
        summary,
        _normalize(row["title"]),
        _normalize(row["subtitle"]),
        _normalize(row["narrative"]),
        _normalize(row["type"]),
    ]).casefold()
    score = 0
    for value, signals in _SIGNALS:
        if any(signal in text for signal in signals):
            score = value
            break
    if (row["type"] or "").casefold() == "decision":
        score = max(score, 400)
    if any(signal in text for signal in _MECHANICS):
        score -= 25
    return score


def _curated_events(rows: list[sqlite3.Row]) -> list[dict]:
    candidates: list[dict] = []
    seen: set[tuple[str | None, str]] = set()
    for index, row in enumerate(rows):
        summary = _summary(row)
        key = (row["project"], summary.casefold())
        if not summary or key in seen:
            continue
        seen.add(key)
        candidates.append({
            "index": index,
            "epoch": row["created_at_epoch"],
            "session": row["memory_session_id"] or f"__row_{index}",
            "score": _score(row, summary),
            "event": {
                "ts": row["created_at"],
                "kind": "journal-highlight",
                "summary": summary,
                "project": row["project"],
                "refs": None,
            },
        })

    best_by_session: dict[tuple[str | None, str], dict] = {}
    for candidate in candidates:
        event = candidate["event"]
        key = (event["project"], candidate["session"])
        current = best_by_session.get(key)
        if current is None or candidate["score"] > current["score"]:
            best_by_session[key] = candidate

    by_project: dict[str | None, list[dict]] = {}
    for candidate in best_by_session.values():
        by_project.setdefault(candidate["event"]["project"], []).append(candidate)

    selected: list[dict] = []
    for candidates_for_project in by_project.values():
        ranked = sorted(
            candidates_for_project,
            key=lambda candidate: (-candidate["score"], candidate["epoch"],
                                   candidate["index"]),
        )
        selected.extend(ranked[:PROJECT_LIMIT])

    selected.sort(key=lambda candidate: (candidate["epoch"], candidate["index"]))
    return [candidate["event"] for candidate in selected]


def _column_expression(columns: set[str], name: str) -> str:
    return name if name in columns else f"NULL AS {name}"


def draft(
    db_path: Path,
    member: str,
    date: str,
    *,
    all_observations: bool = False,
) -> dict:
    lo, hi = _day_epochs(date)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        columns = {
            row["name"] for row in con.execute("PRAGMA table_info(observations)")
        }
        selected = [
            "project",
            "title",
            "narrative",
            "created_at",
            "created_at_epoch",
        ]
        if not all_observations:
            selected.extend([
                _column_expression(columns, "memory_session_id"),
                _column_expression(columns, "subtitle"),
                _column_expression(columns, "facts"),
                _column_expression(columns, "type"),
            ])
        rows = con.execute(
            f"SELECT {', '.join(selected)} FROM observations"
            " WHERE created_at_epoch >= ? AND created_at_epoch < ?"
            " ORDER BY created_at_epoch",
            (lo, hi),
        ).fetchall()
    finally:
        con.close()

    events = _legacy_events(rows) if all_observations else _curated_events(rows)

    return {"schema": SCHEMA, "member": member, "date": date,
            "events": events, "journal_md": render_journal(events, date)}
