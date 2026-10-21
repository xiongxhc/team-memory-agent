"""Audio retention: the encrypted source is gone fourteen days after the meeting.

Every meeting here is imported through the real importer, so the rows, the store
layout and the file on disk are the ones production would have. The clock is
always injected: no test reads the wall clock, and every boundary is asserted at
an exact instant.

The promise being tested is made to meeting participants — the audio is deleted
at fourteen days — and it cuts both ways. Deleting late breaks it. Deleting early
destroys bytes TeamMem can never regenerate, because it never holds the recording
key.
"""

import errno
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from teammem import cli as cli_module
from teammem import config as config_module
from teammem import meeting_retention
from teammem.cli import main
from teammem.meeting_bundles import occurrence_path_segment
from teammem.meeting_importer import audio_revision_dir, import_meeting_inbox
from teammem.meeting_retention import expire_meeting_audio
from teammem.meeting_store import (
    get_meeting,
    list_meeting_revisions,
    mark_audio_deleted,
)
from teammem.store import open_db
from tests.test_meeting_bundles import write_meeting_bundle


FIXTURES = Path(__file__).parent / "fixtures" / "meetings"
COMPLETE = FIXTURES / "teammem-meeting-v1.complete.json"
CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"

IMPORTED_AT = "2026-09-02T16:00:00Z"
# The complete fixture ends at 2026-09-02T14:52:00Z, so TeamMem's own expiry is
# that instant plus fourteen days. The fixture's own `audio.expires_at` claims
# 2026-10-02 — thirty days — and is never authoritative.
EXPIRY = "2026-09-16T14:52:00.000000Z"
JUST_BEFORE = "2026-09-16T14:51:59.000000Z"
LATER = "2026-09-20T09:00:00.000000Z"
FRACTIONAL_EXPIRY = "2026-09-16T14:52:00.500000Z"

# A sentence that exists only inside the complete fixture's transcript.
TRANSCRIPT_SENTENCE = (
    "The staging soak has been green for six days, so I am comfortable with the current build."
)


@pytest.fixture(autouse=True)
def _isolate_operator_env_file(monkeypatch, tmp_path):
    """No test may read the operator's real hub.env; point both resolution
    sites at a file that does not exist."""
    absent = tmp_path / "absent-hub.env"
    monkeypatch.setattr(config_module, "default_env_file", lambda **_: absent)
    monkeypatch.setattr(cli_module, "default_env_file", lambda **_: absent)


def _payload(*, day: str | None = None, revision: int | None = None, **fields) -> dict:
    text = COMPLETE.read_text(encoding="utf-8")
    if day is not None:
        text = text.replace("2026-09-02", day)
    data = json.loads(text)
    if revision is not None:
        data["revision"] = revision
    data.update(fields)
    return data


def _db(tmp_path: Path):
    return open_db(tmp_path / "ledger.db")


def _store(tmp_path: Path) -> Path:
    return tmp_path / "meeting-audio"


def _alerts(tmp_path: Path) -> Path:
    directory = tmp_path / "meeting-alerts"
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def _alert_path(tmp_path: Path):
    return lambda occurrence_id: (
        _alerts(tmp_path) / f"{occurrence_path_segment(occurrence_id)}.json"
    )


def _audio(tmp_path: Path, payload: dict, revision: int | None = None) -> Path:
    revision = payload["revision"] if revision is None else revision
    return audio_revision_dir(
        _store(tmp_path), payload["occurrence_id"], revision) / "audio.enc"


def _import(conn, tmp_path: Path, payload: dict) -> None:
    """Import one bundle the way `process-meetings` would, private directories
    and all, so a retention run over the result is not refused for their modes."""
    inbox = tmp_path / "meeting-inbox"
    for name in ("meeting-inbox", "meeting-archive", "meeting-quarantine",
                 "meeting-audio", "meeting-receipts"):
        (tmp_path / name).mkdir(mode=0o700, exist_ok=True)
    write_meeting_bundle(inbox, payload)
    result = import_meeting_inbox(
        conn,
        inbox,
        tmp_path / "meeting-archive",
        tmp_path / "meeting-quarantine",
        _store(tmp_path),
        vault_dir=tmp_path / "vault",
        now=IMPORTED_AT,
    )
    assert result.quarantined == 0, result.details


def _expire(conn, tmp_path: Path, now: str, **kw):
    return expire_meeting_audio(
        conn, _store(tmp_path), now=now, alert_path=_alert_path(tmp_path), **kw
    )


def _files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def _custody_files(root: Path) -> list[Path]:
    return [path for path in _files(root) if path.name.startswith(".audio.enc.custody.")]


# --- the boundary -----------------------------------------------------------


def test_one_second_before_expiry_the_audio_is_retained(tmp_path):
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)

    result = _expire(conn, tmp_path, JUST_BEFORE)

    assert result.deleted == ()
    assert _audio(tmp_path, payload).exists()
    stored = get_meeting(conn, payload["occurrence_id"])
    assert stored["audio"]["deleted_at"] is None
    assert result.exit_code == 0


def test_exactly_at_expiry_the_audio_is_deleted_and_the_row_marked(tmp_path):
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    occurrence_id = payload["occurrence_id"]
    assert get_meeting(conn, occurrence_id)["audio"]["expires_at"] == EXPIRY

    result = _expire(conn, tmp_path, EXPIRY)

    assert result.deleted == ((occurrence_id, payload["revision"]),)
    assert not _audio(tmp_path, payload).exists()
    assert get_meeting(conn, occurrence_id)["audio"]["deleted_at"] == EXPIRY
    [custody] = _custody_files(_store(tmp_path))
    assert custody.read_bytes() == b""             # only the expired inode's name remains
    assert result.exit_code == 0


