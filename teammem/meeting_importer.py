"""Import meeting bundles from an inbox and adopt their encrypted audio.

The importer is the only thing that turns a producer's directory into stored
state, and it does so in one fixed order per bundle:

1. **Validate everything first.** `teammem.meeting_bundles.load_meeting_bundle`
   checks the payload, the manifest and the audio digest before this module
   touches anything. Nothing is mutated until that returns.
2. **Publish the audio, verified.** On POSIX the encrypted bytes are copied into
   a staging file, fsynced, verified, and atomically renamed. Windows creates
   the final path exclusively so the exact created descriptor can remain open
   through verification and commit. In both cases the database sees only a
   complete verified file; failure can leave only unreferenced zero-byte
   residue from the inode this invocation created.
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
or an unexpected crash in the store destroys bytes through the held created
descriptor, leaves the bundle in the inbox, and re-raises so the run stops
instead of quarantining a bundle that was never wrong. Windows marks the exact
held inode for deletion through its native handle; POSIX may retain a zero-byte
name because cleanup never trusts a mutable pathname.

TeamMem never holds the recording key and never decrypts audio; it only ever
copies and hashes opaque bytes.
"""

import errno
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from .config import _path_within, _physical_path
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
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | _NOFOLLOW
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = _NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

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


def _open_audio_directory(
    store: Path, occurrence_id: str, revision: int
) -> tuple[Path, int | None, int | None]:
    """Create/open the two managed descendants without following either one."""
    directory = audio_revision_dir(store, occurrence_id, revision)
    if os.name == "nt":
        # Python exposes no descriptor-relative mkdir on Windows. Check every
        # managed component, including junction/reparse metadata, before use;
        # `_verified_audio_path` still proves the final physical destination.
        for path in (store, directory.parent, directory):
            try:
                path.mkdir(parents=path == store)
            except FileExistsError:
                pass
            metadata = os.lstat(path)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_reparse_tag", 0)
                or not stat.S_ISDIR(metadata.st_mode)
            ):
                raise _Rejected("path_escape")
        return directory, None, None

    try:
        store.mkdir(parents=True, exist_ok=True)
        store_fd = os.open(store, _DIRECTORY_FLAGS)
    except OSError as error:
        if error.errno in {errno.EEXIST, errno.ELOOP, errno.ENOTDIR}:
            raise _Rejected("path_escape") from error
        raise
    descriptors = []
    parent_fd = store_fd
    try:
        for name in (directory.parent.name, directory.name):
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            try:
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise _Rejected("path_escape") from error
                raise
            descriptors.append(child_fd)
            parent_fd = child_fd
        return directory, descriptors[0], descriptors[1]
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    finally:
        os.close(store_fd)


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


def _zero_owned_descriptor(descriptor: int) -> None:
    """Destroy bytes through an exclusive-created descriptor, never its name."""
    os.ftruncate(descriptor, 0)
    os.fsync(descriptor)


def _windows_file_libraries():
    """Load the Windows APIs lazily so importing TeamMem stays portable."""
    import ctypes
    import msvcrt

    return ctypes, ctypes.WinDLL("kernel32", use_last_error=True), msvcrt


def _open_windows_file(path: Path, disposition: int) -> int:
    """Open a renameable file and transfer its native handle to one fd."""
    ctypes, kernel32, msvcrt = _windows_file_libraries()
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.CreateFileW(
        str(path),
        0x80000000 | 0x40000000 | 0x00010000,  # GENERIC_READ | WRITE | DELETE
        0x00000001 | 0x00000002 | 0x00000004,  # share READ | WRITE | DELETE
        None, disposition,
        0x00000080 | 0x00200000, None,          # NORMAL | OPEN_REPARSE_POINT
    )
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        mapped = (
            errno.EEXIST if error in {80, 183}
            else errno.ENOENT if error in {2, 3}
            else error
        )
        raise OSError(mapped, "CreateFileW failed", str(path))
    try:
        return msvcrt.open_osfhandle(
            handle, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _open_windows_exclusive(path: Path) -> int:
    return _open_windows_file(path, 1)           # CREATE_NEW (O_EXCL)


def _open_windows_existing(path: Path) -> int:
    return _open_windows_file(path, 3)           # OPEN_EXISTING


def _delete_windows_owned_descriptor(descriptor: int) -> None:
    """Mark the fd's exact Windows file object for deletion when it closes."""
    ctypes, kernel32, msvcrt = _windows_file_libraries()
    kernel32.SetFileInformationByHandle.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
    ]
    kernel32.SetFileInformationByHandle.restype = ctypes.c_int
    info = ctypes.c_ubyte(1)
    handle = msvcrt.get_osfhandle(descriptor)     # borrowed: the fd remains sole owner
    if not kernel32.SetFileInformationByHandle(
        handle, 4, ctypes.cast(ctypes.byref(info), ctypes.c_void_p),
        ctypes.sizeof(info),                     # FileDispositionInfo
    ):
        raise OSError(ctypes.get_last_error(), "SetFileInformationByHandle failed")


