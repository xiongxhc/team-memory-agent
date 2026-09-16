"""Durable, audience-scoped state for the TeamMem chat service."""

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SessionKey:
    tenant: str
    app: str
    kind: str
    owner: str
    root: str = ""


@dataclass(frozen=True)
class Turn:
    role: str
    sender: str
    text: str
    projects: frozenset[str]


@dataclass(frozen=True)
class Evidence:
    id: str
    project: str
    timestamp: str
    text: str
    url: str | None
    person: str | None = None


@dataclass(frozen=True)
class QueuedReply:
    reply_id: str
    session: SessionKey
    message_id: str
    text: str
    projects: frozenset[str]
    admitted_projects: frozenset[str]
    sender: str
    chat_id: str
    generation: int
    created_at_ms: int
    status: str


@dataclass(frozen=True)
class PendingReaction:
    tenant: str
    app: str
    message_id: str
    reaction_id: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    tenant TEXT NOT NULL, app TEXT NOT NULL, kind TEXT NOT NULL,
    owner TEXT NOT NULL, root TEXT NOT NULL, generation INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL, revoked_at_ms INTEGER,
    PRIMARY KEY (tenant, app, kind, owner, root)
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, app TEXT NOT NULL,
    kind TEXT NOT NULL, owner TEXT NOT NULL, root TEXT NOT NULL,
    generation INTEGER NOT NULL, role TEXT NOT NULL, sender TEXT NOT NULL,
    text TEXT NOT NULL, projects_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(tenant, app, kind, owner, root, generation, id);
CREATE TABLE IF NOT EXISTS incoming (
    tenant TEXT NOT NULL, app TEXT NOT NULL, message_id TEXT NOT NULL,
    kind TEXT NOT NULL, owner TEXT NOT NULL, root TEXT NOT NULL,
    generation INTEGER NOT NULL, sender TEXT, chat_id TEXT NOT NULL DEFAULT '', text TEXT,
    admitted_projects_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (tenant, app, message_id)
);
CREATE INDEX IF NOT EXISTS idx_incoming_processing ON incoming(status, updated_at_ms);
CREATE TABLE IF NOT EXISTS outbox (
    reply_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, app TEXT NOT NULL,
    kind TEXT NOT NULL, owner TEXT NOT NULL, root TEXT NOT NULL,
    message_id TEXT NOT NULL, generation INTEGER NOT NULL, text TEXT NOT NULL,
    projects_json TEXT NOT NULL, admitted_projects_json TEXT NOT NULL DEFAULT '[]',
    sender TEXT NOT NULL DEFAULT '', chat_id TEXT NOT NULL DEFAULT '', created_at_ms INTEGER NOT NULL,
    status TEXT NOT NULL, platform_message_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status, tenant, app, kind, owner, root, generation, created_at_ms);