def test_one_microsecond_before_fractional_expiry_the_audio_is_retained(tmp_path):
    conn = _db(tmp_path)
    payload = _payload(ended_at="2026-09-02T14:52:00.500000Z")
    _import(conn, tmp_path, payload)

    result = _expire(conn, tmp_path, "2026-09-16T14:52:00.499999Z")

    assert result.deleted == ()
    assert _audio(tmp_path, payload).exists()
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] is None


def test_exactly_at_fractional_expiry_records_the_actual_deletion_instant(tmp_path):
    conn = _db(tmp_path)
    payload = _payload(ended_at="2026-09-02T14:52:00.500000Z")
    _import(conn, tmp_path, payload)
    occurrence_id = payload["occurrence_id"]
    assert get_meeting(conn, occurrence_id)["audio"]["expires_at"] == FRACTIONAL_EXPIRY

    result = _expire(conn, tmp_path, FRACTIONAL_EXPIRY)

    assert result.deleted == ((occurrence_id, payload["revision"]),)
    assert not _audio(tmp_path, payload).exists()
    assert get_meeting(conn, occurrence_id)["audio"]["deleted_at"] == FRACTIONAL_EXPIRY


def test_one_microsecond_after_fractional_expiry_records_the_actual_deletion_instant(tmp_path):
    conn = _db(tmp_path)
    payload = _payload(ended_at="2026-09-02T14:52:00.500000Z")
    _import(conn, tmp_path, payload)
    occurrence_id = payload["occurrence_id"]

    result = _expire(conn, tmp_path, "2026-09-16T14:52:00.500001Z")

    assert result.deleted == ((occurrence_id, payload["revision"]),)
    assert not _audio(tmp_path, payload).exists()
    assert get_meeting(conn, occurrence_id)["audio"]["deleted_at"] == (
        "2026-09-16T14:52:00.500001Z"
    )


def test_a_second_run_after_expiry_deletes_nothing_and_keeps_the_first_timestamp(tmp_path):
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    _expire(conn, tmp_path, EXPIRY)

    result = _expire(conn, tmp_path, LATER)

    assert result.deleted == ()
    assert result.failed == ()
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == EXPIRY
    assert result.exit_code == 0


def test_a_missing_final_with_custody_bytes_blocks_the_deleted_mark(tmp_path):
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    _audio(tmp_path, payload).unlink()

    result = _expire(conn, tmp_path, EXPIRY)

    assert result.deleted == ()
    assert [(failure.occurrence_id, failure.stage) for failure in result.failed] == [
        (payload["occurrence_id"], "containment")
    ]
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] is None


def test_audio_that_no_longer_matches_its_digest_is_deleted_and_reported(tmp_path):
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    _audio(tmp_path, payload).write_bytes(b"\x00\x01\x02" * 64)

    result = _expire(conn, tmp_path, EXPIRY)

    key = (payload["occurrence_id"], payload["revision"])
    assert result.deleted == (key,)                # the promise outranks the anomaly
    assert result.mismatched == (key,)
    assert not _audio(tmp_path, payload).exists()
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == EXPIRY
    assert result.exit_code == 1


# --- occurrence scoping -----------------------------------------------------


def test_two_occurrences_with_identical_audio_expire_independently(tmp_path):
    conn = _db(tmp_path)
    early = _payload(day="2026-08-20")
    late = _payload()
    assert early["audio"]["sha256"] == late["audio"]["sha256"]
    _import(conn, tmp_path, early)
    _import(conn, tmp_path, late)
    assert _audio(tmp_path, early).read_bytes() == _audio(tmp_path, late).read_bytes()

    result = _expire(conn, tmp_path, "2026-09-03T14:52:00Z")

    assert result.deleted == ((early["occurrence_id"], early["revision"]),)
    assert not _audio(tmp_path, early).exists()
    assert _audio(tmp_path, late).exists()
    assert get_meeting(conn, late["occurrence_id"])["audio"]["deleted_at"] is None


def test_a_stale_prior_revision_loses_its_audio_on_its_own_schedule(tmp_path):
    conn = _db(tmp_path)
    first = _payload(revision=1)
    second = _payload(revision=2, ended_at="2026-09-02T15:52:00Z")
    _import(conn, tmp_path, first)
    _import(conn, tmp_path, second)
    occurrence_id = first["occurrence_id"]
    assert get_meeting(conn, occurrence_id)["revision"] == 2

    result = _expire(conn, tmp_path, EXPIRY)

    assert result.deleted == ((occurrence_id, 1),)
    assert not _audio(tmp_path, first, 1).exists()
    assert _audio(tmp_path, second, 2).exists()
    assert get_meeting(conn, occurrence_id, revision=1)["audio"]["deleted_at"] == EXPIRY
    assert get_meeting(conn, occurrence_id, revision=2)["audio"]["deleted_at"] is None

    later = _expire(conn, tmp_path, "2026-09-16T15:52:00Z")
    assert later.deleted == ((occurrence_id, 2),)
    assert not _audio(tmp_path, second, 2).exists()


# --- failures ---------------------------------------------------------------


def test_an_unlink_failure_leaves_the_row_live_for_the_next_run(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    target = _audio(tmp_path, payload)
    real_unlink = os.unlink

    def refuse(path, *args, **kwargs):
        if Path(path) == target:
            raise OSError(errno.EACCES, "permission denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refuse)
    result = _expire(conn, tmp_path, EXPIRY)

    assert result.deleted == ()
    assert [(f.occurrence_id, f.revision, f.stage) for f in result.failed] == [
        (payload["occurrence_id"], payload["revision"], "unlink")
    ]
    assert target.exists()
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] is None
    assert result.exit_code == 1

    monkeypatch.undo()
    retried = _expire(conn, tmp_path, LATER)
    assert retried.deleted == ((payload["occurrence_id"], payload["revision"]),)
    assert not target.exists()


def test_a_database_failure_after_the_unlink_is_retried_until_it_converges(
    tmp_path, monkeypatch
):
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)

    def refuse(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("teammem.meeting_retention.mark_audio_deleted", refuse)
    result = _expire(conn, tmp_path, EXPIRY)

    # The file goes first, so a crash between the two leaves bytes deleted and a
    # row that will be marked on the next run — never the reverse.
    assert not _audio(tmp_path, payload).exists()
    assert [(f.occurrence_id, f.stage) for f in result.failed] == [
        (payload["occurrence_id"], "mark")
    ]
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] is None
    assert result.exit_code == 1

    monkeypatch.undo()
    retried = _expire(conn, tmp_path, LATER)
    assert retried.deleted == ((payload["occurrence_id"], payload["revision"]),)
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == LATER


