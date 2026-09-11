"""Revisioned meeting storage — the whole meeting SQL write path lives here.

A meeting is not an activity event. It has its own tables, its own identity
(`occurrence_id`), and its own immutable revision history, so nothing here
touches `events`, `summaries`, or any query built on them.

Two layers:

- `meeting_artifacts` is the append-only record of every revision ever
  imported, keyed by `(occurrence_id, revision)` and content-addressed by
  `artifact_hash`. Nothing ever updates a row here.
- `meetings` is the current projection: one row per occurrence pointing at the
  artifact revision that is currently authoritative. Every normalized child
  table is keyed by `(occurrence_id, revision)`, so a superseded revision keeps
  all of its rows and stays queryable as provenance.

Importers hand this module validated payloads (`teammem.meeting_bundles`) plus
already-staged audio facts; they never write meeting SQL themselves. One import
is one `BEGIN IMMEDIATE` transaction: it either lands whole or leaves nothing
behind, not even the artifact row.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .meeting_bundles import canonical_artifact_hash


MEETING_AUDIO_RETENTION_DAYS = 14
# How long before that deadline an incomplete transcript is warned about.
# It lives here, beside the window it is measured against, so the setting,
# the retention job and the deadline cannot drift apart.
MEETING_AUDIO_WARN_DAYS = 3

MEETING_TABLES = (
    "meeting_artifacts",
    "meetings",
    "meeting_revisions",
    "meeting_participants",
    "meeting_attendance",
    "meeting_segments",
    "meeting_projects",
    "meeting_decisions",
    "meeting_action_items",
    "meeting_discussion_points",
    "meeting_evidence",
    "meeting_person_summaries",
    "meeting_provenance",
    "meeting_recording_gaps",
    "meeting_processing_warnings",
    "meeting_audio",
    "meeting_source_cleanup",
)

EVIDENCE_KINDS = ("project", "decision", "action_item", "discussion_point")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting_artifacts (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  artifact_hash TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  raw_json TEXT NOT NULL,
  PRIMARY KEY (occurrence_id, revision),
  UNIQUE (artifact_hash)
);

CREATE TABLE IF NOT EXISTS meetings (
  occurrence_id TEXT PRIMARY KEY,
  current_revision INTEGER NOT NULL,
  provider_meeting_id TEXT NOT NULL,
  title TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  organizer_feishu_id TEXT,            -- NULL only for instant meetings
  organizer_name TEXT,
  organizer_source TEXT,               -- 'reservation' | 'calendar_event' | NULL
  runtime_profile TEXT NOT NULL,
  transcript_complete INTEGER NOT NULL,
  FOREIGN KEY (occurrence_id, current_revision)
    REFERENCES meeting_artifacts(occurrence_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_meetings_started_at ON meetings(started_at);

-- Every revision-scoped scalar, including the ones `meetings` does not carry
-- (provider, summary, organizer_unresolved_reason). `meetings` is the current
-- projection; this table is the per-revision truth behind it.
CREATE TABLE IF NOT EXISTS meeting_revisions (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  provider TEXT NOT NULL,
  provider_meeting_id TEXT NOT NULL,
  title TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  organizer_feishu_id TEXT,
  organizer_name TEXT,
  organizer_source TEXT,
  organizer_unresolved_reason TEXT,
  transcript_complete INTEGER NOT NULL,
  summary TEXT NOT NULL,
  PRIMARY KEY (occurrence_id, revision),
  FOREIGN KEY (occurrence_id, revision)
    REFERENCES meeting_artifacts(occurrence_id, revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meeting_participants (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  person_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  PRIMARY KEY (occurrence_id, revision, person_id),
  FOREIGN KEY (occurrence_id, revision)
    REFERENCES meeting_artifacts(occurrence_id, revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meeting_attendance (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  person_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  joined_at TEXT NOT NULL,
  left_at TEXT,                        -- NULL: still present when the meeting ended
  PRIMARY KEY (occurrence_id, revision, person_id, position),
  FOREIGN KEY (occurrence_id, revision, person_id)
    REFERENCES meeting_participants(occurrence_id, revision, person_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meeting_segments (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  segment_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  text TEXT NOT NULL,
  speaker_kind TEXT NOT NULL,          -- 'person' | 'anonymous' | 'ambiguous'
  speaker_person_id TEXT,
  speaker_display_name TEXT,
  speaker_cluster_id TEXT,
  attribution_confidence REAL NOT NULL,
  stage TEXT NOT NULL,                 -- 'live_stream' | 'final_batch'
  model TEXT NOT NULL,
  PRIMARY KEY (occurrence_id, revision, segment_id),
  FOREIGN KEY (occurrence_id, revision)
    REFERENCES meeting_artifacts(occurrence_id, revision) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_meeting_segments_order
  ON meeting_segments(occurrence_id, revision, position);
CREATE INDEX IF NOT EXISTS idx_meeting_segments_speaker
  ON meeting_segments(speaker_person_id);

CREATE TABLE IF NOT EXISTS meeting_projects (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  position INTEGER NOT NULL,
  name TEXT NOT NULL,
  PRIMARY KEY (occurrence_id, revision, position),
  UNIQUE (occurrence_id, revision, name),
  FOREIGN KEY (occurrence_id, revision)
    REFERENCES meeting_artifacts(occurrence_id, revision) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_meeting_projects_name ON meeting_projects(name);

CREATE TABLE IF NOT EXISTS meeting_decisions (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  position INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (occurrence_id, revision, position),
  FOREIGN KEY (occurrence_id, revision)
    REFERENCES meeting_artifacts(occurrence_id, revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meeting_discussion_points (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  position INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (occurrence_id, revision, position),
  FOREIGN KEY (occurrence_id, revision)
    REFERENCES meeting_artifacts(occurrence_id, revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meeting_action_items (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  position INTEGER NOT NULL,
  text TEXT NOT NULL,
  owner_person_id TEXT,                -- NULL: the meeting never named an owner
  owner_display_name TEXT,
  owner_explicitly_stated INTEGER,
  due_date TEXT,                       -- YYYY-MM-DD, NULL when unstated
  due_date_explicitly_stated INTEGER,
  PRIMARY KEY (occurrence_id, revision, position),
  FOREIGN KEY (occurrence_id, revision)
    REFERENCES meeting_artifacts(occurrence_id, revision) ON DELETE CASCADE
);

-- Evidence for projects, decisions, action items and discussion points. The
-- foreign key is on the segment, because that is the integrity that matters:
-- evidence must point at a real segment of the same revision.
CREATE TABLE IF NOT EXISTS meeting_evidence (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  item_kind TEXT NOT NULL,             -- 'project'|'decision'|'action_item'|'discussion_point'
  item_position INTEGER NOT NULL,
  position INTEGER NOT NULL,
  segment_id TEXT NOT NULL,
  PRIMARY KEY (occurrence_id, revision, item_kind, item_position, segment_id),
  FOREIGN KEY (occurrence_id, revision, segment_id)
    REFERENCES meeting_segments(occurrence_id, revision, segment_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_meeting_evidence_segment
  ON meeting_evidence(occurrence_id, revision, segment_id);

CREATE TABLE IF NOT EXISTS meeting_person_summaries (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  person_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  display_name TEXT NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY (occurrence_id, revision, person_id),
  FOREIGN KEY (occurrence_id, revision, person_id)
    REFERENCES meeting_participants(occurrence_id, revision, person_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meeting_provenance (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  runtime_profile TEXT NOT NULL,       -- 'offline' | 'online', pinned for the session
  stt_provider TEXT NOT NULL,
  stt_model TEXT NOT NULL,
  text_provider TEXT NOT NULL,
  text_model TEXT NOT NULL,
  agent_version TEXT NOT NULL,
  processed_at TEXT NOT NULL,
  PRIMARY KEY (occurrence_id, revision),
  FOREIGN KEY (occurrence_id, revision)
    REFERENCES meeting_artifacts(occurrence_id, revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meeting_recording_gaps (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  position INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  PRIMARY KEY (occurrence_id, revision, position),
  FOREIGN KEY (occurrence_id, revision)
    REFERENCES meeting_artifacts(occurrence_id, revision) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS meeting_processing_warnings (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  position INTEGER NOT NULL,
  code TEXT NOT NULL,
  detail TEXT NOT NULL,
  PRIMARY KEY (occurrence_id, revision, position),
  FOREIGN KEY (occurrence_id, revision)
    REFERENCES meeting_artifacts(occurrence_id, revision) ON DELETE CASCADE
);

-- TeamMem never holds the recording key and never decrypts. It stores where the
-- encrypted file sits, what it hashes to, and when it must be destroyed.
-- `audio_expires_at` is derived here as ended_at + 14 days; the payload's own
-- claim is kept beside it as provenance only and is never authoritative.
CREATE TABLE IF NOT EXISTS meeting_audio (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  stored_path TEXT NOT NULL,
  filename TEXT NOT NULL,
  format TEXT NOT NULL,
  encryption TEXT NOT NULL,
  duration_seconds REAL NOT NULL,
  bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  audio_expires_at TEXT NOT NULL,
  declared_expires_at TEXT,
  deleted_at TEXT,
  PRIMARY KEY (occurrence_id, revision),
  FOREIGN KEY (occurrence_id, revision)
    REFERENCES meeting_artifacts(occurrence_id, revision) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_meeting_audio_expiry
  ON meeting_audio(audio_expires_at) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS meeting_source_cleanup (
  occurrence_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  source_root TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  identity TEXT NOT NULL,
  completed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (occurrence_id, revision, source_root, relative_path),
  FOREIGN KEY (occurrence_id, revision)
    REFERENCES meeting_audio(occurrence_id, revision) ON DELETE CASCADE
);
"""


