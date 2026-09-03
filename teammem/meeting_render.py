"""Render the canonical meeting note and the backlinks that point at it.

One current occurrence is one note under `Meetings/<week>/`, linked from every
project it discussed and every person who actually attended. The note carries
the summary and the structured synthesis; it never carries transcript text, an
audio path, or audio bytes — those stay in the ledger and the audio store.

Actual participants come from observed join/leave state, so invitees and
no-shows never appear. A person who joined but never spoke stays an actual
participant and gets no per-person summary. Anonymous speaker clusters stay
anonymous: nothing here promotes a cluster to a name.

Times are rendered in `DISPLAY_TZ`. It is UTC because the renderer is a pure
function of (ledger, today) — a process-local zone would make the same ledger
produce different filenames on different machines — and because every meeting
instant reaches the store as a UTC `Z` spelling.

This module holds no SQL and no filesystem writes: `render.py` owns the vault
tree, and `meeting_store.py` owns every meeting query. The naming and link
helpers it needs are injected as `VaultNaming` so the renderer stays the single
source of vault path conventions and no import cycle is created.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Callable
from urllib.parse import quote

from .meeting_bundles import short_occurrence_id
from .meeting_store import get_meeting, list_meetings
from .queries import week_label, week_monday

DISPLAY_TZ = timezone.utc
MEETINGS_DIR = "Meetings"
ORGANIZER_UNAVAILABLE = "Not available (instant meeting)"
NONE_LINE = "- none\n"
# The one spelling of a canonical note filename. `render.py` builds the same
# string from ledger facts when it verifies the vault
# (`render._meeting_facts`), deliberately without going through a MeetingNote —
# sharing the format keeps the two from drifting; sharing the arguments would
# make the verification tautological. Change one call site, check the other.
NOTE_FILE_NAME = "{start:%Y-%m-%d %H-%M} {topic} [{short}].md"
NOTE_HEADINGS = (
    "Meeting details",
    "Summary",
    "Projects discussed",
    "Explicitly stated decisions",
    "Action items",
    "Key discussion points",
    "Actual participants",
    "Organizer",
    "Per-person summaries",
)


@dataclass(frozen=True)
class VaultNaming:
    """The vault's naming and link conventions, owned by `render.py`.

    `person` resolves a provider identity to `(vault key, display name)`: the
    roster slug and roster name when the identity is mapped, otherwise the
    provider's own id and name. The key is what decides whether two meeting
    identities are the same person; the name only decides the folder.
    """

    person: Callable[[str, str], tuple[str, str]]
    person_folder: Callable[[str], str]
    person_link: Callable[[str, int], str]
    project_link: Callable[[str, int], str]
    safe_filename: Callable[[str], str]


class MeetingWithheld(ValueError):
    """One meeting cannot be filed in the vault, so it is left out of it.

    Raised where a name a producer chose collides with a page the vault already
    owns. The render retries without the named occurrences: a collision costs
    the meeting it came from, never the whole render, which also carries the
    person pages, the project pages and the Work Journal.
    """

    def __init__(self, occurrence_ids, reason: str):
        super().__init__(reason)
        self.occurrence_ids = tuple(occurrence_ids)
        self.reason = reason


@dataclass(frozen=True)
class Attendee:
    key: str
    person_id: str
    display_name: str
    folder: str
    intervals: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True)
class MeetingNote:
    occurrence_id: str
    revision: int
    title: str
    started_at: str
    ended_at: str
    monday: date
    week: str
    file_name: str
    body: str
    projects: tuple[str, ...]
    attendees: tuple[Attendee, ...]

    @property
    def relative_path(self) -> PurePosixPath:
        return PurePosixPath(MEETINGS_DIR, self.week, self.file_name)


@dataclass(frozen=True)
class PersonBacklinks:
    key: str
    display_name: str
    person_ids: tuple[str, ...]
    occurrence_ids: tuple[str, ...]
    bullets: tuple[str, ...]


# --- instants ---------------------------------------------------------------


def _moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(DISPLAY_TZ)


def _day_and_minute(value: str) -> str:
    return _moment(value).strftime("%Y-%m-%d %H:%M")


def _clock(value: str) -> str:
    return _moment(value).strftime("%H:%M")


def _seconds_clock(value: str) -> str:
    return _moment(value).strftime("%H:%M:%S")


def _minutes(started_at: str, ended_at: str) -> str:
    total = int((_moment(ended_at) - _moment(started_at)).total_seconds() // 60)
    return f"{total} {'minute' if total == 1 else 'minutes'}"


# --- note body --------------------------------------------------------------


def _attendees(meeting: dict, naming: VaultNaming) -> tuple[Attendee, ...]:
    """Actual participants, in the order the meeting observed them.

    A participant with no observed attendance interval was never seen in the
    meeting — an invitee or a no-show — and is not an actual participant.
    """
    attendees = []
    for participant in meeting["participants"]:
        intervals = tuple(
            (interval["joined_at"], interval["left_at"])
            for interval in participant["attendance"]
        )
        if not intervals:
            continue
        key, name = naming.person(participant["person_id"], participant["display_name"])
        folder = naming.person_folder(name)
        if folder in (".", ".."):
            # A provider-supplied display name is untrusted input; a dot segment
            # would write the page outside Person/.
            raise MeetingWithheld(
                (meeting["occurrence_id"],),
                f"filename collision in vault render: Person/{folder} "
                f"from participant {participant['person_id']!r}")
        attendees.append(Attendee(
            key=key,
            person_id=participant["person_id"],
            display_name=name,
            folder=folder,
            intervals=intervals,
        ))
    return tuple(attendees)


def _spoke(meeting: dict) -> set[str]:
    """Person ids reconciled to a named speaker on at least one segment."""
    return {
        segment["speaker"]["person_id"]
        for segment in meeting["transcript"]
        if segment["speaker"]["kind"] == "person"
    }


def _intervals_text(attendee: Attendee) -> str:
    parts = []
    for joined_at, left_at in attendee.intervals:
        if left_at is None:
            parts.append(f"{_clock(joined_at)} onward")
        else:
            parts.append(f"{_clock(joined_at)}–{_clock(left_at)}")
    return ", ".join(parts)


def _bullets(lines: list[str]) -> str:
    return "".join(f"- {line}\n" for line in lines) if lines else NONE_LINE


def _details(meeting: dict) -> str:
    lines = [
        f"Started: {_day_and_minute(meeting['started_at'])} UTC",
        f"Ended: {_day_and_minute(meeting['ended_at'])} UTC",
        f"Duration: {_minutes(meeting['started_at'], meeting['ended_at'])}",
        f"Provider: {meeting['provider']} · revision {meeting['revision']}",
        "Transcript: complete" if meeting["transcript_complete"]
        else "Transcript: incomplete",
    ]
    if meeting["organizer_source"]:
        lines.append(f"Organizer identified from: {meeting['organizer_source']}")
    body = _bullets(lines)
    gaps = meeting["recording_gaps"]
    if gaps:
        body += "- Recording gaps:\n" + "".join(
            f"  - {_seconds_clock(gap['started_at'])}–{_seconds_clock(gap['ended_at'])}"
            f" — {gap['reason']}\n"
            for gap in gaps
        )
    else:
        body += "- Recording gaps: none\n"
    warnings = meeting["processing_warnings"]
    if warnings:
        # The code only. `detail` is the one note field a producer writes free
        # text into, up to 500 characters of it, and the vault is a git remote:
        # the ledger keeps the text for provenance, the note names the code.
        body += "- Processing warnings:\n" + "".join(
            f"  - {warning['code']}\n" for warning in warnings
        )
    else:
        body += "- Processing warnings: none\n"
    return body


def _action_items(meeting: dict, folders: dict[str, str], naming: VaultNaming) -> str:
    """Owners and due dates appear only when the meeting explicitly stated them."""
    lines = []
    for item in meeting["action_items"]:
        line = item["text"]
        owner = item["owner"]
        if owner and owner["explicitly_stated"]:
            folder = folders.get(owner["person_id"])
            name = naming.person(owner["person_id"], owner["display_name"])[1]
            line += " — owner " + (
                naming.person_link(folder, 2) if folder else name
            )
        due = item["due_date"]
        if due and due["explicitly_stated"]:
            line += f" · due {due['date']}"
        lines.append(line)
    return _bullets(lines)


def _organizer(meeting: dict, folders: dict[str, str], naming: VaultNaming) -> str:
    """The organizer's name, or the fixed line when the meeting had none.

    An instant meeting has no reservation and no calendar event, so no organizer
    is recorded. The host of the client session is not the organizer and is
    never rendered here.
    """
    organizer = meeting["organizer"]
    if organizer is None:
        return f"{ORGANIZER_UNAVAILABLE}\n"
    folder = folders.get(organizer["person_id"])
    if folder:
        return f"{naming.person_link(folder, 2)}\n"
    return f"{naming.person(organizer['person_id'], organizer['display_name'])[1]}\n"


def _per_person(meeting: dict, attendees: tuple[Attendee, ...], naming: VaultNaming) -> str:
    spoke = _spoke(meeting)
    by_id = {attendee.person_id: attendee for attendee in attendees}
    blocks = []
    for entry in meeting["person_summaries"]:
        attendee = by_id.get(entry["person_id"])
        if attendee is None or entry["person_id"] not in spoke:
            continue
        blocks.append(f"### {attendee.display_name}\n\n{entry['text'].rstrip()}\n")
    if not blocks:
        return NONE_LINE
    return "\n".join(blocks)


def _note_body(meeting: dict, attendees: tuple[Attendee, ...],
               week: str, monday: date, naming: VaultNaming, today: date) -> str:
    folders = {attendee.person_id: attendee.folder for attendee in attendees}
    sections = {
        "Meeting details": _details(meeting),
        "Summary": meeting["summary"].rstrip() + "\n",
        "Projects discussed": _bullets(
            [naming.project_link(project["name"], 2) for project in meeting["projects"]]),
        "Explicitly stated decisions": _bullets(
            [decision["text"] for decision in meeting["decisions"]]),
        "Action items": _action_items(meeting, folders, naming),
        "Key discussion points": _bullets(
            [point["text"] for point in meeting["discussion_points"]]),
        "Actual participants": "Observed join and leave times, UTC.\n\n" + _bullets(
            [f"{naming.person_link(attendee.folder, 2)} — {_intervals_text(attendee)}"
             for attendee in attendees]),
        "Organizer": _organizer(meeting, folders, naming),
        "Per-person summaries": _per_person(meeting, attendees, naming),
    }
    parts = [
        "---\n",
        f"occurrence_id: {_yaml(meeting['occurrence_id'])}\n",
        f"revision: {meeting['revision']}\n",
        f"week: {monday.isoformat()}\n",
        f"generated: {today.isoformat()}\n",
        "---\n",
        f"# {meeting['title']}\n",
        f"\n[{week}](README.md) · [Meetings](../README.md)\n",
    ]
    for heading in NOTE_HEADINGS:
        parts.append(f"\n## {heading}\n\n{sections[heading]}")
    return "".join(parts)


def _yaml(value: str) -> str:
    """A double-quoted YAML scalar. JSON string escaping is valid YAML."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --- collection -------------------------------------------------------------


