"""Destroy encrypted meeting audio fourteen days after the meeting ended, and
warn an operator before an incomplete transcript loses its source.

This module keeps a promise made to the people in the room: the recording is
gone at fourteen days. The promise binds in both directions.

- Deleting late breaks it, so nothing here may wait on a network, a remote, a
  vault, or an operator acting on a warning. A warning is written after the
  deletions this run owes, never before, and a failure to write one cannot stop
  a deletion that is due.
- Deleting early destroys evidence that cannot be recovered. TeamMem never holds
  the recording key and never decrypts, so it can never regenerate those bytes.
  The boundary is therefore exact: `now >= audio_expires_at`, the same `<=`
  comparison `audio_due_for_expiry` already makes, and expiry is always
  `ended_at + 14 days` as TeamMem computed it. A producer asking for longer is
  provenance, never authority.

What is destroyed is the ciphertext file and nothing else. The meeting, its
transcript, its synthesis, its artifact history, and the audio's hash, format,
duration and expiry all remain queryable, now with a deletion timestamp beside
them: the record that a recording existed and was destroyed on time is itself
part of the promise.
"""

import errno
import json
import logging
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import _path_within, _physical_path
from .meeting_bundles import AUDIO_FILENAME, occurrence_path_segment, short_occurrence_id
# Private on purpose, imported on purpose: the audio store's layout, its digest
# spelling and where a prune must stop are each defined once, by the importer
# that wrote the files. Re-deriving any of them here is how two answers to one
# question drift apart.
from .meeting_importer import (
    _CUSTODY_PREFIX,
    _digest_file,
    _discard_windows_staging_residues,
    _prune_empty,
    _same_inode,
    _write_atomic,
    audio_revision_dir,
)
from .meeting_store import (
    MEETING_AUDIO_WARN_DAYS,
    audio_due_for_expiry,
    get_meeting,
    list_meetings,
    mark_audio_deleted,
)

log = logging.getLogger(__name__)

ALERT_SCHEMA = "teammem-meeting-audio-alert/v1"
ALERT_STATUS = "audio_expiring"


@dataclass(frozen=True)
class RetentionFailure:
    """One revision retention could not finish with. `stage` is `path`,
    `symlink`, `digest`, `unlink`, `mark`, `sweep` or `alert`; every one is
    retryable, and every one costs exactly the occurrence it names."""

    occurrence_id: str
    revision: int
    stage: str
    detail: str


@dataclass(frozen=True)
class RetentionResult:
    deleted: tuple[tuple[str, int], ...] = ()      # (occurrence_id, revision)
    swept: tuple[tuple[str, int], ...] = ()        # revived files removed again
    mismatched: tuple[tuple[str, int], ...] = ()   # deleted, but not the recorded bytes
    alerted: tuple[str, ...] = ()                  # occurrence ids warned
    cleared: tuple[str, ...] = ()                  # alerts whose warning has lapsed
    failed: tuple[RetentionFailure, ...] = ()
    dry_run: bool = False

    @property
    def exit_code(self) -> int:
        """An alert is a non-zero outcome: somebody has to act before the
        deadline, and nothing else in this pipeline will tell them."""
        return int(bool(self.failed or self.alerted or self.mismatched))


def _instant(value: str, field: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"{field} must carry a UTC offset")
    return moment.astimezone(timezone.utc)