class MeetingStoreError(RuntimeError):
    """A meeting write was refused. Nothing was changed."""


class MeetingRevisionConflict(MeetingStoreError):
    """This revision already exists with different content, or the hash is taken."""


class MeetingRevisionSuperseded(MeetingStoreError):
    """A newer revision of this occurrence is already current."""


@dataclass(frozen=True)
class StagedAudio:
    """Where the importer put the encrypted audio, and what it verified there.

    `sha256` and `bytes` describe the staged copy and must agree with the
    payload's own audio block; a disagreement means staging corrupted the file
    and the import is refused.
    """

    stored_path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class SourceCleanup:
    """Loader-verified physical location and root/directory/file inode identities.

    Digest, size, artifact hash and exact expiry remain on the referenced revision.
    This obligation is inserted before that revision's commit, never as recovery
    after a failed archive, which would lose ownership on a process crash.
    """

    source_root: str
    relative_path: str
    identity: str


@dataclass(frozen=True)
class RevisionImportResult:
    occurrence_id: str
    revision: int
    artifact_hash: str
    status: str                 # "imported" | "unchanged"
    current_revision: int
    audio_expires_at: str


# --- instants ---------------------------------------------------------------


def _parse_instant(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise MeetingStoreError(f"{field} must be a UTC instant string")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MeetingStoreError(f"{field} is not a UTC instant") from exc
    if moment.tzinfo is None:
        raise MeetingStoreError(f"{field} must carry a UTC offset")
    return moment.astimezone(timezone.utc)


def _format_instant(moment: datetime) -> str:
    """The one spelling stored in retention columns: UTC microseconds.

    Fixed width keeps indexed lexicographic comparisons chronological without
    discarding a fraction and making irreversible deletion happen early.
    """
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def audio_expires_at(ended_at: str) -> str:
    """Authoritative audio retention: `ended_at` + 14 days, exact to microseconds."""
    ended = _parse_instant(ended_at, "ended_at")
    return _format_instant(ended + timedelta(days=MEETING_AUDIO_RETENTION_DAYS))


# --- migration --------------------------------------------------------------


def migrate_meetings(conn: sqlite3.Connection) -> None:
    """Create meeting tables and canonicalize stored retention deadlines.

    This is the same mechanism `open_db` already uses for `events` and
    `summaries`: one `CREATE TABLE IF NOT EXISTS` script, no version registry.
    Legacy deadlines are recomputed from each revision's own `ended_at`, not
    from the current projection. Only `audio_expires_at` is corrected;
    `deleted_at` remains the audit evidence of when deletion actually occurred.
    """
    conn.executescript(_SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if "completed" not in {
            row[1] for row in conn.execute("PRAGMA table_info(meeting_source_cleanup)")
        }:
            conn.execute("ALTER TABLE meeting_source_cleanup ADD COLUMN completed INTEGER NOT NULL DEFAULT 0")
        rows = conn.execute(
            "SELECT a.occurrence_id, a.revision, a.audio_expires_at, r.ended_at "
            "FROM meeting_audio a JOIN meeting_revisions r "
            "ON r.occurrence_id = a.occurrence_id AND r.revision = a.revision"
        ).fetchall()
        corrections = [
            (expected, occurrence_id, revision)
            for occurrence_id, revision, stored, ended_at in rows
            if (expected := audio_expires_at(ended_at)) != stored
        ]
        conn.executemany(
            "UPDATE meeting_audio SET audio_expires_at = ? "
            "WHERE occurrence_id = ? AND revision = ?",
            corrections,
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def _foreign_keys_enforced(conn: sqlite3.Connection):
    """Enforce foreign keys for this block without changing the connection.

    `open_db` leaves `PRAGMA foreign_keys` off and flipping it globally would
    change how every existing write behaves, so meeting writes turn it on for
    their own scope and restore whatever the caller had. The pragma is a no-op
    inside a transaction, so this must wrap the `BEGIN`, not sit inside it.
    """
    previously_on = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if not previously_on:
        conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield
    finally:
        if not previously_on:
            conn.execute("PRAGMA foreign_keys = OFF")


# --- import -----------------------------------------------------------------


def import_meeting_revision(
    conn: sqlite3.Connection,
    *,
    payload: dict,
    occurrence_id: str,
    revision: int,
    artifact_hash: str,
    audio: StagedAudio,
    imported_at: str,
    before_commit: Callable[[], None],
    source_cleanup: SourceCleanup | None = None,
) -> RevisionImportResult:
    """Commit a revision/source obligation, then best-effort wake retention.

    Keep notification outside every rollback/discard boundary, including replay.
    A worker fault cannot turn a committed import into a rejected bundle.
    """
    result = _import_meeting_revision(
        conn, payload=payload, occurrence_id=occurrence_id, revision=revision,
        artifact_hash=artifact_hash, audio=audio, imported_at=imported_at,
        before_commit=before_commit, source_cleanup=source_cleanup)
    if result.status == "imported" or source_cleanup is not None:
        try:
            from .meeting_retention_worker import notify_committed_import
            notify_committed_import(conn)
        except Exception:
            try:
                logging.getLogger(__name__).warning(
                    "retention notification unavailable; committed import preserved; recovery required")
            except Exception:
                pass  # a broken diagnostic sink cannot reject committed audio
    return result


def _import_meeting_revision(
    conn: sqlite3.Connection,
    *,
    payload: dict,
    occurrence_id: str,
    revision: int,
    artifact_hash: str,
    audio: StagedAudio,
    imported_at: str,
    before_commit: Callable[[], None],
    source_cleanup: SourceCleanup | None = None,
) -> RevisionImportResult:
    """Store one validated meeting revision and make it current if it is newer.

    The caller has already validated `payload` against `teammem-meeting/v1` and
    staged the encrypted audio. Everything below happens in one
    `BEGIN IMMEDIATE` transaction with foreign keys enforced, so a failure
    anywhere leaves no artifact row and no child rows.

    `artifact_hash` is recomputed from `payload` before anything else, so this
    layer is genuinely content-addressed rather than trusting the caller's claim.
    A hash that does not match the payload is refused ahead of the replay check,
    so a doctored replay can never be waved through as unchanged.

    `before_commit` runs after every insert and immediately before the commit.
    It lets the importer revalidate filesystem state while this module remains
    the sole owner of the SQL transaction. Callers without adopted filesystem
    state must still make that decision explicit with a no-op callback.

    Replaying the same `(occurrence_id, revision, artifact_hash)` preserves every
    artifact row, but may atomically register another validated source copy.
    The same revision with a different hash raises `MeetingRevisionConflict`; a
    revision below the current one raises `MeetingRevisionSuperseded`.
    """
    recomputed = canonical_artifact_hash(payload)
    if recomputed != artifact_hash:
        raise MeetingStoreError(
            f"artifact hash for {occurrence_id} revision {revision} does not match the payload"
        )
    if audio.sha256 != payload["audio"]["sha256"] or audio.bytes != payload["audio"]["bytes"]:
        raise MeetingStoreError(
            f"staged audio for {occurrence_id} revision {revision} does not match the payload"
        )
    expires_at = audio_expires_at(payload["ended_at"])
    raw_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    with _foreign_keys_enforced(conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT artifact_hash FROM meeting_artifacts "
                "WHERE occurrence_id = ? AND revision = ?",
                (occurrence_id, revision),
            ).fetchone()
            if existing is not None:
                if existing[0] != artifact_hash:
                    raise MeetingRevisionConflict(
                        f"{occurrence_id} revision {revision} is already stored with a "
                        "different artifact hash; a revision is immutable once imported"
                    )
                current, stored_expiry = _current_state(conn, occurrence_id, revision)
                if source_cleanup is not None:
                    _insert_source_cleanup(conn, (occurrence_id, revision), source_cleanup)
                    before_commit()
                    conn.commit()
                else:
                    conn.rollback()
                return RevisionImportResult(
                    occurrence_id=occurrence_id,
                    revision=revision,
                    artifact_hash=artifact_hash,
                    status="unchanged",
                    current_revision=current,
                    audio_expires_at=stored_expiry,
                )

            owner = conn.execute(
                "SELECT occurrence_id, revision FROM meeting_artifacts WHERE artifact_hash = ?",
                (artifact_hash,),
            ).fetchone()
            if owner is not None:
                raise MeetingRevisionConflict(
                    f"artifact hash for {occurrence_id} revision {revision} already belongs "
                    f"to {owner[0]} revision {owner[1]}"
                )

            row = conn.execute(
                "SELECT current_revision FROM meetings WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            if row is not None and revision < row[0]:
                raise MeetingRevisionSuperseded(
                    f"{occurrence_id} is already at revision {row[0]}; "
                    f"revision {revision} is not accepted"
                )

            key = (occurrence_id, revision)
            conn.execute(
                "INSERT INTO meeting_artifacts "
                "(occurrence_id, revision, artifact_hash, imported_at, raw_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (occurrence_id, revision, artifact_hash, imported_at, raw_json),
            )
            _insert_revision(conn, key, payload)
            _insert_participants(conn, key, payload)
            _insert_segments(conn, key, payload)
            _insert_synthesis(conn, key, payload)
            _insert_person_summaries(conn, key, payload)
            _insert_provenance(conn, key, payload)
            _insert_gaps_and_warnings(conn, key, payload)
            _insert_audio(conn, key, payload, audio, expires_at)
            if source_cleanup is not None:
                _insert_source_cleanup(conn, key, source_cleanup)
            _activate_revision(conn, key, payload)
            before_commit()
        except Exception:
            conn.rollback()
            raise
        conn.commit()

    return RevisionImportResult(
        occurrence_id=occurrence_id,
        revision=revision,
        artifact_hash=artifact_hash,
        status="imported",
        current_revision=revision,
        audio_expires_at=expires_at,
    )


def _insert_source_cleanup(conn, key: tuple[str, int], source: SourceCleanup) -> None:
    location = (*key, source.source_root, source.relative_path)
    conn.execute(
        "INSERT OR IGNORE INTO meeting_source_cleanup "
        "(occurrence_id, revision, source_root, relative_path, identity) VALUES (?, ?, ?, ?, ?)",
        (*location, source.identity),
    )
    identity, completed = conn.execute(
        "SELECT identity, completed FROM meeting_source_cleanup WHERE occurrence_id = ? AND revision = ? "
        "AND source_root = ? AND relative_path = ?", location,
    ).fetchone()
    if identity != source.identity and not completed:
        raise MeetingStoreError("source cleanup ownership changed")
    if completed:
        # Completed ownership must never hide a fresh validated delivery, even
        # one which reused the same source path/inode after ciphertext removal.
        conn.execute(
            "UPDATE meeting_source_cleanup SET identity = ?, completed = 0 "
            "WHERE occurrence_id = ? AND revision = ? AND source_root = ? AND relative_path = ?",
            (source.identity, *location),
        )


def pending_source_cleanup(conn: sqlite3.Connection, *, include_completed: bool = False) -> list[dict]:
    cursor = conn.execute(
        "SELECT s.*, a.sha256, a.bytes, a.audio_expires_at, f.artifact_hash "
        "FROM meeting_source_cleanup s JOIN meeting_audio a USING (occurrence_id, revision) "
        "JOIN meeting_artifacts f USING (occurrence_id, revision) "
        "WHERE s.completed = 0 OR ? "
        "ORDER BY s.occurrence_id, s.revision, s.source_root, s.relative_path",
        (include_completed,),
    )
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def clear_source_cleanup(conn: sqlite3.Connection, row: dict) -> None:
    """Complete exact owned ciphertext debt, retaining a source identity tombstone."""
    with conn:
        conn.execute(
            "UPDATE meeting_source_cleanup SET completed = 1 WHERE occurrence_id = ? AND revision = ? "
            "AND source_root = ? AND relative_path = ? AND identity = ?",
            tuple(row[name] for name in (
                "occurrence_id", "revision", "source_root", "relative_path", "identity")),
        )


def _current_state(conn: sqlite3.Connection, occurrence_id: str, revision: int) -> tuple[int, str]:
    current = conn.execute(
        "SELECT current_revision FROM meetings WHERE occurrence_id = ?", (occurrence_id,)
    ).fetchone()
    expiry = conn.execute(
        "SELECT audio_expires_at FROM meeting_audio WHERE occurrence_id = ? AND revision = ?",
        (occurrence_id, revision),
    ).fetchone()
    return (current[0] if current else revision), (expiry[0] if expiry else "")


def _organizer_columns(payload: dict) -> tuple[str | None, str | None, str | None, str | None]:
    organizer = payload["organizer"]
    return (
        organizer["person_id"] if organizer else None,
        organizer["display_name"] if organizer else None,
        payload["organizer_source"],
        payload["organizer_unresolved_reason"],
    )


def _insert_revision(conn: sqlite3.Connection, key: tuple[str, int], payload: dict) -> None:
    person_id, name, source, reason = _organizer_columns(payload)
    conn.execute(
        "INSERT INTO meeting_revisions (occurrence_id, revision, provider, provider_meeting_id, "
        "title, started_at, ended_at, organizer_feishu_id, organizer_name, organizer_source, "
        "organizer_unresolved_reason, transcript_complete, summary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            *key,
            payload["provider"],
            payload["provider_meeting_id"],
            payload["title"],
            payload["started_at"],
            payload["ended_at"],
            person_id,
            name,
            source,
            reason,
            int(payload["transcript_complete"]),
            payload["summary"],
        ),
    )


def _insert_participants(conn: sqlite3.Connection, key: tuple[str, int], payload: dict) -> None:
    conn.executemany(
        "INSERT INTO meeting_participants "
        "(occurrence_id, revision, person_id, position, display_name) VALUES (?, ?, ?, ?, ?)",
        [
            (*key, person["person_id"], index, person["display_name"])
            for index, person in enumerate(payload["participants"])
        ],
    )
    conn.executemany(
        "INSERT INTO meeting_attendance "
        "(occurrence_id, revision, person_id, position, joined_at, left_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (*key, person["person_id"], position, interval["joined_at"], interval["left_at"])
            for person in payload["participants"]
            for position, interval in enumerate(person["attendance"])
        ],
    )


def _insert_segments(conn: sqlite3.Connection, key: tuple[str, int], payload: dict) -> None:
    rows = []
    for index, segment in enumerate(payload["transcript"]):
        speaker = segment["speaker"]
        rows.append((
            *key,
            segment["segment_id"],
            index,
            segment["started_at"],
            segment["ended_at"],
            segment["text"],
            speaker["kind"],
            speaker.get("person_id"),
            speaker.get("display_name"),
            speaker.get("cluster_id"),
            float(segment["attribution_confidence"]),
            segment["provenance"]["stage"],
            segment["provenance"]["model"],
        ))
    conn.executemany(
        "INSERT INTO meeting_segments (occurrence_id, revision, segment_id, position, started_at, "
        "ended_at, text, speaker_kind, speaker_person_id, speaker_display_name, "
        "speaker_cluster_id, attribution_confidence, stage, model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _insert_evidence(
    conn: sqlite3.Connection, key: tuple[str, int], item_kind: str, items: list
) -> None:
    conn.executemany(
        "INSERT INTO meeting_evidence "
        "(occurrence_id, revision, item_kind, item_position, position, segment_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (*key, item_kind, item_position, position, segment_id)
            for item_position, item in enumerate(items)
            for position, segment_id in enumerate(item["evidence_segment_ids"])
        ],
    )


def _insert_synthesis(conn: sqlite3.Connection, key: tuple[str, int], payload: dict) -> None:
    conn.executemany(
        "INSERT INTO meeting_projects (occurrence_id, revision, position, name) "
        "VALUES (?, ?, ?, ?)",
        [(*key, index, item["name"]) for index, item in enumerate(payload["projects"])],
    )
    _insert_evidence(conn, key, "project", payload["projects"])

    for table, source, kind in (
        ("meeting_decisions", "decisions", "decision"),
        ("meeting_discussion_points", "discussion_points", "discussion_point"),
    ):
        conn.executemany(
            f"INSERT INTO {table} (occurrence_id, revision, position, text) VALUES (?, ?, ?, ?)",
            [(*key, index, item["text"]) for index, item in enumerate(payload[source])],
        )
        _insert_evidence(conn, key, kind, payload[source])

    rows = []
    for index, item in enumerate(payload["action_items"]):
        owner = item["owner"]
        due = item["due_date"]
        rows.append((
            *key,
            index,
            item["text"],
            owner["person_id"] if owner else None,
            owner["display_name"] if owner else None,
            int(owner["explicitly_stated"]) if owner else None,
            due["date"] if due else None,
            int(due["explicitly_stated"]) if due else None,
        ))
    conn.executemany(
        "INSERT INTO meeting_action_items (occurrence_id, revision, position, text, "
        "owner_person_id, owner_display_name, owner_explicitly_stated, due_date, "
        "due_date_explicitly_stated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    _insert_evidence(conn, key, "action_item", payload["action_items"])


def _insert_person_summaries(conn: sqlite3.Connection, key: tuple[str, int], payload: dict) -> None:
    conn.executemany(
        "INSERT INTO meeting_person_summaries "
        "(occurrence_id, revision, person_id, position, display_name, text) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (*key, item["person_id"], index, item["display_name"], item["text"])
            for index, item in enumerate(payload["person_summaries"])
        ],
    )


def _insert_provenance(conn: sqlite3.Connection, key: tuple[str, int], payload: dict) -> None:
    provenance = payload["provenance"]
    conn.execute(
        "INSERT INTO meeting_provenance (occurrence_id, revision, runtime_profile, stt_provider, "
        "stt_model, text_provider, text_model, agent_version, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            *key,
            provenance["runtime_profile"],
            provenance["stt_provider"],
            provenance["stt_model"],
            provenance["text_provider"],
            provenance["text_model"],
            provenance["agent_version"],
            provenance["processed_at"],
        ),
    )


