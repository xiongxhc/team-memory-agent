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
import os
import shutil
from pathlib import Path

import pytest

from teammem.meeting_bundles import occurrence_path_segment
from teammem.meeting_importer import (
    REASON_CODES,
    MeetingImportResult,
    audio_revision_dir,
    import_meeting_inbox,
)
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
        self.inbox.mkdir()

    def paths(self) -> tuple[Path, Path, Path, Path]:
        return (self.inbox, self.archive, self.quarantine, self.audio)


def _run(conn, tree: _Tree, *, dry_run: bool = False, now: str = NOW) -> MeetingImportResult:
    return import_meeting_inbox(conn, *tree.paths(), now=now, dry_run=dry_run)


def _db(tmp_path: Path):
    return open_db(tmp_path / "ledger.db")


def _files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())


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


def test_staged_audio_is_byte_identical_and_leaves_no_temporary_behind(tmp_path):
    conn = _db(tmp_path)
    tree = _Tree(tmp_path)
    payload = _payload()
    directory = write_meeting_bundle(tree.inbox, payload)
    source = (directory / "audio.enc").read_bytes()

    _run(conn, tree)

    final = _audio_file(tree, payload)
    assert final.read_bytes() == source
    assert hashlib.sha256(source).hexdigest() == payload["audio"]["sha256"].split(":", 1)[1]
    assert _files(tree.audio) == [final]                            # no staging leftovers


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
    assert _files(tree.audio) == [_audio_file(tree, payload)]
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
    assert not _audio_file(tree, lower).exists()          # nothing durable was staged for it


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


def test_disk_full_during_the_audio_copy_leaves_no_row_and_no_final_file(tmp_path, monkeypatch):
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
    assert _files(tree.audio) == []                                 # and no staging leftovers
    assert (directory / "READY").is_file()                          # the bundle stays for a retry
    assert not tree.quarantine.exists()               # ENOSPC is not the producer's fault

    assert _run(conn, tree).accepted == 1                           # a retry then succeeds


def test_crash_before_db_commit_removes_the_staged_audio_and_a_rescan_succeeds(
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
    assert not observed["path"].exists()                           # the staged file was cleaned up
    assert _files(tree.audio) == []
    assert (directory / "READY").is_file() and not tree.archive.exists()

    monkeypatch.undo()
    result = _run(conn, tree)                                       # the rescan is clean
    assert result.accepted == 1
    assert get_meeting(conn, payload["occurrence_id"]) is not None
    assert observed["path"].is_file()


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
    assert _files(tree.audio) == [kept]
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
    assert _files(tree.audio) == [kept]
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
