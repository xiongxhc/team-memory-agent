"""Draft a teammem-bundle/v1 from the local claude-mem observations db (read-only)."""

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
_PATH_LIKE = (
    re.compile(r"\bfile://[^\s,;!?]+", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9_:/])/(?!/)[^\s,;:!?]+"),
    re.compile(r"(?<!\w)~[\\/][^\s,;:!?]+"),
    re.compile(r"(?<!\w)[A-Za-z]:[\\/][^\s,;:!?]+"),
    re.compile(r"\\\\[^\\/\s]+[\\/][^\\/\s]+"),
    re.compile(r"(?<!:)(?<!/)//[^/\s]+/[^/\s]+"),
    re.compile(r"(?<!\w)\.\.?[\\/][^\s,;:!?]+"),
    re.compile(
        r"(?<![\w./\\])(?:[A-Za-z0-9_.-]+[\\/])+"
        r"[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12}(?!\w)"
    ),
    re.compile(
        r"(?<![\w./\\])(?:src|lib|bin|docs|tests?|packages?|apps?|config|"
        r"scripts?|\.github|node_modules|vendor)"
        r"(?:[\\/][A-Za-z0-9_.-]+)+(?![\w/\\])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\w./\\])(?:README\.(?:md|rst|txt)|pyproject\.toml|"
        r"\.env(?:\.[A-Za-z0-9_-]+)?|Dockerfile(?:\.[A-Za-z0-9_-]+)?|"
        r"(?:docker-)?compose\.ya?ml)(?![\w./\\])",
        re.IGNORECASE,
    ),
)
_SIGNALS = (
    (500, ("security", "privacy", "credential", "secret", "direct message", "dm ")),
    (400, ("decision", "decided", "approved", "architecture", "contract", "design")),
    (300, ("blocker", "blocked", "risk", "unresolved", "unsupported", "failure", "defect")),
    (200, ("release", "released", "publish", "published", "shipped", "deployed", "merged")),
    (100, ("added", "fixed", "completed", "resolved", "implemented", "verified", "prevent")),
)
_MECHANICS_PREFIX = (
    re.compile(r"^(?:progress|update)\b", re.IGNORECASE),
    re.compile(r"^tests? pass(?:ed)?\b", re.IGNORECASE),
    re.compile(r"^code review\b", re.IGNORECASE),
    re.compile(r"^review dispatched\b", re.IGNORECASE),
    re.compile(r"^verification checks?\b", re.IGNORECASE),
    re.compile(r"^pre-push verification\b", re.IGNORECASE),
    re.compile(r"^diff inspection\b.*$", re.IGNORECASE),
    re.compile(r"^(?:public-source scan|check-public)\b.*$", re.IGNORECASE),
    re.compile(r"^commit staged\b", re.IGNORECASE),
    re.compile(r"^(?:red|green)\b", re.IGNORECASE),
    re.compile(
        r"^(?:implementation initiated|task started|tdd approach)\b.*$",
        re.IGNORECASE,
    ),
)
_SUBSTANTIVE_MARKER = re.compile(
    r"\b(?:decision|decided|approved|risk|blocker|blocked|defect|unresolved|"
    r"release|released|resolved|fixed|implemented|enforced|prevented|shipped|"
    r"published|deployed|merged)\b",
    re.IGNORECASE,
)
_OUTCOME_SIGNALS = (
    "enforc", "prevent", "resolv", "fixed", "implemented", "completed",
    "shipped", "published",
)
_TYPE_OUTCOME_SCORE = {
    "bugfix": 50,
    "decision": 40,
    "change": 30,
    "feature": 20,
}


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


def _contains_path_like(text: str) -> bool:
    return any(pattern.search(text) for pattern in _PATH_LIKE)


def _safe_meaningful(text: str | None) -> str:
    selected = _meaningful(text)
    if selected and not _contains_path_like(selected):
        return selected
    return ""


def _safe_narrative(text: str | None) -> str:
    normalized = _normalize(text)
    for sentence in re.split(r"(?<=[.!?])\s+", normalized):
        selected = _safe_meaningful(sentence)
        if selected:
            return selected
    return ""


def _combine_summary(title: str, subtitle: str) -> str:
    return _truncate(f"{title} — {subtitle}", SUMMARY_LIMIT)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    shortened = text[:limit - 1].rstrip()
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(" ,;:—-") + "…"


def _summary(row: sqlite3.Row) -> str:
    title = _safe_meaningful(row["title"])
    subtitle = _safe_meaningful(row["subtitle"])
    narrative = _safe_narrative(row["narrative"])
    if title and subtitle:
        return _combine_summary(title, subtitle)
    return (title or subtitle or narrative)[:SUMMARY_LIMIT]


def _score(row: sqlite3.Row, summary: str) -> tuple[int, int]:
    text = " ".join([
        summary,
        _normalize(row["type"]),
    ]).casefold()
    mechanics_prefix = any(
        pattern.search(summary.strip()) for pattern in _MECHANICS_PREFIX
    )
    if mechanics_prefix and not _has_substantive_marker(summary):
        return (-100, 0)

    primary = 0
    for value, signals in _SIGNALS:
        if any(signal in text for signal in signals):
            primary = value
            break
    observation_type = (row["type"] or "").casefold()
    if observation_type == "decision":
        primary = max(primary, 400)
    secondary = _TYPE_OUTCOME_SCORE.get(observation_type, 0)
    if any(signal in summary.casefold() for signal in _OUTCOME_SIGNALS):
        secondary += 100
    return primary, secondary


def _has_substantive_marker(summary: str) -> bool:
    for match in _SUBSTANTIVE_MARKER.finditer(summary):
        before = summary[max(0, match.start() - 32):match.start()]
        if re.search(
            r"\b(?:no|not|without)\s+(?:[a-z-]+\s+){0,2}$",
            before,
            re.IGNORECASE,
        ):
            continue
        return True
    return False


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
            (
                candidate for candidate in candidates_for_project
                if candidate["score"][0] >= 0
            ),
            key=lambda candidate: (
                -candidate["score"][0],
                -candidate["score"][1],
                candidate["epoch"],
                candidate["index"],
            ),
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
                _column_expression(columns, "type"),
            ])
        order_by = "created_at_epoch"
        if not all_observations:
            order_by += ", id" if "id" in columns else ", rowid"
        rows = con.execute(
            f"SELECT {', '.join(selected)} FROM observations"
            " WHERE created_at_epoch >= ? AND created_at_epoch < ?"
            f" ORDER BY {order_by}",
            (lo, hi),
        ).fetchall()
    finally:
        con.close()

    events = _legacy_events(rows) if all_observations else _curated_events(rows)

    return {"schema": SCHEMA, "member": member, "date": date,
            "events": events, "journal_md": render_journal(events, date)}