def _insert_gaps_and_warnings(conn: sqlite3.Connection, key: tuple[str, int], payload: dict) -> None:
    conn.executemany(
        "INSERT INTO meeting_recording_gaps "
        "(occurrence_id, revision, position, started_at, ended_at, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (*key, index, gap["started_at"], gap["ended_at"], gap["reason"])
            for index, gap in enumerate(payload["recording_gaps"])
        ],
    )
    conn.executemany(
        "INSERT INTO meeting_processing_warnings "
        "(occurrence_id, revision, position, code, detail) VALUES (?, ?, ?, ?, ?)",
        [
            (*key, index, warning["code"], warning["detail"])
            for index, warning in enumerate(payload["processing_warnings"])
        ],
    )


def _insert_audio(
    conn: sqlite3.Connection,
    key: tuple[str, int],
    payload: dict,
    audio: StagedAudio,
    expires_at: str,
) -> None:
    declared = payload["audio"]
    conn.execute(
        "INSERT INTO meeting_audio (occurrence_id, revision, stored_path, filename, format, "
        "encryption, duration_seconds, bytes, sha256, audio_expires_at, declared_expires_at, "
        "deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            *key,
            audio.stored_path,
            declared["filename"],
            declared["format"],
            declared["encryption"],
            float(declared["duration_seconds"]),
            audio.bytes,
            audio.sha256,
            expires_at,
            declared["expires_at"],
        ),
    )


