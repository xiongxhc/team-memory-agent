"""Meeting inbox importer: validate, adopt encrypted audio, route the outcome.

Every bundle here is materialized by the Task 1 fixture helper, so what the
importer sees is exactly the frozen `teammem-meeting/v1` wire format. The
fault-injection tests drive the importer's own copy loop and store call rather
than simulating them, so what is asserted is the real cleanup path.
"""

import errno
import hashlib
import json
import logging
import multiprocessing
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from teammem.meeting_bundles import occurrence_path_segment
from teammem.meeting_importer import (
    REASON_CODES,
    MeetingImportResult,
    audio_revision_dir,
    import_meeting_inbox,
)
from teammem.meeting_retention import expire_meeting_audio
from teammem.meeting_store import get_meeting, list_meeting_revisions, list_meetings
from teammem.store import open_db
from tests.test_meeting_bundles import write_meeting_bundle


FIXTURES = Path(__file__).parent / "fixtures" / "meetings"
COMPLETE = FIXTURES / "teammem-meeting-v1.complete.json"
MINIMAL = FIXTURES / "teammem-meeting-v1.minimal.json"

NOW = "2026-09-02T16:00:00Z"

# A sentence that exists only inside the complete fixture's transcript. Used to
# prove nothing the importer emits carries meeting content.
TRANSCRIPT_SENTENCE = (
    "The staging soak has been green for six days, so I am comfortable with the current build."
)


def _payload(
    path: Path = COMPLETE, *, day: str | None = None, revision: int | None = None
) -> dict:
    """Load a fixture, optionally shifting its day and/or setting its revision.

    Every in-window instant in a fixture carries 2026-09-02, so substituting the
    date yields another internally consistent occurrence.
    """
    text = path.read_text(encoding="utf-8")
    if day is not None:
        text = text.replace("2026-09-02", day)
    data = json.loads(text)
    if revision is not None:
        data["revision"] = revision
    return data


class _Tree:
    """The four directories `import_meeting_inbox` works over."""

    def __init__(self, tmp_path: Path):
        self.inbox = tmp_path / "meeting-inbox"
        self.archive = tmp_path / "meeting-archive"
        self.quarantine = tmp_path / "meeting-quarantine"
        self.audio = tmp_path / "audio-store"
        self.vault = tmp_path / "vault"
        self.inbox.mkdir()

    def paths(self) -> tuple[Path, Path, Path, Path]:
        return (self.inbox, self.archive, self.quarantine, self.audio)


def _run(conn, tree: _Tree, *, dry_run: bool = False, now: str = NOW) -> MeetingImportResult:
    return import_meeting_inbox(
        conn, *tree.paths(), vault_dir=tree.vault, now=now, dry_run=dry_run)


def _db(tmp_path: Path):
    return open_db(tmp_path / "ledger.db")


def _files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def _audio_payload_files(root: Path) -> list[Path]:
    """Audio names that carry a distinct payload rather than a custody link."""
    return [path for path in _files(root) if not path.name.startswith(".audio.enc.custody.")]