def _stamp(moment: datetime) -> str:
    """The whole-second UTC spelling every retention column already uses."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def expire_meeting_audio(
    conn: sqlite3.Connection,
    audio_store: Path,
    *,
    now: str,
    alert_path: Callable[[str], Path],
    warn_days: int = MEETING_AUDIO_WARN_DAYS,
    dry_run: bool = False,
) -> RetentionResult:
    """Delete every expired recording, sweep revived ones, then warn.

    `alert_path` maps an occurrence id to its one alert file; the caller owns
    that spelling (`services.meeting_alert_path`) so it cannot diverge from the
    receipt outbox beside it.

    Deletions run first and alerts last, so nothing about warning can delay or
    prevent a deletion this run owes. `dry_run` reports exactly what a real run
    would do and writes nothing at all: no unlink, no row, no alert.
    """
    root_error = None
    try:
        # This is the same nearest-existing-ancestor normalization used by
        # configuration validation and the importer before it creates a store
        # child.  Retention must not compare a physical stored path with a
        # lexical configured spelling of that same root.
        store = _physical_path(audio_store)
    except (OSError, RuntimeError) as error:
        store = None
        root_error = error
    deleted: list[tuple[str, int]] = []
    mismatched: list[tuple[str, int]] = []
    failed: list[RetentionFailure] = []
    stamp = _stamp(_instant(now, "now"))

    for row in audio_due_for_expiry(conn, now):
        if store is None:
            failed.append(RetentionFailure(
                row["occurrence_id"], row["revision"], "containment", str(root_error)))
            continue
        try:
            _expire_one(conn, store, row, stamp=stamp, dry_run=dry_run,
                        deleted=deleted, mismatched=mismatched, failed=failed)
        except OSError as error:
            # The backstop for every filesystem probe inside, present and future:
            # `Path.is_symlink` re-raises EACCES, so one unsearchable directory
            # would otherwise abort the run and strand every other occurrence
            # due that day. One bad path costs exactly one meeting.
            failed.append(RetentionFailure(
                row["occurrence_id"], row["revision"], "path", str(error)))
    swept = [] if store is None else _sweep_revived(
        conn, store, dry_run=dry_run, failed=failed)
    alerted, cleared = _warn(conn, now=now, warn_days=warn_days,
                             alert_path=alert_path, dry_run=dry_run, failed=failed)
    return RetentionResult(
        deleted=tuple(deleted),
        swept=tuple(swept),
        mismatched=tuple(mismatched),
        alerted=tuple(alerted),
        cleared=tuple(cleared),
        failed=tuple(failed),
        dry_run=dry_run,
    )


def _expire_one(
    conn: sqlite3.Connection,
    store: Path,
    row: dict,
    *,
    stamp: str,
    dry_run: bool,
    deleted: list,
    mismatched: list,
    failed: list,
) -> None:
    """Unlink the ciphertext, then mark the row. Never the other way round.

    A crash between the two leaves the bytes gone and the row still live: the
    next run finds a missing file, marks it, and converges. The reverse order
    would leave a row asserting the bytes are gone while they sit on disk, which
    `audio_due_for_expiry` — filtering on `deleted_at IS NULL` — would never
    return again.
    """
    key = (row["occurrence_id"], row["revision"])
    # TeamMem wrote `stored_path` when it staged those bytes, so it is the
    # authoritative name of the file — not a guess, and not something to
    # re-derive. Deleting the path the *current* setting implies instead would
    # strand every recording on disk the day `TEAMMEM_MEETING_AUDIO_STORE`
    # changes, with only a daily failure line to say so.
    path = Path(row["stored_path"])
    expected_directory = audio_revision_dir(store, *key)

    # A symlink is a local tampering signal, not a configured-root alias. The
    # link itself is safe to remove; its target is never followed.
    if path.parent.is_symlink() or path.parent.parent.is_symlink():
        failed.append(RetentionFailure(
            *key, "symlink", "the stored audio directory is a symlink"))
        return
    try:
        actual_directory = _physical_path(path.parent)
    except (OSError, RuntimeError) as error:
        failed.append(RetentionFailure(*key, "containment", str(error)))
        return
    inside = (
        actual_directory == expected_directory
        and _path_within(expected_directory, store)
        and path.name == AUDIO_FILENAME
    )
    if path.name != AUDIO_FILENAME:
        failed.append(RetentionFailure(
            *key, "containment",
            "stored audio is not named audio.enc"))
        return

    # Reported, never a veto: retention deletes what sits at this occurrence's
    # path, and it is keyed on the occurrence, not on content. Refusing here
    # would leave audio alive past its deadline.
    if path.is_symlink():
        # The importer never stores one. Unlink removes the link and not its
        # target, so the bytes this row describes are somewhere else entirely.
        mismatched.append(key)
    else:
        try:
            digest, size = _digest_file(path)
        except FileNotFoundError:
            pass                                # already gone; the unlink converges
        except OSError as error:
            # A digest that cannot be read is not treated more leniently than one
            # that is read and disagrees: both are reported and both still lose
            # their bytes. Distinct stages, because "unreadable" and "disagreed"
            # ask different questions of an operator.
            failed.append(RetentionFailure(*key, "digest", str(error)))
        else:
            if digest != row["sha256"] or size != row["bytes"]:
                mismatched.append(key)

    if dry_run:
        deleted.append(key)
        return

    custody_descriptors = ()
    if inside:
        try:
            custody_descriptors = _open_posix_custody_descriptors(expected_directory)
        except OSError as error:
            failed.append(RetentionFailure(*key, "containment", str(error)))
            return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass                                    # already gone; still mark it
    except OSError as error:
        for descriptor in custody_descriptors:
            os.close(descriptor)
        failed.append(RetentionFailure(*key, "unlink", str(error)))
        return
    if inside:
        try:
            _discard_windows_staging_residues(expected_directory)
            _zero_opened_custody(custody_descriptors)
            _zero_posix_staging_residues(expected_directory)
            _assert_governed_audio_bytes_gone(expected_directory)
            _prune_empty(expected_directory, store)
        except OSError as error:
            failed.append(RetentionFailure(*key, "containment", str(error)))
            return
        finally:
            for descriptor in custody_descriptors:
                os.close(descriptor)
    try:
        mark_audio_deleted(conn, row["occurrence_id"], row["revision"], stamp)
    except sqlite3.Error as error:
        failed.append(RetentionFailure(*key, "mark", str(error)))
        return
    deleted.append(key)


def _assert_governed_audio_bytes_gone(directory: Path) -> None:
    """Refuse a retention mark while a final or crash-staging file has bytes."""
    prefix = f".{AUDIO_FILENAME}."
    try:
        entries = tuple(directory.iterdir())
    except FileNotFoundError:
        return
    for entry in entries:
        if entry.name != AUDIO_FILENAME and not entry.name.startswith(prefix):
            continue
        metadata = entry.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size:
            raise OSError(
                "governed audio entry remains after retention cleanup", entry)


def _open_posix_custody_descriptors(directory: Path) -> tuple[int, ...]:
    """Hold custody inodes that are still exactly the current final audio."""
    if os.name == "nt":
        return ()
    try:
        directory_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        return ()
    descriptors = []
    try:
        try:
            final_fd = os.open(
                AUDIO_FILENAME,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return ()
        except OSError as error:
            if error.errno == errno.ELOOP:
                return ()
            raise
        try:
            final_metadata = os.fstat(final_fd)
            current = os.stat(
                AUDIO_FILENAME, dir_fd=directory_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(final_metadata.st_mode)
                or not _same_inode(current, final_metadata)
            ):
                return ()
            for entry in sorted(directory.iterdir()):
                if not entry.name.startswith(_CUSTODY_PREFIX):
                    continue
                try:
                    descriptor = os.open(
                        entry.name,
                        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_fd,
                    )
                except FileNotFoundError:
                    continue
                try:
                    metadata = os.fstat(descriptor)
                    named = os.stat(
                        entry.name, dir_fd=directory_fd, follow_symlinks=False)
                    if (
                        stat.S_ISREG(metadata.st_mode)
                        and metadata.st_nlink == 2
                        and _same_inode(metadata, final_metadata)
                        and _same_inode(named, metadata)
                    ):
                        descriptors.append(descriptor)
                        descriptor = None
                except FileNotFoundError:
                    pass
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
        finally:
            os.close(final_fd)
        return tuple(descriptors)
    except BaseException:
        for descriptor in descriptors:
            os.close(descriptor)
        raise
    finally:
        os.close(directory_fd)


def _zero_opened_custody(descriptors: tuple[int, ...]) -> None:
    """Destroy only custody inodes already proven by held descriptors."""
    for descriptor in descriptors:
        os.ftruncate(descriptor, 0)
        os.fsync(descriptor)


def _zero_posix_staging_residues(directory: Path) -> None:
    """Destroy bytes through staging descriptors; leave every mutable name alone."""
    if os.name == "nt":
        return
    try:
        directory_fd = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        return
    try:
        for entry in tuple(directory.iterdir()):
            if (
                not entry.name.startswith(f".{AUDIO_FILENAME}.")
                or entry.name.startswith(_CUSTODY_PREFIX)
            ):
                continue
            try:
                descriptor = os.open(
                    entry.name,
                    os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                continue
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise OSError(
                        "governed audio staging entry is not a private file", entry)
                os.ftruncate(descriptor, 0)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.fsync(directory_fd)
    except FileNotFoundError:
        return
    finally:
        os.close(directory_fd)


def _sweep_revived(
    conn: sqlite3.Connection, store: Path, *, dry_run: bool, failed: list
) -> list[tuple[str, int]]:
    """Remove any ciphertext whose row already says it was destroyed.

    A revision whose audio has expired can be re-delivered to the inbox. The
    importer re-stages the file, the store answers `unchanged` and rolls back
    without clearing `deleted_at`, and the revived file is then invisible to
    `audio_due_for_expiry` forever. The importer is deliberately not changed to
    clear the flag: a replay must not undo a retention decision. The row already
    asserts the bytes are gone, so the file is what is wrong, and removing it
    makes the filesystem agree with the database.

    Only files a marked row claims are touched. A file no row knows about is
    left alone, because the importer publishes audio into its final name before
    the row is committed and a concurrent import would otherwise lose it.
    """
    if not store.is_dir():
        return []
    known = {
        occurrence_path_segment(meeting["occurrence_id"]): meeting["occurrence_id"]
        for meeting in list_meetings(conn)
    }
    swept: list[tuple[str, int]] = []

    def symlink_failure(occurrence_id: str, revision: int, detail: str) -> None:
        if any(
            failure.occurrence_id == occurrence_id
            and failure.stage == "symlink"
            and (revision == 0 or failure.revision == revision)
            for failure in failed
        ):
            return
        failed.append(RetentionFailure(
            occurrence_id, revision, "symlink", detail))

    for segment in sorted(store.iterdir()):
        occurrence_id = known.get(segment.name)
        if occurrence_id is None:
            continue
        # Every probe below can raise on an unreadable directory, and a janitor
        # must never abort the run: one unreachable directory costs that
        # directory. Revision 0 says the fault was above any single revision.
        try:
            if segment.is_symlink():
                symlink_failure(
                    occurrence_id, 0,
                    "the audio occurrence entry is a symlink")
                continue
            entries = [] if not segment.is_dir() else sorted(segment.iterdir())
        except OSError as error:
            failed.append(RetentionFailure(occurrence_id, 0, "sweep", str(error)))
            continue
        for revision_dir in entries:
            try:
                if revision_dir.is_symlink():
                    revision = (
                        int(revision_dir.name)
                        if revision_dir.name.isdigit() else 0)
                    symlink_failure(
                        occurrence_id, revision,
                        "the audio revision entry is a symlink")
                    continue
                found = _sweep_revision(
                    conn, store, occurrence_id, revision_dir, dry_run=dry_run)
            except OSError as error:
                name = revision_dir.name
                failed.append(RetentionFailure(
                    occurrence_id, int(name) if name.isdigit() else 0,
                    "sweep", str(error)))
                continue
            if found is not None:
                swept.append(found)
    return swept


def _sweep_revision(
    conn: sqlite3.Connection,
    store: Path,
    occurrence_id: str,
    revision_dir: Path,
    *,
    dry_run: bool,
) -> tuple[str, int] | None:
    """Remove one revision's ciphertext when its row already says it is gone."""
    if not revision_dir.name.isdigit():
        return None
    path = revision_dir / AUDIO_FILENAME
    revision = int(revision_dir.name)
    if path.is_symlink() or not path.is_file():
        return None
    meeting = get_meeting(conn, occurrence_id, revision=revision)
    audio = meeting["audio"] if meeting else None
    if audio is None or audio["deleted_at"] is None:
        return None
    if dry_run:
        return occurrence_id, revision
    os.unlink(path)
    _prune_empty(revision_dir, store)
    return occurrence_id, revision


