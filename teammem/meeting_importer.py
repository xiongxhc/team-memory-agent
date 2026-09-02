"""Import meeting bundles from an inbox and adopt their encrypted audio.

The importer is the only thing that turns a producer's directory into stored
state, and it does so in one fixed order per bundle:

1. **Validate everything first.** `teammem.meeting_bundles.load_meeting_bundle`
   checks the payload, the manifest and the audio digest before this module
   touches anything. Nothing is mutated until that returns.
2. **Publish the audio, verified.** The encrypted bytes are copied into a
   staging file inside the destination directory, fsynced, hashed by reading
   the staged copy back, checked against the payload, and only then moved into
   their final name with `os.replace`. A crash can leave a staging file, never
   a final-named partial one, so a database row can never point at half a file.
3. **Write the database.** `teammem.meeting_store.import_meeting_revision` owns
   every meeting SQL statement and runs its own `BEGIN IMMEDIATE`. This module
   writes none.
4. **Consume the source.** An accepted bundle's `meeting.json` and
   `manifest.json` move to the archive and the inbox directory is removed. The
   encrypted audio is *not* archived: it already lives in the audio store, and
   a second copy would double the retention surface the expiry job must erase.

Routing is exhaustive. Every `READY` bundle ends as one of `accepted`,
`unchanged`, `activated` or `quarantined`, each with a short machine-readable
reason code — never prose, never transcript text, never a credential. A bundle
directory without its `READY` marker is not an error: the producer is still
writing it, so it is skipped silently and left for the next scan.

Environmental faults are not the producer's fault. A failed copy (a full disk)
or an unexpected crash in the store cleans up the audio it staged, leaves the
bundle in the inbox, and re-raises so the run stops instead of quarantining a
bundle that was never wrong.

TeamMem never holds the recording key and never decrypts audio; it only ever
copies and hashes opaque bytes.
"""

import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .meeting_bundles import (
    AUDIO_FILENAME,
    MANIFEST_FILENAME,
    MEETING_FILENAME,
    READY_FILENAME,
    MeetingBundle,
    MeetingBundleError,
    load_meeting_bundle,
    occurrence_path_segment,
)
from .meeting_store import (
    MeetingRevisionConflict,
    MeetingRevisionSuperseded,
    MeetingStoreError,
    StagedAudio,
    import_meeting_revision,
    list_meeting_revisions,
)


log = logging.getLogger(__name__)

_COPY_CHUNK = 1024 * 1024

# Windows defines no O_NOFOLLOW; 0 there leaves the copy exactly as it was
# rather than dropping the protection everywhere else.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

OUTCOMES = ("accepted", "unchanged", "activated", "quarantined")

REASON_CODES = (
    "ok",                       # accepted, unchanged or activated
    "path_escape",              # a symlink, or a path resolving outside the inbox
    "malformed_json",           # meeting.json or manifest.json is not readable JSON
    "incomplete_bundle",        # a required member is absent or not a regular file
    "bundle_invalid",           # the payload violates teammem-meeting/v1
    "hash_mismatch",            # the manifest's artifact_hash is not the canonical hash
    "audio_hash_mismatch",      # audio.enc does not match its declared digest or size
    "audio_plaintext",          # audio.enc announces itself as an unencrypted container
    "audio_conflict",           # the audio store already holds different bytes here
    "revision_conflict",        # this revision is stored with a different artifact hash
    "revision_superseded",      # a newer revision of this occurrence is already current
    "store_rejected",           # the store refused the write for any other reason
    "archive_failed",           # the revision is stored, but its source copy was not filed
)


@dataclass(frozen=True)
class MeetingImportResult:
    accepted: int = 0
    unchanged: int = 0
    quarantined: int = 0
    activated: int = 0
    details: tuple[tuple[str, str, str], ...] = ()


def audio_revision_dir(audio_store: Path, occurrence_id: str, revision: int) -> Path:
    """`<audio_store>/<occurrence-path-segment>/<revision>/`.

    The occurrence segment is percent-encoded, so a meeting id containing `:`
    or `/` never reaches the filesystem raw and the path stays legal on Windows.
    """
    return Path(audio_store) / occurrence_path_segment(occurrence_id) / str(revision)


