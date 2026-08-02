"""The ledger — Layer 1, the single source of truth. INSERT OR IGNORE on the
UNIQUE(person, source, hash) key makes every ingest path idempotent."""

import json
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

from .events import Event
from .identity import IdentityMaps

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY,
  person  TEXT NOT NULL,
  project TEXT,
  ts      TEXT NOT NULL,
  source  TEXT NOT NULL,
  kind    TEXT NOT NULL,
  summary TEXT NOT NULL,
  refs    TEXT,
  raw     TEXT,
  hash    TEXT NOT NULL,
  UNIQUE(person, source, hash)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_person_ts ON events(person, ts);
CREATE TABLE IF NOT EXISTS summaries (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,
  key        TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  text       TEXT NOT NULL,
  model      TEXT NOT NULL,
  created_ts TEXT NOT NULL,
  UNIQUE(kind, key)
);
"""


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    return conn


def insert_events(conn: sqlite3.Connection, events: Iterable[Event]) -> int:
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO events (person, project, ts, source, kind, summary, refs, raw, hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(e.person, e.project, e.ts, e.source, e.kind, e.summary, e.refs, e.raw, e.hash)
             for e in events],
        )
    return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] - before


def reconcile_gitlab_events(
    conn: sqlite3.Connection,
    events: Iterable[Event],
    ids: IdentityMaps,
) -> int:
    """Atomically replace authoritative GitLab issue/repo facts and insert others."""
    with conn:
        for repair in _legacy_opened_issue_repairs(conn, ids):
            _replace_gitlab_event(conn, repair, existing_only=True)

        inserted = 0
        for event in events:
            if event.source == "gitlab" and event.kind in {"issue", "repo"}:
                inserted += _replace_gitlab_event(conn, event)
            else:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO events "
                    "(person, project, ts, source, kind, summary, refs, raw, hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _event_values(event),
                )
                inserted += cursor.rowcount
    return inserted


def _legacy_opened_issue_repairs(
    conn: sqlite3.Connection,
    ids: IdentityMaps,
) -> list[Event]:
    repairs = []
    rows = conn.execute(
        "SELECT project, ts, raw, hash FROM events "
        "WHERE source = 'gitlab' AND kind = 'issue' "
        "AND summary LIKE '[opened] %' AND raw IS NOT NULL"
    ).fetchall()
    for project, timestamp, raw, event_hash in rows:
        try:
            issue = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(issue, dict):
            continue
        created_at = issue.get("created_at")
        if not isinstance(created_at, str) or created_at == timestamp:
            continue
        author = issue.get("author") or {}
        username = author.get("username", "") if isinstance(author, dict) else ""
        repairs.append(Event(
            person=ids.person("gitlab", username),
            project=project,
            ts=created_at,
            source="gitlab",
            kind="issue",
            summary=f"[opened] {issue['title']}",
            refs=json.dumps({"iid": issue["iid"], "url": issue.get("web_url")}),
            raw=raw,
            hash=event_hash,
        ))
    return repairs


def _replace_gitlab_event(
    conn: sqlite3.Connection,
    event: Event,
    *,
    existing_only: bool = False,
) -> int:
    key = (event.source, event.kind, event.hash)
    existed = conn.execute(
        "SELECT 1 FROM events WHERE source = ? AND kind = ? AND hash = ? LIMIT 1",
        key,
    ).fetchone() is not None
    if existing_only and not existed:
        return 0
    conn.execute(
        "DELETE FROM events WHERE source = ? AND kind = ? AND hash = ?",
        key,
    )
    conn.execute(
        "INSERT INTO events "
        "(person, project, ts, source, kind, summary, refs, raw, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _event_values(event),
    )
    return int(not existed)


def _event_values(event: Event) -> tuple:
    return (
        event.person,
        event.project,
        event.ts,
        event.source,
        event.kind,
        event.summary,
        event.refs,
        event.raw,
        event.hash,
    )


def stats(conn: sqlite3.Connection) -> dict:
    def group(col: str) -> dict:
        return dict(conn.execute(
            f"SELECT {col}, COUNT(*) FROM events GROUP BY {col} ORDER BY {col}"))
    return {
        "total": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        "by_source": group("source"),
        "by_kind": group("kind"),
        "by_person": group("person"),
        "unmapped": [r[0] for r in conn.execute(
            "SELECT DISTINCT person FROM events WHERE person LIKE '_unmapped/%' ORDER BY person")],
    }


def get_or_make(conn: sqlite3.Connection, kind: str, key: str, input_hash: str,
                make: Callable[[], tuple[str, str]], created_ts: str) -> str:
    row = conn.execute("SELECT input_hash, text FROM summaries WHERE kind = ? AND key = ?",
                       (kind, key)).fetchone()
    if row and row[0] == input_hash:
        return row[1]
    text, model = make()
    with conn:
        conn.execute(
            "INSERT INTO summaries (kind, key, input_hash, text, model, created_ts)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(kind, key) DO UPDATE SET input_hash = excluded.input_hash,"
            " text = excluded.text, model = excluded.model, created_ts = excluded.created_ts",
            (kind, key, input_hash, text, model, created_ts))
    return text
