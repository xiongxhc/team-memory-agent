"""Strict validation for the frozen teammem-meeting/v1 bundle protocol.

Meeting bundles are a separate wire format from `teammem-bundle/v1`. Nothing in
this module relaxes, reuses, or subclasses member-bundle assumptions: meetings
arrive as a directory (payload plus encrypted audio plus manifest plus a READY
marker), carry a revision, and are validated field by field before anything is
imported.

The schema document is `docs/schemas/teammem-meeting-v1.md`.
"""

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


MEETING_SCHEMA = "teammem-meeting/v1"
MANIFEST_SCHEMA = "teammem-meeting-manifest/v1"

MEETING_FILENAME = "meeting.json"
AUDIO_FILENAME = "audio.enc"
MANIFEST_FILENAME = "manifest.json"
READY_FILENAME = "READY"
BUNDLE_FILENAMES = (MEETING_FILENAME, AUDIO_FILENAME, MANIFEST_FILENAME, READY_FILENAME)

ORGANIZER_SOURCES = ("reservation", "calendar_event")
ORGANIZER_UNRESOLVED_REASON = "instant_meeting"
RUNTIME_PROFILES = ("offline", "online")
SEGMENT_STAGES = ("live_stream", "final_batch")
SPEAKER_KINDS = ("person", "anonymous", "ambiguous")

_TOP_KEYS = frozenset(
    {
        "schema",
        "occurrence_id",
        "revision",
        "provider",
        "provider_meeting_id",
        "title",
        "started_at",
        "ended_at",
        "organizer",
        "organizer_source",
        "organizer_unresolved_reason",
        "transcript_complete",
        "summary",
        "participants",
        "transcript",
        "projects",
        "decisions",
        "action_items",
        "discussion_points",
        "person_summaries",
        "recording_gaps",
        "processing_warnings",
        "provenance",
        "audio",
    }
)
_MANIFEST_KEYS = frozenset(
    {"schema", "occurrence_id", "revision", "artifact_hash", "audio_sha256", "audio_bytes"}
)
_ORGANIZER_KEYS = frozenset({"person_id", "display_name"})
_PARTICIPANT_KEYS = frozenset({"person_id", "display_name", "attendance"})
_INTERVAL_KEYS = frozenset({"joined_at", "left_at"})
_SEGMENT_KEYS = frozenset(
    {"segment_id", "started_at", "ended_at", "text", "speaker", "attribution_confidence", "provenance"}
)
_SEGMENT_PROVENANCE_KEYS = frozenset({"stage", "model"})
_PROVENANCE_KEYS = frozenset(
    {
        "runtime_profile",
        "stt_provider",
        "stt_model",
        "text_provider",
        "text_model",
        "agent_version",
        "processed_at",
    }
)
_AUDIO_KEYS = frozenset(
    {"filename", "format", "encryption", "duration_seconds", "bytes", "sha256", "expires_at"}
)
_PROJECT_KEYS = frozenset({"name", "evidence_segment_ids"})
_STATEMENT_KEYS = frozenset({"text", "evidence_segment_ids"})
_ACTION_KEYS = frozenset({"text", "owner", "due_date", "evidence_segment_ids"})
_OWNER_KEYS = frozenset({"person_id", "display_name", "explicitly_stated"})
_DUE_DATE_KEYS = frozenset({"date", "explicitly_stated"})
_SUMMARY_KEYS = frozenset({"person_id", "display_name", "text"})
_GAP_KEYS = frozenset({"started_at", "ended_at", "reason"})
_WARNING_KEYS = frozenset({"code", "detail"})