def collect_notes(conn, *, naming: VaultNaming, today: date,
                  withhold=frozenset()) -> list[MeetingNote]:
    """One note per current occurrence, oldest first.

    Every current occurrence is rendered: no time window. The managed
    `Meetings/` subtree is wiped on every render, so a window would delete
    already-published notes from the live vault. `withhold` names the
    occurrences the render has already decided it cannot file.
    """
    notes = []
    for row in list_meetings(conn):
        if row["occurrence_id"] in withhold:
            continue
        meeting = get_meeting(conn, row["occurrence_id"])
        if meeting is None:                     # deleted between the two reads
            continue
        monday = week_monday(_moment(meeting["started_at"]).date())
        week = week_label(monday)
        attendees = _attendees(meeting, naming)
        topic = naming.safe_filename(meeting["title"])
        short = short_occurrence_id(meeting["occurrence_id"])
        started = _moment(meeting["started_at"])
        notes.append(MeetingNote(
            occurrence_id=meeting["occurrence_id"],
            revision=meeting["revision"],
            title=meeting["title"],
            started_at=meeting["started_at"],
            ended_at=meeting["ended_at"],
            monday=monday,
            week=week,
            file_name=NOTE_FILE_NAME.format(start=started, topic=topic, short=short),
            body=_note_body(meeting, attendees, week, monday, naming, today),
            projects=tuple(project["name"] for project in meeting["projects"]),
            attendees=attendees,
        ))
    return notes


