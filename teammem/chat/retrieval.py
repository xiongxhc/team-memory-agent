"""Bounded, read-only retrieval from the TeamMem evidence ledger."""

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .state import Evidence


_MAX_QUERY_TEXT = 400
_MAX_SNIPPET_TEXT = 800
_MAX_URL_LENGTH = 2048
_MAX_RESULTS = 8
_QUERY_KEYS = frozenset({"text", "start", "end", "person"})


def open_ledger_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open the live ledger without migrations and with writes disabled."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _timestamp(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(f"query {name} must be an ISO timestamp")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    except ValueError as exc:
        raise ValueError(f"query {name} must be an ISO timestamp") from exc
    return value


def _query(value: Mapping[str, Any]) -> tuple[str, str | None, str | None, str | None]:
    if not isinstance(value, Mapping) or set(value) - _QUERY_KEYS or "text" not in value:
        raise ValueError("query must contain only text, start, end, and person")
    text = value["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_QUERY_TEXT:
        raise ValueError("query text must be a bounded non-empty string")
    start = _timestamp(value.get("start"), "start")
    end = _timestamp(value.get("end"), "end")
    if start is not None and end is not None and start >= end:
        raise ValueError("query start must be before end")
    person = value.get("person")
    if person is not None and (not isinstance(person, str) or not person or len(person) > 200):
        raise ValueError("query person must be a bounded non-empty string")
    return text.casefold(), start, end, person


def _url(refs: str | None) -> str | None:
    try:
        value = (json.loads(refs) or {}).get("url")
    except (TypeError, ValueError, AttributeError):
        return None
    if not isinstance(value, str) or len(value) > _MAX_URL_LENGTH:
        return None
    if urlparse(value).scheme not in {"http", "https"}:
        return None
    return value


def _scope(project_policy: Mapping[str, str], allowed_projects: frozenset[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(project_policy, Mapping):
        raise ValueError("project policy must be a mapping")
    if not isinstance(allowed_projects, frozenset) or any(not isinstance(project, str) or not project for project in allowed_projects):
        raise ValueError("allowed projects must be a frozenset of slugs")
    detail = tuple(sorted(
        project for project in allowed_projects
        if project_policy.get(project) == "detail"
    ))
    count_only = tuple(sorted(
        project for project in allowed_projects
        if project_policy.get(project) == "count_only"
    ))
    return detail, count_only


def _detail_evidence(
    conn: sqlite3.Connection,
    projects: tuple[str, ...],
    text: str,
    start: str | None,
    end: str | None,
    person: str | None,
    limit: int,
) -> list[Evidence]:
    if not projects:
        return []
    clauses = [f"project IN ({','.join('?' for _ in projects)})", "LOWER(summary) LIKE ?"]
    params: list[Any] = [*projects, f"%{text}%"]
    if start is not None:
        clauses.append("ts >= ?")
        params.append(start)
    if end is not None:
        clauses.append("ts < ?")
        params.append(end)
    if person is not None:
        clauses.append("person = ?")
        params.append(person)
    params.append(limit)
    rows = conn.execute(
        "SELECT id, project, ts, summary, refs FROM events WHERE "
        + " AND ".join(clauses)
        + " ORDER BY ts DESC, id DESC LIMIT ?",
        params,
    ).fetchall()
    return [Evidence(
        id=str(row["id"]), project=row["project"], timestamp=row["ts"],
        text=row["summary"][:_MAX_SNIPPET_TEXT], url=_url(row["refs"]),
    ) for row in rows]


def _count_evidence(
    conn: sqlite3.Connection,
    projects: tuple[str, ...],
    text: str,
    start: str | None,
    end: str | None,
    person: str | None,
    limit: int,
) -> list[Evidence]:
    if not projects:
        return []
    clauses = [
        f"project IN ({','.join('?' for _ in projects)})",
        "LOWER(project || ' commits ' || person || ' ' || commit_count) LIKE ?",
    ]
    params: list[Any] = [*projects, f"%{text}%"]
    if start is not None:
        clauses.append("week_start >= ?")
        params.append(start[:10])
    if end is not None:
        clauses.append("week_start < ?")
        params.append(end[:10])
    if person is not None:
        clauses.append("person = ?")
        params.append(person)
    params.append(limit)
    rows = conn.execute(
        "SELECT project, week_start, person, commit_count FROM weekly_commit_counts WHERE "
        + " AND ".join(clauses)
        + " ORDER BY week_start DESC, project ASC, person ASC LIMIT ?",
        params,
    ).fetchall()
    return [Evidence(
        id=f"count:{row['project']}:{row['week_start']}:{row['person']}",
        project=row["project"], timestamp=row["week_start"],
        text=(f"{row['commit_count']} commits by {row['person']} for week starting "
              f"{row['week_start']}"),
        url=None,
    ) for row in rows]


def search_evidence(
    db_path: str | Path,
    project_policy: Mapping[str, str],
    allowed_projects: frozenset[str],
    query: Mapping[str, Any],
    limit: int = _MAX_RESULTS,
) -> list[Evidence]:
    """Return at most eight project-scoped lexical evidence records.

    Every permitted project is placed in the SQL predicates before any evidence
    rows are fetched. Hidden, unclassified, and no-project events therefore have
    no path into model context.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_RESULTS:
        raise ValueError("limit must be between 1 and 8")
    text, start, end, person = _query(query)
    detail_projects, count_projects = _scope(project_policy, allowed_projects)
    if not detail_projects and not count_projects:
        return []
    with open_ledger_readonly(db_path) as conn:
        evidence = _detail_evidence(conn, detail_projects, text, start, end, person, limit)
        evidence.extend(_count_evidence(conn, count_projects, text, start, end, person, limit))
    return sorted(evidence, key=lambda item: (item.timestamp, item.id), reverse=True)[:limit]