class _Rejected(Exception):
    """A bundle this module refuses, carrying the reason code to report."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class _Candidate:
    """One inbox entry to consider, named only by its on-disk components."""

    occurrence_name: str
    revision_name: str | None       # None for an entry directly under the inbox
    path: Path
    symlink: bool

    @property
    def label(self) -> str:
        if self.revision_name is None:
            return self.occurrence_name
        return f"{self.occurrence_name}/{self.revision_name}"


# --- reason classification --------------------------------------------------

# The validator's messages are frozen by Task 1, so mapping them onto codes is
# stable. Order matters: the first match wins.
_BUNDLE_REASONS = (
    ("symlink", "path_escape"),
    ("outside inbox", "path_escape"),
    ("invalid meeting JSON", "malformed_json"),
    ("invalid manifest JSON", "malformed_json"),
    ("bundle is missing", "incomplete_bundle"),
    ("is not a regular file", "incomplete_bundle"),
    ("does not match audio.enc", "audio_hash_mismatch"),
    ("looks like plaintext audio", "audio_plaintext"),
    ("artifact_hash", "hash_mismatch"),
)


def _bundle_reason(message: str) -> str:
    for needle, code in _BUNDLE_REASONS:
        if needle in message:
            return code
    return "bundle_invalid"


def _store_reason(exc: MeetingStoreError) -> str:
    if isinstance(exc, MeetingRevisionConflict):
        return "revision_conflict"
    if isinstance(exc, MeetingRevisionSuperseded):
        return "revision_superseded"
    return "store_rejected"


# --- durable file helpers ---------------------------------------------------


def _fsync_dir(path: Path) -> None:
    """Make a rename or creation in `path` durable. A no-op where unsupported."""
    if os.name == "nt":                                     # Windows cannot fsync a directory
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except BaseException:
        _unlink(temporary)
        raise


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        offset += os.write(fd, data[offset:])


def _copy_file(source: Path, destination: Path) -> None:
    """Copy opaque bytes into a new file and fsync it.

    The source is opened `O_NOFOLLOW`. `load_meeting_bundle` already refused a
    symlinked `audio.enc`, but that check ran in an earlier pass over the same
    directory: without this flag, swapping the file for a symlink in between
    would make this copy read through it. `O_NOFOLLOW` closes that window on
    every platform that defines it, so the refusal happens at the moment of the
    copy rather than resting on a stale check.
    """
    source_fd = os.open(source, os.O_RDONLY | _NOFOLLOW)
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(source_fd, "rb", closefd=False) as handle:
                while chunk := handle.read(_COPY_CHUNK):
                    _write_all(fd, chunk)
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.close(source_fd)


def _digest_file(path: Path) -> tuple[str, int]:
    """`(sha256:<hex>, size)` read back from disk, not from what was written."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_COPY_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return "sha256:" + digest.hexdigest(), size


