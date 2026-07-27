"""The ledger — Layer 1, the single source of truth. INSERT OR IGNORE on the
UNIQUE(person, source, hash) key makes every ingest path idempotent."""

import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

from .events import Event

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