MAX_REVISION = 1_000_000
MAX_ID = 200
MAX_NAME = 200
MAX_TITLE = 200
MAX_SUMMARY = 20_000
MAX_TEXT = 4_000
MAX_SEGMENT_TEXT = 5_000
MAX_DETAIL = 500
MAX_OCCURRENCE_ID = 400
MAX_PARTICIPANTS = 500
MAX_INTERVALS = 100
MAX_SEGMENTS = 50_000
MAX_ITEMS = 500
MAX_EVIDENCE = 100
MAX_DURATION_SECONDS = 86_400.0
MAX_AUDIO_BYTES = 8 * 1024 * 1024 * 1024

_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TOKEN = re.compile(r"[a-z][a-z0-9_-]*")
_CODE = re.compile(r"[a-z][a-z0-9_]*")
_CLUSTER_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
_SEGMENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
_ARTIFACT_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_REVISION_DIR = re.compile(r"[1-9][0-9]*")


class MeetingBundleError(ValueError):
    pass


@dataclass(frozen=True)
class MeetingBundle:
    path: Path
    occurrence_id: str
    revision: int
    artifact_hash: str
    payload: dict
    audio_path: Path


def _error(message: str) -> MeetingBundleError:
    return MeetingBundleError(message)


# --- scalar helpers ---------------------------------------------------------


def _object(value, keys, label: str) -> dict:
    if not isinstance(value, dict):
        raise _error(f"{label} must be an object")
    if set(value) != set(keys):
        raise _error(f"{label} has invalid fields")
    return value


def _list(container: dict, key: str, maximum: int, label: str) -> list:
    value = container[key]
    if not isinstance(value, list):
        raise _error(f"{label}{key} must be an array")
    if len(value) > maximum:
        raise _error(f"{label}{key} has more than {maximum} entries")
    return value


def _text(container: dict, key: str, maximum: int, label: str = "") -> str:
    value = container[key]
    if not isinstance(value, str) or not value.strip():
        raise _error(f"{label}{key} must be a non-empty string")
    if len(value) > maximum:
        raise _error(f"{label}{key} is longer than {maximum} characters")
    return value


def _pattern(container: dict, key: str, pattern: re.Pattern, maximum: int, label: str = "") -> str:
    value = _text(container, key, maximum, label)
    if not pattern.fullmatch(value):
        raise _error(f"{label}{key} is not a valid token")
    return value


def _instant(container: dict, key: str, label: str = "", *, whole_seconds: bool = False) -> datetime:
    value = container[key]
    if not isinstance(value, str) or not _INSTANT.fullmatch(value):
        raise _error(f"{label}{key} must be a UTC instant such as 2026-09-02T14:00:00Z")
    if whole_seconds and "." in value:
        raise _error(f"{label}{key} must not carry fractional seconds")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error(f"{label}{key} is not a real instant") from exc


# --- occurrence identifiers -------------------------------------------------


def parse_occurrence_id(occurrence_id: str) -> tuple[str, datetime]:
    """Split `<meeting-id>@<UTC start>` into its meeting id and start instant."""
    if not isinstance(occurrence_id, str) or not occurrence_id:
        raise _error("occurrence_id must be a non-empty string")
    if len(occurrence_id) > MAX_OCCURRENCE_ID:
        raise _error(f"occurrence_id is longer than {MAX_OCCURRENCE_ID} characters")
    meeting_id, separator, start = occurrence_id.rpartition("@")
    if not separator or not meeting_id:
        raise _error("occurrence_id must be <meeting-id>@<UTC start>")
    if any(character < " " or character == "\x7f" for character in meeting_id):
        raise _error("occurrence_id meeting id contains control characters")
    started = _instant({"start": start}, "start", "occurrence_id ", whole_seconds=True)
    return meeting_id, started


def occurrence_path_segment(occurrence_id: str) -> str:
    """The one filesystem/URL spelling of an occurrence. Legal on Windows too."""
    meeting_id, started = parse_occurrence_id(occurrence_id)
    return f"{urllib.parse.quote(meeting_id, safe='')}_{started.strftime('%Y%m%dT%H%M%SZ')}"