CREATE TABLE IF NOT EXISTS interactions (
    tenant TEXT NOT NULL, app TEXT NOT NULL, original_message_id TEXT NOT NULL,
    kind TEXT NOT NULL, owner TEXT NOT NULL, root TEXT NOT NULL,
    generation INTEGER NOT NULL, sender TEXT NOT NULL, chat_id TEXT NOT NULL,
    question TEXT NOT NULL, answer TEXT NOT NULL, answer_projects_json TEXT NOT NULL,
    platform_reply_message_id TEXT,
    PRIMARY KEY (tenant, app, original_message_id),
    UNIQUE (tenant, app, platform_reply_message_id)
);
CREATE INDEX IF NOT EXISTS idx_interactions_chat ON interactions(tenant, app, chat_id);
CREATE TABLE IF NOT EXISTS reactions (
    tenant TEXT NOT NULL, app TEXT NOT NULL, message_id TEXT NOT NULL,
    kind TEXT NOT NULL, owner TEXT NOT NULL, root TEXT NOT NULL,
    generation INTEGER NOT NULL, emoji_type TEXT NOT NULL,
    reaction_id TEXT, status TEXT NOT NULL, updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (tenant, app, message_id)
);
CREATE INDEX IF NOT EXISTS idx_reactions_cleanup ON reactions(status, updated_at_ms);
"""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class ChatState:
    """Owns schema migration and transactions for the chat-only SQLite database."""

    def __init__(self, path: Path, *, idle_retention_days: int = 30):
        if idle_retention_days < 1:
            raise ValueError("idle_retention_days must be positive")
        self.path = Path(path)
        self.idle_retention_ms = idle_retention_days * 86_400_000
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._migrate_routing_columns()

    def _migrate_routing_columns(self) -> None:
        for table, columns in {
            "sessions": {
                "revoked_at_ms": "INTEGER",
            },
            "incoming": {
                "chat_id": "TEXT NOT NULL DEFAULT ''",
                "admitted_projects_json": "TEXT NOT NULL DEFAULT '[]'",
            },
            "outbox": {
                "admitted_projects_json": "TEXT NOT NULL DEFAULT '[]'",
                "sender": "TEXT NOT NULL DEFAULT ''",
                "chat_id": "TEXT NOT NULL DEFAULT ''",
            },
        }.items():
            existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _validate_key(key: SessionKey) -> None:
        if key.kind not in {"dm", "group", "thread"}:
            raise ValueError("session kind must be dm, group, or thread")
        if any(not isinstance(value, str) or not value for value in (key.tenant, key.app, key.owner)):
            raise ValueError("session tenant, app, and owner must be non-empty")
        if key.kind == "thread" and not key.root:
            raise ValueError("thread sessions require a root message ID")
        if key.kind != "thread" and key.root:
            raise ValueError("only thread sessions may have a root message ID")

    @staticmethod
    def _key_values(key: SessionKey) -> tuple[str, str, str, str, str]:
        return key.tenant, key.app, key.kind, key.owner, key.root

    def _ensure_session(self, key: SessionKey, now_ms: int) -> int:
        self._validate_key(key)
        self._conn.execute(
            "INSERT OR IGNORE INTO sessions (tenant, app, kind, owner, root, generation, updated_at_ms) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (*self._key_values(key), now_ms),
        )
        row = self._conn.execute(
            "SELECT generation FROM sessions WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ?",
            self._key_values(key),
        ).fetchone()
        return int(row["generation"])

    def _current_generation(self, key: SessionKey) -> int | None:
        row = self._conn.execute(
            "SELECT generation FROM sessions WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ?",
            self._key_values(key),
        ).fetchone()
        return None if row is None else int(row["generation"])

    def _expire(self, key: SessionKey, now_ms: int) -> None:
        row = self._conn.execute(
            "SELECT generation, updated_at_ms FROM sessions WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ?",
            self._key_values(key),
        ).fetchone()
        if row is None or now_ms - int(row["updated_at_ms"]) < self.idle_retention_ms:
            return
        generation = int(row["generation"])
        self._invalidate_generation(key, generation, now_ms, forget=True)

    def _invalidate_generation(self, key: SessionKey, generation: int, now_ms: int, *, forget: bool) -> int:
        new_generation = generation + 1
        values = self._key_values(key)
        self._conn.execute(
            "UPDATE sessions SET generation = ?, updated_at_ms = ? WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ?",
            (new_generation, now_ms, *values),
        )
        self._conn.execute(
            "DELETE FROM turns WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ?",
            values,
        )
        self._conn.execute(
            "DELETE FROM interactions WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? AND generation = ?",
            (*values, generation),
        )
        self._conn.execute(
            "UPDATE incoming SET status = 'invalidated', updated_at_ms = ? WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? AND generation = ? AND status IN ('queued', 'processing')",
            (now_ms, *values, generation),
        )
        self._conn.execute(
            "UPDATE reactions SET status = 'cleanup', updated_at_ms = ? WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? AND generation = ? AND status IN ('creating', 'active')",
            (now_ms, *values, generation),
        )
        if forget:
            self._conn.execute(
                "UPDATE incoming SET sender = NULL, text = NULL WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ?",
                values,
            )
            self._conn.execute(
                "DELETE FROM outbox WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ?",
                values,
            )
            self._conn.execute(
                "DELETE FROM interactions WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ?",
                values,
            )
        else:
            self._conn.execute(
                "DELETE FROM outbox WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? AND generation = ? AND status = 'queued'",
                (*values, generation),
            )
        return new_generation

    def generation(self, key: SessionKey) -> int:
        now_ms = _now_ms()
        with self._conn:
            self._expire(key, now_ms)
            return self._ensure_session(key, now_ms)

    def expire_idle(self, *, now_ms: int | None = None) -> int:
        """Purge content from every idle session while retaining message-ID tombstones."""
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            rows = self._conn.execute(
                "SELECT tenant, app, kind, owner, root, generation FROM sessions WHERE updated_at_ms <= ?",
                (current_time - self.idle_retention_ms,),
            ).fetchall()
            for row in rows:
                key = SessionKey(row["tenant"], row["app"], row["kind"], row["owner"], row["root"])
                self._invalidate_generation(key, int(row["generation"]), current_time, forget=True)
            return len(rows)

    def session_keys(self) -> list[SessionKey]:
        """Return persisted session identities for owner-thread reconciliation only."""
        rows = self._conn.execute(
            "SELECT tenant, app, kind, owner, root FROM sessions ORDER BY tenant, app, kind, owner, root"
        ).fetchall()
        return [SessionKey(row["tenant"], row["app"], row["kind"], row["owner"], row["root"]) for row in rows]

    def append(self, key: SessionKey, turn: Turn, *, generation: int | None = None, now_ms: int | None = None) -> bool:
        if turn.role not in {"user", "assistant", "system"} or not turn.sender or not isinstance(turn.text, str):
            raise ValueError("invalid turn")
        if not isinstance(turn.projects, frozenset) or any(not isinstance(project, str) or not project for project in turn.projects):
            raise ValueError("turn projects must be a frozen set of project slugs")
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            self._expire(key, current_time)
            current = self._ensure_session(key, current_time)
            if generation is not None and generation != current:
                return False
            self._conn.execute(
                "INSERT INTO turns (tenant, app, kind, owner, root, generation, role, sender, text, projects_json, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*self._key_values(key), current, turn.role, turn.sender, turn.text, json.dumps(sorted(turn.projects)), current_time),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at_ms = ? WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ?",
                (current_time, *self._key_values(key)),
            )
        return True

    def history(self, key: SessionKey, allowed_projects: frozenset[str], *, now_ms: int | None = None) -> list[Turn]:
        if not isinstance(allowed_projects, frozenset):
            raise ValueError("allowed_projects must be a frozen set")
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            self._expire(key, current_time)
            generation = self._current_generation(key)
            if generation is None:
                return []
            rows = self._conn.execute(
                "SELECT role, sender, text, projects_json FROM turns WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? AND generation = ? ORDER BY id",
                (*self._key_values(key), generation),
            ).fetchall()
        return [
            Turn(row["role"], row["sender"], row["text"], frozenset(json.loads(row["projects_json"])))
            for row in rows
            if frozenset(json.loads(row["projects_json"])).issubset(allowed_projects)
        ]

    def reset(self, key: SessionKey, *, now_ms: int | None = None) -> int:
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            self._expire(key, current_time)
            current = self._ensure_session(key, current_time)
            return self._invalidate_generation(key, current, current_time, forget=False)

    def forget(self, key: SessionKey, *, now_ms: int | None = None) -> int:
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            self._expire(key, current_time)
            current = self._ensure_session(key, current_time)
            return self._invalidate_generation(key, current, current_time, forget=True)

    def forget_revoked(self, key: SessionKey, *, now_ms: int | None = None) -> bool:
        """Purge a fully revoked identity once, while keeping its deduplication tombstones."""
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            self._expire(key, current_time)
            row = self._conn.execute(
                "SELECT generation, revoked_at_ms FROM sessions WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ?",
                self._key_values(key),
            ).fetchone()
            if row is None or row["revoked_at_ms"] is not None:
                return False
            self._invalidate_generation(key, int(row["generation"]), current_time, forget=True)
            self._conn.execute(
                "UPDATE sessions SET revoked_at_ms = ? WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ?",
                (current_time, *self._key_values(key)),
            )
            return True

    def apply_command(self, key: SessionKey, message_id: str, sender: str, text: str, *, forget: bool, chat_id: str | None = None, projects: frozenset[str] = frozenset(), now_ms: int | None = None) -> bool:
        """Atomically deduplicate a state command before it invalidates a generation."""
        if not message_id or not sender or not isinstance(text, str):
            raise ValueError("incoming command requires ID, sender, and text")
        target_chat = chat_id or key.owner
        if not isinstance(target_chat, str) or not target_chat:
            raise ValueError("incoming command requires a chat ID")
        if not isinstance(projects, frozenset) or any(not isinstance(project, str) or not project for project in projects):
            raise ValueError("incoming projects must be a frozen set of project slugs")
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            self._expire(key, current_time)
            generation = self._ensure_session(key, current_time)
            result = self._conn.execute(
                "INSERT INTO incoming (tenant, app, message_id, kind, owner, root, generation, sender, chat_id, text, admitted_projects_json, status, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?) ON CONFLICT(tenant, app, message_id) DO NOTHING",
                (key.tenant, key.app, message_id, key.kind, key.owner, key.root, generation, sender, target_chat, text, json.dumps(sorted(projects)), current_time, current_time),
            )
            if not result.rowcount:
                return False
            self._invalidate_generation(key, generation, current_time, forget=forget)
            return True

    def record_incoming(self, key: SessionKey, message_id: str, sender: str, text: str, *, chat_id: str | None = None, projects: frozenset[str] = frozenset(), now_ms: int | None = None) -> bool:
        if not message_id or not sender or not isinstance(text, str):
            raise ValueError("incoming message requires ID, sender, and text")
        if not isinstance(chat_id or key.owner, str) or not (chat_id or key.owner):
            raise ValueError("incoming message requires a chat ID")
        if not isinstance(projects, frozenset) or any(not isinstance(project, str) or not project for project in projects):
            raise ValueError("incoming projects must be a frozen set of project slugs")
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            self._expire(key, current_time)
            generation = self._ensure_session(key, current_time)
            result = self._conn.execute(
                "INSERT INTO incoming (tenant, app, message_id, kind, owner, root, generation, sender, chat_id, text, admitted_projects_json, status, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?) ON CONFLICT(tenant, app, message_id) DO NOTHING",
                (key.tenant, key.app, message_id, key.kind, key.owner, key.root, generation, sender, chat_id or key.owner, text, json.dumps(sorted(projects)), current_time, current_time),
            )
            if result.rowcount:
                self._conn.execute(
                    "UPDATE sessions SET updated_at_ms = ?, revoked_at_ms = NULL WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ?",
                    (current_time, *self._key_values(key)),
                )
            return bool(result.rowcount)

    def incoming_content(self, key: SessionKey, message_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT text FROM incoming WHERE tenant = ? AND app = ? AND message_id = ? AND kind = ? AND owner = ? AND root = ?",
            (key.tenant, key.app, message_id, key.kind, key.owner, key.root),
        ).fetchone()
        return None if row is None else row["text"]

    def incoming_generation(self, key: SessionKey, message_id: str) -> int | None:
        """Return the immutable generation captured when this event was admitted."""
        row = self._conn.execute(
            "SELECT generation FROM incoming WHERE tenant = ? AND app = ? AND message_id = ? AND kind = ? AND owner = ? AND root = ?",
            (key.tenant, key.app, message_id, key.kind, key.owner, key.root),
        ).fetchone()
        return None if row is None else int(row["generation"])

    def request_reaction(self, key: SessionKey, message_id: str, emoji_type: str, *, generation: int, now_ms: int | None = None) -> bool:
        """Reserve one platform reaction for a durably admitted message."""
        if not message_id or not emoji_type:
            raise ValueError("reaction message ID and emoji type are required")
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            if self._current_generation(key) != generation:
                return False
            result = self._conn.execute(
                "INSERT INTO reactions (tenant, app, message_id, kind, owner, root, generation, emoji_type, status, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'creating', ?) ON CONFLICT(tenant, app,message_id) DO NOTHING",
                (key.tenant, key.app, message_id, key.kind, key.owner, key.root, generation, emoji_type, current_time),
            )
            return bool(result.rowcount)

    def record_reaction_created(self, tenant: str, app: str, message_id: str, reaction_id: str, *, now_ms: int | None = None) -> bool:
        """Persist the platform ID; return whether the reaction is still wanted."""
        if not reaction_id:
            raise ValueError("reaction_id is required")
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            row = self._conn.execute(
                "SELECT status FROM reactions WHERE tenant = ? AND app = ? AND message_id = ?",
                (tenant, app, message_id),
            ).fetchone()
            if row is None or row["status"] not in {"creating", "cleanup"}:
                return False
            status = "active" if row["status"] == "creating" else "cleanup"
            self._conn.execute(
                "UPDATE reactions SET reaction_id = ?, status = ?, updated_at_ms = ? WHERE tenant = ? AND app = ? AND message_id = ?",
                (reaction_id, status, current_time, tenant, app, message_id),
            )
            return status == "active"

    def mark_reaction_create_failed(self, tenant: str, app: str, message_id: str, *, now_ms: int | None = None) -> bool:
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            result = self._conn.execute(
                "UPDATE reactions SET status = 'done', updated_at_ms = ? WHERE tenant = ? AND app = ? AND message_id = ? AND reaction_id IS NULL AND status IN ('creating', 'cleanup')",
                (current_time, tenant, app, message_id),
            )
            return bool(result.rowcount)

    def request_reaction_cleanup(self, tenant: str, app: str, message_id: str, *, now_ms: int | None = None) -> str | None:
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            self._conn.execute(
                "UPDATE reactions SET status = 'cleanup', updated_at_ms = ? WHERE tenant = ? AND app = ? AND message_id = ? AND status IN ('creating', 'active')",
                (current_time, tenant, app, message_id),
            )
            row = self._conn.execute(
                "SELECT reaction_id FROM reactions WHERE tenant = ? AND app = ? AND message_id = ? AND status = 'cleanup'",
                (tenant, app, message_id),
            ).fetchone()
            return None if row is None else row["reaction_id"]

    def request_session_reaction_cleanup(self, key: SessionKey, *, now_ms: int | None = None) -> int:
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            return self._conn.execute(
                "UPDATE reactions SET status = 'cleanup', updated_at_ms = ? WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? AND status IN ('creating', 'active')",
                (current_time, *self._key_values(key)),
            ).rowcount

    def pending_reaction_cleanup(self) -> list[PendingReaction]:
        rows = self._conn.execute(
            "SELECT tenant, app, message_id, reaction_id FROM reactions WHERE status = 'cleanup' AND reaction_id IS NOT NULL ORDER BY updated_at_ms, message_id"
        ).fetchall()
        return [PendingReaction(row["tenant"], row["app"], row["message_id"], row["reaction_id"]) for row in rows]

    def mark_reaction_removed(self, tenant: str, app: str, message_id: str, reaction_id: str, *, now_ms: int | None = None) -> bool:
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            result = self._conn.execute(
                "UPDATE reactions SET status = 'done', updated_at_ms = ? WHERE tenant = ? AND app = ? AND message_id = ? AND reaction_id = ? AND status = 'cleanup'",
                (current_time, tenant, app, message_id, reaction_id),
            )
            return bool(result.rowcount)

    def recover_reactions(self, *, now_ms: int | None = None) -> int:
        """Queue known active reactions for deletion; never retry an ambiguous create."""
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            result = self._conn.execute(
                "UPDATE reactions SET status = 'cleanup', updated_at_ms = ? WHERE status = 'active' AND reaction_id IS NOT NULL",
                (current_time,),
            )
            self._conn.execute(
                "UPDATE reactions SET status = 'done', updated_at_ms = ? WHERE status = 'creating' AND reaction_id IS NULL",
                (current_time,),
            )
            return result.rowcount

    def start_incoming(self, key: SessionKey, message_id: str, *, now_ms: int | None = None) -> bool:
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            self._expire(key, current_time)
            current = self._current_generation(key)
            if current is None:
                return False
            result = self._conn.execute(
                "UPDATE incoming SET status = 'processing', updated_at_ms = ? WHERE tenant = ? AND app = ? AND message_id = ? AND kind = ? AND owner = ? AND root = ? AND generation = ? AND status = 'queued'",
                (current_time, key.tenant, key.app, message_id, key.kind, key.owner, key.root, current),
            )
            return bool(result.rowcount)

    def finish_incoming(self, key: SessionKey, message_id: str, status: str = "completed", *, now_ms: int | None = None) -> bool:
        if status not in {"completed", "failed"}:
            raise ValueError("incoming status must be completed or failed")
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            current = self._current_generation(key)
            if current is None:
                return False
            result = self._conn.execute(
                "UPDATE incoming SET status = ?, updated_at_ms = ? WHERE tenant = ? AND app = ? AND message_id = ? AND kind = ? AND owner = ? AND root = ? AND generation = ? AND status = 'processing'",
                (status, current_time, key.tenant, key.app, message_id, key.kind, key.owner, key.root, current),
            )
            return bool(result.rowcount)

    def reclaim_abandoned_processing(self, *, now_ms: int | None = None, stale_after_ms: int = 300_000) -> int:
        if stale_after_ms < 1:
            raise ValueError("stale_after_ms must be positive")
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            result = self._conn.execute(
                "UPDATE incoming SET status = 'queued', updated_at_ms = ? WHERE status = 'processing' AND updated_at_ms <= ? AND generation = (SELECT generation FROM sessions WHERE sessions.tenant = incoming.tenant AND sessions.app = incoming.app AND sessions.kind = incoming.kind AND sessions.owner = incoming.owner AND sessions.root = incoming.root)",
                (current_time, current_time - stale_after_ms),
            )
            return result.rowcount

    def fail_incoming(self, key: SessionKey, message_id: str, *, now_ms: int | None = None) -> bool:
        """Terminally record a post-admission authorization denial without a reply."""
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            current = self._current_generation(key)
            if current is None:
                return False
            result = self._conn.execute(
                "UPDATE incoming SET status = 'failed', updated_at_ms = ? WHERE tenant = ? AND app = ? AND message_id = ? AND kind = ? AND owner = ? AND root = ? AND generation = ? AND status IN ('queued', 'processing')",
                (current_time, key.tenant, key.app, message_id, key.kind, key.owner, key.root, current),
            )
            return bool(result.rowcount)

    def recover_interrupted(self, *, now_ms: int | None = None) -> int:
        """After restart, finish accepted work honestly because source resources are not replayed."""
        current_time = _now_ms() if now_ms is None else now_ms
        message = "The service restarted before I could finish that request. Please send it again."
        with self._conn:
            rows = self._conn.execute(
                "SELECT i.* FROM incoming i JOIN sessions s ON s.tenant = i.tenant AND s.app = i.app AND s.kind = i.kind AND s.owner = i.owner AND s.root = i.root AND s.generation = i.generation WHERE i.status IN ('queued', 'processing') AND i.sender IS NOT NULL AND i.chat_id <> ''"
            ).fetchall()
            for row in rows:
                existing = self._conn.execute(
                    "SELECT 1 FROM outbox WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? AND message_id = ? AND generation = ? LIMIT 1",
                    (row["tenant"], row["app"], row["kind"], row["owner"], row["root"], row["message_id"], row["generation"]),
                ).fetchone()
                if existing is None:
                    reply_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"teammem-chat-recovery/{row['tenant']}/{row['app']}/{row['message_id']}"))
                    self._conn.execute(
                        "INSERT INTO outbox (reply_id, tenant, app, kind, owner, root, message_id, generation, text, projects_json, admitted_projects_json, sender, chat_id, created_at_ms, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, 'queued') ON CONFLICT(reply_id) DO NOTHING",
                        (reply_id, row["tenant"], row["app"], row["kind"], row["owner"], row["root"], row["message_id"], row["generation"], message, row["admitted_projects_json"], row["sender"], row["chat_id"], current_time),
                    )
                    status = "failed"
                else:
                    status = "completed"
                self._conn.execute(
                    "UPDATE incoming SET status = ?, updated_at_ms = ? WHERE tenant = ? AND app = ? AND message_id = ?",
                    (status, current_time, row["tenant"], row["app"], row["message_id"]),
                )
            return len(rows)

    def queue_reply(self, key: SessionKey, message_id: str, reply_id: str, text: str, projects: frozenset[str], *, generation: int, now_ms: int | None = None) -> bool:
        try:
            uuid.UUID(reply_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("reply_id must be a UUID") from exc
        if not isinstance(text, str) or not isinstance(projects, frozenset) or any(not isinstance(project, str) or not project for project in projects):
            raise ValueError("reply text and projects are invalid")
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            current = self._current_generation(key)
            if current != generation:
                return False
            incoming = self._conn.execute(
                "SELECT generation, status, sender, chat_id, admitted_projects_json FROM incoming WHERE tenant = ? AND app = ? AND message_id = ? AND kind = ? AND owner = ? AND root = ?",
                (key.tenant, key.app, message_id, key.kind, key.owner, key.root),
            ).fetchone()
            if incoming is None or incoming["generation"] != generation or incoming["status"] != "processing":
                return False
            result = self._conn.execute(
                "INSERT INTO outbox (reply_id, tenant, app, kind, owner, root, message_id, generation, text, projects_json, admitted_projects_json, sender, chat_id, created_at_ms, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued') ON CONFLICT(reply_id) DO NOTHING",
                (reply_id, *self._key_values(key), message_id, generation, text, json.dumps(sorted(projects)), incoming["admitted_projects_json"], incoming["sender"], incoming["chat_id"], current_time),
            )
            return bool(result.rowcount)

    def pending_replies(self) -> list[QueuedReply]:
        rows = self._conn.execute(
            "SELECT o.reply_id, o.tenant, o.app, o.kind, o.owner, o.root, o.message_id, o.text, o.projects_json, o.admitted_projects_json, o.sender, o.chat_id, o.generation, o.created_at_ms, o.status FROM outbox o JOIN sessions s ON s.tenant = o.tenant AND s.app = o.app AND s.kind = o.kind AND s.owner = o.owner AND s.root = o.root AND s.generation = o.generation WHERE o.status = 'queued' ORDER BY o.created_at_ms, o.reply_id"
        ).fetchall()
        return [
            QueuedReply(row["reply_id"], SessionKey(row["tenant"], row["app"], row["kind"], row["owner"], row["root"]), row["message_id"], row["text"], frozenset(json.loads(row["projects_json"])), frozenset(json.loads(row["admitted_projects_json"])), row["sender"], row["chat_id"], row["generation"], row["created_at_ms"], row["status"])
            for row in rows
        ]

    def mark_reply_delivered(self, reply_id: str, platform_message_id: str) -> bool:
        if not platform_message_id:
            raise ValueError("platform_message_id is required")
        with self._conn:
            row = self._conn.execute(
                "SELECT tenant, app, kind, owner, root, message_id, generation FROM outbox WHERE reply_id = ? AND status = 'queued'",
                (reply_id,),
            ).fetchone()
            result = self._conn.execute(
                "UPDATE outbox SET status = 'delivered', platform_message_id = ? WHERE reply_id = ? AND status = 'queued'",
                (platform_message_id, reply_id),
            )
            if result.rowcount and row is not None:
                used = self._conn.execute(
                    "SELECT 1 FROM interactions WHERE tenant = ? AND app = ? AND platform_reply_message_id = ?",
                    (row["tenant"], row["app"], platform_message_id),
                ).fetchone()
                if used is None:
                    self._conn.execute(
                        "UPDATE interactions SET platform_reply_message_id = ? WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? AND original_message_id = ? AND generation = ?",
                        (platform_message_id, row["tenant"], row["app"], row["kind"], row["owner"], row["root"], row["message_id"], row["generation"]),
                    )
            return bool(result.rowcount)

    def record_interaction(self, key: SessionKey, message_id: str, answer: str, projects: frozenset[str], *, generation: int, now_ms: int | None = None) -> bool:
        """Bind a recorded question and its answer for the one permitted thread seed."""
        if not isinstance(answer, str) or not isinstance(projects, frozenset):
            raise ValueError("interaction answer and projects are invalid")
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            if self._current_generation(key) != generation:
                return False
            incoming = self._conn.execute(
                "SELECT sender, chat_id, text FROM incoming WHERE tenant = ? AND app = ? AND message_id = ? AND kind = ? AND owner = ? AND root = ? AND generation = ? AND status = 'processing'",
                (key.tenant, key.app, message_id, key.kind, key.owner, key.root, generation),
            ).fetchone()
            if incoming is None or incoming["sender"] is None or incoming["text"] is None:
                return False
            result = self._conn.execute(
                "INSERT INTO interactions (tenant, app, original_message_id, kind, owner, root, generation, sender, chat_id, question, answer, answer_projects_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(tenant, app, original_message_id) DO NOTHING",
                (key.tenant, key.app, message_id, key.kind, key.owner, key.root, generation, incoming["sender"], incoming["chat_id"], incoming["text"], answer, json.dumps(sorted(projects))),
            )
            return bool(result.rowcount)

    def seed_thread(self, key: SessionKey, parent_message_id: str, allowed_projects: frozenset[str], *, now_ms: int | None = None) -> bool:
        """Seed an empty thread from exactly its recorded same-chat parent interaction."""
        if key.kind != "thread" or not parent_message_id or not isinstance(allowed_projects, frozenset):
            return False
        current_time = _now_ms() if now_ms is None else now_ms
        with self._conn:
            generation = self._ensure_session(key, current_time)
            if self._conn.execute(
                "SELECT 1 FROM turns WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? AND generation = ? LIMIT 1",
                (*self._key_values(key), generation),
            ).fetchone() is not None:
                return False
            source = self._conn.execute(
                "SELECT i.* FROM interactions i JOIN sessions s ON s.tenant = i.tenant AND s.app = i.app AND s.kind = i.kind AND s.owner = i.owner AND s.root = i.root AND s.generation = i.generation WHERE i.tenant = ? AND i.app = ? AND i.chat_id = ? AND (i.original_message_id = ? OR i.platform_reply_message_id = ?) LIMIT 1",
                (key.tenant, key.app, key.owner, parent_message_id, parent_message_id),
            ).fetchone()
            if source is None:
                return False
            projects = frozenset(json.loads(source["answer_projects_json"]))
            if not projects.issubset(allowed_projects):
                return False
            self._conn.execute(
                "INSERT INTO turns (tenant, app, kind, owner, root, generation, role, sender, text, projects_json, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, 'user', ?, ?, '[]', ?)",
                (*self._key_values(key), generation, source["sender"], source["question"], current_time),
            )
            self._conn.execute(
                "INSERT INTO turns (tenant, app, kind, owner, root, generation, role, sender, text, projects_json, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, 'assistant', 'bot', ?, ?, ?)",
                (*self._key_values(key), generation, source["answer"], source["answer_projects_json"], current_time),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at_ms = ? WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ?",
                (current_time, *self._key_values(key)),
            )
            return True

    def quarantine_reply(self, reply_id: str) -> bool:
        with self._conn:
            result = self._conn.execute(
                "UPDATE outbox SET status = 'quarantined' WHERE reply_id = ? AND status = 'queued'",
                (reply_id,),
            )
            return bool(result.rowcount)

    def quarantine_session_replies(self, key: SessionKey) -> int:
        """Suppress queued delivery when a transport toggle disables this session type."""
        with self._conn:
            return self._conn.execute(
                "UPDATE outbox SET status = 'quarantined' WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? AND status = 'queued'",
                self._key_values(key),
            ).rowcount

    def quarantine_expired_replies(self, *, now_ms: int | None = None, window_ms: int = 3_600_000) -> int:
        current = _now_ms() if now_ms is None else now_ms
        with self._conn:
            return self._conn.execute(
                "UPDATE outbox SET status = 'quarantined' WHERE status = 'queued' AND created_at_ms < ?",
                (current - window_ms,),
            ).rowcount