def _discard_owned_descriptor(descriptor: int) -> None:
    _zero_owned_descriptor(descriptor)
    if os.name == "nt":
        _delete_windows_owned_descriptor(descriptor)


def _discard_windows_staging_residues(directory: Path) -> None:
    """Destroy only regular, single-link staging inodes through opened handles."""
    if os.name != "nt":
        return
    prefix = f".{AUDIO_FILENAME}."
    try:
        paths = sorted(directory.iterdir())
    except FileNotFoundError:
        return
    for path in paths:
        if not path.name.startswith(prefix):
            continue
        if path.is_symlink():
            raise OSError(errno.ELOOP, "meeting audio staging entry is a link", path)
        try:
            descriptor = _open_windows_existing(path)
        except FileNotFoundError:
            continue
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or getattr(metadata, "st_reparse_tag", 0)
                or metadata.st_nlink != 1
            ):
                raise OSError(errno.EPERM, "meeting audio staging entry is not owned", path)
            _discard_owned_descriptor(descriptor)
        finally:
            os.close(descriptor)


def _copy_file(source: Path, destination: Path, *, directory_fd: int | None = None) -> int:
    """Copy opaque bytes into a new file and return its open, synced descriptor.

    The source is opened `O_NOFOLLOW`. `load_meeting_bundle` already refused a
    symlinked `audio.enc`, but that check ran in an earlier pass over the same
    directory: without this flag, swapping the file for a symlink in between
    would make this copy read through it. `O_NOFOLLOW` closes that window on
    every platform that defines it, so the refusal happens at the moment of the
    copy rather than resting on a stale check.
    """
    source_fd = os.open(source, os.O_RDONLY | _NOFOLLOW)
    try:
        if os.name == "nt":
            if directory_fd is not None:
                raise RuntimeError("Windows staging does not accept a directory fd")
            fd = _open_windows_exclusive(destination)
        else:
            target = destination.name if directory_fd is not None else destination
            fd = os.open(
                target,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
                0o600,
                dir_fd=directory_fd,
            )
        try:
            with os.fdopen(source_fd, "rb", closefd=False) as handle:
                while chunk := handle.read(_COPY_CHUNK):
                    _write_all(fd, chunk)
            os.fsync(fd)
        except BaseException:
            try:
                _discard_owned_descriptor(fd)
            finally:
                os.close(fd)
            raise
    finally:
        os.close(source_fd)
    return fd