def _entries(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*"))


def _outcomes(result: MeetingImportResult) -> list[tuple[str, str]]:
    return [(outcome, reason) for _, outcome, reason in result.details]


def _audio_file(tree: _Tree, payload: dict, revision: int | None = None) -> Path:
    revision = payload["revision"] if revision is None else revision
    return audio_revision_dir(tree.audio, payload["occurrence_id"], revision) / "audio.enc"


# --- the happy path ---------------------------------------------------------


def test_valid_bundle_is_imported_archived_and_consumed(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    directory = write_meeting_bundle(tree.inbox, payload)

    result = _run(conn, tree)

    counts = (result.accepted, result.unchanged, result.quarantined, result.activated)
    assert counts == (1, 0, 0, 0)
    assert result.details == ((payload["occurrence_id"], "accepted", "ok"),)

    stored = get_meeting(conn, payload["occurrence_id"])
    assert stored is not None and stored["revision"] == payload["revision"]
    assert stored["title"] == payload["title"]
    assert len(stored["transcript"]) == len(payload["transcript"])
    assert stored["audio"]["stored_path"] == str(_audio_file(tree, payload))
    assert stored["audio"]["sha256"] == payload["audio"]["sha256"]

    segment = occurrence_path_segment(payload["occurrence_id"])
    archived = tree.archive / segment / str(payload["revision"])
    assert json.loads((archived / "meeting.json").read_text(encoding="utf-8")) == payload
    manifest = json.loads((archived / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifact_hash"] == stored["artifact_hash"]     # identifiable by occ/rev/hash
    assert manifest["occurrence_id"] == payload["occurrence_id"]
    assert not (archived / "audio.enc").exists()                    # audio lives only in the store

    assert not directory.exists()                                   # the inbox bundle was consumed
    assert _entries(tree.quarantine) == []


def test_staged_audio_is_byte_identical_and_keeps_only_its_custody_link(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    directory = write_meeting_bundle(tree.inbox, payload)
    source = (directory / "audio.enc").read_bytes()

    _run(conn, tree)

    final = _audio_file(tree, payload)
    assert final.read_bytes() == source
    assert hashlib.sha256(source).hexdigest() == payload["audio"]["sha256"].split(":", 1)[1]
    assert _audio_payload_files(tree.audio) == [final]
    [custody] = [
        path for path in _files(tree.audio)
        if path.name.startswith(".audio.enc.custody.")
    ]
    assert custody.samefile(final)


def test_two_occurrences_in_one_scan_are_both_imported(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    first = _payload(COMPLETE)
    second = _payload(MINIMAL, day="2026-09-03")
    write_meeting_bundle(tree.inbox, first)
    write_meeting_bundle(tree.inbox, second)

    result = _run(conn, tree)

    assert result.accepted == 2 and result.quarantined == 0
    assert {row["occurrence_id"] for row in list_meetings(conn)} == {
        first["occurrence_id"],
        second["occurrence_id"],
    }


def test_empty_and_missing_inbox_do_nothing(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)

    assert _run(conn, tree) == MeetingImportResult()
    assert list_meetings(conn) == []

    shutil.rmtree(tree.inbox)
    assert _run(conn, tree) == MeetingImportResult()
    assert not tree.archive.exists() and not tree.audio.exists()
    assert not tree.quarantine.exists()


# --- dry run ----------------------------------------------------------------


def test_dry_run_reports_the_import_and_writes_nothing(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    directory = write_meeting_bundle(tree.inbox, payload)
    before = sorted(p.name for p in directory.iterdir())

    result = _run(conn, tree, dry_run=True)

    assert result.accepted == 1 and result.details[0][1] == "accepted"
    assert get_meeting(conn, payload["occurrence_id"]) is None       # no DB row
    assert not tree.audio.exists()                                   # no staged or final audio
    assert not tree.archive.exists()                                 # no archive move
    assert not tree.quarantine.exists()                              # no quarantine move
    assert sorted(p.name for p in directory.iterdir()) == before     # inbox untouched

    assert _run(conn, tree).accepted == 1                            # the real run still works
    assert get_meeting(conn, payload["occurrence_id"]) is not None


def test_dry_run_classifies_a_second_revision_in_the_same_scan_as_an_activation(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    first = _payload(revision=1)
    second = _payload(revision=2)
    write_meeting_bundle(tree.inbox, first)
    write_meeting_bundle(tree.inbox, second)

    result = _run(conn, tree, dry_run=True)

    assert _outcomes(result) == [("accepted", "ok"), ("activated", "ok")]
    assert (result.accepted, result.activated) == (1, 1)
    assert get_meeting(conn, first["occurrence_id"]) is None


def test_dry_run_reports_a_quarantine_without_moving_it(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    directory = write_meeting_bundle(tree.inbox, _payload())
    (directory / "meeting.json").write_text("{ not json", encoding="utf-8")

    result = _run(conn, tree, dry_run=True)

    assert result.quarantined == 1 and _outcomes(result) == [("quarantined", "malformed_json")]
    assert (directory / "meeting.json").is_file() and not tree.quarantine.exists()


# --- revisions --------------------------------------------------------------


def test_duplicate_rescan_is_unchanged_and_writes_no_second_revision(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    write_meeting_bundle(tree.inbox, payload)
    assert _run(conn, tree).accepted == 1

    write_meeting_bundle(tree.inbox, payload)          # the producer re-delivers it
    replay = _run(conn, tree)

    assert (replay.accepted, replay.unchanged, replay.quarantined) == (0, 1, 0)
    assert replay.details == ((payload["occurrence_id"], "unchanged", "ok"),)
    assert len(list_meeting_revisions(conn, payload["occurrence_id"])) == 1
    assert _audio_payload_files(tree.audio) == [_audio_file(tree, payload)]
    assert not (tree.inbox / occurrence_path_segment(payload["occurrence_id"])).exists()


def test_higher_revision_activates_and_keeps_the_prior_revision_as_provenance(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    first = _payload(revision=1)
    write_meeting_bundle(tree.inbox, first)
    _run(conn, tree)

    second = _payload(revision=2)
    second["summary"] = "Revised after the recording was reprocessed."
    write_meeting_bundle(tree.inbox, second)
    result = _run(conn, tree)

    assert (result.accepted, result.activated, result.quarantined) == (0, 1, 0)
    assert result.details == ((second["occurrence_id"], "activated", "ok"),)

    current = get_meeting(conn, first["occurrence_id"])
    assert current["revision"] == 2 and current["summary"] == second["summary"]
    prior = get_meeting(conn, first["occurrence_id"], revision=1)
    assert prior is not None and prior["is_current"] is False       # provenance survives
    assert [r["revision"] for r in list_meeting_revisions(conn, first["occurrence_id"])] == [2, 1]

    for revision in (1, 2):                                         # each revision keeps its audio
        assert _audio_file(tree, first, revision).is_file()
        assert (tree.archive / occurrence_path_segment(first["occurrence_id"])
                / str(revision) / "meeting.json").is_file()


def test_lower_and_higher_revision_in_one_scan_are_applied_in_revision_order(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    lower = _payload(revision=2)
    higher = _payload(revision=10)                     # "10" sorts before "2" as a string
    write_meeting_bundle(tree.inbox, lower)
    write_meeting_bundle(tree.inbox, higher)

    result = _run(conn, tree)

    assert _outcomes(result) == [("accepted", "ok"), ("activated", "ok")]
    assert get_meeting(conn, lower["occurrence_id"])["revision"] == 10
    assert [r["revision"] for r in list_meeting_revisions(conn, lower["occurrence_id"])] == [10, 2]


def test_same_revision_with_different_content_is_quarantined(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    original = _payload(revision=1)
    write_meeting_bundle(tree.inbox, original)
    _run(conn, tree)

    doctored = _payload(revision=1)
    doctored["summary"] = "A different summary at the same revision number."
    directory = write_meeting_bundle(tree.inbox, doctored)
    result = _run(conn, tree)

    assert (result.quarantined, result.accepted) == (1, 0)
    assert _outcomes(result) == [("quarantined", "revision_conflict")]
    assert get_meeting(conn, original["occurrence_id"])["summary"] == original["summary"]
    assert not directory.exists()

    segment = occurrence_path_segment(original["occurrence_id"])
    quarantined = tree.quarantine / segment / "1"
    assert (quarantined / "meeting.json").is_file()
    reason = json.loads((tree.quarantine / segment / "1.reason.json").read_text(encoding="utf-8"))
    assert reason["reason_code"] == "revision_conflict"
    assert reason["occurrence_id"] == original["occurrence_id"] and reason["revision"] == 1


def test_lower_revision_after_a_higher_one_is_quarantined(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    higher = _payload(revision=5)
    write_meeting_bundle(tree.inbox, higher)
    _run(conn, tree)

    lower = _payload(revision=4)
    write_meeting_bundle(tree.inbox, lower)
    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "revision_superseded")]
    assert get_meeting(conn, higher["occurrence_id"])["revision"] == 5
    assert [r["revision"] for r in list_meeting_revisions(conn, higher["occurrence_id"])] == [5]
    assert (tree.quarantine / occurrence_path_segment(higher["occurrence_id"]) / "4").is_dir()
    assert _audio_file(tree, lower).stat().st_size == 0   # only safe residue remains


# --- invalid bundles --------------------------------------------------------


def test_malformed_meeting_json_is_quarantined(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    directory = write_meeting_bundle(tree.inbox, payload)
    (directory / "meeting.json").write_text('{"schema": "teammem-meeting/v1",', encoding="utf-8")

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "malformed_json")]
    # the occurrence id is unreadable, so the bundle directory identifies the offender
    assert result.details[0][0] == f"{directory.parent.name}/{directory.name}"
    assert list_meetings(conn) == []
    assert not directory.exists()
    assert (tree.quarantine / directory.parent.name / directory.name / "manifest.json").is_file()


def test_incomplete_bundle_is_quarantined(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    directory = write_meeting_bundle(tree.inbox, payload, omit=("manifest.json",))

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "incomplete_bundle")]
    assert list_meetings(conn) == []
    assert (tree.quarantine / directory.parent.name / directory.name / "meeting.json").is_file()


def test_invalid_payload_field_is_quarantined_as_bundle_invalid(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    payload["transcript_complete"] = "yes"                          # must be a boolean
    write_meeting_bundle(tree.inbox, payload)

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "bundle_invalid")]
    assert list_meetings(conn) == []
    assert not tree.audio.exists()                                  # rejected before any staging


def test_manifest_artifact_hash_mismatch_is_quarantined(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    write_meeting_bundle(
        tree.inbox, payload, manifest_overrides={"artifact_hash": "sha256:" + "0" * 64}
    )

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "hash_mismatch")]
    assert list_meetings(conn) == []


def test_missing_ready_is_skipped_silently_and_left_in_place(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    directory = write_meeting_bundle(tree.inbox, payload, omit=("READY",))

    result = _run(conn, tree)

    assert result == MeetingImportResult()                          # not even a detail row
    assert directory.is_dir() and (directory / "meeting.json").is_file()
    assert not tree.quarantine.exists() and not tree.audio.exists()

    (directory / "READY").write_bytes(b"")                          # the producer commits it
    assert _run(conn, tree).accepted == 1
    assert get_meeting(conn, payload["occurrence_id"]) is not None


def test_corrupted_audio_is_quarantined(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    directory = write_meeting_bundle(tree.inbox, payload)
    mutated = bytearray((directory / "audio.enc").read_bytes())
    mutated[0] ^= 0xFF
    mutated.extend(b"\x00" * 16)                                    # hash and size both disagree
    (directory / "audio.enc").write_bytes(bytes(mutated))

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "audio_hash_mismatch")]
    assert list_meetings(conn) == []
    assert not _audio_file(tree, payload).exists()
    assert (tree.quarantine / directory.parent.name / directory.name / "audio.enc").is_file()


# --- symlinks and path escape ----------------------------------------------


def test_symlinked_revision_directory_is_quarantined_without_dereferencing(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    outside = tmp_path / "outside"
    outside.mkdir()
    real = write_meeting_bundle(outside, payload)                  # a real bundle, but outside
    segment = occurrence_path_segment(payload["occurrence_id"])
    (tree.inbox / segment).mkdir()
    link = tree.inbox / segment / str(payload["revision"])
    link.symlink_to(real, target_is_directory=True)

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "path_escape")]
    assert list_meetings(conn) == []
    assert not link.is_symlink() and not link.exists()              # the link itself was removed
    assert (real / "meeting.json").is_file()                        # the target was never consumed
    reason = json.loads(
        (tree.quarantine / segment / f"{payload['revision']}.reason.json").read_text(
            encoding="utf-8"
        )
    )
    assert reason["reason_code"] == "path_escape"
    assert reason["rejected_symlink_target"] == str(real)


def test_symlinked_occurrence_directory_is_quarantined_without_descending(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    outside = tmp_path / "outside"
    outside.mkdir()
    real = write_meeting_bundle(outside, payload)
    link = tree.inbox / occurrence_path_segment(payload["occurrence_id"])
    link.symlink_to(real.parent, target_is_directory=True)

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "path_escape")]
    assert list_meetings(conn) == []
    assert not link.is_symlink()
    assert (real / "meeting.json").is_file()
    assert not tree.audio.exists()


def test_symlinked_audio_pointing_outside_the_inbox_is_quarantined(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    directory = write_meeting_bundle(tree.inbox, payload)
    elsewhere = tmp_path / "elsewhere.bin"
    elsewhere.write_bytes((directory / "audio.enc").read_bytes())
    (directory / "audio.enc").unlink()
    (directory / "audio.enc").symlink_to(elsewhere)

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "path_escape")]
    assert list_meetings(conn) == []
    assert elsewhere.is_file()                                      # never followed, never moved
    assert not tree.audio.exists()


def test_a_symlinked_audio_occurrence_directory_is_not_followed(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    write_meeting_bundle(tree.inbox, payload)
    outside = tmp_path / "outside-occurrence"
    outside.mkdir()
    tree.audio.mkdir()
    occurrence = tree.audio / occurrence_path_segment(payload["occurrence_id"])
    occurrence.symlink_to(outside, target_is_directory=True)

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "path_escape")]
    assert list_meetings(conn) == []
    assert list(outside.iterdir()) == []
    assert occurrence.is_symlink()


def test_a_symlinked_audio_revision_directory_is_not_followed(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    write_meeting_bundle(tree.inbox, payload)
    outside = tmp_path / "outside-revision"
    outside.mkdir()
    occurrence = tree.audio / occurrence_path_segment(payload["occurrence_id"])
    occurrence.mkdir(parents=True)
    revision = occurrence / str(payload["revision"])
    revision.symlink_to(outside, target_is_directory=True)

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "path_escape")]
    assert list_meetings(conn) == []
    assert list(outside.iterdir()) == []
    assert revision.is_symlink()


def test_audio_moved_into_the_vault_before_commit_is_destroyed_and_not_recorded(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    write_meeting_bundle(tree.inbox, payload)
    tree.vault.mkdir()
    revision = audio_revision_dir(
        tree.audio, payload["occurrence_id"], payload["revision"])
    captured = tree.vault / "captured-revision"

    from teammem import meeting_importer

    real_replace = os.replace
    moved = False

    def move_revision_after_adoption(source, destination, *args, **kwargs):
        nonlocal moved
        result = real_replace(source, destination, *args, **kwargs)
        if Path(destination).name == "audio.enc" and not moved:
            moved = True
            revision.rename(captured)
            revision.symlink_to(captured, target_is_directory=True)
        return result

    monkeypatch.setattr(meeting_importer.os, "replace", move_revision_after_adoption)

    result = _run(conn, tree)

    assert moved is True
    assert _outcomes(result) == [("quarantined", "path_escape")]
    assert list_meetings(conn) == []
    assert (captured / "audio.enc").stat().st_size == 0


def test_audio_moved_at_the_store_boundary_is_destroyed_before_commit(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    write_meeting_bundle(tree.inbox, payload)
    tree.vault.mkdir()
    revision = audio_revision_dir(
        tree.audio, payload["occurrence_id"], payload["revision"])
    captured = tree.vault / "captured-revision"

    from teammem import meeting_importer

    real_import = meeting_importer.import_meeting_revision
    moved = False

    def move_revision_after_adoption(*args, **kwargs):
        nonlocal moved
        revision.rename(captured)
        revision.symlink_to(captured, target_is_directory=True)
        moved = True
        return real_import(*args, **kwargs)

    monkeypatch.setattr(
        meeting_importer, "import_meeting_revision", move_revision_after_adoption)

    result = _run(conn, tree)

    assert moved is True
    assert _outcomes(result) == [("quarantined", "path_escape")]
    assert list_meetings(conn) == []
    assert (captured / "audio.enc").stat().st_size == 0


def test_moved_audio_is_destroyed_without_unlinking_its_replacement(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload) / "audio.enc"
    adopted_bytes = source.read_bytes()
    tree.vault.mkdir()
    final = _audio_file(tree, payload)
    captured = tree.vault / "captured.enc"
    replacement = b"unrelated replacement must survive"

    from teammem import meeting_importer

    real_import = meeting_importer.import_meeting_revision
    moved = False

    def move_file_after_adoption(*args, **kwargs):
        nonlocal moved
        final.rename(captured)
        assert captured.read_bytes() == adopted_bytes
        final.write_bytes(replacement)
        moved = True
        return real_import(*args, **kwargs)

    monkeypatch.setattr(
        meeting_importer, "import_meeting_revision", move_file_after_adoption)

    result = _run(conn, tree)

    assert moved is True
    assert _outcomes(result) == [("quarantined", "path_escape")]
    assert list_meetings(conn) == []
    remaining_adopted_bytes = captured.stat().st_size if captured.exists() else 0
    replacement_after = final.read_bytes() if final.exists() else None
    assert (remaining_adopted_bytes, replacement_after) == (0, replacement)


def test_cleanup_does_not_unlink_a_replacement_installed_at_removal(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    write_meeting_bundle(tree.inbox, payload)
    tree.vault.mkdir()
    revision = audio_revision_dir(
        tree.audio, payload["occurrence_id"], payload["revision"])
    captured = tree.vault / "captured-revision"
    managed = captured / "audio.enc"
    zeroed = captured / "zeroed-adopted.enc"
    replacement = b"replacement installed after identity lookup"

    from teammem import meeting_importer

    real_import = meeting_importer.import_meeting_revision
    real_unlink = os.unlink
    swapped = False

    def move_revision_after_adoption(*args, **kwargs):
        revision.rename(captured)
        revision.symlink_to(captured, target_is_directory=True)
        return real_import(*args, **kwargs)

    def replace_at_removal(path, *args, **kwargs):
        nonlocal swapped
        if (
            Path(path).name == "audio.enc"
            and kwargs.get("dir_fd") is not None
            and not swapped
        ):
            managed.rename(zeroed)
            managed.write_bytes(replacement)
            swapped = True
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        meeting_importer, "import_meeting_revision", move_revision_after_adoption)
    monkeypatch.setattr(meeting_importer.os, "unlink", replace_at_removal)

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "path_escape")]
    assert list_meetings(conn) == []
    replacement_after = managed.read_bytes() if managed.exists() else None
    assert not swapped or replacement_after == replacement
    residue = zeroed if swapped else managed
    assert residue.stat().st_size == 0


def test_store_failure_cleanup_does_not_unlink_a_replacement_installed_at_removal(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    write_meeting_bundle(tree.inbox, payload)
    managed = _audio_file(tree, payload)
    zeroed = managed.with_name("zeroed-adopted.enc")
    replacement = b"replacement installed during store-failure cleanup"

    from teammem import meeting_importer

    real_unlink = os.unlink
    swapped = False

    def reject_store(*args, **kwargs):
        raise meeting_importer.MeetingStoreError("forced store refusal")

    def replace_at_removal(path, *args, **kwargs):
        nonlocal swapped
        if (
            Path(path).name == "audio.enc"
            and kwargs.get("dir_fd") is not None
            and not swapped
        ):
            managed.rename(zeroed)
            managed.write_bytes(replacement)
            swapped = True
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(meeting_importer, "import_meeting_revision", reject_store)
    monkeypatch.setattr(meeting_importer.os, "unlink", replace_at_removal)

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "store_rejected")]
    assert list_meetings(conn) == []
    replacement_after = managed.read_bytes() if managed.exists() else None
    assert not swapped or replacement_after == replacement
    residue = zeroed if swapped else managed
    assert residue.stat().st_size == 0


def test_preowned_file_substituted_after_rename_is_never_truncated(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload) / "audio.enc"
    adopted_bytes = source.read_bytes()
    tree.vault.mkdir()
    final = _audio_file(tree, payload)
    captured = tree.vault / "captured.enc"
    preowned = tmp_path / "preowned.enc"
    preowned_bytes = b"PREOWNED AUDIO MUST NOT BE TRUNCATED"
    preowned.write_bytes(preowned_bytes)
    replacement = b"unrelated replacement"

    from teammem import meeting_importer

    real_replace = os.replace
    real_import = meeting_importer.import_meeting_revision
    swapped = False

    def substitute_preowned_after_rename(source_name, destination, *args, **kwargs):
        nonlocal swapped
        result = real_replace(source_name, destination, *args, **kwargs)
        if Path(destination).name == "audio.enc" and not swapped:
            final.rename(captured)
            os.link(preowned, final)
            swapped = True
        return result

    def replace_preowned_before_guard(*args, **kwargs):
        final.unlink()
        final.write_bytes(replacement)
        return real_import(*args, **kwargs)

    monkeypatch.setattr(
        meeting_importer.os, "replace", substitute_preowned_after_rename)
    monkeypatch.setattr(
        meeting_importer, "import_meeting_revision", replace_preowned_before_guard)

    result = _run(conn, tree)

    assert swapped is True
    assert _outcomes(result) == [("quarantined", "path_escape")]
    assert list_meetings(conn) == []
    assert preowned.read_bytes() == preowned_bytes
    remaining_adopted_bytes = captured.stat().st_size if captured.exists() else 0
    assert remaining_adopted_bytes == 0
    assert adopted_bytes != preowned_bytes


def test_no_raw_colon_or_slash_from_a_meeting_id_appears_in_any_created_path(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    good = _payload()
    bad = _payload(MINIMAL, day="2026-09-04")
    for payload in (good, bad):
        meeting_id = payload["occurrence_id"].rpartition("@")[0]
        assert ":" in meeting_id and "/" in meeting_id             # the ids under test are hostile
    bad["title"] = ""                                               # forces a quarantine
    write_meeting_bundle(tree.inbox, good)
    write_meeting_bundle(tree.inbox, bad)

    result = _run(conn, tree)
    assert (result.accepted, result.quarantined) == (1, 1)

    ids = [p["occurrence_id"].rpartition("@")[0] for p in (good, bad)]
    checked = 0
    for root in (tree.audio, tree.archive, tree.quarantine):
        for path in _entries(root):
            for part in path.relative_to(root).parts:
                assert ":" not in part, path                        # illegal in a Windows path
                assert "@" not in part, path
                for meeting_id in ids:
                    assert meeting_id not in part, path
                checked += 1
    assert checked, "the walk must actually have visited created paths"
    assert (tree.audio / occurrence_path_segment(good["occurrence_id"])).is_dir()

    stored = Path(get_meeting(conn, good["occurrence_id"])["audio"]["stored_path"])
    assert ":" not in stored.relative_to(tree.audio).as_posix()


# --- fault injection --------------------------------------------------------


class _PatchedOS:
    """The real `os`, with `write` failing on the nth call. Nothing else changes."""

    def __init__(self, fail_on: int):
        self._calls = 0
        self._fail_on = fail_on

    def __getattr__(self, name):
        return getattr(os, name)

    def write(self, fd, data):
        self._calls += 1
        if self._calls == self._fail_on:
            raise OSError(errno.ENOSPC, "No space left on device")
        return os.write(fd, data)


class _WindowsCustodyAttackOS:
    """Move created bytes if Windows custody is released before the store call."""

    name = "nt"

    def __init__(self, escaped: Path):
        self.escaped = escaped
        self.created = {}
        self.store_started = False
        self.attacked = False
        self.renamed_while_open = False

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, path, flags, *args, **kwargs):
        descriptor = os.open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            self.created[descriptor] = Path(path)
        return descriptor

    def open_exclusive(self, path):
        return self.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)

    def open_existing(self, path):
        descriptor = os.open(path, os.O_RDWR)
        self.created[descriptor] = Path(path)
        return descriptor

    def close(self, descriptor):
        created = self.created.pop(descriptor, None)
        result = os.close(descriptor)
        if created is not None and not self.store_started:
            created.rename(self.escaped)
            created.write_bytes(b"preowned substitute")
            self.attacked = True
        return result

    def replace(self, source, destination, *args, **kwargs):
        self.renamed_while_open |= bool(self.created)
        result = os.replace(source, destination, *args, **kwargs)
        source_path = Path(source)
        destination_path = Path(destination)
        for descriptor, created in tuple(self.created.items()):
            if created == source_path:
                self.created[descriptor] = destination_path
        return result

    def delete_owned(self, descriptor):
        created = self.created.pop(descriptor)
        created.unlink()


def _force_windows(monkeypatch, module, windows):
    monkeypatch.setattr(module, "os", windows)
    monkeypatch.setattr(
        module, "_open_windows_exclusive", windows.open_exclusive)
    monkeypatch.setattr(
        module, "_open_windows_existing", windows.open_existing)
    monkeypatch.setattr(
        module, "_delete_windows_owned_descriptor", windows.delete_owned)


def test_windows_keeps_the_exclusive_created_inode_through_the_store_commit(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload) / "audio.enc"
    source_bytes = source.read_bytes()
    escaped = tmp_path / "escaped-created-audio.enc"

    from teammem import meeting_importer

    windows = _WindowsCustodyAttackOS(escaped)
    real_import = meeting_importer.import_meeting_revision

    def mark_store_started(*args, **kwargs):
        windows.store_started = True
        return real_import(*args, **kwargs)

    _force_windows(monkeypatch, meeting_importer, windows)
    monkeypatch.setattr(meeting_importer, "import_meeting_revision", mark_store_started)

    result = _run(conn, tree)

    assert _outcomes(result) == [("accepted", "ok")]
    assert windows.renamed_while_open is True
    assert windows.attacked is False
    assert not escaped.exists()
    assert _audio_file(tree, payload).read_bytes() == source_bytes


def _native_windows_fakes(monkeypatch, module, *, failure=None):
    import ctypes

    kernel32 = SimpleNamespace(
        CreateFileW=Mock(return_value=0x1234),
        CloseHandle=Mock(return_value=1),
        SetFileInformationByHandle=Mock(return_value=1),
    )
    msvcrt = SimpleNamespace(
        open_osfhandle=Mock(return_value=71, side_effect=failure),
        get_osfhandle=Mock(return_value=0x1234),
    )
    monkeypatch.setattr(
        module, "_windows_file_libraries", lambda: (ctypes, kernel32, msvcrt))
    return ctypes, kernel32, msvcrt


def test_windows_staging_open_requests_atomic_rename_sharing_and_transfers_handle(
    tmp_path, monkeypatch
):
    from teammem import meeting_importer

    _ctypes, kernel32, msvcrt = _native_windows_fakes(
        monkeypatch, meeting_importer)

    descriptor = meeting_importer._open_windows_exclusive(tmp_path / ".audio.enc.token")

    assert descriptor == 71
    assert kernel32.CreateFileW.call_args_list == [call(
        str(tmp_path / ".audio.enc.token"),
        0x80000000 | 0x40000000 | 0x00010000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        1,
        0x00000080 | 0x00200000,
        None,
    )]
    assert msvcrt.open_osfhandle.call_args_list == [
        call(0x1234, os.O_RDWR | getattr(os, "O_BINARY", 0))
    ]
    kernel32.CloseHandle.assert_not_called()


def test_windows_owned_descriptor_deletion_borrows_the_transferred_handle(
    monkeypatch
):
    from teammem import meeting_importer

    ctypes, kernel32, msvcrt = _native_windows_fakes(
        monkeypatch, meeting_importer)
    observed = {}

    def capture_disposition(_handle, _information_class, info, _size):
        observed["delete_file"] = ctypes.cast(
            info, ctypes.POINTER(ctypes.c_ubyte)).contents.value
        return 1

    kernel32.SetFileInformationByHandle.side_effect = capture_disposition

    meeting_importer._delete_windows_owned_descriptor(71)

    msvcrt.get_osfhandle.assert_called_once_with(71)
    [(handle, information_class, info, size)] = (
        [item.args for item in kernel32.SetFileInformationByHandle.call_args_list]
    )
    assert (handle, information_class, size) == (
        0x1234, 4, ctypes.sizeof(ctypes.c_ubyte))
    assert info.value is not None and observed == {"delete_file": 1}
    kernel32.CloseHandle.assert_not_called()


def test_windows_staging_open_closes_the_os_handle_once_if_fd_transfer_fails(
    tmp_path, monkeypatch
):
    from teammem import meeting_importer

    _ctypes, kernel32, _msvcrt = _native_windows_fakes(
        monkeypatch,
        meeting_importer,
        failure=OSError(errno.EMFILE, "too many descriptors"),
    )

    with pytest.raises(OSError) as excinfo:
        meeting_importer._open_windows_exclusive(tmp_path / ".audio.enc.token")

    assert excinfo.value.errno == errno.EMFILE
    kernel32.CloseHandle.assert_called_once_with(0x1234)


def test_windows_staging_cleanup_tolerates_a_removed_revision_directory(
    tmp_path, monkeypatch
):
    from teammem import meeting_importer

    windows = _WindowsCustodyAttackOS(tmp_path / "must-not-escape.enc")
    _force_windows(monkeypatch, meeting_importer, windows)

    meeting_importer._discard_windows_staging_residues(tmp_path / "missing")


def test_handled_windows_store_failure_can_retry_without_a_final_audio_conflict(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload) / "audio.enc"
    source_bytes = source.read_bytes()

    from teammem import meeting_importer

    windows = _WindowsCustodyAttackOS(tmp_path / "must-not-escape.enc")
    real_import = meeting_importer.import_meeting_revision

    def fail_store(*args, **kwargs):
        raise RuntimeError("injected handled store failure")

    _force_windows(monkeypatch, meeting_importer, windows)
    monkeypatch.setattr(meeting_importer, "import_meeting_revision", fail_store)

    with pytest.raises(RuntimeError, match="injected handled store failure"):
        _run(conn, tree)
    assert list_meetings(conn) == []
    assert _files(tree.audio) == []
    assert (source.parent / "READY").is_file()

    def succeed_store(*args, **kwargs):
        windows.store_started = True
        return real_import(*args, **kwargs)

    monkeypatch.setattr(meeting_importer, "import_meeting_revision", succeed_store)
    result = _run(conn, tree)

    assert _outcomes(result) == [("accepted", "ok")]
    assert _audio_file(tree, payload).read_bytes() == source_bytes
    assert get_meeting(conn, payload["occurrence_id"])["revision"] == payload["revision"]
    assert [path for path in _files(tree.audio) if path.name != "audio.enc"] == []


def test_custody_recovery_exception_before_ownership_preserves_replacement(tmp_path):
    if os.name == "nt":
        pytest.skip("the regression exercises real POSIX custody descriptors")
    from teammem import meeting_importer

    final = tmp_path / "audio.enc"
    final.touch()
    custody = tmp_path / ".audio.enc.custody.replaced"
    custody.write_bytes(b"REPLACEMENT MUST SURVIVE")
    source = tmp_path / "source.enc"
    source.write_bytes(b"source ciphertext")
    final_fd = os.open(final, os.O_RDONLY)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        final.unlink()
        with pytest.raises(FileNotFoundError):
            meeting_importer._recover_custody_audio(
                source, tmp_path, final, final_fd, directory_fd)
        assert custody.read_bytes() == b"REPLACEMENT MUST SURVIVE"
    finally:
        os.close(final_fd)
        os.close(directory_fd)


def test_custody_recovery_partial_refill_failure_zeros_only_owned_inode(
    tmp_path, monkeypatch
):
    if os.name == "nt":
        pytest.skip("the regression exercises real POSIX custody descriptors")
    from teammem import meeting_importer

    final = tmp_path / "audio.enc"
    final.touch()
    custody = tmp_path / ".audio.enc.custody.owned"
    os.link(final, custody)
    replacement = tmp_path / "replacement.enc"
    replacement.write_bytes(b"REPLACEMENT MUST SURVIVE")
    source = tmp_path / "source.enc"
    source.write_bytes(b"source ciphertext")
    final_fd = os.open(final, os.O_RDONLY)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

    def fail_partial_write(descriptor, data):
        os.write(descriptor, data[:3])
        os.replace(replacement, custody)
        raise OSError("injected partial refill failure")

    monkeypatch.setattr(meeting_importer, "_write_all", fail_partial_write)
    try:
        with pytest.raises(OSError, match="injected partial refill failure"):
            meeting_importer._recover_custody_audio(
                source, tmp_path, final, final_fd, directory_fd)
        assert final.read_bytes() == b""
        assert custody.read_bytes() == b"REPLACEMENT MUST SURVIVE"
    finally:
        os.close(final_fd)
        os.close(directory_fd)


def test_handled_posix_store_failure_can_retry_without_a_final_audio_conflict(
    tmp_path, monkeypatch
):
    """Losing exact-name recovery for our zeroed POSIX inode breaks this retry."""
    if os.name == "nt":
        pytest.skip("the regression exercises the real POSIX rename path")
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload) / "audio.enc"
    source_bytes = source.read_bytes()

    from teammem import meeting_importer

    real_import = meeting_importer.import_meeting_revision

    def fail_store(*args, **kwargs):
        raise RuntimeError("injected handled store failure")

    monkeypatch.setattr(meeting_importer, "import_meeting_revision", fail_store)

    with pytest.raises(RuntimeError, match="injected handled store failure"):
        _run(conn, tree)
    final = _audio_file(tree, payload)
    assert final.is_file() and final.stat().st_size == 0
    [custody] = [
        path for path in _files(tree.audio)
        if path.name.startswith(".audio.enc.custody.")
    ]
    assert custody.stat().st_size == 0 and custody.samefile(final)
    assert list_meetings(conn) == []
    assert (source.parent / "READY").is_file()

    monkeypatch.setattr(meeting_importer, "import_meeting_revision", real_import)
    result = _run(conn, tree)

    assert _outcomes(result) == [("accepted", "ok")]
    assert final.read_bytes() == source_bytes
    assert get_meeting(conn, payload["occurrence_id"])["revision"] == payload["revision"]
    assert not tree.quarantine.exists()
    [custody] = [
        path for path in _files(tree.audio)
        if path.name.startswith(".audio.enc.custody.")
    ]
    assert custody.samefile(final) and custody.read_bytes() == source_bytes

    expired = expire_meeting_audio(
        conn,
        tree.audio,
        now="2026-09-17T00:00:00Z",
        alert_path=lambda _occurrence_id: tmp_path / "unused-alert.json",
    )

    assert expired.deleted == ((payload["occurrence_id"], payload["revision"]),)
    assert not final.exists() and custody.read_bytes() == b""