def test_revived_audio_from_a_replayed_bundle_is_removed_again(tmp_path):
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    _expire(conn, tmp_path, EXPIRY)
    target = _audio(tmp_path, payload)
    assert not target.exists()

    # The same revision is re-delivered. The importer re-stages the file, the
    # store reports `unchanged` and rolls back, so `deleted_at` still stands and
    # `audio_due_for_expiry` will never return this row again.
    _import(conn, tmp_path, payload)
    assert target.exists()

    result = _expire(conn, tmp_path, LATER)

    assert result.swept == ((payload["occurrence_id"], payload["revision"]),)
    assert not target.exists()
    stored = get_meeting(conn, payload["occurrence_id"])["audio"]
    assert stored["deleted_at"] == EXPIRY           # a replay never moves the decision
    assert stored["sha256"] == payload["audio"]["sha256"]
    assert all(path.stat().st_size == 0 for path in _files(_store(tmp_path)))
    assert result.failed == ()
    retried = _expire(conn, tmp_path, LATER)
    assert retried.swept == () and retried.failed == ()


@pytest.mark.parametrize("interruption", ["missing_final", "replaced_custody", "zero_failure"])
def test_replay_custody_failure_remains_visible_on_missing_final_retries(
    tmp_path, monkeypatch, interruption
):
    if os.name == "nt":
        pytest.skip("the regression exercises real POSIX custody descriptors")
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    _expire(conn, tmp_path, EXPIRY)
    _import(conn, tmp_path, payload)
    target = _audio(tmp_path, payload)
    ciphertext = target.read_bytes()
    [custody] = [path for path in _custody_files(_store(tmp_path)) if path.stat().st_size]
    if interruption == "missing_final":
        target.unlink()
    elif interruption == "replaced_custody":
        replacement = tmp_path / "replacement.enc"
        replacement.write_bytes(b"REPLACEMENT MUST SURVIVE")
        os.replace(replacement, custody)
    else:
        def fail_zero(_descriptors):
            raise OSError("injected custody zero failure")
        monkeypatch.setattr(meeting_retention, "_zero_opened_custody", fail_zero)

    result = _expire(conn, tmp_path, LATER)
    monkeypatch.undo()
    retried = _expire(conn, tmp_path, LATER)

    for outcome in (result, retried):
        assert outcome.swept == ()
        assert outcome.exit_code == 1
        assert [(failure.occurrence_id, failure.revision, failure.stage)
                for failure in outcome.failed] == [(payload["occurrence_id"], 3, "sweep")]
    assert not target.exists()
    assert custody.read_bytes() == (
        b"REPLACEMENT MUST SURVIVE" if interruption == "replaced_custody" else ciphertext)
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == EXPIRY


@pytest.mark.parametrize(("link_level", "failure_revision"), [
    ("occurrence", 0),
    ("revision", 3),
])
def test_a_revived_audio_symlink_is_visible_without_blocking_another_due_deletion(
    tmp_path, link_level, failure_revision
):
    conn = _db(tmp_path)
    revived = _payload(day="2026-08-20")
    due = _payload(day="2026-08-21")
    _import(conn, tmp_path, revived)
    _import(conn, tmp_path, due)
    revived_bytes = _audio(tmp_path, revived).read_bytes()
    assert _expire(conn, tmp_path, "2026-09-03T14:52:00Z").deleted == (
        (revived["occurrence_id"], 3),
    )

    occurrence = _store(tmp_path) / occurrence_path_segment(revived["occurrence_id"])
    shutil.rmtree(occurrence)  # the zeroed custody entry has no bytes after expiry
    if link_level == "occurrence":
        outside = tmp_path / "revived-occurrence"
        external_audio = outside / "3" / "audio.enc"
        external_audio.parent.mkdir(parents=True)
        external_audio.write_bytes(revived_bytes)
        occurrence.symlink_to(outside, target_is_directory=True)
    else:
        occurrence.mkdir()
        outside = tmp_path / "revived-revision"
        outside.mkdir()
        external_audio = outside / "audio.enc"
        external_audio.write_bytes(revived_bytes)
        (occurrence / "3").symlink_to(outside, target_is_directory=True)

    result = _expire(conn, tmp_path, "2026-09-04T14:52:00Z")

    assert result.deleted == ((due["occurrence_id"], 3),)
    assert not _audio(tmp_path, due).exists()
    assert external_audio.exists()
    assert [
        (failure.occurrence_id, failure.revision, failure.stage)
        for failure in result.failed
    ] == [(revived["occurrence_id"], failure_revision, "symlink")]
    assert result.exit_code == 1


# --- what survives ----------------------------------------------------------


def test_everything_but_the_encrypted_bytes_survives_the_deletion(tmp_path):
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    occurrence_id = payload["occurrence_id"]
    before = get_meeting(conn, occurrence_id)

    _expire(conn, tmp_path, EXPIRY)

    after = get_meeting(conn, occurrence_id)
    assert after["title"] == payload["title"]
    assert after["summary"] == payload["summary"]
    assert after["artifact_hash"] == before["artifact_hash"]
    assert after["transcript"] == before["transcript"]
    assert after["participants"] == before["participants"]
    assert after["decisions"] == before["decisions"]
    assert after["action_items"] == before["action_items"]
    assert after["provenance"] == before["provenance"]
    assert [row["revision"] for row in list_meeting_revisions(conn, occurrence_id)] == [
        payload["revision"]
    ]
    audio = after["audio"]
    assert audio["sha256"] == payload["audio"]["sha256"]
    assert audio["format"] == payload["audio"]["format"]
    assert audio["duration_seconds"] == payload["audio"]["duration_seconds"]
    assert audio["bytes"] == payload["audio"]["bytes"]
    assert audio["expires_at"] == EXPIRY
    assert audio["deleted_at"] == EXPIRY


