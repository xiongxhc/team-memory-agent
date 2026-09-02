"""Meeting storage: migration, revisioned import, and the read API.

Every meeting row in this file comes from the frozen `teammem-meeting/v1`
fixtures through `load_meeting_bundle`, never from a hand-written dict, so the
schema is proven against the contract Task 1 froze rather than against prose.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from teammem import store as store_module
from teammem.meeting_bundles import canonical_artifact_hash, load_meeting_bundle
from teammem.meeting_store import (
    MEETING_AUDIO_RETENTION_DAYS,
    MEETING_TABLES,
    MeetingRevisionConflict,
    MeetingRevisionSuperseded,
    MeetingStoreError,
    StagedAudio,
    audio_due_for_expiry,
    audio_expires_at,
    get_meeting,
    import_meeting_revision,
    list_meeting_revisions,
    list_meetings,
    mark_audio_deleted,
    migrate_meetings,
)
from teammem.queries import events_between
from teammem.store import open_db, stats
from tests.test_meeting_bundles import write_meeting_bundle


FIXTURES = Path(__file__).parent / "fixtures" / "meetings"
COMPLETE = FIXTURES / "teammem-meeting-v1.complete.json"
MINIMAL = FIXTURES / "teammem-meeting-v1.minimal.json"

IMPORTED_AT = "2026-09-02T16:00:00Z"


def _payload(path: Path, *, day: str | None = None) -> dict:
    """Load a fixture, optionally shifting its whole meeting day.

    Every instant inside a fixture that belongs to the meeting window carries
    the date 2026-09-02, so a plain text substitution yields another valid
    occurrence without hand-editing occurrence_id, attendance and transcript
    timings out of agreement.
    """
    text = path.read_text(encoding="utf-8")
    if day is not None:
        text = text.replace("2026-09-02", day)
    return json.loads(text)


def _bundle(tmp_path: Path, payload: dict, name: str = "inbox"):
    inbox = tmp_path / name
    inbox.mkdir(exist_ok=True)
    directory = write_meeting_bundle(inbox, payload)
    return load_meeting_bundle(directory, inbox)


def _staged(bundle) -> StagedAudio:
    return StagedAudio(
        stored_path=f"audio/{bundle.occurrence_id}/{bundle.revision}/audio.enc",
        sha256=bundle.payload["audio"]["sha256"],
        bytes=bundle.payload["audio"]["bytes"],
    )


def _import(conn, bundle, *, imported_at: str = IMPORTED_AT, audio: StagedAudio | None = None):
    return import_meeting_revision(
        conn,
        payload=bundle.payload,
        occurrence_id=bundle.occurrence_id,
        revision=bundle.revision,
        artifact_hash=bundle.artifact_hash,
        audio=audio if audio is not None else _staged(bundle),
        imported_at=imported_at,
    )


def _counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in MEETING_TABLES}


# --- migration --------------------------------------------------------------


def test_open_db_creates_every_meeting_table(tmp_path):
    conn = open_db(tmp_path / "ledger.db")
    present = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert set(MEETING_TABLES) <= present
    assert "meeting_artifacts" in present and "meetings" in present


def test_migration_is_idempotent_and_leaves_no_broken_references(tmp_path):
    path = tmp_path / "ledger.db"
    conn = open_db(path)
    migrate_meetings(conn)          # a second run must not raise or duplicate
    conn.close()
    conn = open_db(path)            # and a reopen re-runs it on a populated file
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _counts(conn)["meeting_artifacts"] == 0


def test_migration_preserves_populated_events_and_summaries(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(store_module._SCHEMA)      # the pre-meeting schema
    legacy.executemany(
        "INSERT INTO events (person, project, ts, source, kind, summary, refs, raw, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("alex", "atlas", "2026-07-14T09:00:00Z", "gitlab", "commit", "fix auth",
             json.dumps({"url": "https://example.invalid/1"}), None, "sha-1"),
            ("bea", "borealis", "2026-07-15T10:30:00Z", "gitlab", "mr", "ship runbook",
             None, None, "sha-2"),
            ("alex", None, "2026-07-16T08:00:00Z", "feishu", "message", "standup note",
             None, None, "sha-3"),
        ],
    )
    legacy.execute(
        "INSERT INTO summaries (kind, key, input_hash, text, model, created_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("week", "Week 2026-07-13-17", "ih-1", "a quiet week", "test-model",
         "2026-07-18T00:00:00Z"),
    )
    legacy.commit()
    before_stats = stats(legacy)
    before_events = events_between(legacy, "2026-07-01T00:00:00", "2026-08-01T00:00:00")
    before_summaries = legacy.execute(
        "SELECT kind, key, input_hash, text, model, created_ts FROM summaries ORDER BY id"
    ).fetchall()
    legacy.close()

    conn = open_db(path)                             # migration runs here

    assert stats(conn) == before_stats
    assert events_between(conn, "2026-07-01T00:00:00", "2026-08-01T00:00:00") == before_events
    assert conn.execute(
        "SELECT kind, key, input_hash, text, model, created_ts FROM summaries ORDER BY id"
    ).fetchall() == before_summaries
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _counts(conn)["meetings"] == 0            # migration adds structure, not rows


# --- import: the happy path -------------------------------------------------


def test_import_stores_the_current_projection_and_the_artifact(tmp_path):
    conn = open_db(tmp_path / "l.db")
    bundle = _bundle(tmp_path, _payload(COMPLETE))

    result = _import(conn, bundle)

    assert result.status == "imported"
    assert (result.occurrence_id, result.revision) == (bundle.occurrence_id, 3)
    assert result.current_revision == 3
    row = conn.execute(
        "SELECT occurrence_id, current_revision, provider_meeting_id, title, started_at, "
        "ended_at, organizer_feishu_id, organizer_name, organizer_source, runtime_profile, "
        "transcript_complete FROM meetings"
    ).fetchall()
    assert row == [(
        "vc:9f3a/board-sync@2026-09-02T14:00:00Z", 3, "vc:9f3a/board-sync",
        "Atlas Launch Readiness", "2026-09-02T14:00:00Z", "2026-09-02T14:52:00Z",
        "ou_ada", "Ada Chen", "reservation", "online", 1,
    )]
    stored_hash, imported_at, raw_json = conn.execute(
        "SELECT artifact_hash, imported_at, raw_json FROM meeting_artifacts"
    ).fetchone()
    assert stored_hash == bundle.artifact_hash
    assert imported_at == IMPORTED_AT
    assert canonical_artifact_hash(json.loads(raw_json)) == bundle.artifact_hash


def test_import_stores_every_child_row_of_the_complete_fixture(tmp_path):
    conn = open_db(tmp_path / "l.db")
    payload = _payload(COMPLETE)
    _import(conn, _bundle(tmp_path, payload))

    counts = _counts(conn)
    assert counts["meeting_participants"] == 8
    assert counts["meeting_attendance"] == 9          # Chidi rejoined, so 8 people, 9 intervals
    assert counts["meeting_segments"] == 14
    assert counts["meeting_projects"] == 3
    assert counts["meeting_decisions"] == 3
    assert counts["meeting_action_items"] == 3
    assert counts["meeting_discussion_points"] == 3
    assert counts["meeting_person_summaries"] == 7    # Hana joined but never spoke
    assert counts["meeting_recording_gaps"] == 2
    assert counts["meeting_processing_warnings"] == 2
    assert counts["meeting_provenance"] == 1
    assert counts["meeting_audio"] == 1
    expected_evidence = sum(
        len(item["evidence_segment_ids"])
        for key in ("projects", "decisions", "action_items", "discussion_points")
        for item in payload[key]
    )
    assert counts["meeting_evidence"] == expected_evidence == 22


def test_get_meeting_round_trips_the_synthesis_of_the_complete_fixture(tmp_path):
    conn = open_db(tmp_path / "l.db")
    payload = _payload(COMPLETE)
    bundle = _bundle(tmp_path, payload)
    _import(conn, bundle)

    meeting = get_meeting(conn, bundle.occurrence_id)

    assert meeting["revision"] == 3 and meeting["is_current"] is True
    assert meeting["title"] == payload["title"]
    assert meeting["summary"] == payload["summary"]
    assert meeting["transcript_complete"] is True
    assert meeting["organizer"] == payload["organizer"]
    assert meeting["organizer_source"] == "reservation"
    assert meeting["organizer_unresolved_reason"] is None
    assert meeting["participants"] == payload["participants"]
    assert meeting["transcript"] == payload["transcript"]
    assert meeting["projects"] == payload["projects"]
    assert meeting["decisions"] == payload["decisions"]
    assert meeting["action_items"] == payload["action_items"]
    assert meeting["discussion_points"] == payload["discussion_points"]
    assert meeting["person_summaries"] == payload["person_summaries"]
    assert meeting["recording_gaps"] == payload["recording_gaps"]
    assert meeting["processing_warnings"] == payload["processing_warnings"]
    assert meeting["provenance"] == payload["provenance"]


def test_get_meeting_keeps_anonymous_and_ambiguous_speakers_distinct(tmp_path):
    conn = open_db(tmp_path / "l.db")
    bundle = _bundle(tmp_path, _payload(COMPLETE))
    _import(conn, bundle)

    speakers = {s["segment_id"]: s["speaker"] for s in get_meeting(conn, bundle.occurrence_id)["transcript"]}
    assert speakers["s008"] == {"kind": "anonymous", "cluster_id": "spk_3"}
    assert speakers["s009"] == {"kind": "ambiguous"}
    assert speakers["s001"]["person_id"] == "ou_ada" and speakers["s001"]["reconciled"] is True


def test_minimal_fixture_stores_a_null_organizer_with_its_reason(tmp_path):
    conn = open_db(tmp_path / "l.db")
    bundle = _bundle(tmp_path, _payload(MINIMAL))
    _import(conn, bundle)

    assert conn.execute(
        "SELECT organizer_feishu_id, organizer_name, organizer_source FROM meetings"
    ).fetchone() == (None, None, None)
    meeting = get_meeting(conn, bundle.occurrence_id)
    assert meeting["organizer"] is None
    assert meeting["organizer_unresolved_reason"] == "instant_meeting"
    assert meeting["projects"] == [] and meeting["person_summaries"] == []


def test_get_meeting_returns_none_for_an_unknown_occurrence(tmp_path):
    conn = open_db(tmp_path / "l.db")
    assert get_meeting(conn, "vc:none/absent@2026-09-02T14:00:00Z") is None


# --- revisions --------------------------------------------------------------


def test_same_revision_same_hash_is_a_no_op(tmp_path):
    conn = open_db(tmp_path / "l.db")
    bundle = _bundle(tmp_path, _payload(COMPLETE))
    _import(conn, bundle)
    before = _counts(conn)

    result = _import(conn, bundle, imported_at="2026-09-03T08:00:00Z")

    assert result.status == "unchanged"
    assert result.current_revision == 3
    assert _counts(conn) == before
    assert conn.execute("SELECT imported_at FROM meeting_artifacts").fetchone()[0] == IMPORTED_AT


def test_same_revision_with_a_different_hash_is_rejected(tmp_path):
    conn = open_db(tmp_path / "l.db")
    bundle = _bundle(tmp_path, _payload(COMPLETE))
    _import(conn, bundle)
    before = _counts(conn)

    edited = _payload(COMPLETE)
    edited["title"] = "Atlas Launch Readiness (edited)"
    tampered = _bundle(tmp_path, edited, name="inbox2")
    assert tampered.artifact_hash != bundle.artifact_hash

    with pytest.raises(MeetingRevisionConflict):
        _import(conn, tampered)
    assert _counts(conn) == before
    assert conn.execute("SELECT title FROM meetings").fetchone()[0] == "Atlas Launch Readiness"


def test_a_lower_revision_is_rejected(tmp_path):
    conn = open_db(tmp_path / "l.db")
    _import(conn, _bundle(tmp_path, _payload(COMPLETE)))
    before = _counts(conn)

    older = _payload(COMPLETE)
    older["revision"] = 2
    with pytest.raises(MeetingRevisionSuperseded):
        _import(conn, _bundle(tmp_path, older, name="inbox2"))
    assert _counts(conn) == before
    assert conn.execute("SELECT current_revision FROM meetings").fetchone()[0] == 3


def test_a_higher_revision_becomes_current_and_the_prior_one_stays_queryable(tmp_path):
    conn = open_db(tmp_path / "l.db")
    first = _bundle(tmp_path, _payload(COMPLETE))
    _import(conn, first)

    newer = _payload(COMPLETE)
    newer["revision"] = 4
    newer["title"] = "Atlas Launch Readiness (final)"
    newer["decisions"] = newer["decisions"][:1]
    second = _bundle(tmp_path, newer, name="inbox2")
    result = _import(conn, second, imported_at="2026-09-04T09:00:00Z")

    assert result.status == "imported" and result.current_revision == 4
    assert conn.execute("SELECT current_revision, title FROM meetings").fetchone() == (
        4, "Atlas Launch Readiness (final)")
    assert conn.execute("SELECT COUNT(*) FROM meeting_artifacts").fetchone()[0] == 2

    current = get_meeting(conn, first.occurrence_id)
    assert current["revision"] == 4 and len(current["decisions"]) == 1

    prior = get_meeting(conn, first.occurrence_id, revision=3)
    assert prior["is_current"] is False
    assert prior["revision"] == 3
    assert prior["title"] == "Atlas Launch Readiness"
    assert len(prior["decisions"]) == 3                 # provenance survives the newer revision
    assert len(prior["transcript"]) == 14
    assert prior["artifact_hash"] == first.artifact_hash


def test_list_meeting_revisions_reports_both_revisions_newest_first(tmp_path):
    conn = open_db(tmp_path / "l.db")
    first = _bundle(tmp_path, _payload(COMPLETE))
    _import(conn, first)
    newer = _payload(COMPLETE)
    newer["revision"] = 4
    _import(conn, _bundle(tmp_path, newer, name="inbox2"), imported_at="2026-09-04T09:00:00Z")

    revisions = list_meeting_revisions(conn, first.occurrence_id)
    assert [r["revision"] for r in revisions] == [4, 3]
    assert [r["is_current"] for r in revisions] == [True, False]
    assert revisions[1]["artifact_hash"] == first.artifact_hash
    assert revisions[1]["imported_at"] == IMPORTED_AT


def test_an_artifact_hash_already_held_by_another_revision_is_rejected(tmp_path):
    conn = open_db(tmp_path / "l.db")
    bundle = _bundle(tmp_path, _payload(COMPLETE))
    _import(conn, bundle)
    before = _counts(conn)

    with pytest.raises(MeetingRevisionConflict):
        import_meeting_revision(
            conn,
            payload=bundle.payload,
            occurrence_id=bundle.occurrence_id,
            revision=9,                                  # a new revision reusing an old hash
            artifact_hash=bundle.artifact_hash,
            audio=_staged(bundle),
            imported_at=IMPORTED_AT,
        )
    assert _counts(conn) == before


# --- listing ----------------------------------------------------------------


def test_list_meetings_returns_current_occurrences_in_start_order(tmp_path):
    conn = open_db(tmp_path / "l.db")
    _import(conn, _bundle(tmp_path, _payload(COMPLETE)))
    _import(conn, _bundle(tmp_path, _payload(MINIMAL), name="inbox2"))

    rows = list_meetings(conn)
    assert [r["title"] for r in rows] == ["Instant Sync", "Atlas Launch Readiness"]
    assert [r["started_at"] for r in rows] == ["2026-09-02T09:15:00Z", "2026-09-02T14:00:00Z"]
    assert rows[1]["current_revision"] == 3
    assert rows[1]["summary"].startswith("The team walked the Atlas launch checklist")
    assert rows[0]["transcript_complete"] is True


def test_list_meetings_filters_by_a_half_open_date_range(tmp_path):
    conn = open_db(tmp_path / "l.db")
    _import(conn, _bundle(tmp_path, _payload(MINIMAL), name="a"))                 # 09-02
    _import(conn, _bundle(tmp_path, _payload(MINIMAL, day="2026-09-05"), name="b"))
    _import(conn, _bundle(tmp_path, _payload(MINIMAL, day="2026-09-09"), name="c"))

    week = list_meetings(conn, start="2026-09-05T00:00:00Z", end="2026-09-09T00:00:00Z")
    assert [r["started_at"] for r in week] == ["2026-09-05T09:15:00Z"]
    assert [r["started_at"] for r in list_meetings(conn, start="2026-09-05T00:00:00Z")] == [
        "2026-09-05T09:15:00Z", "2026-09-09T09:15:00Z"]
    assert [r["started_at"] for r in list_meetings(conn, end="2026-09-05T00:00:00Z")] == [
        "2026-09-02T09:15:00Z"]


# --- foreign keys -----------------------------------------------------------


def test_foreign_key_check_is_empty_after_imports(tmp_path):
    conn = open_db(tmp_path / "l.db")
    _import(conn, _bundle(tmp_path, _payload(COMPLETE)))
    _import(conn, _bundle(tmp_path, _payload(MINIMAL), name="inbox2"))
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_child_row_without_its_artifact_violates_a_foreign_key(tmp_path):
    conn = open_db(tmp_path / "l.db")
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO meeting_provenance (occurrence_id, revision, runtime_profile, "
            "stt_provider, stt_model, text_provider, text_model, agent_version, processed_at) "
            "VALUES ('vc:ghost/x@2026-09-02T14:00:00Z', 1, 'online', 'p', 'm', 'p', 'm', 'v', "
            "'2026-09-02T15:00:00Z')")
    conn.rollback()


def test_an_orphan_attendance_interval_violates_its_participant_foreign_key(tmp_path):
    conn = open_db(tmp_path / "l.db")
    bundle = _bundle(tmp_path, _payload(COMPLETE))
    _import(conn, bundle)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO meeting_attendance (occurrence_id, revision, person_id, position, "
            "joined_at, left_at) VALUES (?, 3, 'ou_nobody', 0, '2026-09-02T14:00:00Z', NULL)",
            (bundle.occurrence_id,))
    conn.rollback()


def test_import_does_not_globally_enable_foreign_keys(tmp_path):
    conn = open_db(tmp_path / "l.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0   # open_db leaves them off
    _import(conn, _bundle(tmp_path, _payload(COMPLETE)))
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0   # and import restores that


def test_import_keeps_a_callers_enabled_foreign_keys_enabled(tmp_path):
    conn = open_db(tmp_path / "l.db")
    conn.execute("PRAGMA foreign_keys = ON")
    _import(conn, _bundle(tmp_path, _payload(COMPLETE)))
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# --- transactionality -------------------------------------------------------


def test_a_failure_partway_through_child_inserts_writes_nothing(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "l.db")
    bundle = _bundle(tmp_path, _payload(COMPLETE))

    import teammem.meeting_store as meeting_store

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure after the transcript was written")

    monkeypatch.setattr(meeting_store, "_insert_person_summaries", boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        _import(conn, bundle)

    assert _counts(conn) == {table: 0 for table in MEETING_TABLES}
    assert conn.execute("SELECT COUNT(*) FROM meeting_artifacts").fetchone()[0] == 0
    assert get_meeting(conn, bundle.occurrence_id) is None
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_failed_higher_revision_leaves_the_previous_revision_current(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "l.db")
    _import(conn, _bundle(tmp_path, _payload(COMPLETE)))
    before = _counts(conn)

    import teammem.meeting_store as meeting_store

    monkeypatch.setattr(meeting_store, "_insert_audio", lambda *a, **k: 1 / 0)
    newer = _payload(COMPLETE)
    newer["revision"] = 4
    with pytest.raises(ZeroDivisionError):
        _import(conn, _bundle(tmp_path, newer, name="inbox2"))

    assert _counts(conn) == before
    assert conn.execute("SELECT current_revision FROM meetings").fetchone()[0] == 3


def test_staged_audio_disagreeing_with_the_payload_is_rejected(tmp_path):
    conn = open_db(tmp_path / "l.db")
    bundle = _bundle(tmp_path, _payload(COMPLETE))
    wrong = StagedAudio(stored_path="audio/x.enc", sha256="sha256:" + "0" * 64, bytes=2048)

    with pytest.raises(MeetingStoreError):
        _import(conn, bundle, audio=wrong)
    assert _counts(conn) == {table: 0 for table in MEETING_TABLES}


# --- audio retention --------------------------------------------------------


def test_audio_expires_fourteen_days_after_ended_at_not_when_the_payload_asks(tmp_path):
    conn = open_db(tmp_path / "l.db")
    payload = _payload(COMPLETE)
    assert payload["audio"]["expires_at"] == "2026-10-02T14:52:00Z"   # 30 days, deliberately long
    bundle = _bundle(tmp_path, payload)

    result = _import(conn, bundle)

    ended = datetime(2026, 9, 2, 14, 52, tzinfo=timezone.utc)
    expected = (ended + timedelta(days=MEETING_AUDIO_RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert expected == "2026-09-16T14:52:00Z"
    assert result.audio_expires_at == expected
    stored, declared = conn.execute(
        "SELECT audio_expires_at, declared_expires_at FROM meeting_audio").fetchone()
    assert stored == expected
    assert declared == "2026-10-02T14:52:00Z"                          # kept only as provenance
    assert get_meeting(conn, bundle.occurrence_id)["audio"]["expires_at"] == expected


def test_audio_expires_at_helper_matches_the_stored_value(tmp_path):
    assert audio_expires_at("2026-09-02T14:52:00Z") == "2026-09-16T14:52:00Z"
    assert audio_expires_at("2026-09-02T14:52:00.500000Z") == "2026-09-16T14:52:00Z"


def test_audio_due_for_expiry_lists_only_rows_at_or_past_their_expiry(tmp_path):
    conn = open_db(tmp_path / "l.db")
    complete = _bundle(tmp_path, _payload(COMPLETE))       # expires 2026-09-16T14:52:00Z
    minimal = _bundle(tmp_path, _payload(MINIMAL), name="inbox2")   # expires 2026-09-16T09:22:00Z
    _import(conn, complete)
    _import(conn, minimal)

    assert audio_due_for_expiry(conn, "2026-09-16T09:00:00Z") == []
    due = audio_due_for_expiry(conn, "2026-09-16T10:00:00Z")
    assert [(r["occurrence_id"], r["revision"]) for r in due] == [(minimal.occurrence_id, 1)]
    assert due[0]["stored_path"].endswith("audio.enc")
    assert due[0]["audio_expires_at"] == "2026-09-16T09:22:00Z"

    both = audio_due_for_expiry(conn, "2026-09-17T00:00:00Z")
    assert [r["audio_expires_at"] for r in both] == [
        "2026-09-16T09:22:00Z", "2026-09-16T14:52:00Z"]


def test_marking_audio_deleted_removes_it_from_the_expiry_queue(tmp_path):
    conn = open_db(tmp_path / "l.db")
    bundle = _bundle(tmp_path, _payload(MINIMAL))
    _import(conn, bundle)

    assert mark_audio_deleted(conn, bundle.occurrence_id, 1, "2026-09-17T01:00:00Z") is True
    assert audio_due_for_expiry(conn, "2026-09-17T00:00:00Z") == []
    assert mark_audio_deleted(conn, bundle.occurrence_id, 1, "2026-09-18T01:00:00Z") is False
    assert conn.execute("SELECT deleted_at FROM meeting_audio").fetchone()[0] == \
        "2026-09-17T01:00:00Z"
    assert get_meeting(conn, bundle.occurrence_id)["audio"]["deleted_at"] == \
        "2026-09-17T01:00:00Z"
    assert get_meeting(conn, bundle.occurrence_id)["transcript"], \
        "deleting audio must not touch the transcript"


def test_a_prior_revisions_audio_expires_on_its_own_schedule(tmp_path):
    conn = open_db(tmp_path / "l.db")
    _import(conn, _bundle(tmp_path, _payload(COMPLETE)))
    newer = _payload(COMPLETE)
    newer["revision"] = 4
    _import(conn, _bundle(tmp_path, newer, name="inbox2"), imported_at="2026-09-04T09:00:00Z")

    due = audio_due_for_expiry(conn, "2026-09-17T00:00:00Z")
    assert [(r["revision"], r["is_current"]) for r in due] == [(3, False), (4, True)]


def test_foreign_keys_are_enforced_inside_the_import_transaction(tmp_path, monkeypatch):
    """Prove the import scope enforces them even though open_db leaves them off.

    Dropping the participants makes every person summary an orphan, which only
    fails if the connection is enforcing foreign keys while the transaction runs.
    """
    conn = open_db(tmp_path / "l.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0

    import teammem.meeting_store as meeting_store

    monkeypatch.setattr(meeting_store, "_insert_participants", lambda *a, **k: None)

    with pytest.raises(sqlite3.IntegrityError):
        _import(conn, _bundle(tmp_path, _payload(COMPLETE)))
    assert _counts(conn) == {table: 0 for table in MEETING_TABLES}