def _activate_revision(conn: sqlite3.Connection, key: tuple[str, int], payload: dict) -> None:
    person_id, name, source, _ = _organizer_columns(payload)
    conn.execute(
        "INSERT INTO meetings (occurrence_id, current_revision, provider_meeting_id, title, "
        "started_at, ended_at, organizer_feishu_id, organizer_name, organizer_source, "
        "runtime_profile, transcript_complete) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(occurrence_id) DO UPDATE SET "
        "current_revision = excluded.current_revision, "
        "provider_meeting_id = excluded.provider_meeting_id, title = excluded.title, "
        "started_at = excluded.started_at, ended_at = excluded.ended_at, "
        "organizer_feishu_id = excluded.organizer_feishu_id, "
        "organizer_name = excluded.organizer_name, "
        "organizer_source = excluded.organizer_source, "
        "runtime_profile = excluded.runtime_profile, "
        "transcript_complete = excluded.transcript_complete",
        (
            key[0],
            key[1],
            payload["provider_meeting_id"],
            payload["title"],
            payload["started_at"],
            payload["ended_at"],
            person_id,
            name,
            source,
            payload["provenance"]["runtime_profile"],
            int(payload["transcript_complete"]),
        ),
    )


# --- read API ---------------------------------------------------------------

_SUMMARY_COLUMNS = (
    "m.occurrence_id, m.current_revision, r.provider, m.provider_meeting_id, m.title, "
    "m.started_at, m.ended_at, r.organizer_feishu_id, r.organizer_name, r.organizer_source, "
    "r.organizer_unresolved_reason, r.transcript_complete, r.summary, a.artifact_hash, "
    "a.imported_at"
)


