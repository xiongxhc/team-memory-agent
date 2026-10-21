"""Session-local attachment admission, retention, and model-context projection."""

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .state import ChatState, SessionKey


class AttachmentAdmissionError(ValueError):
    """A source resource or downloaded byte stream is outside its safe bounds."""


@dataclass(frozen=True)
class AttachmentLimits:
    allowed_extensions: frozenset[str]
    max_files_per_request: int
    max_file_bytes: int
    max_request_bytes: int
    raw_retention_hours: int
    max_extracted_characters_per_file: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.allowed_extensions, frozenset)
            or not self.allowed_extensions
            or any(not isinstance(ext, str) or not ext.startswith(".") for ext in self.allowed_extensions)
        ):
            raise AttachmentAdmissionError("attachment extensions are invalid")
        values = (
            self.max_files_per_request, self.max_file_bytes, self.max_request_bytes,
            self.raw_retention_hours, self.max_extracted_characters_per_file,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in values):
            raise AttachmentAdmissionError("attachment limits must be positive integers")
        if self.max_request_bytes < self.max_file_bytes:
            raise AttachmentAdmissionError("request byte budget is smaller than file budget")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AttachmentLimits":
        try:
            return cls(
                allowed_extensions=frozenset(value["allowed_extensions"]),
                max_files_per_request=value["max_files_per_request"],
                max_file_bytes=value["max_file_bytes"],
                max_request_bytes=value["max_request_bytes"],
                raw_retention_hours=value["raw_retention_hours"],
                max_extracted_characters_per_file=value["max_extracted_characters_per_file"],
            )
        except (KeyError, TypeError) as exc:
            raise AttachmentAdmissionError("attachment limits are incomplete") from exc


@dataclass(frozen=True)
class AttachmentJob:
    id: str
    session: SessionKey
    generation: int
    message_id: str
    resource_id: str
    filename: str
    mime_type: str | None
    declared_bytes: int
    raw_path: Path
    derived_dir: Path
    raw_expires_at_ms: int
    limits: AttachmentLimits


_SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL, app TEXT NOT NULL, kind TEXT NOT NULL,
    owner TEXT NOT NULL, root TEXT NOT NULL, generation INTEGER NOT NULL,
    message_id TEXT NOT NULL, resource_id TEXT NOT NULL,
    filename TEXT NOT NULL, mime_type TEXT, declared_bytes INTEGER NOT NULL,
    actual_bytes INTEGER, raw_path TEXT, derived_dir TEXT NOT NULL,
    raw_expires_at_ms INTEGER NOT NULL, visual_expired_at_ms INTEGER,
    status TEXT NOT NULL, result_json TEXT,
    UNIQUE(tenant, app, kind, owner, root, generation, message_id, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_attachments_session
ON attachments(tenant, app, kind, owner, root, generation, message_id);
"""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _key_values(session: SessionKey) -> tuple[str, str, str, str, str]:
    return session.tenant, session.app, session.kind, session.owner, session.root


def _source_id(value: Mapping[str, Any], names: tuple[str, ...], label: str) -> str:
    candidate = next((value.get(name) for name in names if name in value), None)
    if not isinstance(candidate, str) or not candidate or len(candidate) > 500:
        raise AttachmentAdmissionError(f"attachment {label} is invalid")
    return candidate


def _safe_filename(value: Any, limits: AttachmentLimits) -> str:
    if not isinstance(value, str) or not value or len(value) > 255 or "\x00" in value:
        raise AttachmentAdmissionError("attachment filename is invalid")
    if "/" in value or "\\" in value or Path(value).name != value:
        raise AttachmentAdmissionError("attachment filename must be a basename")
    if Path(value).suffix.lower() not in limits.allowed_extensions:
        raise AttachmentAdmissionError("attachment type is not allowed")
    return value


class AttachmentStore:
    """Owns only attachment metadata and files, never TeamMem project evidence.

    The Feishu adapter must establish that a `message` and `resource` belong to the
    exact authorized chat before calling admission. This class stores their opaque
    IDs and does no network work itself.
    """

    def __init__(self, root: Path, state: ChatState):
        self.root = Path(root).resolve()
        self.state = state
        self.root.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.root / "attachments.sqlite3")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(attachments)")}
        if "visual_expired_at_ms" not in columns:
            self._conn.execute("ALTER TABLE attachments ADD COLUMN visual_expired_at_ms INTEGER")

    def close(self) -> None:
        self._conn.close()

    def _job(self, row: sqlite3.Row, limits: AttachmentLimits) -> AttachmentJob:
        session = SessionKey(row["tenant"], row["app"], row["kind"], row["owner"], row["root"])
        return AttachmentJob(
            id=row["id"], session=session, generation=int(row["generation"]),
            message_id=row["message_id"], resource_id=row["resource_id"],
            filename=row["filename"], mime_type=row["mime_type"],
            declared_bytes=int(row["declared_bytes"]),
            raw_path=(Path(row["raw_path"]) if row["raw_path"] is not None
                      else self.root / "raw" / row["id"] / "content"),
            derived_dir=Path(row["derived_dir"]), raw_expires_at_ms=int(row["raw_expires_at_ms"]),
            limits=limits,
        )

    def _current(self, session: SessionKey, generation: int) -> bool:
        # ChatState is intentionally called only by the service/state-owner thread.
        return self.state.generation(session) == generation

    def admit_attachment(
        self,
        session: SessionKey,
        generation: int,
        message: Mapping[str, Any],
        resource: Mapping[str, Any],
        limits: AttachmentLimits,
        *,
        now_ms: int | None = None,
    ) -> AttachmentJob:
        """Persist bounded source provenance before any resource bytes are read."""
        if not self._current(session, generation):
            raise AttachmentAdmissionError("attachment session generation is no longer current")
        if not isinstance(message, Mapping) or not isinstance(resource, Mapping):
            raise AttachmentAdmissionError("attachment source metadata is invalid")
        message_id = _source_id(message, ("id", "message_id"), "message ID")
        resource_id = _source_id(resource, ("id", "resource_id", "resource_key", "file_key"), "resource ID")
        filename = _safe_filename(resource.get("filename"), limits)
        mime_type = resource.get("mime_type")
        if mime_type is not None and (not isinstance(mime_type, str) or len(mime_type) > 255):
            raise AttachmentAdmissionError("attachment MIME type is invalid")
        declared_bytes = resource.get("size", 0)
        if isinstance(declared_bytes, bool) or not isinstance(declared_bytes, int) or declared_bytes < 0:
            raise AttachmentAdmissionError("attachment size is invalid")
        if declared_bytes > limits.max_file_bytes:
            raise AttachmentAdmissionError("attachment exceeds file byte budget")
        current_time = _now_ms() if now_ms is None else now_ms
        values = _key_values(session)
        with self._conn:
            existing = self._conn.execute(
                "SELECT * FROM attachments WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? "
                "AND generation = ? AND message_id = ? AND resource_id = ?",
                (*values, generation, message_id, resource_id),
            ).fetchone()
            if existing is not None:
                return self._job(existing, limits)
            count, bytes_reserved = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(COALESCE(actual_bytes, declared_bytes)), 0) "
                "FROM attachments WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? "
                "AND generation = ? AND message_id = ? AND status != 'failed'",
                (*values, generation, message_id),
            ).fetchone()
            if int(count) >= limits.max_files_per_request:
                raise AttachmentAdmissionError("attachment request exceeds file count budget")
            if int(bytes_reserved) + declared_bytes > limits.max_request_bytes:
                raise AttachmentAdmissionError("attachment request exceeds byte budget")
            identifier = uuid.uuid4().hex
            raw_path = self.root / "raw" / identifier / "content"
            derived_dir = self.root / "derived" / identifier
            expires_at = current_time + limits.raw_retention_hours * 3_600_000
            self._conn.execute(
                "INSERT INTO attachments (id, tenant, app, kind, owner, root, generation, message_id, resource_id, filename, mime_type, declared_bytes, raw_path, derived_dir, raw_expires_at_ms, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admitted')",
                (identifier, *values, generation, message_id, resource_id, filename, mime_type,
                 declared_bytes, str(raw_path), str(derived_dir), expires_at),
            )
        return AttachmentJob(identifier, session, generation, message_id, resource_id, filename,
                             mime_type, declared_bytes, raw_path, derived_dir, expires_at, limits)

    def _set_failed(self, job: AttachmentJob) -> None:
        with self._conn:
            self._conn.execute("UPDATE attachments SET status = 'failed' WHERE id = ?", (job.id,))

    def _other_request_bytes(self, job: AttachmentJob) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(COALESCE(actual_bytes, declared_bytes)), 0) FROM attachments "
            "WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? AND generation = ? "
            "AND message_id = ? AND id != ? AND status != 'failed'",
            (*_key_values(job.session), job.generation, job.message_id, job.id),
        ).fetchone()
        return int(row[0])

    def write_raw(self, job: AttachmentJob, chunks: Iterable[bytes], *, now_ms: int | None = None) -> Path:
        """Stream a Feishu-verified resource with actual file and request bounds."""
        if not self._current(job.session, job.generation):
            raise AttachmentAdmissionError("attachment session generation is no longer current")
        row = self._conn.execute("SELECT status FROM attachments WHERE id = ?", (job.id,)).fetchone()
        if row is None or row["status"] != "admitted":
            raise AttachmentAdmissionError("attachment is not awaiting download")
        job.raw_path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        total = 0
        try:
            descriptor, raw_temp = tempfile.mkstemp(prefix="download-", dir=job.raw_path.parent)
            temporary = Path(raw_temp)
            with os.fdopen(descriptor, "wb") as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise AttachmentAdmissionError("attachment stream yielded non-bytes")
                    total += len(chunk)
                    if total > job.limits.max_file_bytes or self._other_request_bytes(job) + total > job.limits.max_request_bytes:
                        raise AttachmentAdmissionError("attachment stream exceeds byte budget")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if not self._current(job.session, job.generation):
                raise AttachmentAdmissionError("attachment session generation changed during download")
            os.replace(temporary, job.raw_path)
            temporary = None
            with self._conn:
                self._conn.execute(
                    "UPDATE attachments SET actual_bytes = ?, status = 'downloaded' WHERE id = ?",
                    (total, job.id),
                )
            return job.raw_path
        except Exception:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            job.raw_path.unlink(missing_ok=True)
            self._set_failed(job)
            raise

    def record_result(self, job: AttachmentJob, result: Mapping[str, Any]) -> bool:
        """Persist parser output only if its originating generation is still current."""
        if not self._current(job.session, job.generation):
            self._remove_paths(job)
            return False
        row = self._conn.execute("SELECT status FROM attachments WHERE id = ?", (job.id,)).fetchone()
        if row is None or row["status"] not in {"downloaded", "parsed"}:
            raise AttachmentAdmissionError("attachment is not ready for parse output")
        if not isinstance(result, Mapping) or result.get("filename") != job.filename:
            raise AttachmentAdmissionError("parser result has invalid provenance")
        fragments = result.get("fragments")
        images = result.get("images")
        coverage = result.get("coverage")
        if not isinstance(fragments, list) or not isinstance(images, list) or not isinstance(coverage, Mapping):
            raise AttachmentAdmissionError("parser result is incomplete")
        characters = 0
        normalized_fragments = []
        for fragment in fragments:
            if not isinstance(fragment, Mapping):
                raise AttachmentAdmissionError("parser fragment is invalid")
            text, locator, kind = fragment.get("text"), fragment.get("locator"), fragment.get("kind")
            if not isinstance(text, str) or not isinstance(locator, str) or kind not in {"text", "ocr", "notes"}:
                raise AttachmentAdmissionError("parser fragment is invalid")
            characters += len(text)
            normalized_fragments.append({"text": text, "locator": locator, "kind": kind})
        if characters > job.limits.max_extracted_characters_per_file:
            raise AttachmentAdmissionError("parser result exceeds extracted text budget")
        normalized_images = []
        root = job.derived_dir.resolve()
        for image in images:
            if not isinstance(image, Mapping) or image.get("kind") != "visual":
                raise AttachmentAdmissionError("parser image is invalid")
            path, locator = image.get("path"), image.get("locator")
            if not isinstance(path, str) or not isinstance(locator, str):
                raise AttachmentAdmissionError("parser image is invalid")
            candidate = Path(path).resolve()
            if root not in candidate.parents or candidate.suffix.lower() not in {".png", ".jpg", ".jpeg"} or not candidate.is_file():
                raise AttachmentAdmissionError("parser image is outside the attachment namespace")
            normalized_images.append({"path": str(candidate), "locator": locator, "kind": "visual"})
        stored = {"fragments": normalized_fragments, "images": normalized_images, "coverage": dict(coverage)}
        if not self._current(job.session, job.generation):
            self._remove_paths(job)
            return False
        with self._conn:
            self._conn.execute(
                "UPDATE attachments SET result_json = ?, status = 'parsed' WHERE id = ?",
                (json.dumps(stored, ensure_ascii=False), job.id),
            )
        return True

    def normalize_downloaded_image(self, job: AttachmentJob) -> AttachmentJob:
        """Correct a Feishu image placeholder suffix from verified local bytes."""
        row = self._conn.execute("SELECT status FROM attachments WHERE id = ?", (job.id,)).fetchone()
        if row is None or row["status"] != "downloaded" or not job.raw_path.is_file():
            raise AttachmentAdmissionError("attachment image is not ready for signature inspection")
        with job.raw_path.open("rb") as source:
            extension = source.read(16)
        detected = ".png" if extension.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg" if extension.startswith(b"\xff\xd8\xff") else None
        if detected is None or Path(job.filename).suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            return job
        filename = Path(job.filename).with_suffix(detected).name
        if filename == job.filename:
            return job
        with self._conn:
            self._conn.execute("UPDATE attachments SET filename = ? WHERE id = ?", (filename, job.id))
        return replace(job, filename=filename)

    def context_fragments(self, session: SessionKey, generation: int) -> list[dict[str, Any]]:
        """Flatten current attachment citations for the model without project scopes."""
        # A request can arrive between periodic maintenance passes; never expose an
        # expired local image merely because the cleanup timer has not run yet.
        self.cleanup_expired()
        if not self._current(session, generation):
            return []
        rows = self._conn.execute(
            "SELECT id, filename, result_json FROM attachments WHERE tenant = ? AND app = ? AND kind = ? "
            "AND owner = ? AND root = ? AND generation = ? AND status = 'parsed' ORDER BY id",
            (*_key_values(session), generation),
        ).fetchall()
        context: list[dict[str, Any]] = []
        for row in rows:
            result = json.loads(row["result_json"])
            for index, fragment in enumerate(result["fragments"]):
                context.append({
                    "id": f"{row['id']}:fragment:{index}", "filename": row["filename"],
                    "locator": fragment["locator"], "text": fragment["text"],
                    "coverage": result["coverage"],
                })
            for index, image in enumerate(result["images"]):
                context.append({
                    "id": f"{row['id']}:image:{index}", "filename": row["filename"],
                    "locator": image["locator"], "text": "", "image_path": image["path"],
                    "coverage": result["coverage"],
                })
        return context

    def select_context(
        self, session: SessionKey, generation: int, question: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Choose bounded lexical attachment context, retaining selected-file visuals."""
        if (
            not isinstance(question, str) or not question.strip() or len(question) > 2_000
            or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
        ):
            raise AttachmentAdmissionError("attachment context selector arguments are invalid")
        terms = tuple(dict.fromkeys(
            term.casefold() for term in re.findall(r"[A-Za-z0-9_]{2,}|[\u3400-\u9fff]+", question)
        ))
        if not terms:
            terms = (question.casefold(),)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in self.context_fragments(session, generation):
            grouped.setdefault(item["id"].split(":", 1)[0], []).append(item)
        ranked = []
        for order, (attachment_id, items) in enumerate(grouped.items()):
            matches = [
                item for item in items
                if item["text"] and any(term in item["text"].casefold() for term in terms)
            ]
            first_text = next((item for item in items if item["text"]), items[0])
            ranked.append((not bool(matches), order, attachment_id, matches[0] if matches else first_text, items))
        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()
        selected_groups: dict[str, list[dict[str, Any]]] = {}
        for _, _, attachment_id, primary, items in sorted(ranked):
            for item in (primary, *(candidate for candidate in items if "image_path" in candidate)):
                if item["id"] not in selected_ids and len(selected) < limit:
                    selected.append(item)
                    selected_ids.add(item["id"])
                    selected_groups.setdefault(attachment_id, []).append(item)
        remaining = [
            item for _, _, _, _, items in sorted(ranked) for item in items
            if item["id"] not in selected_ids and item["text"]
        ]
        for item in remaining:
            if len(selected) == limit:
                break
            attachment_id = item["id"].split(":", 1)[0]
            selected.append(item)
            selected_ids.add(item["id"])
            selected_groups.setdefault(attachment_id, []).append(item)
        compact: list[dict[str, Any]] = []
        for item in selected:
            attachment_id = item["id"].split(":", 1)[0]
            output = dict(item)
            group = grouped[attachment_id]
            if selected_groups[attachment_id][0]["id"] == item["id"]:
                coverage = dict(output["coverage"])
                coverage["context_omitted_fragments"] = len(group) - len(selected_groups[attachment_id])
                output["coverage"] = coverage
            else:
                output.pop("coverage", None)
            compact.append(output)
        return compact

    def _remove_paths(self, job: AttachmentJob) -> None:
        self._remove_owned_paths(job.raw_path, job.derived_dir)

    def _remove_row_paths(self, row: sqlite3.Row) -> None:
        raw_path = row["raw_path"]
        self._remove_owned_paths(
            None if raw_path is None else Path(raw_path), Path(row["derived_dir"])
        )

    def _remove_owned_paths(self, raw_path: Path | None, derived_dir: Path | None) -> None:
        paths = ((raw_path, True), (derived_dir, False))
        for path, is_file in paths:
            if path is None:
                continue
            candidate = path.resolve()
            if self.root not in candidate.parents:
                raise AttachmentAdmissionError("attachment cleanup path is outside its namespace")
            if is_file:
                candidate.unlink(missing_ok=True)
                candidate = candidate.parent
            if candidate.exists():
                shutil.rmtree(candidate)

    def invalidate_session(self, session: SessionKey) -> None:
        """Delete stale attachment files after a synchronous reset, forget, or revocation."""
        current = self.state.generation(session)
        rows = self._conn.execute(
            "SELECT * FROM attachments WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? AND generation != ?",
            (*_key_values(session), current),
        ).fetchall()
        for row in rows:
            self._remove_row_paths(row)
        with self._conn:
            self._conn.execute(
                "DELETE FROM attachments WHERE tenant = ? AND app = ? AND kind = ? AND owner = ? AND root = ? AND generation != ?",
                (*_key_values(session), current),
            )

    def discard(self, job: AttachmentJob) -> None:
        """Remove a cancelled parser job even when reset already removed its DB row."""
        self._remove_paths(job)
        with self._conn:
            self._conn.execute("DELETE FROM attachments WHERE id = ?", (job.id,))

    def cleanup_expired(self, *, now_ms: int | None = None) -> int:
        """Remove expired raw and derived artifacts, retaining only current extracted text."""
        current_time = _now_ms() if now_ms is None else now_ms
        stale_rows = []
        for row in self._conn.execute("SELECT * FROM attachments").fetchall():
            session = SessionKey(row["tenant"], row["app"], row["kind"], row["owner"], row["root"])
            if self.state.generation(session) != int(row["generation"]):
                stale_rows.append(row)
        for row in stale_rows:
            self._remove_row_paths(row)
        with self._conn:
            self._conn.executemany("DELETE FROM attachments WHERE id = ?", [(row["id"],) for row in stale_rows])
        rows = self._conn.execute(
            "SELECT id, raw_path, derived_dir, result_json FROM attachments WHERE visual_expired_at_ms IS NULL AND raw_expires_at_ms <= ?",
            (current_time,),
        ).fetchall()
        for row in rows:
            self._remove_owned_paths(None if row["raw_path"] is None else Path(row["raw_path"]), Path(row["derived_dir"]))
        with self._conn:
            for row in rows:
                result_json = row["result_json"]
                if result_json is not None:
                    result = json.loads(result_json)
                    images = result.get("images") if isinstance(result, Mapping) else None
                    if isinstance(images, list) and images:
                        result["images"] = []
                        coverage = dict(result.get("coverage") or {})
                        warnings = coverage.get("warnings")
                        warnings = list(warnings) if isinstance(warnings, list) else []
                        if "visual artifacts expired" not in warnings:
                            warnings.append("visual artifacts expired")
                        coverage["warnings"] = warnings
                        result["coverage"] = coverage
                    result_json = json.dumps(result, ensure_ascii=False)
                self._conn.execute(
                    "UPDATE attachments SET raw_path = NULL, result_json = ?, visual_expired_at_ms = ? WHERE id = ?",
                    (result_json, current_time, row["id"]),
                )
        return len(stale_rows) + len(rows)