# --- files and backlinks ----------------------------------------------------


def note_href(note: MeetingNote, up: int) -> str:
    return (f"{'../' * up}{MEETINGS_DIR}/{quote(note.week)}/{quote(note.file_name)}")


def note_link(note: MeetingNote, up: int) -> str:
    label = f"{_day_and_minute(note.started_at)} — {note.title}"
    return f"[{label}]({note_href(note, up)})"


def meeting_files(notes: list[MeetingNote], today: date) -> dict[PurePosixPath, str]:
    """Every file of the `Meetings/` projection, in write order."""
    by_week: dict[str, list[MeetingNote]] = {}
    for note in notes:
        by_week.setdefault(note.week, []).append(note)
    weeks = sorted(by_week, key=lambda week: (by_week[week][0].monday, week), reverse=True)

    index = [f"---\ngenerated: {today.isoformat()}\n---\n", f"# {MEETINGS_DIR}\n\n"]
    if weeks:
        for week in weeks:
            count = len(by_week[week])
            index.append(f"- [{week}]({quote(week)}/README.md) — "
                         f"{count} {'meeting' if count == 1 else 'meetings'}\n")
    else:
        index.append("- No meetings recorded.\n")

    files: dict[PurePosixPath, str] = {
        PurePosixPath(MEETINGS_DIR, "README.md"): "".join(index)
    }
    for week in weeks:
        week_notes = by_week[week]
        page = [f"---\nweek: {week_notes[0].monday.isoformat()}\n"
                f"generated: {today.isoformat()}\n---\n",
                f"# {week} — {MEETINGS_DIR}\n",
                "\n[Meetings](../README.md)\n\n"]
        for note in week_notes:
            people = len(note.attendees)
            projects = len(note.projects)
            page.append(
                f"- [{note.title}]({quote(note.file_name)}) — "
                f"{_day_and_minute(note.started_at)} UTC · "
                f"{people} {'participant' if people == 1 else 'participants'} · "
                f"{projects} {'project' if projects == 1 else 'projects'}\n")
        files[PurePosixPath(MEETINGS_DIR, week, "README.md")] = "".join(page)
        for note in week_notes:
            files[note.relative_path] = note.body
    return files