class _WindowsCrashDuringCopyOS(_WindowsCustodyAttackOS):
    def __init__(self):
        super().__init__(Path("unused-escape"))
        self.write_calls = 0

    def write(self, descriptor, data):
        self.write_calls += 1
        if self.write_calls == 2:
            os._exit(23)
        return os.write(descriptor, data)


def test_abrupt_windows_copy_exit_exposes_only_staging_and_a_retry_succeeds(
    tmp_path, monkeypatch
):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("abrupt-exit harness requires fork; native Windows is not claimed")
    db_path = tmp_path / "ledger.db"
    _db(tmp_path).close()
    tree = _Tree(tmp_path)
    payload = _payload()
    source = write_meeting_bundle(tree.inbox, payload) / "audio.enc"
    source_bytes = source.read_bytes()

    def crash_mid_copy():
        from teammem import meeting_importer

        windows = _WindowsCrashDuringCopyOS()
        meeting_importer.os = windows
        meeting_importer._COPY_CHUNK = 512
        meeting_importer._open_windows_exclusive = windows.open_exclusive
        connection = open_db(db_path)
        import_meeting_inbox(
            connection,
            *tree.paths(),
            vault_dir=tree.vault,
            now=NOW,
        )

    process = multiprocessing.get_context("fork").Process(target=crash_mid_copy)
    process.start()
    process.join(10)
    assert process.exitcode == 23

    conn = open_db(db_path)
    final = _audio_file(tree, payload)
    staging = [path for path in _files(tree.audio) if path.name.startswith(".audio.enc.")]
    assert not final.exists()
    assert len(staging) == 1 and staging[0].stat().st_size == 512
    assert list_meetings(conn) == []

    from teammem import meeting_importer

    windows = _WindowsCustodyAttackOS(tmp_path / "must-not-escape.enc")
    _force_windows(monkeypatch, meeting_importer, windows)

    real_import = meeting_importer.import_meeting_revision

    def mark_store_started(*args, **kwargs):
        windows.store_started = True
        return real_import(*args, **kwargs)

    monkeypatch.setattr(meeting_importer, "import_meeting_revision", mark_store_started)

    result = _run(conn, tree)

    assert _outcomes(result) == [("accepted", "ok")]
    assert final.read_bytes() == source_bytes
    assert get_meeting(conn, payload["occurrence_id"])["revision"] == payload["revision"]

    expired = expire_meeting_audio(
        conn,
        tree.audio,
        now="2026-09-17T00:00:00Z",
        alert_path=lambda _occurrence_id: tmp_path / "unused-alert.json",
    )

    assert expired.deleted == ((payload["occurrence_id"], payload["revision"]),)
    assert expired.failed == ()
    assert not final.exists()
    assert not staging[0].exists()


