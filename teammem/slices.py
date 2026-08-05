"""Deterministic input assembly for synthesis. Pure — no LLM, no wall-clock.
Day boundary = the local date prefix of the offset-aware ISO ts (documented
approximation, consistent with M2 queries)."""

import hashlib
import json
import sqlite3


def _rows(conn: sqlite3.Connection, person: str, day: str) -> list[dict]:
    cur = conn.execute(
        "SELECT project, ts, kind, summary, raw, source, hash FROM events"
        " WHERE person = ? AND substr(ts, 1, 10) = ?"
        " ORDER BY ts, kind, summary, project, source, hash", (person, day))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _message_text(row: dict) -> str:
    try:
        raw = json.loads(row["raw"])
        if raw.get("msg_type") == "text":
            return json.loads(raw["body"]["content"]).get("text") or row["summary"]
    except (TypeError, ValueError, KeyError, AttributeError):
        pass
    return row["summary"]


def daily_person_slice(conn: sqlite3.Connection, person: str, day: str) -> str:
    lines = []
    for r in _rows(conn, person, day):
        text = _message_text(r) if r["kind"] == "message" else r["summary"]
        lines.append(f"{r['ts']}  {r['kind']}  {r['project'] or '-'}  {text}")
    return "\n".join(lines)


def daily_person_projects(
    conn: sqlite3.Connection, person: str, day: str
) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT project FROM events"
            " WHERE person = ? AND substr(ts, 1, 10) = ?"
            " AND project IS NOT NULL ORDER BY project",
            (person, day),
        )
    ]


def daily_person_event_count(
    conn: sqlite3.Connection, person: str, day: str
) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM events"
        " WHERE person = ? AND substr(ts, 1, 10) = ?",
        (person, day),
    ).fetchone()[0]


def slice_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def weekly_team_input(daily_texts: list[dict]) -> str:
    return "\n\n".join(
        f"## {d['person']} — {d['day']}\n{d['text']}"
        for d in sorted(daily_texts, key=lambda d: (d["person"], d["day"])))


def active_person_days(conn: sqlite3.Connection, start_day: str,
                       end_day: str) -> list[tuple[str, str]]:
    return [tuple(r) for r in conn.execute(
        "SELECT DISTINCT person, substr(ts, 1, 10) AS day FROM events"
        " WHERE day >= ? AND day <= ? AND person NOT LIKE '_unmapped/%'"
        " ORDER BY person, day", (start_day, end_day))]