def _summary_row(row: tuple) -> dict:
    return {
        "occurrence_id": row[0],
        "current_revision": row[1],
        "provider": row[2],
        "provider_meeting_id": row[3],
        "title": row[4],
        "started_at": row[5],
        "ended_at": row[6],
        "organizer": (
            {"person_id": row[7], "display_name": row[8]} if row[7] is not None else None
        ),
        "organizer_source": row[9],
        "organizer_unresolved_reason": row[10],
        "transcript_complete": bool(row[11]),
        "summary": row[12],
        "artifact_hash": row[13],
        "imported_at": row[14],
    }


def list_meetings(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[dict]:
    """Current revision of every occurrence, oldest first, without children.

    `start`/`end` are half-open ISO bounds on `started_at`, compared as strings
    the way `queries.week_range` compares `events.ts`. Callers that need the
    transcript and synthesis of a row call `get_meeting` for it.
    """
    clauses = []
    params: list[str] = []
    if start is not None:
        clauses.append("m.started_at >= ?")
        params.append(start)
    if end is not None:
        clauses.append("m.started_at < ?")
        params.append(end)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT {_SUMMARY_COLUMNS} FROM meetings m "
        "JOIN meeting_revisions r ON r.occurrence_id = m.occurrence_id "
        "AND r.revision = m.current_revision "
        "JOIN meeting_artifacts a ON a.occurrence_id = m.occurrence_id "
        "AND a.revision = m.current_revision"
        f"{where} ORDER BY m.started_at, m.occurrence_id",
        params,
    ).fetchall()
    return [_summary_row(row) for row in rows]