def test_alias_store_expiry_removes_final_and_windows_staging_before_marking(
    tmp_path, monkeypatch
):
    """A lexical-root comparison strands governed crash ciphertext."""
    conn = _db(tmp_path)
    physical_parent = tmp_path / "physical-parent"
    physical_parent.mkdir()
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(physical_parent, target_is_directory=True)
    tree = _Tree(tmp_path)
    tree.audio = alias_parent / "audio-store"
    payload = _payload()
    write_meeting_bundle(tree.inbox, payload)

    assert _outcomes(_run(conn, tree)) == [("accepted", "ok")]
    stored = Path(get_meeting(conn, payload["occurrence_id"])["audio"]["stored_path"])
    lexical = _audio_file(tree, payload)
    assert stored != lexical and stored.samefile(lexical)
    staging = stored.with_name(".audio.enc.crash-residue")
    staging.write_bytes(b"orphaned ciphertext")

    from teammem import meeting_importer

    windows = _WindowsCustodyAttackOS(tmp_path / "must-not-escape.enc")
    _force_windows(monkeypatch, meeting_importer, windows)

    expired = expire_meeting_audio(
        conn,
        tree.audio,
        now="2026-09-17T00:00:00Z",
        alert_path=lambda _occurrence_id: tmp_path / "unused-alert.json",
    )

    assert expired.deleted == ((payload["occurrence_id"], payload["revision"]),)
    assert expired.failed == ()
    assert not stored.exists()
    assert not staging.exists()
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == (
        "2026-09-17T00:00:00.000000Z"
    )


