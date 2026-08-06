"""The ledger — Layer 1, the single source of truth. INSERT OR IGNORE on the
UNIQUE(person, source, hash) key makes every ingest path idempotent."""

import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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
  evidence_cutoff      TEXT,
  cutoff_precision     TEXT,
  coverage_state       TEXT,
  source_input_hash    TEXT,
  effective_flags_json TEXT,
  UNIQUE(kind, key)
);
"""


_SUMMARY_PROVENANCE_COLUMNS = {
    "evidence_cutoff": "TEXT",
    "cutoff_precision": "TEXT",
    "coverage_state": "TEXT",
    "source_input_hash": "TEXT",
    "effective_flags_json": "TEXT",
}


@dataclass(frozen=True)
class SummaryRecord:
    kind: str
    key: str
    input_hash: str
    text: str
    model: str
    created_ts: str
    evidence_cutoff: str | None = None
    cutoff_precision: str | None = None
    coverage_state: str | None = None
    source_input_hash: str | None = None
    effective_flags_json: str | None = None


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(summaries)")}
        for name, type_ in _SUMMARY_PROVENANCE_COLUMNS.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE summaries ADD COLUMN {name} {type_}")
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
    return conn


def get_summary(conn: sqlite3.Connection, kind: str, key: str) -> SummaryRecord | None:
    row = conn.execute(
        "SELECT kind, key, input_hash, text, model, created_ts, evidence_cutoff, "
        "cutoff_precision, coverage_state, source_input_hash, effective_flags_json "
        "FROM summaries WHERE kind = ? AND key = ?",
        (kind, key),
    ).fetchone()
    return SummaryRecord(*row) if row else None


def put_summary(conn: sqlite3.Connection, record: SummaryRecord) -> None:
    with conn:
        conn.execute(
            "INSERT INTO summaries (kind, key, input_hash, text, model, created_ts, "
            "evidence_cutoff, cutoff_precision, coverage_state, source_input_hash, "
            "effective_flags_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, key) DO UPDATE SET "
            "input_hash = excluded.input_hash, text = excluded.text, model = excluded.model, "
            "created_ts = excluded.created_ts, evidence_cutoff = excluded.evidence_cutoff, "
            "cutoff_precision = excluded.cutoff_precision, coverage_state = excluded.coverage_state, "
            "source_input_hash = excluded.source_input_hash, "
            "effective_flags_json = excluded.effective_flags_json",
            (
                record.kind,
                record.key,
                record.input_hash,
                record.text,
                record.model,
                record.created_ts,
                record.evidence_cutoff,
                record.cutoff_precision,
                record.coverage_state,
                record.source_input_hash,
                record.effective_flags_json,
            ),
        )


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
    """Atomically reconcile authoritative and legacy GitLab event identities."""
    with conn:
        for repair in _legacy_opened_issue_repairs(conn, ids):
            _replace_gitlab_event(conn, repair, existing_only=True)

        inserted = 0
        for event in events:
            if event.source == "gitlab" and event.kind in {"issue", "repo"}:
                inserted += _replace_gitlab_event(conn, event)
            elif event.source == "gitlab" and event.kind == "commit":
                inserted += _reconcile_gitlab_commit(conn, event)
            else:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO events "
                    "(person, project, ts, source, kind, summary, refs, raw, hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _event_values(event),
                )
                inserted += cursor.rowcount
    return inserted


def _reconcile_gitlab_commit(conn: sqlite3.Connection, event: Event) -> int:
    legacy_sha = _commit_sha(event.refs)
    legacy_id = None
    if legacy_sha and legacy_sha != event.hash:
        candidates = conn.execute(
            "SELECT id, refs FROM events "
            "WHERE source = 'gitlab' AND kind = 'commit' AND hash = ? "
            "AND (project IS ? OR project IS NULL)",
            (legacy_sha, event.project),
        ).fetchall()
        candidates = [row for row in candidates if row[1] == event.refs]
        if len(candidates) == 1:
            legacy_id = candidates[0][0]

    if legacy_id is not None:
        conn.execute("DELETE FROM events WHERE id = ?", (legacy_id,))
        conn.execute(
            "INSERT OR IGNORE INTO events "
            "(person, project, ts, source, kind, summary, refs, raw, hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _event_values(event),
        )
        return 0

    cursor = conn.execute(
        "INSERT OR IGNORE INTO events "
        "(person, project, ts, source, kind, summary, refs, raw, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        _event_values(event),
    )
    return cursor.rowcount


def _commit_sha(refs: str | None) -> str | None:
    try:
        parsed = json.loads(refs or "")
    except (TypeError, json.JSONDecodeError):
        return None
    sha = parsed.get("sha") if isinstance(parsed, dict) else None
    return sha if isinstance(sha, str) and sha else None


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
    existing = get_summary(conn, kind, key)
    if existing and existing.input_hash == input_hash:
        return existing.text
    text, model = make()
    put_summary(conn, SummaryRecord(kind, key, input_hash, text, model, created_ts))
    return text