def list_meeting_revisions(conn: sqlite3.Connection, occurrence_id: str) -> list[dict]:
    """Every stored revision of one occurrence, newest first. Provenance index."""
    current = conn.execute(
        "SELECT current_revision FROM meetings WHERE occurrence_id = ?", (occurrence_id,)
    ).fetchone()
    rows = conn.execute(
        "SELECT revision, artifact_hash, imported_at FROM meeting_artifacts "
        "WHERE occurrence_id = ? ORDER BY revision DESC",
        (occurrence_id,),
    ).fetchall()
    return [
        {
            "occurrence_id": occurrence_id,
            "revision": row[0],
            "artifact_hash": row[1],
            "imported_at": row[2],
            "is_current": current is not None and row[0] == current[0],
        }
        for row in rows
    ]


def get_meeting(
    conn: sqlite3.Connection,
    occurrence_id: str,
    *,
    revision: int | None = None,
) -> dict | None:
    """One meeting with every child row, shaped like the bundle payload.

    Without `revision` this returns the current revision. With one it returns
    that exact revision, current or superseded, which is how prior revisions
    stay provable. Returns `None` when the occurrence or revision is unknown.
    """
    current_row = conn.execute(
        "SELECT current_revision FROM meetings WHERE occurrence_id = ?", (occurrence_id,)
    ).fetchone()
    current = current_row[0] if current_row else None
    wanted = current if revision is None else revision
    if wanted is None:
        return None

    key = (occurrence_id, wanted)
    scalars = conn.execute(
        "SELECT r.provider, r.provider_meeting_id, r.title, r.started_at, r.ended_at, "
        "r.organizer_feishu_id, r.organizer_name, r.organizer_source, "
        "r.organizer_unresolved_reason, r.transcript_complete, r.summary, a.artifact_hash, "
        "a.imported_at FROM meeting_revisions r "
        "JOIN meeting_artifacts a ON a.occurrence_id = r.occurrence_id AND a.revision = r.revision "
        "WHERE r.occurrence_id = ? AND r.revision = ?",
        key,
    ).fetchone()
    if scalars is None:
        return None

    return {
        "occurrence_id": occurrence_id,
        "revision": wanted,
        "current_revision": current,
        "is_current": current == wanted,
        "artifact_hash": scalars[11],
        "imported_at": scalars[12],
        "provider": scalars[0],
        "provider_meeting_id": scalars[1],
        "title": scalars[2],
        "started_at": scalars[3],
        "ended_at": scalars[4],
        "organizer": (
            {"person_id": scalars[5], "display_name": scalars[6]}
            if scalars[5] is not None
            else None
        ),
        "organizer_source": scalars[7],
        "organizer_unresolved_reason": scalars[8],
        "transcript_complete": bool(scalars[9]),
        "summary": scalars[10],
        "participants": _read_participants(conn, key),
        "transcript": _read_transcript(conn, key),
        "projects": _read_projects(conn, key),
        "decisions": _read_statements(conn, key, "meeting_decisions", "decision"),
        "action_items": _read_action_items(conn, key),
        "discussion_points": _read_statements(
            conn, key, "meeting_discussion_points", "discussion_point"
        ),
        "person_summaries": _read_person_summaries(conn, key),
        "recording_gaps": _read_recording_gaps(conn, key),
        "processing_warnings": _read_processing_warnings(conn, key),
        "provenance": _read_provenance(conn, key),
        "audio": _read_audio(conn, key),
    }