# --- the pre-expiry alert ---------------------------------------------------


def test_an_incomplete_transcript_inside_the_window_is_alerted(tmp_path):
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)

    result = _expire(conn, tmp_path, "2026-09-14T00:00:00Z")

    assert result.alerted == (payload["occurrence_id"],)
    path = _alert_path(tmp_path)(payload["occurrence_id"])
    alert = json.loads(path.read_text(encoding="utf-8"))
    assert alert["occurrence_id"] == payload["occurrence_id"]
    assert alert["revision"] == payload["revision"]
    assert alert["audio_expires_at"] == EXPIRY
    assert alert["transcript_complete"] is False
    assert result.exit_code == 1
    assert _audio(tmp_path, payload).exists()       # warning is not deletion
    assert result.deleted == ()


def test_the_alert_carries_no_transcript_text(tmp_path):
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    payload["summary"] = TRANSCRIPT_SENTENCE
    payload["transcript"][0]["text"] = TRANSCRIPT_SENTENCE
    _import(conn, tmp_path, payload)

    _expire(conn, tmp_path, "2026-09-14T00:00:00Z")

    text = _alert_path(tmp_path)(payload["occurrence_id"]).read_text(encoding="utf-8")
    assert TRANSCRIPT_SENTENCE not in text
    assert payload["title"] not in text
    for segment in payload["transcript"]:
        assert segment["text"] not in text


def test_a_complete_transcript_inside_the_window_is_not_alerted(tmp_path):
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)

    result = _expire(conn, tmp_path, "2026-09-14T00:00:00Z")

    assert result.alerted == ()
    assert _files(_alerts(tmp_path)) == []
    assert result.exit_code == 0


def test_an_incomplete_transcript_before_the_window_is_not_alerted(tmp_path):
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)

    result = _expire(conn, tmp_path, "2026-09-13T14:51:59Z")

    assert result.alerted == ()
    assert _files(_alerts(tmp_path)) == []


def test_a_failed_alert_write_leaves_no_partial_file(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)

    def refuse(*_args, **_kwargs):
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(os, "replace", refuse)
    result = _expire(conn, tmp_path, "2026-09-14T00:00:00Z")

    assert result.alerted == ()
    assert [(f.occurrence_id, f.stage) for f in result.failed] == [
        (payload["occurrence_id"], "alert")
    ]
    assert sorted(p.name for p in _alerts(tmp_path).iterdir()) == []
    assert result.exit_code == 1


def test_an_alert_does_not_delay_a_deletion_due_in_the_same_run(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    due = _payload(day="2026-08-20")
    warned = _payload(transcript_complete=False)
    _import(conn, tmp_path, due)
    _import(conn, tmp_path, warned)

    def refuse(*_args, **_kwargs):
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(os, "replace", refuse)
    result = _expire(conn, tmp_path, "2026-09-14T00:00:00Z")

    assert result.deleted == ((due["occurrence_id"], due["revision"]),)
    assert not _audio(tmp_path, due).exists()
    assert get_meeting(conn, due["occurrence_id"])["audio"]["deleted_at"] == (
        "2026-09-14T00:00:00.000000Z"
    )
    assert [f.stage for f in result.failed] == ["alert"]


def test_dry_run_deletes_nothing_marks_nothing_and_writes_no_alert(tmp_path):
    conn = _db(tmp_path)
    due = _payload(day="2026-08-20")
    warned = _payload(transcript_complete=False)
    _import(conn, tmp_path, due)
    _import(conn, tmp_path, warned)

    result = _expire(conn, tmp_path, "2026-09-14T00:00:00Z", dry_run=True)

    assert result.deleted == ((due["occurrence_id"], due["revision"]),)
    assert result.alerted == (warned["occurrence_id"],)
    assert _audio(tmp_path, due).exists()
    assert _audio(tmp_path, warned).exists()
    assert get_meeting(conn, due["occurrence_id"])["audio"]["deleted_at"] is None
    assert _files(_alerts(tmp_path)) == []


def test_dry_run_removes_no_revived_audio(tmp_path):
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    _expire(conn, tmp_path, EXPIRY)
    _import(conn, tmp_path, payload)                # the replay revives the file

    result = _expire(conn, tmp_path, LATER, dry_run=True)

    assert result.swept == ((payload["occurrence_id"], payload["revision"]),)
    assert _audio(tmp_path, payload).exists()


# --- the command ------------------------------------------------------------


def _hub(tmp_path, monkeypatch) -> None:
    values = {
        "TEAMMEM_DB": str(tmp_path / "ledger.db"),
        "TEAMMEM_CONFIG_DIR": str(CONFIG_DIR),
        "TEAMMEM_VAULT": str(tmp_path / "vault"),
        "TEAMMEM_MEETING_INBOX": str(tmp_path / "meeting-inbox"),
        "TEAMMEM_MEETING_ARCHIVE": str(tmp_path / "meeting-archive"),
        "TEAMMEM_MEETING_QUARANTINE": str(tmp_path / "meeting-quarantine"),
        "TEAMMEM_MEETING_AUDIO_STORE": str(_store(tmp_path)),
        "TEAMMEM_MEETING_RECEIPTS": str(tmp_path / "meeting-receipts"),
        "TEAMMEM_MEETING_ALERTS": str(tmp_path / "meeting-alerts"),
        "TEAMMEM_VAULT_WEB_BASE": "https://git.example.com/team/team-vault",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_main_expire_meeting_audio_deletes_and_exits_zero(tmp_path, monkeypatch, capsys):
    _hub(tmp_path, monkeypatch)
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    conn.close()

    assert main(["expire-meeting-audio", "--now", EXPIRY]) == 0

    assert not _audio(tmp_path, payload).exists()
    assert payload["occurrence_id"] in capsys.readouterr().out


def test_main_expire_meeting_audio_exits_one_when_an_alert_is_written(
    tmp_path, monkeypatch, capsys
):
    _hub(tmp_path, monkeypatch)
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)
    conn.close()

    assert main(["expire-meeting-audio", "--now", "2026-09-14T00:00:00Z"]) == 1

    capsys.readouterr()
    alert = json.loads(
        (tmp_path / "meeting-alerts" /
         f"{occurrence_path_segment(payload['occurrence_id'])}.json"
         ).read_text(encoding="utf-8")
    )
    assert alert["audio_expires_at"] == EXPIRY


def test_main_expire_meeting_audio_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    _hub(tmp_path, monkeypatch)
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)
    conn.close()

    assert main(["expire-meeting-audio", "--now", EXPIRY, "--dry-run"]) == 0

    assert "dry-run" in capsys.readouterr().out
    assert _audio(tmp_path, payload).exists()
    assert not (tmp_path / "meeting-alerts").exists()