def _warn(
    conn: sqlite3.Connection,
    *,
    now: str,
    warn_days: int,
    alert_path: Callable[[str], Path],
    dry_run: bool,
    failed: list,
) -> tuple[list[str], list[str]]:
    """Write one alert per occurrence whose current transcript is incomplete and
    whose recording is about to go, and clear the ones that have lapsed.

    TeamMem never re-transcribes. Backfill is Meeting Agent's, and it arrives as
    a higher revision; all this can do is say which occurrence and by when, in
    the same terms as Meeting Agent's own warning. Retention is not extended for
    a warned meeting, ever — that is the whole point of warning.

    An alert whose warning no longer applies — the transcript completed, or the
    audio is already gone — is removed. A spool where a live warning cannot be
    told from a dead one is worse than no spool: the operator's only signal is
    which files are in it. `warn_days` of 0 warns about nothing and still clears,
    so turning warnings off does not freeze yesterday's warnings on disk.
    """
    moment = _instant(now, "now")
    alerted: list[str] = []
    cleared: list[str] = []
    for meeting in list_meetings(conn):
        occurrence_id = meeting["occurrence_id"]
        audio = _at_risk(conn, meeting, moment, warn_days)
        if audio is None:
            _clear(alert_path(occurrence_id), occurrence_id, meeting,
                   dry_run=dry_run, cleared=cleared, failed=failed)
            continue
        if dry_run:
            alerted.append(occurrence_id)
            continue
        alert = {
            "schema": ALERT_SCHEMA,
            "status": ALERT_STATUS,
            "occurrence_id": occurrence_id,
            "note_id": short_occurrence_id(occurrence_id),
            "revision": meeting["current_revision"],
            "transcript_complete": False,
            "audio_expires_at": audio["expires_at"],
            "warned_at": _stamp(moment),
            "action": (
                "deliver a higher revision with a complete transcript before the "
                "deadline; the encrypted source audio is destroyed at that instant "
                "and TeamMem cannot re-transcribe it"
            ),
        }
        blob = (json.dumps(alert, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False) + "\n").encode("utf-8")
        try:
            _write_atomic(alert_path(occurrence_id), blob)
        except OSError as error:
            failed.append(RetentionFailure(
                occurrence_id, meeting["current_revision"], "alert", str(error)))
            continue
        log.warning("meeting audio expiring with an incomplete transcript: %s by %s",
                    occurrence_id, alert["audio_expires_at"])
        alerted.append(occurrence_id)
    return alerted, cleared