def _read_participants(conn: sqlite3.Connection, key: tuple[str, int]) -> list[dict]:
    intervals: dict[str, list[dict]] = {}
    for person_id, joined_at, left_at in conn.execute(
        "SELECT person_id, joined_at, left_at FROM meeting_attendance "
        "WHERE occurrence_id = ? AND revision = ? ORDER BY person_id, position",
        key,
    ):
        intervals.setdefault(person_id, []).append({"joined_at": joined_at, "left_at": left_at})
    return [
        {
            "person_id": person_id,
            "display_name": display_name,
            "attendance": intervals.get(person_id, []),
        }
        for person_id, display_name in conn.execute(
            "SELECT person_id, display_name FROM meeting_participants "
            "WHERE occurrence_id = ? AND revision = ? ORDER BY position",
            key,
        )
    ]


def _read_transcript(conn: sqlite3.Connection, key: tuple[str, int]) -> list[dict]:
    segments = []
    for row in conn.execute(
        "SELECT segment_id, started_at, ended_at, text, speaker_kind, speaker_person_id, "
        "speaker_display_name, speaker_cluster_id, attribution_confidence, stage, model "
        "FROM meeting_segments WHERE occurrence_id = ? AND revision = ? ORDER BY position",
        key,
    ):
        if row[4] == "person":
            speaker = {
                "kind": "person",
                "person_id": row[5],
                "display_name": row[6],
                "reconciled": True,   # validation rejects an unreconciled person speaker
            }
        elif row[4] == "anonymous":
            speaker = {"kind": "anonymous", "cluster_id": row[7]}
        else:
            speaker = {"kind": "ambiguous"}
        segments.append({
            "segment_id": row[0],
            "started_at": row[1],
            "ended_at": row[2],
            "text": row[3],
            "speaker": speaker,
            "attribution_confidence": row[8],
            "provenance": {"stage": row[9], "model": row[10]},
        })
    return segments


def _read_evidence(conn: sqlite3.Connection, key: tuple[str, int], item_kind: str) -> dict[int, list[str]]:
    evidence: dict[int, list[str]] = {}
    for item_position, segment_id in conn.execute(
        "SELECT item_position, segment_id FROM meeting_evidence "
        "WHERE occurrence_id = ? AND revision = ? AND item_kind = ? "
        "ORDER BY item_position, position",
        (*key, item_kind),
    ):
        evidence.setdefault(item_position, []).append(segment_id)
    return evidence


def _read_projects(conn: sqlite3.Connection, key: tuple[str, int]) -> list[dict]:
    evidence = _read_evidence(conn, key, "project")
    return [
        {"name": name, "evidence_segment_ids": evidence.get(position, [])}
        for position, name in conn.execute(
            "SELECT position, name FROM meeting_projects "
            "WHERE occurrence_id = ? AND revision = ? ORDER BY position",
            key,
        )
    ]


def _read_statements(
    conn: sqlite3.Connection, key: tuple[str, int], table: str, item_kind: str
) -> list[dict]:
    evidence = _read_evidence(conn, key, item_kind)
    return [
        {"text": text, "evidence_segment_ids": evidence.get(position, [])}
        for position, text in conn.execute(
            f"SELECT position, text FROM {table} "
            "WHERE occurrence_id = ? AND revision = ? ORDER BY position",
            key,
        )
    ]


def _read_action_items(conn: sqlite3.Connection, key: tuple[str, int]) -> list[dict]:
    evidence = _read_evidence(conn, key, "action_item")
    items = []
    for row in conn.execute(
        "SELECT position, text, owner_person_id, owner_display_name, owner_explicitly_stated, "
        "due_date, due_date_explicitly_stated FROM meeting_action_items "
        "WHERE occurrence_id = ? AND revision = ? ORDER BY position",
        key,
    ):
        items.append({
            "text": row[1],
            "owner": (
                {
                    "person_id": row[2],
                    "display_name": row[3],
                    "explicitly_stated": bool(row[4]),
                }
                if row[2] is not None
                else None
            ),
            "due_date": (
                {"date": row[5], "explicitly_stated": bool(row[6])}
                if row[5] is not None
                else None
            ),
            "evidence_segment_ids": evidence.get(row[0], []),
        })
    return items