def test_main_expire_meeting_audio_without_configuration_exits_2(
    tmp_path, monkeypatch, capsys
):
    for key in ("TEAMMEM_MEETING_INBOX", "TEAMMEM_MEETING_ARCHIVE",
                "TEAMMEM_MEETING_QUARANTINE", "TEAMMEM_MEETING_AUDIO_STORE",
                "TEAMMEM_MEETING_RECEIPTS", "TEAMMEM_MEETING_ALERTS",
                "TEAMMEM_VAULT_WEB_BASE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TEAMMEM_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("TEAMMEM_CONFIG_DIR", str(CONFIG_DIR))

    assert main(["expire-meeting-audio", "--now", EXPIRY]) == 2
    assert "TEAMMEM_MEETING_AUDIO_STORE" in capsys.readouterr().err


def test_main_expire_meeting_audio_rejects_a_now_without_a_utc_offset(
    tmp_path, monkeypatch, capsys
):
    _hub(tmp_path, monkeypatch)

    assert main(["expire-meeting-audio", "--now", "2026-09-16T14:52:00"]) == 2
    assert "UTC offset" in capsys.readouterr().err


def test_the_warning_window_is_configurable(tmp_path, monkeypatch):
    _hub(tmp_path, monkeypatch)
    monkeypatch.setenv("TEAMMEM_MEETING_WARN_DAYS", "1")
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)
    conn.close()

    # Inside the three-day default, outside a one-day window.
    assert main(["expire-meeting-audio", "--now", "2026-09-14T00:00:00Z"]) == 0
    assert not any((tmp_path / "meeting-alerts").iterdir())

    assert main(["expire-meeting-audio", "--now", "2026-09-16T00:00:00Z"]) == 1
    assert any((tmp_path / "meeting-alerts").iterdir())


def test_expire_meeting_audio_does_not_need_a_vault_repository(tmp_path, monkeypatch):
    """Retention must keep its promise with no remote and no git at all: the
    deletion deadline cannot wait on a network."""
    _hub(tmp_path, monkeypatch)
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    conn.close()
    assert not (tmp_path / "vault").exists()

    assert main(["expire-meeting-audio", "--now", EXPIRY]) == 0
    assert not _audio(tmp_path, payload).exists()


def test_meeting_directories_are_created_privately_by_the_retention_run(
    tmp_path, monkeypatch
):
    _hub(tmp_path, monkeypatch)
    conn = _db(tmp_path)
    _import(conn, tmp_path, _payload())
    conn.close()

    assert main(["expire-meeting-audio", "--now", JUST_BEFORE]) == 0

    alerts = tmp_path / "meeting-alerts"
    assert alerts.is_dir()
    assert oct(alerts.stat().st_mode & 0o777) == "0o700"


def test_a_group_readable_alert_directory_is_refused(tmp_path, monkeypatch, capsys):
    _hub(tmp_path, monkeypatch)
    alerts = tmp_path / "meeting-alerts"
    alerts.mkdir(mode=0o755)

    assert main(["expire-meeting-audio", "--now", EXPIRY]) == 2
    assert "TEAMMEM_MEETING_ALERTS" in capsys.readouterr().err


def test_an_alert_that_cannot_be_written_at_all_still_leaves_the_audio_deleted(
    tmp_path, monkeypatch
):
    """Deletions are performed before any warning is attempted, so even an
    unhandled fault while warning cannot cost a meeting its deadline."""
    conn = _db(tmp_path)
    due = _payload(day="2026-08-20")
    warned = _payload(transcript_complete=False)
    _import(conn, tmp_path, due)
    _import(conn, tmp_path, warned)

    def explode(*_args, **_kwargs):
        raise RuntimeError("the alert spool is on fire")

    monkeypatch.setattr("teammem.meeting_retention._write_atomic", explode)
    with pytest.raises(RuntimeError):
        _expire(conn, tmp_path, "2026-09-14T00:00:00Z")

    assert not _audio(tmp_path, due).exists()
    assert get_meeting(conn, due["occurrence_id"])["audio"]["deleted_at"] == (
        "2026-09-14T00:00:00.000000Z"
    )


def test_the_window_opens_exactly_warn_days_before_expiry(tmp_path):
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)

    assert _expire(conn, tmp_path, "2026-09-13T14:52:00Z").alerted == (
        payload["occurrence_id"],
    )