def short_occurrence_id(occurrence_id: str) -> str:
    """First 8 lowercase hex chars of sha256(path segment). Used in note filenames."""
    segment = occurrence_path_segment(occurrence_id).encode("utf-8")
    return hashlib.sha256(segment).hexdigest()[:8]


def canonical_artifact_hash(payload: dict) -> str:
    """`sha256:<hex>` over the canonical JSON serialization of a meeting payload."""
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


# --- payload validation -----------------------------------------------------


def _evidence(container: dict, known: set, label: str) -> None:
    ids = _list(container, "evidence_segment_ids", MAX_EVIDENCE, label)
    if not ids:
        raise _error(f"{label}evidence_segment_ids must not be empty")
    seen = set()
    for value in ids:
        if not isinstance(value, str):
            raise _error(f"{label}evidence_segment_ids must contain strings")
        if value not in known:
            raise _error(f"{label}evidence_segment_ids references unknown segment {value}")
        if value in seen:
            raise _error(f"{label}evidence_segment_ids has a duplicate entry {value}")
        seen.add(value)


def _validate_organizer(data: dict) -> None:
    organizer = data["organizer"]
    if organizer is None:
        if data["organizer_source"] is not None:
            raise _error("organizer_source must be null when organizer is null")
        if data["organizer_unresolved_reason"] != ORGANIZER_UNRESOLVED_REASON:
            raise _error(
                "organizer_unresolved_reason must be "
                f'"{ORGANIZER_UNRESOLVED_REASON}" when organizer is null'
            )
        return
    _object(organizer, _ORGANIZER_KEYS, "organizer")
    _text(organizer, "person_id", MAX_ID, "organizer ")
    _text(organizer, "display_name", MAX_NAME, "organizer ")
    if data["organizer_source"] not in ORGANIZER_SOURCES:
        raise _error("organizer_source must be reservation or calendar_event")
    if data["organizer_unresolved_reason"] is not None:
        raise _error("organizer_unresolved_reason must be null when organizer is present")


def _validate_participants(data: dict, ended: datetime) -> dict[str, str]:
    participants = _list(data, "participants", MAX_PARTICIPANTS, "")
    if not participants:
        raise _error("participants must not be empty")
    people: dict[str, str] = {}
    for index, participant in enumerate(participants):
        label = f"participant {index} "
        _object(participant, _PARTICIPANT_KEYS, f"participant {index}")
        person_id = _text(participant, "person_id", MAX_ID, label)
        display_name = _text(participant, "display_name", MAX_NAME, label)
        if person_id in people:
            raise _error(f"duplicate participant {person_id}")
        intervals = _list(participant, "attendance", MAX_INTERVALS, label)
        if not intervals:
            raise _error(f"{label}has no observed attendance interval")
        previous_end = None
        for position, interval in enumerate(intervals):
            interval_label = f"{label}attendance {position} "
            _object(interval, _INTERVAL_KEYS, f"{label}attendance {position}")
            # An observed join may precede started_at (early joiners sit in the
            # client before the occurrence starts); it may never follow the end.
            joined = _instant(interval, "joined_at", interval_label)
            if joined > ended:
                raise _error(f"{interval_label}joined_at is outside the meeting window")
            left = None
            if interval["left_at"] is None:
                # Still present at the end, so nothing can follow this interval.
                if position != len(intervals) - 1:
                    raise _error(f"{interval_label}attendance with no left_at must be the last one")
            else:
                left = _instant(interval, "left_at", interval_label)
                if left < joined:
                    raise _error(f"{interval_label}left_at precedes joined_at")
                if left > ended:
                    raise _error(f"{interval_label}left_at is outside the meeting window")
            if previous_end is not None and joined < previous_end:
                raise _error(f"{interval_label}intervals must be ordered and disjoint")
            previous_end = ended if left is None else left
        people[person_id] = display_name
    return people


