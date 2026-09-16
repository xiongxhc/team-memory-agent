"""Bounded, read-only retrieval from the TeamMem evidence ledger."""

import json
import re
import sqlite3
import time
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .state import Evidence


_MAX_QUERY_TEXT = 400
_MAX_SNIPPET_TEXT = 800
_MAX_URL_LENGTH = 2048
_MAX_RESULTS = 8
_MAX_CANDIDATES = 128
_MAX_QUERY_TOKENS = 24
_MAX_RAW_SEARCH_TEXT = 32_768
_SEARCH_TIMEOUT_SECONDS = 5.0
_QUERY_KEYS = frozenset({"text", "start", "end", "person", "project"})
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_ORDINAL = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)$")
_STOP_WORDS = frozenset({
    "a", "about", "an", "and", "are", "did", "do", "doing", "for", "from",
    "how", "i", "is", "it", "make", "many", "me", "of", "ok", "on", "our",
    "please", "tell", "the", "this", "to", "us", "was", "we", "what", "when",
    "where", "who", "why", "with", "you",
})
_MONTHS = {
    "jan": "01", "january": "01", "feb": "02", "february": "02",
    "mar": "03", "march": "03", "apr": "04", "april": "04", "may": "05",
    "jun": "06", "june": "06", "jul": "07", "july": "07", "aug": "08",
    "august": "08", "sep": "09", "sept": "09", "september": "09",
    "oct": "10", "october": "10", "nov": "11", "november": "11",
    "dec": "12", "december": "12",
}
_MONTH_ALIASES = {
    number: tuple(name for name, value in _MONTHS.items() if value == number)
    for number in frozenset(_MONTHS.values())
}


class RetrievalTimeoutError(RuntimeError):
    """The bounded ledger search did not finish before its local deadline."""


def open_ledger_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open the live ledger without migrations and with writes disabled."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _timestamp(value: Any, name: str) -> tuple[str | None, datetime | None]:
    if value is None:
        return None, None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(f"query {name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"query {name} must be an ISO timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"query {name} must include a timezone offset")
    return value, parsed


def _query(value: Mapping[str, Any]) -> tuple[str, str | None, str | None, str | None, str | None]:
    if not isinstance(value, Mapping) or set(value) - _QUERY_KEYS or "text" not in value:
        raise ValueError("query must contain only text, start, end, person, and project")
    text = value["text"]
    if not isinstance(text, str) or len(text) > _MAX_QUERY_TEXT:
        raise ValueError("query text must be a bounded string")
    start, start_at = _timestamp(value.get("start"), "start")
    end, end_at = _timestamp(value.get("end"), "end")
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise ValueError("query start must be before end")
    person = value.get("person")
    if person is not None and (not isinstance(person, str) or not person.strip() or len(person) > 200):
        raise ValueError("query person must be a bounded non-empty string")
    project = value.get("project")
    if project is not None and (not isinstance(project, str) or not project.strip() or len(project) > 200):
        raise ValueError("query project must be a bounded non-empty string")
    text = text.strip()
    if not text and person is None and project is None:
        raise ValueError("an empty query requires a person or project")
    return text, start, end, person, project