def test_an_incomplete_transcript_at_expiry_is_deleted_rather_than_warned(tmp_path):
    """The window closes where the deadline begins: past it the loss has already
    happened and a warning would only invite someone to ask for an extension."""
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)

    result = _expire(conn, tmp_path, EXPIRY)

    assert result.alerted == ()
    assert result.deleted == ((payload["occurrence_id"], payload["revision"]),)
    assert not _audio(tmp_path, payload).exists()
    assert _files(_alerts(tmp_path)) == []


def test_a_moved_audio_store_still_destroys_the_bytes_the_row_records(tmp_path):
    """`stored_path` is not a guess: TeamMem wrote it. Changing the setting must
    not strand a recording on disk with only a daily failure line to say so."""
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    recorded = Path(get_meeting(conn, payload["occurrence_id"])["audio"]["stored_path"])
    assert recorded.exists()
    moved = tmp_path / "somewhere-else"           # the setting changed under the rows

    result = expire_meeting_audio(
        conn, moved, now=EXPIRY, alert_path=_alert_path(tmp_path))

    assert result.deleted == ((payload["occurrence_id"], payload["revision"]),)
    assert result.failed == ()
    assert not recorded.exists()
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == EXPIRY
    assert result.exit_code == 0
    # Pruning stops at the configured store, and this path is not under it, so
    # its empty directories are left to whoever moved them.
    assert recorded.parent.is_dir() and _store(tmp_path).is_dir()


def test_a_recorded_path_that_is_gone_is_marked_rather_than_failing_forever(tmp_path):
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    Path(get_meeting(conn, payload["occurrence_id"])["audio"]["stored_path"]).unlink()

    result = expire_meeting_audio(
        conn, tmp_path / "somewhere-else", now=EXPIRY,
        alert_path=_alert_path(tmp_path))

    assert result.deleted == ((payload["occurrence_id"], payload["revision"]),)
    assert result.failed == ()
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == EXPIRY


def test_a_symlinked_audio_directory_is_refused_rather_than_followed(tmp_path):
    """The sweep declines to walk into a linked directory; deletion declines to
    unlink through one, loudly, because that is tampering and not a setting."""
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    real = _audio(tmp_path, payload).parent
    elsewhere = tmp_path / "planted"
    elsewhere.mkdir()
    (elsewhere / "audio.enc").write_bytes(b"\x00\x01\x02" * 64)
    for entry in real.iterdir():
        entry.unlink()
    real.rmdir()
    real.symlink_to(elsewhere, target_is_directory=True)

    result = _expire(conn, tmp_path, EXPIRY)

    assert result.deleted == ()
    assert [(f.occurrence_id, f.stage) for f in result.failed] == [
        (payload["occurrence_id"], "symlink")
    ]
    assert (elsewhere / "audio.enc").exists()     # never unlinked through the link
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] is None
    assert result.exit_code == 1


def test_a_sweep_that_cannot_unlink_is_reported_and_retried(tmp_path, monkeypatch):
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    _expire(conn, tmp_path, EXPIRY)
    _import(conn, tmp_path, payload)                # the replay revives the file
    target = _audio(tmp_path, payload)
    real_unlink = os.unlink

    def refuse(path, *args, **kwargs):
        if Path(path) == target:
            raise OSError(errno.EACCES, "permission denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refuse)
    result = _expire(conn, tmp_path, LATER)

    assert result.swept == ()
    assert [(f.occurrence_id, f.stage) for f in result.failed] == [
        (payload["occurrence_id"], "sweep")
    ]
    assert target.exists()

    monkeypatch.undo()
    assert _expire(conn, tmp_path, LATER).swept == (
        (payload["occurrence_id"], payload["revision"]),
    )
    assert not target.exists()


def test_a_symlink_in_place_of_the_audio_is_reported_and_not_followed(tmp_path):
    """The link is what gets unlinked, so the bytes the row describes are not the
    ones destroyed — even when the target hashes exactly right."""
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    target = _audio(tmp_path, payload)
    outside = tmp_path / "somebody-elses.enc"
    outside.write_bytes(target.read_bytes())     # a perfect digest match
    target.unlink()
    target.symlink_to(outside)

    result = _expire(conn, tmp_path, EXPIRY)

    assert result.mismatched == ((payload["occurrence_id"], payload["revision"]),)
    assert not target.exists() and not target.is_symlink()
    assert outside.exists()                      # the link went, its target stayed
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] is None
    assert result.exit_code == 1


def test_audio_that_is_already_gone_is_not_warned_about(tmp_path):
    """A warning exists to save a source that can still be saved."""
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)
    mark_audio_deleted(
        conn, payload["occurrence_id"], payload["revision"], "2026-09-05T00:00:00Z")

    result = _expire(conn, tmp_path, "2026-09-14T00:00:00Z")

    assert result.alerted == ()
    assert _files(_alerts(tmp_path)) == []


def test_a_zero_day_window_warns_about_nothing(tmp_path):
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)

    result = _expire(conn, tmp_path, "2026-09-14T00:00:00Z", warn_days=0)

    assert result.alerted == ()
    assert _files(_alerts(tmp_path)) == []
    assert result.exit_code == 0
    assert _audio(tmp_path, payload).exists()    # and retention is not extended


# --- the alert spool holds only live warnings --------------------------------


def _alert_file(tmp_path: Path, payload: dict) -> Path:
    return _alert_path(tmp_path)(payload["occurrence_id"])


def test_an_alert_is_cleared_when_the_transcript_is_completed(tmp_path):
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)
    _expire(conn, tmp_path, "2026-09-14T00:00:00Z")
    assert _alert_file(tmp_path, payload).exists()

    # Backfill arrives the only way it can: as a higher revision.
    _import(conn, tmp_path, _payload(revision=payload["revision"] + 1))
    result = _expire(conn, tmp_path, "2026-09-15T00:00:00Z")

    assert result.cleared == (payload["occurrence_id"],)
    assert result.alerted == ()
    assert not _alert_file(tmp_path, payload).exists()
    assert result.exit_code == 0