def _validate_transcript(
    data: dict, people: dict[str, str], started: datetime, ended: datetime
) -> tuple[set[str], set[str]]:
    segments = _list(data, "transcript", MAX_SEGMENTS, "")
    segment_ids: set[str] = set()
    spoke: set[str] = set()
    previous_start = None
    previous_end = None
    for index, segment in enumerate(segments):
        label = f"transcript segment {index} "
        _object(segment, _SEGMENT_KEYS, f"transcript segment {index}")
        segment_id = _pattern(segment, "segment_id", _SEGMENT_ID, MAX_ID, label)
        if segment_id in segment_ids:
            raise _error(f"duplicate segment_id {segment_id}")
        segment_start = _instant(segment, "started_at", label)
        segment_end = _instant(segment, "ended_at", label)
        if segment_end < segment_start:
            raise _error(f"{label}ended_at precedes started_at")
        if segment_start < started or segment_end > ended:
            raise _error(f"{label}timing is outside the meeting window")
        if previous_start is not None and segment_start < previous_start:
            raise _error(f"{label}breaks transcript start order")
        if previous_end is not None and segment_start < previous_end:
            raise _error(f"{label}overlaps the previous segment")
        _text(segment, "text", MAX_SEGMENT_TEXT, label)
        confidence = segment["attribution_confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise _error(f"{label}attribution_confidence must be a number in [0, 1]")
        if not 0.0 <= float(confidence) <= 1.0:
            raise _error(f"{label}attribution_confidence must be a number in [0, 1]")
        _validate_speaker(segment["speaker"], people, spoke, label)
        provenance = _object(segment["provenance"], _SEGMENT_PROVENANCE_KEYS, f"{label}provenance")
        if provenance["stage"] not in SEGMENT_STAGES:
            raise _error(f"{label}provenance stage must be live_stream or final_batch")
        _text(provenance, "model", MAX_NAME, f"{label}provenance ")
        segment_ids.add(segment_id)
        previous_start = segment_start
        previous_end = segment_end
    return segment_ids, spoke


def _validate_speaker(speaker, people: dict[str, str], spoke: set[str], label: str) -> None:
    if not isinstance(speaker, dict) or "kind" not in speaker:
        raise _error(f"{label}speaker must be an object with a kind")
    kind = speaker["kind"]
    if kind == "person":
        _object(speaker, {"kind", "person_id", "display_name", "reconciled"}, f"{label}speaker")
        person_id = _text(speaker, "person_id", MAX_ID, f"{label}speaker ")
        if person_id not in people:
            raise _error(f"{label}speaker {person_id} is not an actual participant")
        if speaker["display_name"] != people[person_id]:
            raise _error(f"{label}speaker display_name does not match the participant")
        if speaker["reconciled"] is not True:
            raise _error(f"{label}speaker must be reconciled; a guessed identity is rejected")
        spoke.add(person_id)
    elif kind == "anonymous":
        _object(speaker, {"kind", "cluster_id"}, f"{label}speaker")
        _pattern(speaker, "cluster_id", _CLUSTER_ID, MAX_ID, f"{label}speaker ")
    elif kind == "ambiguous":
        _object(speaker, {"kind"}, f"{label}speaker")
    else:
        raise _error(f"{label}speaker kind must be one of {', '.join(SPEAKER_KINDS)}")


def _validate_synthesis(data: dict, people: dict[str, str], segment_ids: set[str], spoke: set[str]) -> None:
    names: set[str] = set()
    for index, project in enumerate(_list(data, "projects", MAX_ITEMS, "")):
        label = f"project {index} "
        _object(project, _PROJECT_KEYS, f"project {index}")
        name = _text(project, "name", MAX_NAME, label)
        if name in names:
            raise _error(f"duplicate project {name}")
        names.add(name)
        _evidence(project, segment_ids, label)

    for key in ("decisions", "discussion_points"):
        for index, item in enumerate(_list(data, key, MAX_ITEMS, "")):
            label = f"{key} {index} "
            _object(item, _STATEMENT_KEYS, f"{key} {index}")
            _text(item, "text", MAX_TEXT, label)
            _evidence(item, segment_ids, label)

    for index, item in enumerate(_list(data, "action_items", MAX_ITEMS, "")):
        label = f"action item {index} "
        _object(item, _ACTION_KEYS, f"action item {index}")
        _text(item, "text", MAX_TEXT, label)
        _evidence(item, segment_ids, label)
        owner = item["owner"]
        if owner is not None:
            _object(owner, _OWNER_KEYS, f"{label}owner")
            _text(owner, "person_id", MAX_ID, f"{label}owner ")
            _text(owner, "display_name", MAX_NAME, f"{label}owner ")
            if owner["explicitly_stated"] is not True:
                raise _error(f"{label}owner must be marked explicitly_stated")
        due_date = item["due_date"]
        if due_date is not None:
            _object(due_date, _DUE_DATE_KEYS, f"{label}due_date")
            value = due_date["date"]
            if not isinstance(value, str) or not _DATE.fullmatch(value):
                raise _error(f"{label}due_date date must be YYYY-MM-DD")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise _error(f"{label}due_date date is not a real date") from exc
            if due_date["explicitly_stated"] is not True:
                raise _error(f"{label}due_date must be marked explicitly_stated")

    seen: set[str] = set()
    for index, item in enumerate(_list(data, "person_summaries", MAX_ITEMS, "")):
        label = f"person summary {index} "
        _object(item, _SUMMARY_KEYS, f"person summary {index}")
        person_id = _text(item, "person_id", MAX_ID, label)
        _text(item, "text", MAX_SUMMARY, label)
        if person_id not in people:
            raise _error(f"{label}{person_id} is not an actual participant")
        if person_id not in spoke:
            raise _error(f"{label}{person_id} has no reconciled speech in this meeting")
        if person_id in seen:
            raise _error(f"duplicate person summary for {person_id}")
        if item["display_name"] != people[person_id]:
            raise _error(f"{label}display_name does not match the participant")
        seen.add(person_id)


def _validate_gaps_and_warnings(data: dict, started: datetime, ended: datetime) -> None:
    previous_end = None
    for index, gap in enumerate(_list(data, "recording_gaps", MAX_ITEMS, "")):
        label = f"recording_gaps {index} "
        _object(gap, _GAP_KEYS, f"recording_gaps {index}")
        gap_start = _instant(gap, "started_at", label)
        gap_end = _instant(gap, "ended_at", label)
        if gap_end < gap_start:
            raise _error(f"{label}ended_at precedes started_at")
        if gap_start < started or gap_end > ended:
            raise _error(f"{label}is outside the meeting window")
        if previous_end is not None and gap_start < previous_end:
            raise _error(f"{label}must be ordered and disjoint")
        _pattern(gap, "reason", _CODE, MAX_NAME, label)
        previous_end = gap_end

    for index, warning in enumerate(_list(data, "processing_warnings", MAX_ITEMS, "")):
        label = f"processing_warnings {index} "
        _object(warning, _WARNING_KEYS, f"processing_warnings {index}")
        code = warning["code"]
        if not isinstance(code, str) or not _CODE.fullmatch(code) or len(code) > MAX_NAME:
            raise _error(f"{label}code must be a lowercase token")
        detail = warning["detail"]
        if not isinstance(detail, str) or len(detail) > MAX_DETAIL:
            raise _error(f"{label}detail must be a string of at most {MAX_DETAIL} characters")


def _validate_provenance(data: dict) -> None:
    provenance = _object(data["provenance"], _PROVENANCE_KEYS, "provenance")
    if provenance["runtime_profile"] not in RUNTIME_PROFILES:
        raise _error("provenance runtime_profile must be offline or online")
    for key in ("stt_provider", "stt_model", "text_provider", "text_model", "agent_version"):
        _text(provenance, key, MAX_NAME, "provenance ")
    _instant(provenance, "processed_at", "provenance ")


def _validate_audio(data: dict) -> None:
    audio = _object(data["audio"], _AUDIO_KEYS, "audio")
    if audio["filename"] != AUDIO_FILENAME:
        raise _error(f"audio filename must be {AUDIO_FILENAME}")
    _pattern(audio, "format", _TOKEN, MAX_NAME, "audio ")
    _pattern(audio, "encryption", _TOKEN, MAX_NAME, "audio ")
    duration = audio["duration_seconds"]
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise _error("audio duration_seconds must be a number")
    if not 0 < float(duration) <= MAX_DURATION_SECONDS:
        raise _error(f"audio duration_seconds must be between 0 and {MAX_DURATION_SECONDS}")
    size = audio["bytes"]
    if isinstance(size, bool) or not isinstance(size, int):
        raise _error("audio bytes must be an integer")
    if not 0 < size <= MAX_AUDIO_BYTES:
        raise _error(f"audio bytes must be between 1 and {MAX_AUDIO_BYTES}")
    digest = audio["sha256"]
    if not isinstance(digest, str) or not _ARTIFACT_HASH.fullmatch(digest):
        raise _error("audio sha256 must be sha256:<64 lowercase hex>")
    # Advisory only: TeamMem recomputes expiry as ended_at + 14 days and never
    # honours a longer caller-supplied retention.
    _instant(audio, "expires_at", "audio ")


def _validate_payload(data) -> None:
    if not isinstance(data, dict):
        raise _error("top-level meeting.json must be an object")
    if set(data) != _TOP_KEYS:
        raise _error("meeting.json has invalid top-level fields")
    if data["schema"] != MEETING_SCHEMA:
        raise _error(f"invalid schema: expected {MEETING_SCHEMA}")

    revision = data["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise _error("revision must be an integer")
    if not 1 <= revision <= MAX_REVISION:
        raise _error(f"revision must be between 1 and {MAX_REVISION}")

    meeting_id, occurrence_start = parse_occurrence_id(data["occurrence_id"])
    _pattern(data, "provider", _TOKEN, MAX_NAME)
    if data["provider_meeting_id"] != meeting_id:
        raise _error("provider_meeting_id must equal the occurrence_id meeting id")
    _text(data, "title", MAX_TITLE)
    started = _instant(data, "started_at", whole_seconds=True)
    if started != occurrence_start:
        raise _error("started_at must equal the occurrence_id start")
    ended = _instant(data, "ended_at")
    if ended < started:
        raise _error("ended_at must not precede started_at")

    _validate_organizer(data)
    if not isinstance(data["transcript_complete"], bool):
        raise _error("transcript_complete must be a boolean")
    _text(data, "summary", MAX_SUMMARY)

    people = _validate_participants(data, ended)
    segment_ids, spoke = _validate_transcript(data, people, started, ended)
    _validate_synthesis(data, people, segment_ids, spoke)
    _validate_gaps_and_warnings(data, started, ended)
    _validate_provenance(data)
    _validate_audio(data)


# --- audio screening --------------------------------------------------------


def _hash_file(path: Path) -> tuple[bytes, str]:
    """Return the first 16 bytes and `sha256:<hex>` without loading the file."""
    digest = hashlib.sha256()
    head = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            if not head:
                head = chunk[:16]
            digest.update(chunk)
    return head, "sha256:" + digest.hexdigest()


def _looks_like_plaintext_audio(head: bytes) -> bool:
    """Screen for audio-container magic numbers.

    TeamMem never holds the recording key, so it cannot prove that bytes are
    ciphertext. It can only refuse bytes that announce themselves as a
    plaintext container.
    """
    if head[:4] in (b"RIFF", b"OggS", b"fLaC"):
        return True
    if head[:3] == b"ID3":
        return True
    if head[4:8] == b"ftyp":
        return True
    if len(head) >= 2 and head[0] == 0xFF and 0xE0 <= head[1] <= 0xFF:
        return True
    return False


# --- entrypoint -------------------------------------------------------------


def load_meeting_bundle(path: Path, inbox: Path) -> MeetingBundle:
    """Validate one `<inbox>/<occurrence-segment>/<revision>/` bundle directory."""
    if path.is_symlink():
        raise _error("bundle path is a symlink")
    inbox_root = inbox.resolve()
    try:
        relative = path.resolve().relative_to(inbox_root)
    except ValueError as exc:
        raise _error("bundle path is outside inbox") from exc
    if len(relative.parts) != 2:
        raise _error("bundle path must be <inbox>/<occurrence-segment>/<revision>")
    segment_name, revision_name = relative.parts
    if (inbox_root / segment_name).is_symlink():
        raise _error("bundle occurrence directory is a symlink")
    if not path.is_dir():
        raise _error("bundle path is not a directory")
    if not _REVISION_DIR.fullmatch(revision_name):
        raise _error("bundle revision directory must be a positive decimal integer")

    entries = sorted(entry.name for entry in path.iterdir())
    for name in BUNDLE_FILENAMES:
        if name not in entries:
            raise _error(f"bundle is missing {name}")
    for name in entries:
        if name not in BUNDLE_FILENAMES:
            raise _error(f"unexpected file in bundle: {name}")
        member = path / name
        if member.is_symlink():
            raise _error(f"bundle file {name} is a symlink")
        if not member.is_file():
            raise _error(f"bundle file {name} is not a regular file")

    try:
        data = json.loads((path / MEETING_FILENAME).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error(f"invalid meeting JSON: {exc}") from exc
    _validate_payload(data)

    occurrence_id = data["occurrence_id"]
    if segment_name != occurrence_path_segment(occurrence_id):
        raise _error("bundle directory segment does not match occurrence_id")
    revision = data["revision"]
    if int(revision_name) != revision:
        raise _error("bundle revision directory does not match the payload revision")
    artifact_hash = canonical_artifact_hash(data)

    try:
        manifest = json.loads((path / MANIFEST_FILENAME).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error(f"invalid manifest JSON: {exc}") from exc
    _object(manifest, _MANIFEST_KEYS, "manifest")
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise _error(f"invalid manifest schema: expected {MANIFEST_SCHEMA}")
    if manifest["occurrence_id"] != occurrence_id:
        raise _error("manifest occurrence_id does not match meeting.json")
    if manifest["revision"] != revision:
        raise _error("manifest revision does not match meeting.json")
    if manifest["artifact_hash"] != artifact_hash:
        raise _error("manifest artifact_hash does not match the recomputed canonical hash")
    claimed_size = manifest["audio_bytes"]
    if isinstance(claimed_size, bool) or not isinstance(claimed_size, int):
        raise _error("manifest audio_bytes must be an integer")

    audio_path = path / AUDIO_FILENAME
    size = audio_path.stat().st_size
    if claimed_size != size:
        raise _error(f"manifest audio_bytes does not match audio.enc ({size} bytes on disk)")
    if data["audio"]["bytes"] != size:
        raise _error("audio bytes in meeting.json does not match audio.enc")
    head, digest = _hash_file(audio_path)
    if manifest["audio_sha256"] != digest:
        raise _error("manifest audio_sha256 does not match audio.enc")
    if data["audio"]["sha256"] != digest:
        raise _error("audio sha256 in meeting.json does not match audio.enc")
    if _looks_like_plaintext_audio(head):
        raise _error("audio.enc looks like plaintext audio (container magic number)")

    return MeetingBundle(
        path=path,
        occurrence_id=occurrence_id,
        revision=revision,
        artifact_hash=artifact_hash,
        payload=data,
        audio_path=audio_path,
    )
