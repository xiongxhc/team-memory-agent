import copy
import hashlib
import json
from pathlib import Path

import pytest

from teammem.meeting_bundles import (
    MEETING_SCHEMA,
    MANIFEST_SCHEMA,
    MeetingBundleError,
    canonical_artifact_hash,
    load_meeting_bundle,
    occurrence_path_segment,
    parse_occurrence_id,
    short_occurrence_id,
)


FIXTURES = Path(__file__).parent / "fixtures" / "meetings"
COMPLETE = FIXTURES / "teammem-meeting-v1.complete.json"
MINIMAL = FIXTURES / "teammem-meeting-v1.minimal.json"
V2_MINIMAL = FIXTURES / "teammem-meeting-v2.minimal.json"
V2_OVERLAP = FIXTURES / "teammem-meeting-v2.overlap.json"


def _fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _audio_bytes(size: int) -> bytes:
    """Deterministic stand-in for encrypted audio.

    Starts with 0x00 0x01 0x02 ... so it collides with none of the audio
    container magic numbers the loader screens for. Never committed as a blob.
    """
    assert size % 256 == 0, "fixture audio sizes are whole 256-byte cycles"
    return bytes(range(256)) * (size // 256)


def _set_audio(payload: dict, data: bytes) -> None:
    """Keep the payload's declared audio facts true for `data`."""
    payload["audio"]["bytes"] = len(data)
    payload["audio"]["sha256"] = "sha256:" + hashlib.sha256(data).hexdigest()


def write_meeting_bundle(
    inbox: Path,
    payload: dict,
    *,
    audio: bytes | None = None,
    manifest_overrides: dict | None = None,
    manifest_drop: tuple[str, ...] = (),
    segment: str | None = None,
    revision_dir: str | None = None,
    omit: tuple[str, ...] = (),
    extra: dict[str, bytes] | None = None,
) -> Path:
    """Materialize <inbox>/<segment>/<revision>/ from a payload. Returns that dir.

    Later meeting tasks should copy this helper rather than hand-rolling bundles.
    """
    seg = segment if segment is not None else occurrence_path_segment(payload["occurrence_id"])
    rev = revision_dir if revision_dir is not None else str(payload["revision"])
    directory = inbox / seg / rev
    directory.mkdir(parents=True, exist_ok=True)

    data = audio if audio is not None else _audio_bytes(payload["audio"]["bytes"])
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "occurrence_id": payload["occurrence_id"],
        "revision": payload["revision"],
        "artifact_hash": canonical_artifact_hash(payload),
        "audio_sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
        "audio_bytes": len(data),
    }
    manifest.update(manifest_overrides or {})
    for key in manifest_drop:
        manifest.pop(key, None)

    if "meeting.json" not in omit:
        (directory / "meeting.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    if "audio.enc" not in omit:
        (directory / "audio.enc").write_bytes(data)
    if "manifest.json" not in omit:
        (directory / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    if "READY" not in omit:
        (directory / "READY").write_bytes(b"")
    for name, blob in (extra or {}).items():
        (directory / name).write_bytes(blob)
    return directory


def _load(tmp_path, payload, **kwargs):
    inbox = tmp_path / "meeting-inbox"
    directory = write_meeting_bundle(inbox, payload, **kwargs)
    return load_meeting_bundle(directory, inbox)


def _reject(tmp_path, payload, **kwargs):
    with pytest.raises(MeetingBundleError) as excinfo:
        _load(tmp_path, payload, **kwargs)
    return str(excinfo.value)


# --- identifier helpers -----------------------------------------------------


def test_parse_occurrence_id_splits_meeting_id_and_utc_start():
    meeting_id, start = parse_occurrence_id("vc:9f3a/board-sync@2026-09-02T14:00:00Z")
    assert meeting_id == "vc:9f3a/board-sync"      # ':' and '/' survive in the logical id
    assert start.isoformat() == "2026-09-02T14:00:00+00:00"


def test_parse_occurrence_id_rejects_bad_forms():
    for bad in [
        "vc:9f3a/board-sync",                       # no '@'
        "@2026-09-02T14:00:00Z",                    # empty meeting id
        "vc:9f3a@2026-09-02T14:00:00",              # not UTC-marked
        "vc:9f3a@2026-09-02T14:00:00.500Z",         # fractional seconds in a path source
        "vc:9f3a@2026-02-30T14:00:00Z",             # not a real date
    ]:
        with pytest.raises(MeetingBundleError):
            parse_occurrence_id(bad)


def test_occurrence_path_segment_encodes_every_path_hostile_character():
    segment = occurrence_path_segment("vc:9f3a/board-sync@2026-09-02T14:00:00Z")
    assert segment == "vc%3A9f3a%2Fboard-sync_20260902T140000Z"
    assert ":" not in segment and "/" not in segment and "@" not in segment  # Windows-legal


def test_short_occurrence_id_is_eight_hex_chars_of_the_segment_hash():
    occurrence = "vc:9f3a/board-sync@2026-09-02T14:00:00Z"
    expected = hashlib.sha256(
        occurrence_path_segment(occurrence).encode("utf-8")
    ).hexdigest()[:8]
    assert short_occurrence_id(occurrence) == expected
    assert len(expected) == 8 and expected == expected.lower()


def test_short_occurrence_id_differs_per_occurrence_start():
    first = short_occurrence_id("vc:9f3a@2026-09-02T14:00:00Z")
    second = short_occurrence_id("vc:9f3a@2026-09-09T14:00:00Z")
    assert first != second                          # recurring meetings get distinct notes


# --- accepted bundles -------------------------------------------------------


def test_complete_bundle_loads_with_recomputed_hash(tmp_path):
    payload = _fixture(COMPLETE)
    bundle = _load(tmp_path, payload)
    assert bundle.occurrence_id == "vc:9f3a/board-sync@2026-09-02T14:00:00Z"
    assert bundle.revision == 3
    assert bundle.artifact_hash == canonical_artifact_hash(payload)
    assert bundle.payload == payload
    assert bundle.audio_path.name == "audio.enc"
    assert bundle.audio_path.read_bytes() == _audio_bytes(2048)
    assert bundle.path.name == "3"


def test_minimal_bundle_has_null_organizer_with_instant_meeting_reason(tmp_path):
    payload = _fixture(MINIMAL)
    bundle = _load(tmp_path, payload)
    assert bundle.payload["organizer"] is None
    assert bundle.payload["organizer_source"] is None
    assert bundle.payload["organizer_unresolved_reason"] == "instant_meeting"
    assert bundle.revision == 1


def test_v2_bundle_loads_overlapping_observations_without_losing_facts(tmp_path):
    payload = _fixture(MINIMAL)
    payload["schema"] = "teammem-meeting/v2"
    payload["transcript"] = [
        {
            "segment_id": "obs-live",
            "started_at": "2026-09-02T09:15:00Z",
            "ended_at": "2026-09-02T09:15:00.020000Z",
            "text": "Live wording retained.",
            "speaker": {"kind": "anonymous", "cluster_id": "live-1"},
            "attribution_confidence": 0.61,
            "provenance": {"stage": "live_stream", "model": "live-stt-1"},
        },
        {
            "segment_id": "obs-final",
            "started_at": "2026-09-02T09:15:00.001000Z",
            "ended_at": "2026-09-02T09:15:00.019000Z",
            "text": "Final wording retained.",
            "speaker": {"kind": "anonymous", "cluster_id": "final-1"},
            "attribution_confidence": 0.97,
            "provenance": {"stage": "final_batch", "model": "final-stt-2"},
        },
    ]

    bundle = _load(tmp_path, payload)

    assert bundle.payload["transcript"] == payload["transcript"]
    assert bundle.payload["transcript"][0]["text"] == "Live wording retained."
    assert bundle.payload["transcript"][1]["text"] == "Final wording retained."


def test_shared_v2_fixtures_load_with_literal_canonical_hashes(tmp_path):
    expected = {
        V2_MINIMAL: "sha256:e243b84f155324483fabb4d88bf07af2beab6639c0c2555fecfe082a6126a7cc",
        V2_OVERLAP: "sha256:4efa7b78605d8c0a60a5515adda74181071079ab2bdebbfec69662a334cda9fa",
    }
    for index, (path, expected_hash) in enumerate(expected.items()):
        payload = _fixture(path)
        bundle = _load(tmp_path / str(index), payload)
        assert payload["schema"] == "teammem-meeting/v2"
        assert bundle.artifact_hash == expected_hash


def test_canonical_hash_includes_schema_and_original_transcript_order():
    v1 = _fixture(MINIMAL)
    v2 = copy.deepcopy(v1)
    v2["schema"] = "teammem-meeting/v2"
    reordered = _fixture(V2_OVERLAP)
    original_hash = canonical_artifact_hash(reordered)
    reordered["transcript"].reverse()

    assert canonical_artifact_hash(v1) != canonical_artifact_hash(v2)
    assert canonical_artifact_hash(reordered) != original_hash


def test_complete_fixture_shape_is_the_cross_repository_contract():
    payload = _fixture(COMPLETE)
    assert payload["schema"] == MEETING_SCHEMA
    assert payload["organizer_source"] in {"reservation", "calendar_event"}
    assert len(payload["participants"]) == 8
    assert len(payload["projects"]) == 3
    spoke = {
        segment["speaker"]["person_id"]
        for segment in payload["transcript"]
        if segment["speaker"]["kind"] == "person"
    }
    silent = [p["person_id"] for p in payload["participants"] if p["person_id"] not in spoke]
    assert silent == ["ou_hana"]                    # joined, never spoke
    assert len(payload["person_summaries"]) == 7    # every speaker, no silent joiner
    assert any(s["speaker"]["kind"] == "anonymous" for s in payload["transcript"])
    assert any(s["speaker"]["kind"] == "ambiguous" for s in payload["transcript"])
    owners = [item["owner"] for item in payload["action_items"]]
    assert any(owner is None for owner in owners)
    assert any(owner is not None for owner in owners)
    assert any(item["due_date"] is not None for item in payload["action_items"])
    assert any(item["due_date"] is None for item in payload["action_items"])
    assert payload["recording_gaps"] and payload["processing_warnings"]


def test_fixture_audio_hashes_match_the_generated_bytes():
    for fixture, size in ((COMPLETE, 2048), (MINIMAL, 512)):
        payload = _fixture(fixture)
        data = _audio_bytes(size)
        assert payload["audio"]["bytes"] == size
        assert payload["audio"]["sha256"] == "sha256:" + hashlib.sha256(data).hexdigest()


def test_artifact_hash_ignores_meeting_json_formatting(tmp_path):
    payload = _fixture(MINIMAL)
    inbox = tmp_path / "meeting-inbox"
    directory = write_meeting_bundle(inbox, payload)
    (directory / "meeting.json").write_text(
        json.dumps(payload, indent=8, sort_keys=True), encoding="utf-8"
    )
    assert load_meeting_bundle(directory, inbox).artifact_hash == canonical_artifact_hash(payload)


def test_artifact_hash_changes_when_any_field_changes(tmp_path):
    payload = _fixture(MINIMAL)
    first = _load(tmp_path, payload).artifact_hash
    higher = copy.deepcopy(payload)
    higher["revision"] = 2
    second = _load(tmp_path, higher).artifact_hash
    assert first != second
    assert first.startswith("sha256:") and len(first) == len("sha256:") + 64


def test_caller_supplied_expiry_is_recorded_but_longer_than_the_teammem_rule(tmp_path):
    # ended_at + 14 days is 2026-09-16; the fixture asks for 2026-10-02 on purpose.
    bundle = _load(tmp_path, _fixture(COMPLETE))
    assert bundle.payload["audio"]["expires_at"] == "2026-10-02T14:52:00Z"
    assert bundle.payload["ended_at"] == "2026-09-02T14:52:00Z"


def test_optional_structures_may_all_be_empty(tmp_path):
    payload = _fixture(MINIMAL)
    for key in (
        "projects",
        "decisions",
        "action_items",
        "discussion_points",
        "person_summaries",
        "recording_gaps",
        "processing_warnings",
        "transcript",
    ):
        payload[key] = []
    assert _load(tmp_path, payload).payload["transcript"] == []


def test_anonymous_and_ambiguous_speakers_need_no_participant(tmp_path):
    payload = _fixture(MINIMAL)
    payload["transcript"][0]["speaker"] = {"kind": "anonymous", "cluster_id": "spk_1"}
    payload["person_summaries"] = []
    assert _load(tmp_path, payload).payload["transcript"][0]["speaker"]["cluster_id"] == "spk_1"


# --- container / path rejects ----------------------------------------------


@pytest.mark.parametrize("missing", ["READY", "manifest.json", "audio.enc", "meeting.json"])
def test_missing_required_file_is_rejected(tmp_path, missing):
    message = _reject(tmp_path, _fixture(MINIMAL), omit=(missing,))
    assert missing in message


def test_unexpected_file_in_the_bundle_is_rejected(tmp_path):
    message = _reject(tmp_path, _fixture(MINIMAL), extra={"notes.txt": b"hi"})
    assert "notes.txt" in message


def test_symlinked_bundle_directory_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    real = tmp_path / "elsewhere"
    directory = write_meeting_bundle(real, payload)
    inbox = tmp_path / "meeting-inbox"
    link_parent = inbox / occurrence_path_segment(payload["occurrence_id"])
    link_parent.mkdir(parents=True)
    link = link_parent / str(payload["revision"])
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(MeetingBundleError, match="symlink"):
        load_meeting_bundle(link, inbox)


def test_symlinked_audio_file_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    inbox = tmp_path / "meeting-inbox"
    directory = write_meeting_bundle(inbox, payload)
    target = tmp_path / "outside.enc"
    target.write_bytes(_audio_bytes(512))
    (directory / "audio.enc").unlink()
    (directory / "audio.enc").symlink_to(target)
    with pytest.raises(MeetingBundleError, match="symlink"):
        load_meeting_bundle(directory, inbox)


def test_bundle_outside_the_inbox_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    directory = write_meeting_bundle(tmp_path / "elsewhere", payload)
    inbox = tmp_path / "meeting-inbox"
    inbox.mkdir()
    with pytest.raises(MeetingBundleError, match="outside"):
        load_meeting_bundle(directory, inbox)


def test_wrong_depth_under_the_inbox_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    inbox = tmp_path / "meeting-inbox"
    directory = write_meeting_bundle(inbox / "extra", payload)
    with pytest.raises(MeetingBundleError, match="<occurrence-segment>/<revision>"):
        load_meeting_bundle(directory, inbox)


def test_directory_segment_must_match_the_occurrence_id(tmp_path):
    message = _reject(tmp_path, _fixture(MINIMAL), segment="some-other-meeting_20260902T091500Z")
    assert "segment" in message


def test_revision_directory_must_match_the_payload_revision(tmp_path):
    assert "revision" in _reject(tmp_path, _fixture(MINIMAL), revision_dir="2")
    assert "revision" in _reject(tmp_path, _fixture(MINIMAL), revision_dir="01")


def test_malformed_meeting_json_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    inbox = tmp_path / "meeting-inbox"
    directory = write_meeting_bundle(inbox, payload)
    (directory / "meeting.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(MeetingBundleError, match="invalid meeting JSON"):
        load_meeting_bundle(directory, inbox)


def test_non_object_meeting_json_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    inbox = tmp_path / "meeting-inbox"
    directory = write_meeting_bundle(inbox, payload)
    (directory / "meeting.json").write_text("[]", encoding="utf-8")
    with pytest.raises(MeetingBundleError, match="object"):
        load_meeting_bundle(directory, inbox)


# --- manifest rejects -------------------------------------------------------


def test_manifest_artifact_hash_is_never_trusted(tmp_path):
    lie = "sha256:" + "0" * 64
    message = _reject(tmp_path, _fixture(MINIMAL), manifest_overrides={"artifact_hash": lie})
    assert "artifact_hash" in message


def test_manifest_key_set_is_exact(tmp_path):
    assert "manifest" in _reject(tmp_path, _fixture(MINIMAL), manifest_overrides={"extra": 1})
    assert "manifest" in _reject(tmp_path, _fixture(MINIMAL), manifest_drop=("audio_bytes",))


def test_manifest_must_agree_with_the_payload_identity(tmp_path):
    assert "occurrence_id" in _reject(
        tmp_path, _fixture(MINIMAL), manifest_overrides={"occurrence_id": "other@2026-09-02T09:15:00Z"}
    )
    assert "revision" in _reject(tmp_path, _fixture(MINIMAL), manifest_overrides={"revision": 9})
    assert "schema" in _reject(tmp_path, _fixture(MINIMAL), manifest_overrides={"schema": "nope/v1"})


def test_audio_hash_mismatch_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    other = _audio_bytes(512)[::-1]
    message = _reject(tmp_path, payload, audio=other)   # payload still claims the forward bytes
    assert "sha256" in message


def test_audio_size_mismatch_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    data = _audio_bytes(256)
    _set_audio(payload, data)
    message = _reject(tmp_path, payload, manifest_overrides={"audio_bytes": 999})
    assert "audio_bytes" in message


@pytest.mark.parametrize(
    "encryption",
    ["none", "identity", "raw", "no-encryption", "plaintext", "unencrypted"],
)
def test_no_encryption_aliases_are_rejected_for_nonmagic_raw_audio(tmp_path, encryption):
    payload = _fixture(MINIMAL)
    raw_pcm = b"\x01\x00\x02\x00" * 128
    _set_audio(payload, raw_pcm)
    payload["audio"]["encryption"] = encryption

    message = _reject(tmp_path, payload, audio=raw_pcm)

    assert "audio encryption" in message


def test_unknown_encryption_scheme_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    payload["audio"]["encryption"] = "future-cipher"

    message = _reject(tmp_path, payload)

    assert "audio encryption" in message


@pytest.mark.parametrize("encryption", ["age-x25519", "aes-256-gcm"])
def test_supported_encryption_schemes_are_accepted(tmp_path, encryption):
    payload = _fixture(MINIMAL)
    payload["audio"]["encryption"] = encryption

    bundle = _load(tmp_path, payload)

    assert bundle.payload["audio"]["encryption"] == encryption


@pytest.mark.parametrize(
    "head",
    [
        b"RIFF\x00\x00\x00\x00WAVE",
        b"OggS\x00\x02\x00\x00\x00\x00\x00\x00",
        b"fLaC\x00\x00\x00\x22\x00\x00\x00\x00",
        b"ID3\x04\x00\x00\x00\x00\x00\x00\x00",
        b"\x00\x00\x00\x18ftypM4A \x00\x00",
        b"\xff\xfb\x90\x64\x00\x00\x00\x00\x00\x00",
        b"\xff\xe3\x18\xc4\x00\x00\x00\x00\x00\x00",
    ],
)
def test_plaintext_audio_containers_are_rejected(tmp_path, head):
    payload = _fixture(MINIMAL)
    data = head + _audio_bytes(256)[len(head):]
    _set_audio(payload, data)
    message = _reject(tmp_path, payload, audio=data)
    assert "plaintext" in message


def test_audio_filename_must_be_audio_enc(tmp_path):
    payload = _fixture(MINIMAL)
    payload["audio"]["filename"] = "recording.enc"
    assert "filename" in _reject(tmp_path, payload)


# --- payload rejects --------------------------------------------------------


def test_unknown_major_schema_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    payload["schema"] = "teammem-meeting/v3"
    assert "teammem-meeting/v1" in _reject(tmp_path, payload)


def test_unknown_or_missing_top_level_key_is_rejected(tmp_path):
    extra = _fixture(MINIMAL)
    extra["notes"] = "x"
    assert "top-level" in _reject(tmp_path, extra)
    missing = _fixture(MINIMAL)
    del missing["summary"]
    assert "top-level" in _reject(tmp_path, missing)


def test_revision_bounds(tmp_path):
    for bad in (0, -1, True, "1", 1.0):
        payload = _fixture(MINIMAL)
        payload["revision"] = bad
        assert "revision" in _reject(tmp_path, payload, revision_dir="1")


def test_ended_at_must_not_precede_started_at(tmp_path):
    payload = _fixture(MINIMAL)
    payload["ended_at"] = "2026-09-02T09:14:00Z"
    assert "ended_at" in _reject(tmp_path, payload)


def test_started_at_must_equal_the_occurrence_instant(tmp_path):
    payload = _fixture(MINIMAL)
    payload["started_at"] = "2026-09-02T09:16:00Z"
    assert "started_at" in _reject(tmp_path, payload)


def test_provider_meeting_id_must_equal_the_occurrence_meeting_id(tmp_path):
    payload = _fixture(MINIMAL)
    payload["provider_meeting_id"] = "vc:other"
    assert "provider_meeting_id" in _reject(tmp_path, payload)


def test_empty_title_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    payload["title"] = "   "
    assert "title" in _reject(tmp_path, payload)


def test_non_utc_instants_are_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    payload["ended_at"] = "2026-09-02T09:22:00+04:00"
    assert "ended_at" in _reject(tmp_path, payload)


# --- organizer --------------------------------------------------------------


def test_null_organizer_requires_the_instant_meeting_reason(tmp_path):
    payload = _fixture(MINIMAL)
    payload["organizer_unresolved_reason"] = None
    assert "organizer_unresolved_reason" in _reject(tmp_path, payload)
    payload["organizer_unresolved_reason"] = "could_not_look_up"
    assert "instant_meeting" in _reject(tmp_path, payload)


def test_present_organizer_must_not_carry_an_unresolved_reason(tmp_path):
    payload = _fixture(COMPLETE)
    payload["organizer_unresolved_reason"] = "instant_meeting"
    assert "organizer_unresolved_reason" in _reject(tmp_path, payload)


def test_organizer_source_must_be_reservation_or_calendar_event(tmp_path):
    payload = _fixture(COMPLETE)
    payload["organizer_source"] = "host_user"
    assert "organizer_source" in _reject(tmp_path, payload)
    payload["organizer_source"] = None
    assert "organizer_source" in _reject(tmp_path, payload)


def test_null_organizer_must_not_carry_a_source(tmp_path):
    payload = _fixture(MINIMAL)
    payload["organizer_source"] = "reservation"
    assert "organizer_source" in _reject(tmp_path, payload)


# --- participants and attendance -------------------------------------------


def test_participant_without_observed_attendance_is_rejected(tmp_path):
    payload = _fixture(COMPLETE)
    payload["participants"].append(
        {"person_id": "ou_invitee", "display_name": "Never Joined", "attendance": []}
    )
    assert "attendance" in _reject(tmp_path, payload)


def test_attendance_outside_the_meeting_window_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    payload["participants"][0]["attendance"][0]["left_at"] = "2026-09-02T10:00:00Z"
    assert "attendance" in _reject(tmp_path, payload)


def test_attendance_intervals_must_be_ordered_and_disjoint(tmp_path):
    payload = _fixture(COMPLETE)
    payload["participants"][2]["attendance"] = [
        {"joined_at": "2026-09-02T14:24:05Z", "left_at": "2026-09-02T14:52:00Z"},
        {"joined_at": "2026-09-02T14:00:20Z", "left_at": "2026-09-02T14:20:00Z"},
    ]
    assert "attendance" in _reject(tmp_path, payload)


def test_duplicate_participant_is_rejected(tmp_path):
    payload = _fixture(COMPLETE)
    payload["participants"].append(copy.deepcopy(payload["participants"][0]))
    assert "duplicate" in _reject(tmp_path, payload)


def test_no_participants_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    payload["participants"] = []
    payload["transcript"] = []
    payload["person_summaries"] = []
    assert "participants" in _reject(tmp_path, payload)


# --- transcript -------------------------------------------------------------


def test_transcript_must_be_ordered(tmp_path):
    payload = _fixture(COMPLETE)
    payload["transcript"][3], payload["transcript"][4] = (
        payload["transcript"][4],
        payload["transcript"][3],
    )
    assert "order" in _reject(tmp_path, payload)


def test_transcript_segments_must_not_overlap(tmp_path):
    payload = _fixture(COMPLETE)
    payload["transcript"][1]["started_at"] = "2026-09-02T14:00:20Z"   # inside s001
    assert "overlap" in _reject(tmp_path, payload)


def test_v1_same_overlap_stays_rejected_after_v2_support(tmp_path):
    payload = _fixture(MINIMAL)
    first = payload["transcript"][0]
    first["started_at"] = "2026-09-02T09:15:10Z"
    first["ended_at"] = "2026-09-02T09:15:40Z"
    second = copy.deepcopy(first)
    second["segment_id"] = "s002"
    second["started_at"] = "2026-09-02T09:15:20Z"
    second["ended_at"] = "2026-09-02T09:15:30Z"
    payload["transcript"] = [first, second]

    assert "overlap" in _reject(tmp_path, payload)


def test_v2_equal_time_ids_use_python_ordinal_order(tmp_path):
    payload = _fixture(MINIMAL)
    payload["schema"] = "teammem-meeting/v2"
    first = payload["transcript"][0]
    first["segment_id"] = "A"
    first["started_at"] = "2026-09-02T09:15:10Z"
    first["ended_at"] = "2026-09-02T09:15:20Z"
    second = copy.deepcopy(first)
    second["segment_id"] = "a"
    payload["transcript"] = [first, second]

    assert [s["segment_id"] for s in _load(tmp_path, payload).payload["transcript"]] == [
        "A", "a"
    ]
    payload["transcript"].reverse()
    assert "order" in _reject(tmp_path / "reversed", payload)


def test_v2_preserves_the_shared_ascii_segment_id_grammar(tmp_path):
    payload = _fixture(MINIMAL)
    payload["schema"] = "teammem-meeting/v2"
    payload["transcript"][0]["segment_id"] = "\ue000"

    assert "segment_id" in _reject(tmp_path, payload)


def test_v2_equal_instants_use_parsed_fraction_and_then_id(tmp_path):
    payload = _fixture(MINIMAL)
    payload["schema"] = "teammem-meeting/v2"
    first = payload["transcript"][0]
    first["segment_id"] = "a"
    first["started_at"] = "2026-09-02T09:15:10.1Z"
    first["ended_at"] = "2026-09-02T09:15:20.10Z"
    second = copy.deepcopy(first)
    second["segment_id"] = "b"
    second["started_at"] = "2026-09-02T09:15:10.100000Z"
    second["ended_at"] = "2026-09-02T09:15:20.100000Z"
    payload["transcript"] = [first, second]

    assert [s["segment_id"] for s in _load(tmp_path, payload).payload["transcript"]] == ["a", "b"]
    payload["transcript"].reverse()
    assert "order" in _reject(tmp_path / "reversed", payload)


@pytest.mark.parametrize("end", [
    "2026-09-02T09:15:20Z", "2026-09-02T09:15:30Z",
])
def test_v2_equal_start_requires_end_order_before_id(tmp_path, end):
    payload = _fixture(MINIMAL)
    payload["schema"] = "teammem-meeting/v2"
    first = payload["transcript"][0]
    first["segment_id"] = "z"
    first["started_at"] = "2026-09-02T09:15:10Z"
    first["ended_at"] = "2026-09-02T09:15:10Z"
    second = copy.deepcopy(first)
    second["segment_id"] = "a"
    second["ended_at"] = end
    payload["transcript"] = [first, second]

    assert [s["segment_id"] for s in _load(tmp_path, payload).payload["transcript"]] == ["z", "a"]
    payload["transcript"].reverse()
    assert "order" in _reject(tmp_path / "reversed", payload)


def test_v2_rejects_earlier_start_even_when_intervals_overlap(tmp_path):
    payload = _fixture(MINIMAL)
    payload["schema"] = "teammem-meeting/v2"
    first = payload["transcript"][0]
    first["segment_id"] = "a"
    first["started_at"] = "2026-09-02T09:15:20Z"
    first["ended_at"] = "2026-09-02T09:15:40Z"
    second = copy.deepcopy(first)
    second["segment_id"] = "b"
    second["started_at"] = "2026-09-02T09:15:10Z"
    payload["transcript"] = [first, second]

    assert "order" in _reject(tmp_path, payload)


@pytest.mark.parametrize(("mutation", "message"), [
    ("duplicate_id", "duplicate"),
    ("reversed_interval", "ended_at"),
    ("before_window", "window"),
    ("after_window", "window"),
    ("bad_speaker", "speaker"),
    ("bad_evidence", "unknown segment"),
    ("bad_provenance", "stage"),
    ("extra_segment_key", "invalid fields"),
    ("extra_top_key", "top-level"),
])
def test_v2_keeps_shared_rejections(tmp_path, mutation, message):
    payload = _fixture(MINIMAL)
    payload["schema"] = "teammem-meeting/v2"
    first = payload["transcript"][0]
    if mutation == "duplicate_id":
        payload["transcript"].append(copy.deepcopy(first))
    elif mutation == "reversed_interval":
        first["ended_at"] = "2026-09-02T09:15:09Z"
    elif mutation == "before_window":
        first["started_at"] = "2026-09-02T09:14:59Z"
    elif mutation == "after_window":
        first["ended_at"] = "2026-09-02T09:22:01Z"
    elif mutation == "bad_speaker":
        first["speaker"] = {
            "kind": "person",
            "person_id": "ou_ghost",
            "display_name": "Ghost",
            "reconciled": True,
        }
    elif mutation == "bad_evidence":
        payload["decisions"] = [{"text": "Unsupported claim.", "evidence_segment_ids": ["ghost"]}]
    elif mutation == "bad_provenance":
        first["provenance"]["stage"] = "guess"
    elif mutation == "extra_segment_key":
        first["private_note"] = "not a wire field"
    elif mutation == "extra_top_key":
        payload["private_note"] = "not a wire field"

    assert message in _reject(tmp_path, payload)


def test_segment_end_before_start_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    payload["transcript"][0]["ended_at"] = "2026-09-02T09:15:05Z"
    assert "ended_at" in _reject(tmp_path, payload)


def test_duplicate_segment_id_is_rejected(tmp_path):
    payload = _fixture(COMPLETE)
    payload["transcript"][5]["segment_id"] = "s001"
    assert "duplicate" in _reject(tmp_path, payload)


def test_person_speaker_must_be_an_actual_participant(tmp_path):
    payload = _fixture(COMPLETE)
    payload["transcript"][0]["speaker"]["person_id"] = "ou_ghost"
    assert "participant" in _reject(tmp_path, payload)


def test_person_speaker_name_must_match_the_participant(tmp_path):
    payload = _fixture(COMPLETE)
    payload["transcript"][0]["speaker"]["display_name"] = "Someone Else"
    assert "display_name" in _reject(tmp_path, payload)


def test_unreconciled_person_speaker_is_a_guess_and_is_rejected(tmp_path):
    payload = _fixture(COMPLETE)
    payload["transcript"][0]["speaker"]["reconciled"] = False
    assert "reconciled" in _reject(tmp_path, payload)


def test_unknown_speaker_kind_is_rejected(tmp_path):
    payload = _fixture(MINIMAL)
    payload["transcript"][0]["speaker"] = {"kind": "probably", "person_id": "ou_ada"}
    assert "speaker" in _reject(tmp_path, payload)


def test_attribution_confidence_bounds(tmp_path):
    for bad in (-0.1, 1.2, "high", None):
        payload = _fixture(MINIMAL)
        payload["transcript"][0]["attribution_confidence"] = bad
        assert "attribution_confidence" in _reject(tmp_path, payload)


def test_segment_provenance_stage_is_constrained(tmp_path):
    payload = _fixture(MINIMAL)
    payload["transcript"][0]["provenance"]["stage"] = "guessed"
    assert "stage" in _reject(tmp_path, payload)


# --- synthesis --------------------------------------------------------------


def test_project_evidence_must_reference_real_segments(tmp_path):
    payload = _fixture(COMPLETE)
    payload["projects"][0]["evidence_segment_ids"] = ["s999"]
    assert "s999" in _reject(tmp_path, payload)


def test_project_without_evidence_is_rejected(tmp_path):
    payload = _fixture(COMPLETE)
    payload["projects"][1]["evidence_segment_ids"] = []
    assert "evidence_segment_ids" in _reject(tmp_path, payload)


def test_duplicate_project_is_rejected(tmp_path):
    payload = _fixture(COMPLETE)
    payload["projects"][2]["name"] = "project-atlas"
    assert "duplicate" in _reject(tmp_path, payload)


def test_decision_and_discussion_evidence_must_reference_real_segments(tmp_path):
    payload = _fixture(COMPLETE)
    payload["decisions"][0]["evidence_segment_ids"] = ["s404"]
    assert "s404" in _reject(tmp_path, payload)
    payload = _fixture(COMPLETE)
    payload["discussion_points"][0]["evidence_segment_ids"] = ["s404"]
    assert "s404" in _reject(tmp_path, payload)


def test_action_owner_not_marked_explicitly_stated_is_rejected(tmp_path):
    payload = _fixture(COMPLETE)
    payload["action_items"][0]["owner"]["explicitly_stated"] = False
    assert "explicitly_stated" in _reject(tmp_path, payload)


def test_action_due_date_not_marked_explicitly_stated_is_rejected(tmp_path):
    payload = _fixture(COMPLETE)
    payload["action_items"][0]["due_date"]["explicitly_stated"] = False
    assert "explicitly_stated" in _reject(tmp_path, payload)


def test_action_due_date_must_be_a_calendar_date(tmp_path):
    payload = _fixture(COMPLETE)
    payload["action_items"][0]["due_date"]["date"] = "2026-09-03T00:00:00Z"
    assert "due_date" in _reject(tmp_path, payload)


def test_person_summary_for_a_silent_participant_is_rejected(tmp_path):
    payload = _fixture(COMPLETE)
    payload["person_summaries"].append(
        {"person_id": "ou_hana", "display_name": "Hana Suzuki", "text": "Listened throughout."}
    )
    assert "ou_hana" in _reject(tmp_path, payload)


def test_person_summary_for_a_non_participant_is_rejected(tmp_path):
    payload = _fixture(COMPLETE)
    payload["person_summaries"].append(
        {"person_id": "ou_absent", "display_name": "Absent Person", "text": "Sent notes later."}
    )
    assert "ou_absent" in _reject(tmp_path, payload)


def test_duplicate_person_summary_is_rejected(tmp_path):
    payload = _fixture(COMPLETE)
    payload["person_summaries"].append(copy.deepcopy(payload["person_summaries"][0]))
    assert "duplicate" in _reject(tmp_path, payload)


def test_person_summary_name_must_match_the_participant(tmp_path):
    payload = _fixture(COMPLETE)
    payload["person_summaries"][0]["display_name"] = "A. Chen"
    assert "display_name" in _reject(tmp_path, payload)


# --- gaps, warnings, provenance, audio --------------------------------------


def test_recording_gaps_must_be_ordered_inside_the_meeting(tmp_path):
    payload = _fixture(COMPLETE)
    payload["recording_gaps"][0]["started_at"] = "2026-09-02T13:00:00Z"
    assert "recording_gaps" in _reject(tmp_path, payload)
    payload = _fixture(COMPLETE)
    payload["recording_gaps"].reverse()
    assert "recording_gaps" in _reject(tmp_path, payload)


def test_processing_warning_code_must_be_a_token(tmp_path):
    payload = _fixture(COMPLETE)
    payload["processing_warnings"][0]["code"] = "Overlapping Speech!"
    assert "code" in _reject(tmp_path, payload)


def test_runtime_profile_is_constrained(tmp_path):
    payload = _fixture(MINIMAL)
    payload["provenance"]["runtime_profile"] = "hybrid"
    assert "runtime_profile" in _reject(tmp_path, payload)


def test_audio_duration_must_be_positive(tmp_path):
    payload = _fixture(MINIMAL)
    payload["audio"]["duration_seconds"] = 0
    assert "duration_seconds" in _reject(tmp_path, payload)


def test_audio_hash_field_must_be_prefixed_sha256(tmp_path):
    payload = _fixture(MINIMAL)
    data = _audio_bytes(512)
    payload["audio"]["sha256"] = hashlib.sha256(data).hexdigest()   # bare hex, no prefix
    assert "sha256" in _reject(tmp_path, payload)


def test_transcript_complete_must_be_boolean(tmp_path):
    payload = _fixture(MINIMAL)
    payload["transcript_complete"] = "yes"
    assert "transcript_complete" in _reject(tmp_path, payload)


def test_open_attendance_interval_must_be_the_last_one(tmp_path):
    payload = _fixture(COMPLETE)
    payload["participants"][2]["attendance"] = [
        {"joined_at": "2026-09-02T14:00:20Z", "left_at": None},
        {"joined_at": "2026-09-02T14:24:05Z", "left_at": "2026-09-02T14:52:00Z"},
    ]
    assert "left_at" in _reject(tmp_path, payload)