def test_an_alert_is_cleared_when_the_audio_is_destroyed(tmp_path):
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)
    _expire(conn, tmp_path, "2026-09-14T00:00:00Z")
    assert _alert_file(tmp_path, payload).exists()

    result = _expire(conn, tmp_path, EXPIRY)

    assert result.deleted == ((payload["occurrence_id"], payload["revision"]),)
    assert result.cleared == (payload["occurrence_id"],)
    assert not _alert_file(tmp_path, payload).exists()


def test_a_zero_day_window_still_clears_a_standing_alert(tmp_path):
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)
    _expire(conn, tmp_path, "2026-09-14T00:00:00Z")

    result = _expire(conn, tmp_path, "2026-09-15T00:00:00Z", warn_days=0)

    assert result.cleared == (payload["occurrence_id"],)
    assert not _alert_file(tmp_path, payload).exists()


def test_a_standing_alert_is_left_alone_while_the_warning_still_applies(tmp_path):
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)
    _expire(conn, tmp_path, "2026-09-14T00:00:00Z")
    first = _alert_file(tmp_path, payload).read_bytes()

    result = _expire(conn, tmp_path, "2026-09-14T06:00:00Z")

    assert result.cleared == ()
    assert result.alerted == (payload["occurrence_id"],)
    assert _alert_file(tmp_path, payload).read_bytes() != first    # rewritten, not stale


def test_dry_run_clears_no_alert(tmp_path):
    conn = _db(tmp_path)
    payload = _payload(transcript_complete=False)
    _import(conn, tmp_path, payload)
    _expire(conn, tmp_path, "2026-09-14T00:00:00Z")

    result = _expire(conn, tmp_path, EXPIRY, dry_run=True)

    assert result.cleared == (payload["occurrence_id"],)
    assert _alert_file(tmp_path, payload).exists()


# --- one bad file costs exactly one meeting ----------------------------------


def test_an_unreadable_digest_still_deletes_and_never_stops_the_other_meetings(
    tmp_path, monkeypatch
):
    """A digest that cannot be read is not treated more leniently than one that
    is read and disagrees: the bytes still go, and every other occurrence due
    that day is still processed."""
    conn = _db(tmp_path)
    first = _payload(day="2026-08-20")
    second = _payload()
    _import(conn, tmp_path, first)
    _import(conn, tmp_path, second)
    blocked = _audio(tmp_path, first)
    real_digest = meeting_retention._digest_file

    def refuse(path, *args, **kwargs):
        if Path(path) == blocked:
            raise PermissionError(errno.EACCES, "permission denied")
        return real_digest(path, *args, **kwargs)

    monkeypatch.setattr(meeting_retention, "_digest_file", refuse)
    result = _expire(conn, tmp_path, EXPIRY)

    assert result.deleted == (
        (first["occurrence_id"], first["revision"]),
        (second["occurrence_id"], second["revision"]),
    )
    assert not blocked.exists() and not _audio(tmp_path, second).exists()
    assert get_meeting(conn, first["occurrence_id"])["audio"]["deleted_at"] == EXPIRY
    assert get_meeting(conn, second["occurrence_id"])["audio"]["deleted_at"] == EXPIRY
    assert [(f.occurrence_id, f.stage) for f in result.failed] == [
        (first["occurrence_id"], "digest")
    ]
    assert result.exit_code == 1


def test_an_unsearchable_directory_costs_one_meeting_and_not_the_run(
    tmp_path, monkeypatch
):
    """`Path.is_symlink` re-raises EACCES, so the probes before the unlink need a
    backstop of their own."""
    conn = _db(tmp_path)
    first = _payload(day="2026-08-20")
    second = _payload()
    _import(conn, tmp_path, first)
    _import(conn, tmp_path, second)
    blocked = _audio(tmp_path, first).parent
    real_is_symlink = Path.is_symlink

    def refuse(self):
        if self == blocked:
            raise PermissionError(errno.EACCES, "permission denied")
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", refuse)
    result = _expire(conn, tmp_path, EXPIRY)

    assert result.deleted == ((second["occurrence_id"], second["revision"]),)
    assert not _audio(tmp_path, second).exists()
    # The deletion pass and the janitor both trip over the same directory, and
    # both report it against that one occurrence rather than ending the run.
    assert [(f.occurrence_id, f.stage) for f in result.failed] == [
        (first["occurrence_id"], "path"),
        (first["occurrence_id"], "sweep"),
    ]
    assert get_meeting(conn, first["occurrence_id"])["audio"]["deleted_at"] is None
    assert result.exit_code == 1

    monkeypatch.undo()
    assert _expire(conn, tmp_path, LATER).deleted == (
        (first["occurrence_id"], first["revision"]),
    )


def test_an_audio_store_moved_on_disk_and_repointed_loses_its_bytes_in_one_run(
    tmp_path
):
    """The other half of a store move: the recorded path 404s, so the row is
    marked, and the sweep destroys the relocated ciphertext in the same run
    because it reads its own write on the same connection."""
    conn = _db(tmp_path)
    payload = _payload()
    _import(conn, tmp_path, payload)
    recorded = Path(get_meeting(conn, payload["occurrence_id"])["audio"]["stored_path"])
    moved = tmp_path / "meeting-audio-moved"
    shutil.move(str(_store(tmp_path)), str(moved))
    relocated = audio_revision_dir(
        moved, payload["occurrence_id"], payload["revision"]) / "audio.enc"
    assert relocated.exists() and not recorded.exists()

    result = expire_meeting_audio(
        conn, moved, now=EXPIRY, alert_path=_alert_path(tmp_path))

    key = (payload["occurrence_id"], payload["revision"])
    assert result.deleted == (key,)
    assert result.swept == (key,)
    assert not relocated.exists()
    assert result.failed == ()
    assert get_meeting(conn, payload["occurrence_id"])["audio"]["deleted_at"] == EXPIRY
    assert result.exit_code == 0