def _instant(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _tokens(text: str, limit: int | None = _MAX_QUERY_TOKENS) -> tuple[str, ...]:
    """Return bounded lexical terms, including overlapping CJK bigrams."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    output: list[str] = []
    for match in _WORDS.finditer(normalized):
        value = match.group()
        start = 0
        while start < len(value):
            cjk = _is_cjk(value[start])
            end = start + 1
            while end < len(value) and _is_cjk(value[end]) == cjk:
                end += 1
            part = value[start:end]
            if cjk:
                output.extend(
                    part[index:index + 2] for index in range(max(1, len(part) - 1))
                )
            else:
                ordinal = _ORDINAL.fullmatch(part)
                term = ordinal.group(1) if ordinal else _MONTHS.get(part, part)
                if (len(term) > 3 and term.endswith("s")
                        and not term.endswith(("ss", "us", "is"))):
                    term = term[:-1]
                if term not in _STOP_WORDS and len(term) >= 2:
                    output.append(term)
            start = end
    unique = tuple(dict.fromkeys(output))
    return unique if limit is None else unique[:limit]


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _candidate_patterns(tokens: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for token in tokens:
        values.append(token)
        values.extend(_MONTH_ALIASES.get(token, ()))
        if any(_is_cjk(character) for character in token):
            values.append(json.dumps(token, ensure_ascii=True)[1:-1])
    return tuple(_like_pattern(value) for value in dict.fromkeys(values))


def _longest_phrase(query: tuple[str, ...], candidate: tuple[str, ...]) -> int:
    longest = 0
    for query_start in range(len(query)):
        for candidate_start, term in enumerate(candidate):
            if term != query[query_start]:
                continue
            length = 1
            while (query_start + length < len(query)
                   and candidate_start + length < len(candidate)
                   and query[query_start + length] == candidate[candidate_start + length]):
                length += 1
            longest = max(longest, length)
    return longest


def _rank(tokens: tuple[str, ...], text: str, metadata: str) -> tuple[int, int, int]:
    content_sequence = _tokens(text, None)
    context_sequence = _tokens(metadata, None)
    content = set(content_sequence)
    context = set(context_sequence)
    lexical = tuple(token for token in tokens if not token.isdigit())
    temporal = tuple(token for token in tokens if token.isdigit())
    content_hits = sum(token in content for token in lexical)
    context_hits = sum(token in context and token not in content for token in lexical)
    temporal_hits = sum(token in content or token in context for token in temporal)
    phrase = max(
        _longest_phrase(tokens, content_sequence),
        _longest_phrase(tokens, context_sequence),
    )
    phrase_bonus = phrase * phrase * 6 if phrase >= 2 else 0
    return (
        content_hits * 8 + context_hits * 2 + temporal_hits + phrase_bonus,
        phrase,
        content_hits + context_hits,
    )


def _snippet(text: str, tokens: tuple[str, ...], summary: str) -> str:
    if len(text) <= _MAX_SNIPPET_TEXT:
        return text
    normalized = unicodedata.normalize("NFKC", text).casefold()
    positions = {
        position
        for token in tokens
        for term in (token, *_MONTH_ALIASES.get(token, ()))
        if (position := normalized.find(term)) >= 0
    }
    if not positions:
        return text[:_MAX_SNIPPET_TEXT]
    best_position, best_hits = 0, -1
    for position in positions:
        start = max(0, min(position - 160, len(normalized) - _MAX_SNIPPET_TEXT))
        window = normalized[start:start + _MAX_SNIPPET_TEXT]
        hits = sum(
            any(term in window for term in (token, *_MONTH_ALIASES.get(token, ())))
            for token in tokens
        )
        if hits > best_hits or (hits == best_hits and position < best_position):
            best_position, best_hits = position, hits
    if best_position < _MAX_SNIPPET_TEXT:
        return text[:_MAX_SNIPPET_TEXT]
    prefix = summary[:200]
    separator = "\n…\n"
    available = _MAX_SNIPPET_TEXT - len(prefix) - len(separator)
    start = max(len(prefix), best_position - min(160, available // 3))
    end = min(len(text), start + available)
    if not _is_cjk(text[start]):
        while start < best_position and start > 0 and text[start - 1].isalnum():
            start += 1
    return prefix + separator + text[start:end]


def _object(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _rich_event_text(row: sqlite3.Row) -> str:
    """Extract only user-authored content whose projection matches this event."""
    summary = row["summary"]
    raw = _object(row["raw"])
    if raw is None:
        return summary
    source, kind = row["source"], row["kind"]

    if source == "feishu-channel" and kind == "message" and raw.get("msg_type") == "text":
        try:
            content = json.loads(raw["body"]["content"])
            text = content.get("text") if isinstance(content, dict) else None
        except (KeyError, TypeError, ValueError):
            text = None
        if isinstance(text, str) and text and summary == text[:100]:
            return text

    if source == "github" and kind == "commit":
        commit = raw.get("commit")
        message = commit.get("message") if isinstance(commit, dict) else None
        if isinstance(message, str) and message.splitlines() and message.splitlines()[0] == summary:
            return message

    if source == "gitlab" and kind == "commit":
        message = raw.get("message")
        if isinstance(message, str) and raw.get("title") == summary:
            return message

    if source == "gitlab" and kind == "comment":
        body = raw.get("body")
        if isinstance(body, str):
            collapsed = " ".join(body.split())
            projection = collapsed if len(collapsed) <= 120 else collapsed[:119] + "…"
            prefix = summary[:-len(projection)] if projection and summary.endswith(projection) else ""
            if re.fullmatch(r"\[[!#]\d+\] ", prefix):
                return prefix + collapsed

    return summary


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
) -> list[tuple[tuple[int, int, int], Evidence]]:
    if not projects:
        return []
    tokens = _tokens(text)
    patterns = _candidate_patterns(tokens)
    if text and not patterns:
        return []
    rich_text = (
        "CASE "
        "WHEN source = 'feishu-channel' AND kind = 'message' AND json_valid(raw) "
        "THEN COALESCE(json_extract(raw, '$.body.content'), '') "
        "WHEN source = 'github' AND kind = 'commit' AND json_valid(raw) "
        "THEN COALESCE(json_extract(raw, '$.commit.message'), '') "
        "WHEN source = 'gitlab' AND kind = 'commit' AND json_valid(raw) "
        "THEN COALESCE(json_extract(raw, '$.message'), '') "
        "WHEN source = 'gitlab' AND kind = 'comment' AND json_valid(raw) "
        "THEN COALESCE(json_extract(raw, '$.body'), '') ELSE '' END"
    )
    search_text = (
        "LOWER(COALESCE(project, '') || ' ' || person || ' ' || ts || ' ' || kind || "
        "' ' || summary || ' ' || SUBSTR(" + rich_text + f", 1, {_MAX_RAW_SEARCH_TEXT}))"
    )
    matches = [f"{search_text} LIKE ? ESCAPE '\\'" for _ in patterns]
    clauses = [f"project IN ({','.join('?' for _ in projects)})"]
    params: list[Any] = [*projects]
    if matches:
        clauses.append("(" + " OR ".join(matches) + ")")
        params.extend(patterns)
    if start is not None:
        clauses.append("julianday(ts) >= julianday(?)")
        params.append(start)
    if end is not None:
        clauses.append("julianday(ts) < julianday(?)")
        params.append(end)
    if person is not None:
        clauses.append("person = ?")
        params.append(person)
    relevance = " + ".join(f"CASE WHEN {search_text} LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
                           for _ in patterns) or "0.0"
    if patterns:
        params.extend(patterns)
    params.append(_MAX_CANDIDATES)
    rows = conn.execute(
        "SELECT id, project, person, ts, source, kind, summary, refs, "
        f"SUBSTR(raw, 1, {_MAX_RAW_SEARCH_TEXT}) AS raw FROM events WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY ({relevance}) DESC, julianday(ts) DESC, id DESC LIMIT ?",
        params,
    ).fetchall()
    ranked = []
    for row in rows:
        full_text = _rich_event_text(row)
        evidence = Evidence(
            id=str(row["id"]), project=row["project"], timestamp=row["ts"],
            text=_snippet(full_text, tokens, row["summary"]), url=_url(row["refs"]),
            person=row["person"],
        )
        score = _rank(
            tokens, full_text,
            f"{row['project']} {row['person']} {row['ts']} {row['kind']}",
        )
        if score[0] or not tokens:
            ranked.append((score, evidence))
    return ranked


def _count_evidence(
    conn: sqlite3.Connection,
    projects: tuple[str, ...],
    text: str,
    start: str | None,
    end: str | None,
    person: str | None,
    limit: int,
) -> list[tuple[tuple[int, int, int], Evidence]]:
    if not projects:
        return []
    tokens = _tokens(text)
    patterns = _candidate_patterns(tokens)
    if text and not patterns:
        return []
    search_text = "LOWER(project || ' commits ' || person || ' ' || week_start || ' ' || commit_count)"
    matches = [f"{search_text} LIKE ? ESCAPE '\\'" for _ in patterns]
    clauses = [f"project IN ({','.join('?' for _ in projects)})"]
    params: list[Any] = [*projects]
    if matches:
        clauses.append("(" + " OR ".join(matches) + ")")
        params.extend(patterns)
    if start is not None:
        clauses.append("julianday(week_start) >= julianday(?)")
        params.append(start)
    if end is not None:
        clauses.append("julianday(week_start, '+7 days') <= julianday(?)")
        params.append(end)
    if person is not None:
        clauses.append("person = ?")
        params.append(person)
    relevance = " + ".join(f"CASE WHEN {search_text} LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"
                           for _ in patterns) or "0.0"
    if patterns:
        params.extend(patterns)
    params.append(_MAX_CANDIDATES)
    rows = conn.execute(
        "SELECT project, week_start, person, commit_count FROM weekly_commit_counts WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY ({relevance}) DESC, week_start DESC, project ASC, person ASC LIMIT ?",
        params,
    ).fetchall()
    ranked = []
    for row in rows:
        evidence = Evidence(
            id=f"count:{row['project']}:{row['week_start']}:{row['person']}",
            project=row["project"], timestamp=row["week_start"],
            text=(f"{row['commit_count']} commits by {row['person']} for week starting "
                  f"{row['week_start']}"),
            url=None,
        )
        score = _rank(tokens, evidence.text, row["project"])
        if score[0] or not tokens:
            ranked.append((score, evidence))
    return ranked


def _resource_parent(evidence: Evidence) -> str | None:
    if not evidence.id.isdigit() or not evidence.url:
        return None
    parsed = urlparse(evidence.url)
    if parsed.fragment and not re.fullmatch(r"note_\d+", parsed.fragment):
        return None
    parent = evidence.url.rsplit("#", 1)[0]
    if not re.search(r"/-/(?:issues|merge_requests)/\d+/?$", parent):
        return None
    return parent


def _latest_resource_update(
    conn: sqlite3.Connection,
    evidence: Evidence,
    parent: str,
    start: str | None,
    end: str | None,
    person: str | None,
) -> Evidence | None:
    """Find the latest allowed event for the same GitLab resource and query scope."""
    source = conn.execute(
        "SELECT source, kind FROM events WHERE id = ? AND project = ?",
        (int(evidence.id), evidence.project),
    ).fetchone()
    if (source is None or source["source"] != "gitlab"
            or source["kind"] not in {"comment", "issue", "mr"}):
        return None
    clauses = [
        "project = ?",
        "source = 'gitlab'",
        "kind IN ('comment', 'issue', 'mr')",
        "CASE WHEN json_valid(refs) THEN "
        "RTRIM(SUBSTR(json_extract(refs, '$.url'), 1, "
        "CASE WHEN INSTR(json_extract(refs, '$.url'), '#note_') > 0 "
        "THEN INSTR(json_extract(refs, '$.url'), '#note_') - 1 "
        "ELSE LENGTH(json_extract(refs, '$.url')) END), '/') = ? ELSE 0 END",
    ]
    params: list[Any] = [evidence.project, parent.rstrip("/")]
    if start is not None:
        clauses.append("julianday(ts) >= julianday(?)")
        params.append(start)
    if end is not None:
        clauses.append("julianday(ts) < julianday(?)")
        params.append(end)
    if person is not None:
        clauses.append("person = ?")
        params.append(person)
    row = conn.execute(
        "SELECT id, project, person, ts, source, kind, summary, refs, "
        f"SUBSTR(raw, 1, {_MAX_RAW_SEARCH_TEXT}) AS raw FROM events WHERE "
        + " AND ".join(clauses)
        + " ORDER BY julianday(ts) DESC, id DESC LIMIT 1",
        params,
    ).fetchone()
    if row is None or str(row["id"]) == evidence.id:
        return None
    # A malformed or forged source URL cannot define the discussion lineage.
    url = _url(row["refs"])
    if not url or url.rsplit("#", 1)[0].rstrip("/") != parent.rstrip("/"):
        return None
    text = _rich_event_text(row)
    return Evidence(
        id=str(row["id"]), project=row["project"], timestamp=row["ts"],
        text=_snippet(text, (), row["summary"]), url=url, person=row["person"],
    )


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
    text, start, end, person, project = _query(query)
    detail_projects, count_projects = _scope(project_policy, allowed_projects)
    if project is not None:
        detail_projects = tuple(value for value in detail_projects if value == project)
        count_projects = tuple(value for value in count_projects if value == project)
    if not detail_projects and not count_projects:
        return []
    with open_ledger_readonly(db_path) as conn:
        deadline = time.monotonic() + _SEARCH_TIMEOUT_SECONDS
        conn.set_progress_handler(
            lambda: int(time.monotonic() >= deadline),
            100,
        )
        try:
            ranked = _detail_evidence(conn, detail_projects, text, start, end, person, limit)
            ranked.extend(_count_evidence(conn, count_projects, text, start, end, person, limit))
            ranked.sort(key=lambda item: (item[0], _instant(item[1].timestamp), item[1].id), reverse=True)
            selected = [evidence for _, evidence in ranked[:limit]]
            visited: dict[tuple[str, str], Evidence | None] = {}
            for index, evidence in enumerate(selected):
                parent = _resource_parent(evidence)
                if parent is None:
                    continue
                key = (evidence.project, parent)
                if key not in visited:
                    visited[key] = _latest_resource_update(
                        conn, evidence, parent, start, end, person,
                    )
                latest = visited[key]
                if latest is not None and _instant(latest.timestamp) >= _instant(evidence.timestamp):
                    selected[index] = latest
        except sqlite3.OperationalError as exc:
            if "interrupted" not in str(exc).casefold():
                raise
            raise RetrievalTimeoutError(
                "The evidence search took too long. Please narrow the question."
            ) from exc
        finally:
            conn.set_progress_handler(None, 0)
    return list(dict((evidence.id, evidence) for evidence in selected).values())