def _unlink(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _prune_empty(path: Path, stop: Path) -> None:
    """Remove `path` and its parents while they are empty, never past `stop`."""
    current = path
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


# --- audio adoption ---------------------------------------------------------


def _stage_audio(bundle: MeetingBundle, audio_store: Path) -> tuple[StagedAudio, Path | None]:
    """Publish the encrypted audio and describe it.

    Returns the `StagedAudio` to hand to the store plus the final path *if this
    call created it*. That second value is the only thing the failure path is
    ever allowed to delete: audio already present belongs to a stored revision.
    """
    directory = audio_revision_dir(audio_store, bundle.occurrence_id, bundle.revision)
    final = directory / AUDIO_FILENAME
    declared_sha = bundle.payload["audio"]["sha256"]
    declared_bytes = bundle.payload["audio"]["bytes"]

    if final.is_symlink():
        raise _Rejected("audio_conflict")
    if final.exists():
        digest, size = _digest_file(final)
        if digest == declared_sha and size == declared_bytes:
            return StagedAudio(stored_path=str(final), sha256=digest, bytes=size), None
        raise _Rejected("audio_conflict")

    directory.mkdir(parents=True, exist_ok=True)
    staging = directory / f".{AUDIO_FILENAME}.{secrets.token_hex(8)}"
    try:
        _copy_file(bundle.audio_path, staging)
        _fsync_dir(directory)
        digest, size = _digest_file(staging)
        if digest != declared_sha or size != declared_bytes:
            raise _Rejected("audio_hash_mismatch")
        os.replace(staging, final)
        _fsync_dir(directory)
    except BaseException:
        _unlink(staging)
        _prune_empty(directory, Path(audio_store))
        raise
    return StagedAudio(stored_path=str(final), sha256=digest, bytes=size), final


def _discard_staged(
    conn: sqlite3.Connection, created: Path | None, bundle: MeetingBundle, audio_store: Path
) -> None:
    """Remove audio this run staged, unless a database row now references it."""
    if created is None:
        return
    for row in list_meeting_revisions(conn, bundle.occurrence_id):
        if row["revision"] == bundle.revision:
            return                                          # the revision landed after all
    _unlink(created)
    _prune_empty(created.parent, Path(audio_store))


# --- archive and quarantine -------------------------------------------------


def _archive_bundle(bundle: MeetingBundle, archive: Path) -> None:
    """Keep the source JSON by occurrence/revision, then consume the inbox copy.

    `manifest.json` carries the occurrence id, the revision and the artifact
    hash, so an archived bundle is identifiable by all three without a
    side-car. `audio.enc` is deliberately not archived — the encrypted bytes
    live in the audio store, and a duplicate would double what expiry must
    delete.
    """
    destination = (
        Path(archive)
        / occurrence_path_segment(bundle.occurrence_id)
        / str(bundle.revision)
    )
    destination.mkdir(parents=True, exist_ok=True)
    for name in (MEETING_FILENAME, MANIFEST_FILENAME):
        _write_atomic(destination / name, (bundle.path / name).read_bytes())
    shutil.rmtree(bundle.path)
    _prune_empty(bundle.path.parent, bundle.path.parent.parent)


def _free_name(parent: Path, name: str) -> Path:
    candidate = parent / name
    suffix = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = parent / f"{name}.{suffix}"
        suffix += 1
    return candidate


def _write_reason(path: Path, payload: dict) -> None:
    _write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")


def _quarantine_bundle(
    candidate: _Candidate,
    quarantine: Path,
    code: str,
    *,
    bundle: MeetingBundle | None = None,
) -> None:
    """Move a rejected bundle out of the inbox with a redacted reason file."""
    parent = Path(quarantine) / candidate.occurrence_name
    parent.mkdir(parents=True, exist_ok=True)
    destination = _free_name(parent, candidate.revision_name)
    shutil.move(str(candidate.path), str(destination))
    _write_reason(
        parent / f"{destination.name}.reason.json",
        {
            "reason_code": code,
            "bundle": candidate.label,
            "occurrence_id": bundle.occurrence_id if bundle else None,
            "revision": bundle.revision if bundle else None,
            "artifact_hash": bundle.artifact_hash if bundle else None,
        },
    )


def _quarantine_symlink(candidate: _Candidate, quarantine: Path) -> None:
    """Record a rejected symlink and remove it. The target is never followed."""
    if candidate.revision_name is None:
        parent, name = Path(quarantine), candidate.occurrence_name
    else:
        parent, name = Path(quarantine) / candidate.occurrence_name, candidate.revision_name
    parent.mkdir(parents=True, exist_ok=True)
    target = str(candidate.path.readlink())
    candidate.path.unlink()
    _write_reason(
        parent / f"{_free_name(parent, name).name}.reason.json",
        {
            "reason_code": "path_escape",
            "bundle": candidate.label,
            "rejected_symlink_target": target,
            "occurrence_id": None,
            "revision": None,
            "artifact_hash": None,
        },
    )


# --- scanning ---------------------------------------------------------------


def _sort_key(candidate: _Candidate) -> tuple:
    """Occurrence, then revision as a number so 2 is applied before 10."""
    if candidate.revision_name is None:
        return (candidate.occurrence_name, 0, 0, "")
    if candidate.revision_name.isdigit():
        return (candidate.occurrence_name, 1, int(candidate.revision_name), "")
    return (candidate.occurrence_name, 2, 0, candidate.revision_name)


def _candidates(inbox: Path) -> list[_Candidate]:
    found: list[_Candidate] = []
    for occurrence in inbox.iterdir():
        if occurrence.is_symlink():
            # Never descend through a link: its contents may be anywhere.
            found.append(_Candidate(occurrence.name, None, occurrence, True))
            continue
        if not occurrence.is_dir():
            continue
        for revision in occurrence.iterdir():
            found.append(
                _Candidate(occurrence.name, revision.name, revision, revision.is_symlink())
            )
    found.sort(key=_sort_key)
    return found


class _Projection:
    """What the database would look like part-way through a dry run.

    A dry run writes nothing, so two revisions of one occurrence in a single
    scan would otherwise both be classified against the same starting state.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._cache: dict[str, list] = {}

    def _state(self, occurrence_id: str) -> list:
        if occurrence_id not in self._cache:
            rows = list_meeting_revisions(self._conn, occurrence_id)
            current = next((row["revision"] for row in rows if row["is_current"]), None)
            hashes = {row["revision"]: row["artifact_hash"] for row in rows}
            self._cache[occurrence_id] = [current, hashes]
        return self._cache[occurrence_id]

    def classify(self, bundle: MeetingBundle) -> tuple[str, str]:
        state = self._state(bundle.occurrence_id)
        current, hashes = state
        if bundle.revision in hashes:
            if hashes[bundle.revision] == bundle.artifact_hash:
                return "unchanged", "ok"
            return "quarantined", "revision_conflict"
        if current is not None and bundle.revision < current:
            return "quarantined", "revision_superseded"
        outcome = "accepted" if current is None else "activated"
        hashes[bundle.revision] = bundle.artifact_hash
        state[0] = bundle.revision if current is None else max(current, bundle.revision)
        return outcome, "ok"


def _current_revision(conn: sqlite3.Connection, occurrence_id: str) -> int | None:
    for row in list_meeting_revisions(conn, occurrence_id):
        if row["is_current"]:
            return row["revision"]
    return None


# --- entrypoint -------------------------------------------------------------


def import_meeting_inbox(
    conn: sqlite3.Connection,
    inbox: Path,
    archive: Path,
    quarantine: Path,
    audio_store: Path,
    *,
    now: str,
    dry_run: bool = False,
) -> MeetingImportResult:
    """Walk `<inbox>/<occurrence-segment>/<revision>/` and route every bundle.

    `now` is the UTC instant recorded as each revision's `imported_at`.

    With `dry_run=True` nothing at all is written: no row, no staged or final
    audio, no archive move and no quarantine move. The reported outcomes are
    the ones a real run would produce, including for a second revision of the
    same occurrence later in the same scan.
    """
    counts = dict.fromkeys(OUTCOMES, 0)
    details: list[tuple[str, str, str]] = []
    inbox = Path(inbox)
    if not inbox.is_dir():
        return MeetingImportResult()

    projection = _Projection(conn) if dry_run else None

    def record(identity: str, outcome: str, code: str, label: str) -> None:
        counts[outcome] += 1
        details.append((identity, outcome, code))
        # The label is the bundle's own directory spelling: no title, no
        # transcript, nothing but percent-encoded path components.
        log.info("meeting bundle %s: %s (%s)", label, outcome, code)

    for candidate in _candidates(inbox):
        if candidate.symlink:
            if not dry_run:
                _quarantine_symlink(candidate, quarantine)
            record(candidate.label, "quarantined", "path_escape", candidate.label)
            continue
        if candidate.revision_name is None:
            continue
        if not os.path.lexists(candidate.path / READY_FILENAME):
            # The producer has not committed this directory yet. Not an error.
            log.debug("meeting bundle %s is not READY; leaving it in place", candidate.label)
            continue

        try:
            bundle = load_meeting_bundle(candidate.path, inbox)
        except MeetingBundleError as exc:
            code = _bundle_reason(str(exc))
            if not dry_run:
                _quarantine_bundle(candidate, quarantine, code)
            record(candidate.label, "quarantined", code, candidate.label)
            continue

        if dry_run:
            outcome, code = projection.classify(bundle)
            record(bundle.occurrence_id, outcome, code, candidate.label)
            continue

        before = _current_revision(conn, bundle.occurrence_id)
        try:
            staged, created = _stage_audio(bundle, audio_store)
        except _Rejected as exc:
            _quarantine_bundle(candidate, quarantine, exc.code, bundle=bundle)
            record(bundle.occurrence_id, "quarantined", exc.code, candidate.label)
            continue

        try:
            stored = import_meeting_revision(
                conn,
                payload=bundle.payload,
                occurrence_id=bundle.occurrence_id,
                revision=bundle.revision,
                artifact_hash=bundle.artifact_hash,
                audio=staged,
                imported_at=now,
            )
        except MeetingStoreError as exc:
            _discard_staged(conn, created, bundle, audio_store)
            code = _store_reason(exc)
            _quarantine_bundle(candidate, quarantine, code, bundle=bundle)
            record(bundle.occurrence_id, "quarantined", code, candidate.label)
            continue
        except BaseException:
            # An environmental fault, not a bad bundle: undo our own staging and
            # leave the inbox exactly as it was so a rescan can retry it.
            _discard_staged(conn, created, bundle, audio_store)
            raise

        if stored.status == "unchanged":
            outcome = "unchanged"
        elif before is None:
            outcome = "accepted"
        else:
            outcome = "activated"
        try:
            _archive_bundle(bundle, archive)
        except OSError:
            # The revision is committed. Failing to file the source copy does
            # not undo that, so report the outcome the store actually produced
            # with a code saying the archive is incomplete, and carry on with
            # the rest of the scan. Both halves of archiving are idempotent —
            # the atomic writes overwrite, and a re-delivered bundle replays
            # through the store's `unchanged` path — so a rescan self-heals.
            record(bundle.occurrence_id, outcome, "archive_failed", candidate.label)
            continue
        record(bundle.occurrence_id, outcome, "ok", candidate.label)

    return MeetingImportResult(
        accepted=counts["accepted"],
        unchanged=counts["unchanged"],
        quarantined=counts["quarantined"],
        activated=counts["activated"],
        details=tuple(details),
    )