def _read_person_summaries(conn: sqlite3.Connection, key: tuple[str, int]) -> list[dict]:
    return [
        {"person_id": person_id, "display_name": display_name, "text": text}
        for person_id, display_name, text in conn.execute(
            "SELECT person_id, display_name, text FROM meeting_person_summaries "
            "WHERE occurrence_id = ? AND revision = ? ORDER BY position",
            key,
        )
    ]


def _read_recording_gaps(conn: sqlite3.Connection, key: tuple[str, int]) -> list[dict]:
    return [
        {"started_at": started_at, "ended_at": ended_at, "reason": reason}
        for started_at, ended_at, reason in conn.execute(
            "SELECT started_at, ended_at, reason FROM meeting_recording_gaps "
            "WHERE occurrence_id = ? AND revision = ? ORDER BY position",
            key,
        )
    ]


def _read_processing_warnings(conn: sqlite3.Connection, key: tuple[str, int]) -> list[dict]:
    return [
        {"code": code, "detail": detail}
        for code, detail in conn.execute(
            "SELECT code, detail FROM meeting_processing_warnings "
            "WHERE occurrence_id = ? AND revision = ? ORDER BY position",
            key,
        )
    ]


def _read_provenance(conn: sqlite3.Connection, key: tuple[str, int]) -> dict | None:
    row = conn.execute(
        "SELECT runtime_profile, stt_provider, stt_model, text_provider, text_model, "
        "agent_version, processed_at FROM meeting_provenance "
        "WHERE occurrence_id = ? AND revision = ?",
        key,
    ).fetchone()
    if row is None:
        return None
    return {
        "runtime_profile": row[0],
        "stt_provider": row[1],
        "stt_model": row[2],
        "text_provider": row[3],
        "text_model": row[4],
        "agent_version": row[5],
        "processed_at": row[6],
    }


def _read_audio(conn: sqlite3.Connection, key: tuple[str, int]) -> dict | None:
    row = conn.execute(
        "SELECT stored_path, filename, format, encryption, duration_seconds, bytes, sha256, "
        "audio_expires_at, declared_expires_at, deleted_at FROM meeting_audio "
        "WHERE occurrence_id = ? AND revision = ?",
        key,
    ).fetchone()
    if row is None:
        return None
    return {
        "stored_path": row[0],
        "filename": row[1],
        "format": row[2],
        "encryption": row[3],
        "duration_seconds": row[4],
        "bytes": row[5],
        "sha256": row[6],
        "expires_at": row[7],            # authoritative: ended_at + 14 days
        "declared_expires_at": row[8],   # what the producer asked for, provenance only
        "deleted_at": row[9],
    }


# --- retention --------------------------------------------------------------


def retention_deadlines(
    conn: sqlite3.Connection, *, after: datetime, warn_days: int,
) -> tuple[datetime | None, datetime | None]:
    """Next future boundary and oldest overdue obligation, including history.

    Past warning boundaries never force a zero wait. Pending source copies keep
    their own original expiry even when the primary row was previously marked.
    """
    rows = conn.execute(
        "SELECT a.audio_expires_at, a.deleted_at, "
        "m.current_revision = a.revision AND m.transcript_complete = 0 "
        "FROM meeting_audio a LEFT JOIN meetings m USING (occurrence_id) "
        "WHERE a.deleted_at IS NULL OR EXISTS (SELECT 1 FROM meeting_source_cleanup s "
        "WHERE s.occurrence_id = a.occurrence_id AND s.revision = a.revision AND s.completed = 0)"
    ).fetchall()
    future, overdue = [], []
    for value, deleted, incomplete in rows:
        expiry = _parse_instant(value, "audio_expires_at")
        if expiry > after:
            future.append(expiry)
        else:
            overdue.append(expiry)
        if not deleted and incomplete and warn_days > 0:
            warning = expiry - timedelta(days=warn_days)
            if warning > after:
                future.append(warning)
    return min(future, default=None), min(overdue, default=None)


def audio_due_for_expiry(conn: sqlite3.Connection, now: str) -> list[dict]:
    """Every still-present encrypted audio row whose expiry has arrived.

    `now` is a UTC instant; it is normalized to the same fixed-width spelling
    the expiry column uses, so the comparison is a sound string comparison.
    Superseded revisions are included — each revision's audio expires on its own
    schedule — and `is_current` says which projection a row belongs to.
    """
    cutoff = _format_instant(_parse_instant(now, "now"))
    rows = conn.execute(
        "SELECT a.occurrence_id, a.revision, a.stored_path, a.sha256, a.bytes, "
        "a.audio_expires_at, m.current_revision FROM meeting_audio a "
        "LEFT JOIN meetings m ON m.occurrence_id = a.occurrence_id "
        "WHERE a.deleted_at IS NULL AND a.audio_expires_at <= ? "
        "ORDER BY a.audio_expires_at, a.occurrence_id, a.revision",
        (cutoff,),
    ).fetchall()
    return [
        {
            "occurrence_id": row[0],
            "revision": row[1],
            "stored_path": row[2],
            "sha256": row[3],
            "bytes": row[4],
            "audio_expires_at": row[5],
            "is_current": row[6] == row[1],
        }
        for row in rows
    ]


def mark_audio_deleted(
    conn: sqlite3.Connection, occurrence_id: str, revision: int, deleted_at: str
) -> bool:
    """Record that one revision's encrypted audio is gone from disk.

    Returns True when this call was the one that marked it, False when the row
    is unknown or was already marked, so a retention job is replay-safe. The
    transcript and every other child row are untouched.
    """
    with conn:
        cursor = conn.execute(
            "UPDATE meeting_audio SET deleted_at = ? "
            "WHERE occurrence_id = ? AND revision = ? AND deleted_at IS NULL",
            (deleted_at, occurrence_id, revision),
        )
    return cursor.rowcount == 1