def _at_risk(
    conn: sqlite3.Connection, meeting: dict, moment: datetime, warn_days: int
) -> dict | None:
    """This occurrence's live audio row when its transcript is incomplete and the
    deadline is inside the window, otherwise None.

    The deadline comes from the stored `audio_expires_at` column — the exact
    value `audio_due_for_expiry` compares — rather than a second derivation from
    `ended_at`, so a warning and a deletion cannot disagree about when it is.
    """
    if meeting["transcript_complete"]:
        return None
    stored = get_meeting(conn, meeting["occurrence_id"])
    audio = stored["audio"] if stored else None
    if audio is None or audio["deleted_at"] is not None:
        return None                             # nothing left to lose
    deadline = _instant(audio["expires_at"], "audio_expires_at")
    if not deadline - timedelta(days=warn_days) <= moment < deadline:
        return None
    return audio


def _clear(
    path: Path, occurrence_id: str, meeting: dict, *,
    dry_run: bool, cleared: list, failed: list,
) -> None:
    """Remove a lapsed alert. `os.unlink` is itself the atomic step: the file is
    either there in full or not there at all, never half-retracted."""
    try:
        if not path.exists():
            return
        if dry_run:
            cleared.append(occurrence_id)
            return
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as error:
        failed.append(RetentionFailure(
            occurrence_id, meeting["current_revision"], "alert", str(error)))
        return
    cleared.append(occurrence_id)