def person_backlinks(notes: list[MeetingNote]) -> dict[str, PersonBacklinks]:
    """Person folder -> the meetings that person actually attended, newest first.

    Two different people whose names land on the same folder is a collision the
    renderer must refuse, exactly as it refuses one between two projects.
    """
    collected: dict[str, dict] = {}
    for note in reversed(notes):
        for attendee in note.attendees:
            entry = collected.setdefault(attendee.folder, {
                "key": attendee.key,
                "display_name": attendee.display_name,
                "person_ids": [],
                "occurrence_ids": [],
                "bullets": [],
            })
            if entry["key"] != attendee.key:
                raise MeetingWithheld(
                    (note.occurrence_id,),
                    "filename collision in vault render: "
                    f"Person/{attendee.folder} claimed by both "
                    f"{entry['key']!r} and {attendee.key!r}")
            if attendee.person_id not in entry["person_ids"]:
                entry["person_ids"].append(attendee.person_id)
            if note.occurrence_id not in entry["occurrence_ids"]:
                entry["occurrence_ids"].append(note.occurrence_id)
            entry["bullets"].append(f"- {note_link(note, 2)}\n")
    return {
        folder: PersonBacklinks(
            key=entry["key"],
            display_name=entry["display_name"],
            person_ids=tuple(entry["person_ids"]),
            occurrence_ids=tuple(entry["occurrence_ids"]),
            bullets=tuple(entry["bullets"]),
        )
        for folder, entry in sorted(collected.items())
    }


def project_backlinks(notes: list[MeetingNote]) -> dict[str, tuple[str, ...]]:
    """Project name -> the meetings that discussed it, newest first."""
    collected: dict[str, list[str]] = {}
    for note in reversed(notes):
        for project in note.projects:
            collected.setdefault(project, []).append(f"- {note_link(note, 2)}\n")
    return {project: tuple(bullets) for project, bullets in sorted(collected.items())}


def person_page(backlinks: PersonBacklinks, today: date) -> str:
    """The whole page for someone the ledger knows only from meetings."""
    return "".join([
        "---\n",
        f"person_ids: {', '.join(backlinks.person_ids)}\n",
        f"generated: {today.isoformat()}\n",
        "---\n",
        f"# {backlinks.display_name}\n",
        "\n## Meetings\n\n",
        *backlinks.bullets,
    ])


def project_page(project: str, bullets: tuple[str, ...], today: date) -> str:
    """The whole page for a project the ledger knows only from meetings."""
    return "".join([
        "---\n",
        f"project: {project}\n",
        f"generated: {today.isoformat()}\n",
        "---\n",
        f"# {project}\n",
        "\n## Meetings\n\n",
        *bullets,
    ])


def meetings_section(bullets: tuple[str, ...]) -> str:
    return "\n## Meetings\n\n" + "".join(bullets)