class _StagingCleanupRaceOS:
    """Replace a staging name as an injected post-copy fsync failure fires."""

    def __init__(self, directory: Path, escaped: Path, replacement: bytes):
        self.directory = directory
        self.escaped = escaped
        self.replacement = replacement
        self.fsync_calls = 0
        self.staging = None
        self.swapped = False

    def __getattr__(self, name):
        return getattr(os, name)

    def fsync(self, descriptor):
        self.fsync_calls += 1
        if self.fsync_calls == 2:
            [self.staging] = [
                path for path in self.directory.iterdir()
                if path.name.startswith(".audio.enc.")
            ]
            self.staging.rename(self.escaped)
            self.staging.write_bytes(self.replacement)
            self.swapped = True
            raise OSError(errno.EIO, "injected directory fsync failure")
        return os.fsync(descriptor)


def test_environmental_cleanup_zeroes_only_the_held_staging_inode(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    directory = write_meeting_bundle(tree.inbox, payload)
    revision = audio_revision_dir(
        tree.audio, payload["occurrence_id"], payload["revision"])
    escaped = tmp_path / "escaped-staging.enc"
    replacement = b"replacement installed after the staging copy"

    from teammem import meeting_importer

    raced_os = _StagingCleanupRaceOS(revision, escaped, replacement)
    monkeypatch.setattr(meeting_importer, "os", raced_os)

    with pytest.raises(OSError) as excinfo:
        _run(conn, tree)
    assert excinfo.value.errno == errno.EIO

    monkeypatch.undo()
    assert raced_os.swapped is True
    assert list_meetings(conn) == []
    assert escaped.stat().st_size == 0
    assert raced_os.staging.read_bytes() == replacement
    assert (directory / "READY").is_file()
    assert not tree.quarantine.exists()


def test_disk_full_during_the_audio_copy_leaves_only_a_zero_byte_staging_residue(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    directory = write_meeting_bundle(tree.inbox, payload)

    from teammem import meeting_importer

    monkeypatch.setattr(meeting_importer, "_COPY_CHUNK", 512)       # 2048 bytes -> four writes
    monkeypatch.setattr(meeting_importer, "os", _PatchedOS(fail_on=2))

    with pytest.raises(OSError) as excinfo:
        _run(conn, tree)
    assert excinfo.value.errno == errno.ENOSPC

    monkeypatch.undo()
    assert list_meetings(conn) == []
    assert not _audio_file(tree, payload).exists()                  # no final-named audio
    [residue] = _files(tree.audio)
    assert residue.name.startswith(".audio.enc.")
    assert residue.stat().st_size == 0                              # only owned bytes were changed
    assert (directory / "READY").is_file()                          # the bundle stays for a retry
    assert not tree.quarantine.exists()               # ENOSPC is not the producer's fault

    assert _run(conn, tree).accepted == 1                           # a retry then succeeds


def test_crash_before_db_commit_leaves_zero_byte_final_and_custody_residue(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    directory = write_meeting_bundle(tree.inbox, payload)

    from teammem import meeting_importer

    observed = {}

    def exploding_import(connection, **kwargs):
        observed["path"] = Path(kwargs["audio"].stored_path)
        observed["staged"] = observed["path"].is_file()            # staged before the DB call
        raise RuntimeError("process died before commit")

    monkeypatch.setattr(meeting_importer, "import_meeting_revision", exploding_import)

    with pytest.raises(RuntimeError):
        _run(conn, tree)

    assert observed["staged"] is True
    assert list_meetings(conn) == []
    assert observed["path"].stat().st_size == 0                     # no ciphertext remains
    [custody] = [
        path for path in _files(tree.audio)
        if path.name.startswith(".audio.enc.custody.")
    ]
    assert custody.stat().st_size == 0 and custody.samefile(observed["path"])
    assert (directory / "READY").is_file() and not tree.archive.exists()


def test_a_store_rejection_removes_only_the_audio_it_did_not_already_own(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    first = _payload(revision=1)
    write_meeting_bundle(tree.inbox, first)
    _run(conn, tree)
    kept = _audio_file(tree, first)
    kept_bytes = kept.read_bytes()

    conflicting = _payload(revision=1)
    conflicting["summary"] = "Same revision, different content."
    write_meeting_bundle(tree.inbox, conflicting)
    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "revision_conflict")]
    assert kept.read_bytes() == kept_bytes                         # the stored audio is intact
    assert _audio_payload_files(tree.audio) == [kept]
    assert get_meeting(conn, first["occurrence_id"])["audio"]["stored_path"] == str(kept)


def test_a_quarantine_never_touches_audio_belonging_to_another_occurrence(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    good = _payload()
    write_meeting_bundle(tree.inbox, good)
    _run(conn, tree)
    kept = _audio_file(tree, good)

    bad = _payload(MINIMAL, day="2026-09-05")
    bad["summary"] = ""                                             # invalid: empty summary
    write_meeting_bundle(tree.inbox, bad)
    _run(conn, tree)

    assert kept.is_file()
    assert _audio_payload_files(tree.audio) == [kept]
    assert not audio_revision_dir(tree.audio, bad["occurrence_id"], bad["revision"]).exists()


# --- redaction --------------------------------------------------------------


def test_result_and_logs_never_carry_transcript_text(tmp_path, caplog):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    good = _payload()
    write_meeting_bundle(tree.inbox, good)
    broken = _payload(revision=9)
    broken["summary"] = TRANSCRIPT_SENTENCE
    broken["transcript"][0]["text"] = TRANSCRIPT_SENTENCE
    broken["participants"][0]["person_id"] = ""                     # invalid: forces a quarantine
    write_meeting_bundle(tree.inbox, broken)

    with caplog.at_level(logging.DEBUG, logger="teammem.meeting_importer"):
        result = _run(conn, tree)

    assert (result.accepted, result.quarantined) == (1, 1)
    assert TRANSCRIPT_SENTENCE not in repr(result)
    assert TRANSCRIPT_SENTENCE not in str(result.details)
    assert caplog.records, "the importer must log something for this test to mean anything"
    for record in caplog.records:
        assert TRANSCRIPT_SENTENCE not in record.getMessage()
    assert TRANSCRIPT_SENTENCE not in caplog.text

    reasons = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in _files(tree.quarantine)
        if p.name.endswith(".reason.json")
    ]
    assert reasons, "a rejected bundle records a machine-readable reason"
    for reason in reasons:
        assert TRANSCRIPT_SENTENCE not in json.dumps(reason)
        assert reason["reason_code"] in REASON_CODES


def test_every_reason_code_is_a_short_machine_token():
    assert "ok" in REASON_CODES
    for code in REASON_CODES:
        assert code == code.lower()
        assert code.replace("_", "").isalnum()
        assert len(code) <= 32


def test_different_audio_already_in_the_store_is_a_conflict_and_is_not_overwritten(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    write_meeting_bundle(tree.inbox, payload)
    planted = _audio_file(tree, payload)
    planted.parent.mkdir(parents=True)
    planted.write_bytes(b"\x07" * payload["audio"]["bytes"])       # someone else's bytes

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "audio_conflict")]
    assert list_meetings(conn) == []
    assert planted.read_bytes() == b"\x07" * payload["audio"]["bytes"]
    assert _files(tree.audio) == [planted]


def test_plaintext_audio_container_is_quarantined(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    plaintext = b"RIFF" + bytes(range(256)) * 2                    # a WAV magic number
    payload["audio"]["bytes"] = len(plaintext)
    payload["audio"]["sha256"] = "sha256:" + hashlib.sha256(plaintext).hexdigest()
    write_meeting_bundle(tree.inbox, payload, audio=plaintext)

    result = _run(conn, tree)

    assert _outcomes(result) == [("quarantined", "audio_plaintext")]
    assert list_meetings(conn) == []
    assert not tree.audio.exists()


def test_a_failed_archive_still_reports_the_committed_import_and_the_scan_continues(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    first = _payload(COMPLETE)
    second = _payload(MINIMAL, day="2026-09-06")
    write_meeting_bundle(tree.inbox, first)
    write_meeting_bundle(tree.inbox, second)

    from teammem import meeting_importer

    real_archive = meeting_importer._archive_bundle

    def failing_archive(bundle, archive):
        if bundle.occurrence_id == first["occurrence_id"]:
            raise OSError(errno.EACCES, "Permission denied")
        return real_archive(bundle, archive)

    monkeypatch.setattr(meeting_importer, "_archive_bundle", failing_archive)

    result = _run(conn, tree)

    # the minimal fixture's occurrence segment sorts first, so it is the "ok" one
    assert _outcomes(result) == [("accepted", "ok"), ("accepted", "archive_failed")]
    assert result.accepted == 2                                    # both imports did happen
    assert get_meeting(conn, first["occurrence_id"])["revision"] == first["revision"]
    assert get_meeting(conn, second["occurrence_id"]) is not None   # the scan carried on
    first_archive = tree.archive / occurrence_path_segment(first["occurrence_id"])
    assert not first_archive.exists()
    assert (tree.archive / occurrence_path_segment(second["occurrence_id"])
            / str(second["revision"]) / "meeting.json").is_file()
    assert not tree.quarantine.exists()               # a stored revision is never quarantined

    monkeypatch.undo()
    rescan = _run(conn, tree)                                      # the rescan self-heals

    assert _outcomes(rescan) == [("unchanged", "ok")]
    assert (first_archive / str(first["revision"]) / "meeting.json").is_file()
    assert (first_archive / str(first["revision"]) / "manifest.json").is_file()
    assert [r["revision"] for r in list_meeting_revisions(conn, first["occurrence_id"])] == [
        first["revision"]
    ]
    assert not (tree.inbox / occurrence_path_segment(first["occurrence_id"])).exists()


def test_audio_swapped_for_a_symlink_after_validation_is_not_copied_through(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    directory = write_meeting_bundle(tree.inbox, payload)
    secret = tmp_path / "outside.bin"
    secret.write_bytes(b"\x11" * payload["audio"]["bytes"])

    from teammem import meeting_importer

    real_load = meeting_importer.load_meeting_bundle

    def load_then_swap(path, inbox):
        bundle = real_load(path, inbox)                            # validation sees a real file
        bundle.audio_path.unlink()                                 # ...and then it is swapped
        bundle.audio_path.symlink_to(secret)
        return bundle

    monkeypatch.setattr(meeting_importer, "load_meeting_bundle", load_then_swap)

    with pytest.raises(OSError):
        _run(conn, tree)

    assert list_meetings(conn) == []
    assert _files(tree.audio) == []                                # nothing was copied through
    assert secret.read_bytes() == b"\x11" * payload["audio"]["bytes"]
    assert (directory / "audio.enc").is_symlink()                  # the plant was left as found