def _digest_descriptor(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, _COPY_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    return "sha256:" + digest.hexdigest(), size


def _digest_file(path: Path) -> tuple[str, int]:
    """`(sha256:<hex>, size)` read back from disk, not from what was written."""
    descriptor = os.open(path, os.O_RDONLY | _FILE_FLAGS)
    try:
        return _digest_descriptor(descriptor)
    finally:
        os.close(descriptor)


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


def _verified_audio_path(
    descriptor: int,
    final: Path,
    store: Path,
    vault: Path,
) -> Path:
    """Prove the opened ciphertext is the safe physical path being recorded."""
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise _Rejected("audio_conflict")
    try:
        physical = _physical_path(final)
        path_metadata = os.stat(physical, follow_symlinks=False)
    except (OSError, RuntimeError) as error:
        raise _Rejected("path_escape") from error
    if (
        not _path_within(physical, store)
        or _path_within(physical, vault)
        or (metadata.st_dev, metadata.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
    ):
        raise _Rejected("path_escape")
    return physical


@dataclass
class _AdoptedAudio:
    """Open ownership of one verified ciphertext through its ledger commit."""

    staged: StagedAudio
    final: Path
    configured_store: Path
    configured_vault: Path
    created: bool
    final_fd: int | None
    occurrence_fd: int | None
    revision_fd: int | None

    def before_commit(self) -> None:
        if self.final_fd is None:
            raise _Rejected("path_escape")
        try:
            store = _physical_path(self.configured_store)
            vault = _physical_path(self.configured_vault)
        except (OSError, RuntimeError) as error:
            raise _Rejected("path_escape") from error
        if _path_within(store, vault) or _path_within(vault, store):
            raise _Rejected("path_escape")
        _verified_audio_path(self.final_fd, self.final, store, vault)

    def discard_created(self) -> None:
        if not self.created:
            return
        _discard_owned_descriptor(self.final_fd)

    def close(self) -> None:
        for field in ("final_fd", "revision_fd", "occurrence_fd"):
            descriptor = getattr(self, field)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, field, None)


def _stage_audio(
    bundle: MeetingBundle,
    audio_store: Path,
    vault_dir: Path,
) -> _AdoptedAudio:
    """Publish the encrypted audio and describe it.

    The returned owner keeps the adopted file and managed directories open
    until the store transaction commits. On failure, only the bytes reached
    through that exclusive-created descriptor may be destroyed; audio already
    present belongs to a stored revision and mutable names are never removed.
    """
    try:
        store = _physical_path(audio_store)
        vault = _physical_path(vault_dir)
    except (OSError, RuntimeError) as error:
        raise _Rejected("path_escape") from error
    if _path_within(store, vault) or _path_within(vault, store):
        raise _Rejected("path_escape")

    directory, occurrence_fd, revision_fd = _open_audio_directory(
        store, bundle.occurrence_id, bundle.revision)
    final = directory / AUDIO_FILENAME
    declared_sha = bundle.payload["audio"]["sha256"]
    declared_bytes = bundle.payload["audio"]["bytes"]
    staging = directory / f".{AUDIO_FILENAME}.{secrets.token_hex(8)}"
    final_name = final.name if revision_fd is not None else final
    created = False
    owns_fd = False
    final_fd = None
    try:
        if final.is_symlink():
            raise _Rejected("audio_conflict")
        try:
            final_fd = os.open(
                final_name, os.O_RDONLY | _FILE_FLAGS, dir_fd=revision_fd)
        except FileNotFoundError:
            final_fd = None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise _Rejected("audio_conflict") from error
            raise
        if final_fd is not None:
            digest, size = _digest_descriptor(final_fd)
            physical = _verified_audio_path(final_fd, final, store, vault)
            if digest != declared_sha or size != declared_bytes:
                raise _Rejected("audio_conflict")
        else:
            final_fd = _copy_file(
                bundle.audio_path, staging, directory_fd=revision_fd)
            owns_fd = True
            if revision_fd is None:
                _fsync_dir(directory)
            else:
                os.fsync(revision_fd)
            digest, size = _digest_descriptor(final_fd)
            if digest != declared_sha or size != declared_bytes:
                raise _Rejected("audio_hash_mismatch")
            if revision_fd is None:
                os.replace(staging, final)
            else:
                os.replace(
                    staging.name,
                    final.name,
                    src_dir_fd=revision_fd,
                    dst_dir_fd=revision_fd,
                )
            created = True
            if revision_fd is None:
                _fsync_dir(directory)
            else:
                os.fsync(revision_fd)
            physical = _verified_audio_path(final_fd, final, store, vault)
        adopted = _AdoptedAudio(
            staged=StagedAudio(
                stored_path=str(physical), sha256=digest, bytes=size),
            final=physical,
            configured_store=Path(audio_store),
            configured_vault=Path(vault_dir),
            created=created,
            final_fd=final_fd,
            occurrence_fd=occurrence_fd,
            revision_fd=revision_fd,
        )
        final_fd = occurrence_fd = revision_fd = None
        return adopted
    except BaseException:
        if owns_fd and final_fd is not None:
            _discard_owned_descriptor(final_fd)
        raise
    finally:
        for descriptor in (final_fd, revision_fd, occurrence_fd):
            if descriptor is not None:
                os.close(descriptor)


def _discard_staged(
    conn: sqlite3.Connection,
    adopted: _AdoptedAudio,
    bundle: MeetingBundle,
    *,
    guard_refused: bool = False,
) -> None:
    """Destroy created audio bytes unless a database row now references them."""
    if not adopted.created:
        return
    if not guard_refused:
        for row in list_meeting_revisions(conn, bundle.occurrence_id):
            if row["revision"] == bundle.revision:
                return                                      # the revision landed after all
    adopted.discard_created()


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
    vault_dir: Path,
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
            adopted = _stage_audio(bundle, audio_store, vault_dir)
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
                audio=adopted.staged,
                imported_at=now,
                before_commit=adopted.before_commit,
            )
        except _Rejected as exc:
            _discard_staged(
                conn, adopted, bundle, guard_refused=True)
            _quarantine_bundle(candidate, quarantine, exc.code, bundle=bundle)
            record(bundle.occurrence_id, "quarantined", exc.code, candidate.label)
            continue
        except MeetingStoreError as exc:
            _discard_staged(conn, adopted, bundle)
            code = _store_reason(exc)
            _quarantine_bundle(candidate, quarantine, code, bundle=bundle)
            record(bundle.occurrence_id, "quarantined", code, candidate.label)
            continue
        except BaseException:
            # An environmental fault, not a bad bundle: undo our own staging and
            # leave the inbox exactly as it was so a rescan can retry it.
            _discard_staged(conn, adopted, bundle)
            raise
        finally:
            adopted.close()

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