def test_an_unlistable_occurrence_directory_costs_only_the_janitor(
    tmp_path, monkeypatch
):
    """The sweep is a janitor with no deadline: a directory it cannot list is
    reported against that occurrence and the deletion that is due still lands."""
    conn = _db(tmp_path)
    due = _payload(day="2026-08-20")
    later = _payload()
    _import(conn, tmp_path, due)
    _import(conn, tmp_path, later)
    blocked = _audio(tmp_path, later).parent.parent      # not due, so still there
    real_iterdir = Path.iterdir

    def refuse(self):
        if self == blocked:
            raise PermissionError(errno.EACCES, "permission denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", refuse)
    result = _expire(conn, tmp_path, "2026-09-03T14:52:00Z")

    assert result.deleted == ((due["occurrence_id"], due["revision"]),)
    assert not _audio(tmp_path, due).exists()
    assert _audio(tmp_path, later).exists()
    assert [(f.occurrence_id, f.revision, f.stage) for f in result.failed] == [
        (later["occurrence_id"], 0, "sweep")     # 0: above any single revision
    ]
    assert result.exit_code == 1


# --- quarantined copies -----------------------------------------------------
# A rejected bundle is moved whole into the quarantine, ciphertext included.
# The fourteen-day promise covers every copy, so expiry must reach those too.


def _quarantine(tmp_path: Path) -> Path:
    return tmp_path / "meeting-quarantine"


def _quarantined_audio(tmp_path: Path) -> list[Path]:
    return [path for path in _files(_quarantine(tmp_path))
            if path.name == "audio.enc" and path.stat().st_size > 0]


def _quarantine_conflict(conn, tmp_path: Path) -> dict:
    """Publish revision 1, then redeliver revision 1 with a changed title."""
    payload = _payload()
    _import(conn, tmp_path, payload)
    conflicting = {**payload, "title": payload["title"] + " (resent)"}
    write_meeting_bundle(tmp_path / "meeting-inbox", conflicting)
    result = import_meeting_inbox(
        conn, tmp_path / "meeting-inbox", tmp_path / "meeting-archive",
        _quarantine(tmp_path), _store(tmp_path), vault_dir=tmp_path / "vault",
        now=IMPORTED_AT)
    assert [code for *_, code in result.details if code != "ok"] == ["revision_conflict"]
    assert len(_quarantined_audio(tmp_path)) == 1
    return payload


def test_quarantined_ciphertext_is_retained_before_expiry(tmp_path):
    conn = _db(tmp_path)
    _quarantine_conflict(conn, tmp_path)

    result = _expire(conn, tmp_path, JUST_BEFORE, quarantine=_quarantine(tmp_path))

    assert len(_quarantined_audio(tmp_path)) == 1
    assert result.quarantine_deleted == ()


def test_quarantined_ciphertext_is_deleted_at_expiry_with_every_other_copy(tmp_path):
    conn = _db(tmp_path)
    _quarantine_conflict(conn, tmp_path)

    result = _expire(conn, tmp_path, EXPIRY, quarantine=_quarantine(tmp_path))

    assert len(result.quarantine_deleted) == 1
    assert result.failed == ()
    leftovers = [path for path in _files(tmp_path)
                 if path.name.endswith("audio.enc") and path.stat().st_size > 0]
    assert leftovers == []
    # The reason file and the metadata stay: only the ciphertext is the promise.
    assert any(path.name.endswith(".reason.json") for path in _files(_quarantine(tmp_path)))
    assert _expire(conn, tmp_path, LATER,
                   quarantine=_quarantine(tmp_path)).quarantine_deleted == ()


def test_quarantine_dry_run_reports_and_keeps_the_ciphertext(tmp_path):
    conn = _db(tmp_path)
    _quarantine_conflict(conn, tmp_path)

    result = _expire(conn, tmp_path, EXPIRY, quarantine=_quarantine(tmp_path),
                     dry_run=True)

    assert len(result.quarantine_deleted) == 1
    assert len(_quarantined_audio(tmp_path)) == 1


def test_unparseable_quarantined_bundle_expires_from_its_audio_mtime(tmp_path):
    conn = _db(tmp_path)
    inbox = tmp_path / "meeting-inbox"
    for name in ("meeting-inbox", "meeting-archive", "meeting-quarantine", "meeting-audio"):
        (tmp_path / name).mkdir(mode=0o700, exist_ok=True)
    bundle = write_meeting_bundle(inbox, _payload())
    (bundle / "meeting.json").write_text("{not json", encoding="utf-8")
    written = _instant_seconds("2026-09-10T00:00:00Z")
    os.utime(bundle / "audio.enc", (written, written))
    result = import_meeting_inbox(
        conn, inbox, tmp_path / "meeting-archive", _quarantine(tmp_path), _store(tmp_path),
        vault_dir=tmp_path / "vault", now=IMPORTED_AT)
    assert result.quarantined == 1

    assert _expire(conn, tmp_path, "2026-09-23T23:59:59Z",
                   quarantine=_quarantine(tmp_path)).quarantine_deleted == ()
    assert len(_quarantined_audio(tmp_path)) == 1
    _expire(conn, tmp_path, "2026-09-24T00:00:00Z", quarantine=_quarantine(tmp_path))
    assert _quarantined_audio(tmp_path) == []


def test_a_symlink_in_the_quarantine_is_never_followed(tmp_path):
    conn = _db(tmp_path)
    outside = tmp_path / "outside.enc"
    outside.write_bytes(b"x" * 64)
    old = _instant_seconds("2026-01-01T00:00:00Z")
    os.utime(outside, (old, old))
    planted = _quarantine(tmp_path) / "seg" / "1"
    planted.mkdir(parents=True)
    (planted / "audio.enc").symlink_to(outside)

    _expire(conn, tmp_path, LATER, quarantine=_quarantine(tmp_path))

    assert outside.read_bytes() == b"x" * 64


def _instant_seconds(value: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
