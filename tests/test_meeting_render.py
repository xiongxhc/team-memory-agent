"""The canonical meeting note and its backlinks.

Meetings are imported through `meeting_store.import_meeting_revision` from
supported contract fixtures, so what is rendered is what the store actually
holds. The vault must carry the summary note and its backlinks and never the
transcript or the audio.
"""

import copy
import dataclasses
import json
import os
import re
import shutil
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

import pytest

from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.meeting_bundles import (canonical_artifact_hash, load_meeting_bundle,
                                     short_occurrence_id)
from teammem.meeting_store import (StagedAudio, import_meeting_revision,
                                   list_meetings)
from teammem import meeting_render
from teammem import render as render_module
from teammem.render import (MANAGED, MEETING_INVARIANTS, render_vault,
                            verify_meeting_semantics, verify_vault)
from teammem.store import insert_events, open_db
from tests.test_meeting_bundles import write_meeting_bundle

CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"
FIXTURES = Path(__file__).parent / "fixtures" / "meetings"
COMPLETE = FIXTURES / "teammem-meeting-v1.complete.json"
V2_OVERLAP = FIXTURES / "teammem-meeting-v2.overlap.json"

TODAY = date(2026, 9, 3)
IMPORTED_AT = "2026-09-02T16:00:00Z"
AUDIO_ROOT = "/srv/teammem/audio-store"

SHORT = "02ef68db"
WEEK = "Week 2026-08-31-04"
NOTE = f"2026-09-02 14-00 Atlas Launch Readiness [{SHORT}].md"

HEADINGS = [
    "Meeting details",
    "Summary",
    "Projects discussed",
    "Explicitly stated decisions",
    "Action items",
    "Key discussion points",
    "Actual participants",
    "Organizer",
    "Per-person summaries",
]


def _allow_commit() -> None:
    """Renderer tests store facts without adopting filesystem audio."""


# --- fixtures ---------------------------------------------------------------


def _payload(*, day: str | None = None) -> dict:
    """The complete fixture, optionally moved wholesale to another day.

    Every instant in the meeting window carries 2026-09-02, so one substitution
    yields another internally consistent occurrence.
    """
    text = COMPLETE.read_text(encoding="utf-8")
    if day is not None:
        text = text.replace("2026-09-02", day)
    return json.loads(text)


SHORT_TURNS = ("Red fox seven.", "Blue kite eleven.", "Gold moon thirteen.")


def _short_transcript_payload(*texts: str) -> dict:
    """A real-schema fixture with no synthesis tied to removed segments."""
    payload = _payload()
    source = payload["transcript"]
    payload["transcript"] = []
    for index, text in enumerate(texts or SHORT_TURNS):
        segment = copy.deepcopy(source[index])
        segment["text"] = text
        payload["transcript"].append(segment)
    payload["summary"] = "A concise status exchange."
    payload["projects"] = []
    payload["decisions"] = []
    payload["action_items"] = []
    payload["discussion_points"] = []
    payload["person_summaries"] = []
    return payload


def _retitle(payload: dict, title: str) -> dict:
    payload = copy.deepcopy(payload)
    payload["title"] = title
    return payload


def _renumber(payload: dict, meeting_id: str) -> dict:
    """Give a payload a different meeting id, keeping the occurrence id consistent."""
    payload = copy.deepcopy(payload)
    payload["provider_meeting_id"] = meeting_id
    payload["occurrence_id"] = f"{meeting_id}@{payload['started_at']}"
    return payload


def _import(conn, payload: dict) -> str:
    occurrence_id = payload["occurrence_id"]
    import_meeting_revision(
        conn,
        payload=payload,
        occurrence_id=occurrence_id,
        revision=payload["revision"],
        artifact_hash=canonical_artifact_hash(payload),
        audio=StagedAudio(
            stored_path=f"{AUDIO_ROOT}/{short_occurrence_id(occurrence_id)}/audio.enc",
            sha256=payload["audio"]["sha256"],
            bytes=payload["audio"]["bytes"],
        ),
        imported_at=IMPORTED_AT,
        before_commit=_allow_commit,
    )
    return occurrence_id


def _seeded(tmp_path, *payloads, name="l.db"):
    conn = open_db(tmp_path / name)
    for payload in payloads or (_payload(),):
        _import(conn, payload)
    return conn


def _validated_seeded(tmp_path, payload: dict, name="l.db"):
    """Load the real on-disk bundle contract before placing it in the ledger."""
    inbox = tmp_path / "meeting-inbox"
    directory = write_meeting_bundle(inbox, payload)
    bundle = load_meeting_bundle(directory, inbox)
    conn = open_db(tmp_path / name)
    _import(conn, bundle.payload)
    return conn


def _render(tmp_path, conn, *, today=TODAY, name="vault") -> Path:
    vault = tmp_path / name
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, today)
    return vault


def _note(vault: Path, week: str = WEEK, note: str = NOTE) -> str:
    return (vault / "Meetings" / week / note).read_text()


def _sections(text: str) -> dict[str, str]:
    """Map each `## heading` to its body text."""
    parts = re.split(r"^## (.+)$", text, flags=re.M)[1:]
    return {parts[i].strip(): parts[i + 1] for i in range(0, len(parts), 2)}


def _vault_files(vault: Path) -> list[Path]:
    return sorted(p for p in vault.rglob("*") if p.is_file())


# --- path, naming, week -----------------------------------------------------


def test_meeting_note_lands_at_the_required_path(tmp_path):
    vault = _render(tmp_path, _seeded(tmp_path))
    assert (vault / "Meetings" / WEEK / "README.md").is_file()
    assert (vault / "Meetings" / WEEK / NOTE).is_file()


def test_note_headings_appear_in_the_required_order(tmp_path):
    note = _note(_render(tmp_path, _seeded(tmp_path)))
    assert re.findall(r"^# (.+)$", note, flags=re.M) == ["Atlas Launch Readiness"]
    assert re.findall(r"^## (.+)$", note, flags=re.M) == HEADINGS


def test_note_filename_is_sanitized_and_keeps_the_short_id(tmp_path):
    payload = _retitle(_payload(), 'Q3: launch/no-go? <final>')
    vault = _render(tmp_path, _seeded(tmp_path, payload))
    names = [p.name for p in (vault / "Meetings" / WEEK).iterdir() if p.name != "README.md"]
    assert names == [f"2026-09-02 14-00 Q3- launch-no-go- -final- [{SHORT}].md"]
    assert "/" not in names[0] and ":" not in names[0]      # no path separator survives


def test_same_title_in_one_week_does_not_collide(tmp_path):
    other = _renumber(_payload(), "vc:9f3a/board-sync-2")
    conn = _seeded(tmp_path, _payload(), other)
    vault = _render(tmp_path, conn)
    notes = sorted(p.name for p in (vault / "Meetings" / WEEK).iterdir()
                   if p.name != "README.md")
    assert len(notes) == 2                                  # the short id keeps them apart
    assert notes[0].startswith("2026-09-02 14-00 Atlas Launch Readiness [")
    assert notes[1].startswith("2026-09-02 14-00 Atlas Launch Readiness [")
    assert notes[0] != notes[1]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="POSIX TZ control only")
def test_note_time_is_the_meeting_start_in_utc_not_process_local_time(tmp_path):
    """A UTC+14 process must still file a 14:00Z meeting at 14-00 on 2026-09-02."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Kiritimati"
    time.tzset()
    try:
        vault = _render(tmp_path, _seeded(tmp_path))
        assert (vault / "Meetings" / WEEK / NOTE).is_file()
        assert "Started: 2026-09-02 14:00 UTC" in _note(vault)
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_cross_month_week_label(tmp_path):
    """queries.week_label truncates Friday to its day of month; meetings inherit it."""
    payload = _payload(day="2026-07-02")
    vault = _render(tmp_path, _seeded(tmp_path, payload), today=date(2026, 7, 3))
    week = vault / "Meetings" / "Week 2026-06-29-03"
    assert week.is_dir()
    assert [p.name for p in week.iterdir() if p.name != "README.md"] == [
        f"2026-07-02 14-00 Atlas Launch Readiness "
        f"[{short_occurrence_id(payload['occurrence_id'])}].md"
    ]


def test_every_current_occurrence_renders_with_no_time_window(tmp_path):
    """A window would delete published notes on the next render."""
    vault = _render(tmp_path, _seeded(tmp_path), today=date(2026, 11, 25))
    assert (vault / "Meetings" / WEEK / NOTE).is_file()


# --- section content --------------------------------------------------------


def test_meeting_details_carry_the_window_and_provenance(tmp_path):
    details = _sections(_note(_render(tmp_path, _seeded(tmp_path))))["Meeting details"]
    assert "- Started: 2026-09-02 14:00 UTC" in details
    assert "- Ended: 2026-09-02 14:52 UTC" in details
    assert "- Duration: 52 minutes" in details
    assert "- Transcript: complete" in details
    assert "bridge_reconnect" in details                    # recording gaps are stated
    assert "overlapping_speech_ambiguous" in details         # so are processing warnings


def test_summary_and_project_links(tmp_path):
    sections = _sections(_note(_render(tmp_path, _seeded(tmp_path))))
    assert "cleared the Borealis migration blocker" in sections["Summary"]
    assert sections["Projects discussed"].count("\n- ") == 3
    for project in ("project-atlas", "project-borealis", "project-cygnus"):
        assert (f"- [{project}](../../Projects/{project}/README.md)"
                in sections["Projects discussed"])


def test_decisions_and_discussion_points(tmp_path):
    sections = _sections(_note(_render(tmp_path, _seeded(tmp_path))))
    assert ("- The Atlas go/no-go call moves to Friday morning."
            in sections["Explicitly stated decisions"])
    assert ("- The staging soak has been green for six days."
            in sections["Key discussion points"])


def test_empty_optional_sections_say_none(tmp_path):
    payload = _payload()
    for key in ("projects", "decisions", "action_items",
                "discussion_points", "person_summaries"):
        payload[key] = []
    sections = _sections(_note(_render(tmp_path, _seeded(tmp_path, payload))))
    for heading in ("Projects discussed", "Explicitly stated decisions", "Action items",
                    "Key discussion points", "Per-person summaries"):
        assert sections[heading].strip() == "- none", heading
    assert "###" not in sections["Per-person summaries"]
    assert sections["Actual participants"].count("\n- ") == 8   # never optional


# --- organizer --------------------------------------------------------------


def test_organizer_section_names_the_organizer(tmp_path):
    note = _note(_render(tmp_path, _seeded(tmp_path)))
    organizer = _sections(note)["Organizer"].strip()
    assert organizer == "[Ada Chen](../../Person/Ada%20Chen/README.md)"
    assert "host" not in note.casefold()                     # never a host name


def test_null_organizer_renders_the_fixed_instant_meeting_line(tmp_path):
    payload = _payload()
    payload["organizer"] = None
    payload["organizer_source"] = None
    payload["organizer_unresolved_reason"] = "instant_meeting"
    note = _note(_render(tmp_path, _seeded(tmp_path, payload)))
    assert _sections(note)["Organizer"].strip() == "Not available (instant meeting)"
    assert "host" not in note.casefold()


# --- participants and per-person summaries ----------------------------------


def test_every_actual_participant_appears_in_section_seven(tmp_path):
    participants = _sections(_note(_render(tmp_path, _seeded(tmp_path))))["Actual participants"]
    for name in ("Ada Chen", "Ben Ortiz", "Chidi Okafor", "Dana Whitfield",
                 "Eli Novak", "Fen Zhao", "Gus Petrov", "Hana Suzuki"):
        assert f"[{name}](../../Person/" in participants
    assert "14:00–14:20, 14:24–14:52" in participants        # both observed intervals
    assert "14:01 onward" in participants                    # left_at is null


def test_a_participant_with_no_observed_attendance_is_not_an_actual_participant(tmp_path):
    """Invitees and no-shows carry no join/leave state and are not participants."""
    payload = _payload()
    payload["participants"].append(
        {"person_id": "ou_invited", "display_name": "Invited Noshow", "attendance": []})
    vault = _render(tmp_path, _seeded(tmp_path, payload))
    note = _note(vault)
    assert "Invited Noshow" not in note
    assert not (vault / "Person" / "Invited Noshow").exists()
    assert _sections(note)["Actual participants"].count("\n- ") == 8


def test_section_nine_covers_only_participants_who_spoke(tmp_path):
    note = _note(_render(tmp_path, _seeded(tmp_path)))
    per_person = _sections(note)["Per-person summaries"]
    assert re.findall(r"^### (.+)$", per_person, flags=re.M) == [
        "Ada Chen", "Ben Ortiz", "Chidi Okafor", "Dana Whitfield",
        "Eli Novak", "Fen Zhao", "Gus Petrov",
    ]
    assert "Hana Suzuki" not in per_person                    # joined, never spoke
    assert "Hana Suzuki" in _sections(note)["Actual participants"]


def test_a_summary_for_a_silent_joiner_is_not_rendered(tmp_path):
    payload = _payload()
    payload["person_summaries"].append(
        {"person_id": "ou_hana", "display_name": "Hana Suzuki", "text": "Said nothing at all."})
    per_person = _sections(_note(_render(tmp_path, _seeded(tmp_path, payload))))[
        "Per-person summaries"]
    assert "Said nothing at all." not in per_person
    assert "### Hana Suzuki" not in per_person


def test_anonymous_speaker_clusters_stay_anonymous(tmp_path):
    note = _note(_render(tmp_path, _seeded(tmp_path)))
    assert "spk_3" not in note                               # no cluster id, no guess
    assert "### Unknown" not in note


# --- action items -----------------------------------------------------------


def test_action_owner_and_due_date_render_only_when_explicitly_stated(tmp_path):
    items = _sections(_note(_render(tmp_path, _seeded(tmp_path))))["Action items"]
    assert ("- Write the Borealis migration runbook and circulate it. — owner "
            "[Dana Whitfield](../../Person/Dana%20Whitfield/README.md) · due 2026-09-03"
            in items)
    assert ("- Land the two open Atlas packaging items. — owner "
            "[Ben Ortiz](../../Person/Ben%20Ortiz/README.md)\n" in items)
    assert "- Review the open Cygnus interface note.\n" in items   # no owner, no due


def test_an_unstated_owner_or_due_date_is_not_rendered(tmp_path):
    payload = _payload()
    payload["action_items"][0]["owner"]["explicitly_stated"] = False
    payload["action_items"][0]["due_date"]["explicitly_stated"] = False
    items = _sections(_note(_render(tmp_path, _seeded(tmp_path, payload))))["Action items"]
    assert "- Write the Borealis migration runbook and circulate it.\n" in items
    assert "Dana Whitfield" not in items
    assert "2026-09-03" not in items


# --- backlinks --------------------------------------------------------------


def test_one_note_is_linked_from_every_project_and_every_participant(tmp_path):
    vault = _render(tmp_path, _seeded(tmp_path))
    notes = [p for p in (vault / "Meetings").rglob("*.md") if p.name != "README.md"]
    assert len(notes) == 1                                   # one canonical note
    target = f"../../Meetings/{WEEK}/{NOTE}".replace(" ", "%20").replace(
        "[", "%5B").replace("]", "%5D")

    for project in ("project-atlas", "project-borealis", "project-cygnus"):
        page = (vault / "Projects" / project / "README.md").read_text()
        assert target in page, project
    for name in ("Ada Chen", "Ben Ortiz", "Chidi Okafor", "Dana Whitfield",
                 "Eli Novak", "Fen Zhao", "Gus Petrov", "Hana Suzuki"):
        page = (vault / "Person" / name / "README.md").read_text()
        assert target in page, name


def test_the_note_body_is_never_duplicated_under_person_or_project(tmp_path):
    vault = _render(tmp_path, _seeded(tmp_path))
    summary = _payload()["summary"]
    carriers = [p for p in _vault_files(vault) if summary in p.read_text()]
    assert [p.name for p in carriers] == [NOTE]


def test_backlinks_land_on_the_existing_page_of_a_rostered_participant(tmp_path):
    payload = _payload()
    payload["participants"][0]["person_id"] = "ou_example_alex"
    payload["transcript"][0]["speaker"]["person_id"] = "ou_example_alex"
    payload["person_summaries"][0]["person_id"] = "ou_example_alex"
    payload["organizer"]["person_id"] = "ou_example_alex"
    conn = _seeded(tmp_path, payload)
    insert_events(conn, [Event(person="alex", ts="2026-09-02T09:00:00+04:00",
                               source="gitlab", kind="commit", summary="fix: a thing",
                               hash="a1", project="project-alpha")])
    vault = _render(tmp_path, conn)

    assert not (vault / "Person" / "Ada Chen").exists()       # resolved to the roster name
    page = (vault / "Person" / "Alex Rivera" / "README.md").read_text()
    assert "fix: a thing" in page                             # ledger work is still there
    assert f"../../Meetings/{WEEK.replace(' ', '%20')}/" in page


def test_meeting_only_project_gets_a_page_and_an_index_entry(tmp_path):
    vault = _render(tmp_path, _seeded(tmp_path))
    page = (vault / "Projects" / "project-atlas" / "README.md").read_text()
    assert page.startswith('---\nproject: "project-atlas"\n')
    assert "## Meetings" in page
    index = (vault / "Projects" / "README.md").read_text()
    assert "[project-atlas](project-atlas/README.md)" in index


def test_person_and_week_indexes_link_the_note(tmp_path):
    vault = _render(tmp_path, _seeded(tmp_path))
    week_index = (vault / "Meetings" / WEEK / "README.md").read_text()
    assert NOTE.replace(" ", "%20").replace("[", "%5B").replace("]", "%5D") in week_index
    top = (vault / "Meetings" / "README.md").read_text()
    assert f"[{WEEK}]({WEEK.replace(' ', '%20')}/README.md)" in top
    assert "[Meetings](Meetings/README.md)" in (vault / "README.md").read_text()


@pytest.mark.parametrize(("title", "label"), [
    ("Agenda [draft", r"Agenda \[draft"),
    ("Agenda final]", r"Agenda final\]"),
    ("Agenda \\ path", r"Agenda \\ path"),
    ("Agenda [draft] \\ path", r"Agenda \[draft\] \\ path"),
    ("Sprint [3] Review", r"Sprint \[3\] Review"),
])
def test_title_metacharacters_keep_every_note_link_valid_and_deterministic(
        tmp_path, title, label):
    payload = _retitle(_payload(), title)
    conn = _validated_seeded(tmp_path, payload)
    first = _render(tmp_path, conn, name="one")
    second = _render(tmp_path, conn, name="two")
    notes = [path for path in (first / "Meetings" / WEEK).glob("*.md")
             if path.name != "README.md"]
    assert len(notes) == 1
    note = notes[0]
    target = quote(note.name)

    week_index = (note.parent / "README.md").read_text()
    assert f"- [{label}]({target}) — " in week_index
    backlink = f"[2026-09-02 14:00 — {label}](../../Meetings/{quote(WEEK)}/{target})"
    for page in [
        first / "Person" / "Ada Chen" / "README.md",
        first / "Projects" / "project-atlas" / "README.md",
    ]:
        assert backlink in page.read_text()

    left = {path.relative_to(first).as_posix(): path.read_bytes()
            for path in _vault_files(first)}
    right = {path.relative_to(second).as_posix(): path.read_bytes()
             for path in _vault_files(second)}
    assert left == right
    assert verify_vault(
        conn, IdentityMaps.load(CONFIG_DIR), first, TODAY, semantic=True
    ) == {"missing": [], "unexpected": [], "differing": [], "semantic": []}


def test_participant_and_project_metacharacters_are_escaped_only_in_link_labels(tmp_path):
    payload = _retitle(_payload(), "Agenda [draft] \\ path")
    person = "Ada [Lead] \\ Ops"
    project = "project-[atlas]\\ops"
    for value in [payload["organizer"], *payload["participants"],
                  *(segment["speaker"] for segment in payload["transcript"]),
                  *(item["owner"] for item in payload["action_items"] if item["owner"]),
                  *payload["person_summaries"]]:
        if value.get("person_id") == "ou_ada":
            value["display_name"] = person
    payload["projects"][0]["name"] = project

    conn = _validated_seeded(tmp_path, payload)
    vault = _render(tmp_path, conn)
    note = next(path for path in (vault / "Meetings" / WEEK).glob("*.md")
                if path.name != "README.md")
    text = note.read_text()
    assert "# Agenda [draft] \\ path\n" in text
    assert (r"[Ada \[Lead\] - Ops](../../Person/Ada%20%5BLead%5D%20-%20Ops/README.md)"
            in text)
    assert (r"[project-\[atlas\]\\ops]"
            r"(../../Projects/project-%5Batlas%5D-ops/README.md)" in text)
    project_index = (vault / "Projects" / "README.md").read_text()
    assert (r"[project-\[atlas\]\\ops](project-%5Batlas%5D-ops/README.md)"
            in project_index)
    assert verify_vault(
        conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY, semantic=True
    )["semantic"] == []


def test_no_wikilinks_anywhere_under_meetings(tmp_path):
    vault = _render(tmp_path, _seeded(tmp_path))
    for path in (vault / "Meetings").rglob("*.md"):
        assert "[[" not in path.read_text(), path


# --- transcript and audio containment ---------------------------------------


def test_no_transcript_text_and_no_audio_reaches_the_vault(tmp_path):
    payload = _payload()
    vault = _render(tmp_path, _seeded(tmp_path, payload))
    contents = {p: p.read_text() for p in _vault_files(vault)}
    assert contents                                           # the walk is not vacuous

    distinctive = "That is everything on the checklist, thank you all."
    assert distinctive == payload["transcript"][-1]["text"]
    for path, text in contents.items():
        assert distinctive not in text, path
        for segment in payload["transcript"]:
            assert segment["text"] not in text, (path, segment["segment_id"])
        assert payload["audio"]["filename"] not in text, path
        assert payload["audio"]["sha256"] not in text, path
        assert AUDIO_ROOT not in text, path
        assert "audio" not in text.casefold(), path


def test_v2_overlap_keeps_transcript_warning_detail_and_audio_out_of_vault(tmp_path):
    payload = json.loads(V2_OVERLAP.read_text(encoding="utf-8"))
    vault = _render(tmp_path, _validated_seeded(tmp_path, payload))
    contents = {path: path.read_text() for path in _vault_files(vault)}

    assert contents
    for path, text in contents.items():
        for segment in payload["transcript"]:
            assert segment["text"] not in text, (path, segment["segment_id"])
        assert payload["processing_warnings"][0]["detail"] not in text, path
        assert payload["audio"]["filename"] not in text, path
        assert payload["audio"]["sha256"] not in text, path
        assert AUDIO_ROOT not in text, path
    note = next(
        text for path, text in contents.items()
        if path.parent.name == "Week 2026-08-31-04" and path.name != "README.md"
    )
    assert "- Duration: 7 minutes" in note
    assert "- overlapping_observations_retained" in note


# --- projection management --------------------------------------------------


def test_meetings_is_a_managed_projection(tmp_path):
    assert "Meetings" in MANAGED
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    stale = vault / "Meetings" / "Week 1999-01-04-08" / "hand written.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale\n")
    _render(tmp_path, conn)
    assert not stale.exists()
    assert (vault / "Meetings" / WEEK / NOTE).is_file()


def test_rerender_is_byte_for_byte_identical(tmp_path):
    conn = _seeded(tmp_path)
    first = _render(tmp_path, conn, name="one")
    second = _render(tmp_path, conn, name="two")
    left = {p.relative_to(first).as_posix(): p.read_bytes() for p in _vault_files(first)}
    right = {p.relative_to(second).as_posix(): p.read_bytes() for p in _vault_files(second)}
    assert left == right
    assert any(path.startswith("Meetings/") for path in left)


def test_verify_vault_covers_the_meetings_projection(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    ids = IdentityMaps.load(CONFIG_DIR)
    assert verify_vault(conn, ids, vault, TODAY) == {
        "missing": [], "unexpected": [], "differing": []}
    (vault / "Meetings" / WEEK / NOTE).write_text("hand edited\n")
    assert verify_vault(conn, ids, vault, TODAY)["differing"] == [
        f"Meetings/{WEEK}/{NOTE}"]


def test_a_vault_with_no_meetings_still_renders(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [Event(person="alex", ts="2026-09-02T09:00:00+04:00",
                               source="gitlab", kind="commit", summary="fix: a thing",
                               hash="a1", project="project-alpha")])
    vault = _render(tmp_path, conn)
    assert (vault / "Meetings" / "README.md").read_text().endswith(
        "- No meetings recorded.\n")
    assert not any(p.is_dir() for p in (vault / "Meetings").iterdir())


def test_a_dot_segment_display_name_costs_the_meeting_not_the_render(tmp_path):
    """Provider display names are untrusted; `..` must never become a folder."""
    payload = _payload()
    payload["participants"][0]["display_name"] = ".."
    conn = _seeded(tmp_path, payload)
    vault = tmp_path / "vault"
    out = render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    assert list(out["withheld"]) == [payload["occurrence_id"]]
    assert "Person/.." in out["withheld"][payload["occurrence_id"]]
    assert not (vault / "Meetings" / WEEK).exists()      # the meeting is not filed
    assert (vault / "Work Journal").is_dir()             # the render still ran


def test_an_unrostered_participant_cannot_take_over_a_rostered_person_page(tmp_path):
    """Two different people must never be merged onto one Person folder."""
    payload = _payload()
    payload["participants"][0]["display_name"] = "Alex Rivera"   # ou_ada, not the roster alex
    conn = _seeded(tmp_path, payload)
    insert_events(conn, [Event(person="alex", ts="2026-09-02T09:00:00+04:00",
                               source="gitlab", kind="commit", summary="fix: a thing",
                               hash="a1", project="project-alpha")])
    vault = tmp_path / "vault"
    out = render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    assert list(out["withheld"]) == [payload["occurrence_id"]]
    page = (vault / "Person" / "Alex Rivera" / "README.md").read_text()
    assert "fix: a thing" in page and "Meetings" not in page   # still the roster page


def test_a_colliding_project_name_costs_the_later_meeting_only(tmp_path):
    """One producer's casing choice must not unpublish another meeting."""
    other = _renumber(_payload(day="2026-09-01"), "vc:9f3a/board-sync-2")
    for project in other["projects"]:
        project["name"] = project["name"].upper()
    conn = _seeded(tmp_path, _payload(), other)
    vault = tmp_path / "vault"
    out = render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    assert list(out["withheld"]) == [_payload()["occurrence_id"]]
    assert (vault / "Projects" / "PROJECT-ATLAS" / "README.md").is_file()
    assert [p.name for p in (vault / "Meetings").rglob("*.md")
            if p.name != "README.md"] == [
        f"2026-09-01 14-00 Atlas Launch Readiness "
        f"[{short_occurrence_id(other['occurrence_id'])}].md"]
    assert _findings(conn, vault) == [{
        "invariant": ONE_PER_OCCURRENCE,
        "path": f"Meetings/{WEEK}/{NOTE}",
        "detail": "no canonical note for a current occurrence",
        "occurrence_id": _payload()["occurrence_id"],
    }]


# --- semantic verification --------------------------------------------------
#
# `verify_vault` re-renders and diffs: it proves the vault matches what the
# renderer would write right now. These tests cover the other half — that what
# the renderer wrote satisfies the meeting invariants at all. Every negative
# fixture is a one-line corruption of a known-good rendered tree, and every one
# of them asserts the exact set of invariants it broke, so the checks are proven
# independent rather than one blanket failure.


ONE_PER_OCCURRENCE = "meeting.note.one-per-occurrence"
HEADING_ORDER = "meeting.note.heading-order"
PER_PERSON = "meeting.note.per-person-summaries"
REVISION_CURRENT = "meeting.note.revision-current"
WEEK_INDEX = "meeting.week-index.membership"
PERSON_BACKLINKS = "meeting.person.backlinks"
PROJECT_BACKLINKS = "meeting.project.backlinks"
LINK_RESOLVES = "meeting.link.resolves"
LEAKAGE = "meeting.leakage.forbidden-content"


def _findings(conn, vault: Path) -> list[dict]:
    return verify_meeting_semantics(conn, IdentityMaps.load(CONFIG_DIR), vault)


def _broken(conn, vault: Path) -> set[str]:
    return {f["invariant"] for f in _findings(conn, vault)}


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"fixture drifted: {old!r} not in {path}"
    path.write_text(text.replace(old, new, 1))


def _tail(week: str = WEEK, note: str = NOTE) -> str:
    return f"{quote(week)}/{quote(note)}"


def test_a_clean_vault_satisfies_every_meeting_invariant(tmp_path):
    conn = _seeded(tmp_path)
    assert _findings(conn, _render(tmp_path, conn)) == []


def test_a_clean_multi_meeting_vault_with_a_ledger_is_also_clean(tmp_path):
    """The checks must be quiet on a vault that also carries event activity."""
    conn = _seeded(tmp_path, _payload(), _renumber(_payload(day="2026-06-30"), "vc:9f3b/x"))
    insert_events(conn, [Event(person="alex", ts="2026-09-02T09:00:00+04:00",
                               source="gitlab", kind="commit", summary="fix: a thing",
                               hash="a1", project="project-alpha")])
    assert _findings(conn, _render(tmp_path, conn)) == []


def test_verification_never_writes_to_the_vault(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    before = {p: p.read_bytes() for p in sorted(vault.rglob("*")) if p.is_file()}
    _edit(vault / "Projects" / "project-cygnus" / "README.md", "- [2026-09-02", "- [x](y) [2026-09-02")
    after_edit = {p: p.read_bytes() for p in sorted(vault.rglob("*")) if p.is_file()}
    _findings(conn, vault)
    assert {p: p.read_bytes() for p in sorted(vault.rglob("*")) if p.is_file()} == after_edit
    assert before != after_edit          # the snapshot really is byte-sensitive


def test_verify_vault_keeps_its_three_key_contract_and_adds_semantics_on_request(tmp_path):
    conn = _seeded(tmp_path)
    ids = IdentityMaps.load(CONFIG_DIR)
    vault = _render(tmp_path, conn)
    assert verify_vault(conn, ids, vault, TODAY) == {
        "missing": [], "unexpected": [], "differing": []}
    assert verify_vault(conn, ids, vault, TODAY, semantic=True) == {
        "missing": [], "unexpected": [], "differing": [], "semantic": []}


def test_a_renderer_bug_the_diff_cannot_see_fails_the_semantic_check(tmp_path, monkeypatch):
    """The whole point of the task: re-rendering agrees with a broken renderer."""
    conn = _seeded(tmp_path)
    ids = IdentityMaps.load(CONFIG_DIR)
    real = meeting_render.project_backlinks

    def _forgets_cygnus(notes):
        out = dict(real(notes))
        out["project-cygnus"] = ()
        return out

    monkeypatch.setattr(meeting_render, "project_backlinks", _forgets_cygnus)
    vault = _render(tmp_path, conn)
    assert _tail() not in (vault / "Projects" / "project-cygnus" / "README.md").read_text()
    out = verify_vault(conn, ids, vault, TODAY, semantic=True)
    assert (out["missing"], out["unexpected"], out["differing"]) == ([], [], [])
    assert [f["invariant"] for f in out["semantic"]] == [PROJECT_BACKLINKS]
    assert out["semantic"][0]["path"] == "Projects/project-cygnus/README.md"


def test_a_missing_project_backlink_fails_only_the_project_invariant(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    page = vault / "Projects" / "project-cygnus" / "README.md"
    page.write_text("".join(line for line in page.read_text().splitlines(keepends=True)
                            if _tail() not in line))
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [PROJECT_BACKLINKS]
    assert found[0]["path"] == "Projects/project-cygnus/README.md"


def test_a_backlink_on_a_project_the_meeting_never_discussed_is_reported(tmp_path):
    conn = _seeded(tmp_path)
    insert_events(conn, [Event(person="alex", ts="2026-09-02T09:00:00+04:00",
                               source="gitlab", kind="commit", summary="fix: a thing",
                               hash="a1", project="project-alpha")])
    vault = _render(tmp_path, conn)
    page = vault / "Projects" / "project-alpha" / "README.md"
    page.write_text(page.read_text() + f"\n- [meeting](../../Meetings/{_tail()})\n")
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [PROJECT_BACKLINKS]
    assert found[0]["path"] == "Projects/project-alpha/README.md"


def test_a_missing_participant_backlink_fails_only_the_person_invariant(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    page = vault / "Person" / "Hana Suzuki" / "README.md"
    page.write_text("".join(line for line in page.read_text().splitlines(keepends=True)
                            if _tail() not in line))
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [PERSON_BACKLINKS]
    assert found[0]["path"] == "Person/Hana Suzuki/README.md"


def test_a_backlink_on_a_non_participant_page_is_reported(tmp_path):
    conn = _seeded(tmp_path)
    insert_events(conn, [Event(person="alex", ts="2026-09-02T09:00:00+04:00",
                               source="gitlab", kind="commit", summary="fix: a thing",
                               hash="a1", project="project-alpha")])
    vault = _render(tmp_path, conn)
    page = vault / "Person" / "Alex Rivera" / "README.md"
    page.write_text(page.read_text() + f"\n- [meeting](../../Meetings/{_tail()})\n")
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [PERSON_BACKLINKS]
    assert found[0]["path"] == "Person/Alex Rivera/README.md"


def test_a_per_person_section_for_a_silent_participant_fails_only_that_invariant(tmp_path):
    """Hana joined at 14:04 and never spoke: section 7 yes, section 9 never."""
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    note = vault / "Meetings" / WEEK / NOTE
    note.write_text(note.read_text() + "\n### Hana Suzuki\n\nJoined and listened.\n")
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [PER_PERSON]
    assert found[0]["path"] == f"Meetings/{WEEK}/{NOTE}"
    assert "Hana Suzuki" in found[0]["detail"]


def test_a_duplicate_note_fails_only_the_one_note_per_occurrence_invariant(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    note = vault / "Meetings" / WEEK / NOTE
    copy_name = f"2026-09-02 14-00 Atlas Launch Readiness [{SHORT}] (1).md"
    (note.parent / copy_name).write_text(note.read_text())
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [ONE_PER_OCCURRENCE]
    assert found[0]["path"] == f"Meetings/{WEEK}/{copy_name}"


def test_a_missing_note_for_a_current_occurrence_is_reported(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    (vault / "Meetings" / WEEK / NOTE).unlink()
    assert ONE_PER_OCCURRENCE in _broken(conn, vault)


def test_a_note_for_an_occurrence_the_ledger_does_not_hold_is_reported(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    note = vault / "Meetings" / WEEK / NOTE
    stray = note.parent / "2026-09-02 15-00 Ghost [deadbeef].md"
    stray.write_text(note.read_text().replace('board-sync@', 'ghost-sync@'))
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [ONE_PER_OCCURRENCE]
    assert found[0]["path"] == f"Meetings/{WEEK}/{stray.name}"


def test_a_stale_revision_on_disk_fails_only_the_revision_invariant(tmp_path):
    """Import a higher revision, restore the old bytes: the vault is behind."""
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    stale = (vault / "Meetings" / WEEK / NOTE).read_text()
    assert "revision: 3\n" in stale
    newer = _payload()
    newer["revision"] = 4
    _import(conn, newer)
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    note = vault / "Meetings" / WEEK / NOTE
    assert "revision: 4\n" in note.read_text()
    note.write_text(stale)
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [REVISION_CURRENT]
    assert found[0]["path"] == f"Meetings/{WEEK}/{NOTE}"
    assert "3" in found[0]["detail"] and "4" in found[0]["detail"]


def test_a_broken_relative_link_fails_only_the_link_invariant(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    _edit(vault / "Meetings" / WEEK / NOTE, "[Meetings](../README.md)",
          "[Meetings](../READMEE.md)")
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [LINK_RESOLVES]
    assert found[0]["path"] == f"Meetings/{WEEK}/{NOTE}"
    assert "READMEE.md" in found[0]["detail"]


def test_a_link_escaping_the_vault_is_reported(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    _edit(vault / "Meetings" / WEEK / NOTE, "[Meetings](../README.md)",
          "[Meetings](../../../etc/hosts)")
    assert _broken(conn, vault) == {LINK_RESOLVES}


def test_a_week_index_that_omits_a_note_fails_the_membership_invariant(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    index = vault / "Meetings" / WEEK / "README.md"
    index.write_text("".join(line for line in index.read_text().splitlines(keepends=True)
                             if quote(NOTE) not in line))
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [WEEK_INDEX]
    assert found[0]["path"] == f"Meetings/{WEEK}/README.md"


def test_a_week_index_that_lists_a_stranger_fails_the_membership_invariant(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    index = vault / "Meetings" / WEEK / "README.md"
    (index.parent / "notes.txt").write_text("x")
    index.write_text(index.read_text() + "- [something](notes.txt)\n")
    assert _broken(conn, vault) == {WEEK_INDEX}


def test_a_top_index_that_omits_a_week_fails_the_membership_invariant(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    index = vault / "Meetings" / "README.md"
    index.write_text("".join(line for line in index.read_text().splitlines(keepends=True)
                             if quote(WEEK) not in line))
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [WEEK_INDEX]
    assert found[0]["path"] == "Meetings/README.md"


def test_out_of_order_sections_fail_the_heading_invariant(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    _edit(vault / "Meetings" / WEEK / NOTE, "## Organizer", "## Zzz Organizer")
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [HEADING_ORDER]
    assert found[0]["path"] == f"Meetings/{WEEK}/{NOTE}"


def test_a_retitled_note_fails_the_heading_invariant(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    _edit(vault / "Meetings" / WEEK / NOTE, "# Atlas Launch Readiness", "# Something Else")
    assert _broken(conn, vault) == {HEADING_ORDER}


def test_transcript_text_in_the_vault_is_reported_without_quoting_it(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    sentence = _payload()["transcript"][2]["text"]
    page = vault / "Person" / "Hana Suzuki" / "README.md"
    page.write_text(page.read_text() + f"\n{sentence}\n")
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [LEAKAGE]
    assert found[0]["path"] == "Person/Hana Suzuki/README.md"
    assert sentence not in json.dumps(found)
    assert "staging soak" not in json.dumps(found)


@pytest.mark.parametrize("copied", [
    "\n".join(SHORT_TURNS),
    "".join(SHORT_TURNS),
    "Red\tfox seven. Blue\u2003kite eleven.\nGold moon\tthirteen.",
])
def test_a_complete_all_short_transcript_is_forbidden_across_spacing_variants(
    tmp_path, copied
):
    payload = _short_transcript_payload()
    conn = _validated_seeded(tmp_path, payload)
    vault = _render(tmp_path, conn)
    page = vault / "Docs" / "preserved.md"
    page.parent.mkdir()
    page.write_text(f"# Preserved notes\n\n{copied}\n", encoding="utf-8")

    found = _findings(conn, vault)

    assert [item["invariant"] for item in found] == [LEAKAGE]
    assert found[0]["path"] == "Docs/preserved.md"
    diagnostics = json.dumps(found)
    assert all(turn not in diagnostics for turn in SHORT_TURNS)
    assert "Redfoxseven.Bluekiteeleven.Goldmoonthirteen." not in diagnostics


def test_short_turn_fragments_do_not_match_without_the_complete_ordered_transcript(
    tmp_path,
):
    payload = _short_transcript_payload()
    conn = _validated_seeded(tmp_path, payload)
    vault = _render(tmp_path, conn)
    page = vault / "Docs" / "sound-notes.md"
    page.parent.mkdir()
    page.write_text(
        "# Sound notes\n\n"
        "Red fox seven. This isolated phrase is ordinary.\n"
        "Gold moon thirteen. Blue kite eleven. Red fox seven.\n"
        "Red fox seven.Blue kite eleventh.Gold moon thirteen.\n"
        "red fox seven.blue kite eleven.gold moon thirteen.\n"
        "Red fox seven Blue kite eleven Gold moon thirteen\n",
        encoding="utf-8",
    )

    assert _findings(conn, vault) == []


@pytest.mark.parametrize(("page_text", "leaks"), [
    ("Yesterday", False),
    ("## Status\nYes\n", True),
    ("Yesterday\nYes", True),
])
def test_a_tiny_complete_transcript_requires_lexical_boundaries(
    tmp_path, page_text, leaks
):
    payload = _short_transcript_payload("Yes")
    conn = _validated_seeded(tmp_path, payload)
    vault = _render(tmp_path, conn)
    page = vault / "Docs" / "tiny.md"
    page.parent.mkdir()
    page.write_text(page_text, encoding="utf-8")

    found = _findings(conn, vault)

    assert bool(found) is leaks
    assert {item["invariant"] for item in found} <= {LEAKAGE}
    assert "Yes" not in json.dumps(found)


def test_a_complete_all_short_transcript_from_a_superseded_revision_is_forbidden(
    tmp_path,
):
    old = _short_transcript_payload()
    conn = _validated_seeded(tmp_path, old)
    current = _payload()
    current["revision"] = old["revision"] + 1
    _import(conn, current)
    vault = _render(tmp_path, conn)
    page = vault / "Docs" / "old.md"
    page.parent.mkdir()
    page.write_text("".join(SHORT_TURNS), encoding="utf-8")

    assert _broken(conn, vault) == {LEAKAGE}


def test_an_audio_path_in_the_vault_is_reported_without_quoting_it(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    stored = f"{AUDIO_ROOT}/{SHORT}/audio.enc"
    (vault / "Meetings" / "leak.txt").write_text(f"{stored}\n")
    found = _findings(conn, vault)
    assert {f["invariant"] for f in found} == {LEAKAGE}
    assert {f["path"] for f in found} == {"Meetings/leak.txt"}
    assert {f["detail"] for f in found} == {
        "audio store path from the ledger appears in this file",
        "audio file name from the ledger appears in this file"}
    assert stored not in json.dumps(found)
    assert AUDIO_ROOT not in json.dumps(found)


def test_a_transcript_from_a_superseded_revision_is_still_forbidden(tmp_path):
    """A revision the vault no longer renders keeps its transcript secret."""
    payload = _payload()
    payload["transcript"][0]["text"] = "The superseded revision said this one sentence only."
    conn = _seeded(tmp_path, payload)
    newer = _payload()
    newer["revision"] = 4
    _import(conn, newer)
    vault = _render(tmp_path, conn)
    assert _findings(conn, vault) == []
    (vault / "Meetings" / "old.txt").write_text(payload["transcript"][0]["text"])
    assert _broken(conn, vault) == {LEAKAGE}


def test_findings_are_sorted_and_carry_only_paths_and_invariant_names(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    (vault / "Meetings" / WEEK / NOTE).unlink()
    found = _findings(conn, vault)
    assert found == sorted(found, key=lambda f: (f["invariant"], f["path"], f["detail"]))
    assert all(set(f) == {"invariant", "path", "detail", "occurrence_id"}
               for f in found)
    assert {f["invariant"] for f in found} <= set(MEETING_INVARIANTS)
    # every finding is either the fault of a current occurrence or of nothing
    # smaller than the projection itself
    current = {row["occurrence_id"] for row in list_meetings(conn)}
    assert all(f["occurrence_id"] in current or f["occurrence_id"] is None
               for f in found)


def test_a_no_show_is_not_expected_to_have_a_page_or_a_backlink(tmp_path):
    """Truth is the ledger's observed attendance, not its invitee list."""
    payload = _payload()
    payload["participants"].append(
        {"person_id": "ou_invited", "display_name": "Invited Noshow", "attendance": []})
    conn = _seeded(tmp_path, payload)
    vault = _render(tmp_path, conn)
    assert not (vault / "Person" / "Invited Noshow").exists()
    assert _findings(conn, vault) == []


def test_a_summary_for_a_silent_joiner_is_not_expected_in_the_note(tmp_path):
    """A person_summaries row for someone who never spoke must stay unrendered."""
    payload = _payload()
    payload["person_summaries"].append(
        {"person_id": "ou_hana", "display_name": "Hana Suzuki", "text": "Said nothing at all."})
    conn = _seeded(tmp_path, payload)
    vault = _render(tmp_path, conn)
    assert "### Hana Suzuki" not in _note(vault)
    assert _findings(conn, vault) == []


def test_a_note_moved_off_its_canonical_path_is_reported(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    note = vault / "Meetings" / WEEK / NOTE
    note.rename(note.parent / "renamed.md")
    found = _findings(conn, vault)
    assert [f for f in found if f["invariant"] == ONE_PER_OCCURRENCE] == [{
        "invariant": ONE_PER_OCCURRENCE,
        "path": f"Meetings/{WEEK}/renamed.md",
        "detail": f"the note is not at its canonical path Meetings/{WEEK}/{NOTE}",
        "occurrence_id": _payload()["occurrence_id"],
    }]


def test_a_missing_project_page_is_reported_as_a_backlink_failure(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    shutil.rmtree(vault / "Projects" / "project-cygnus")
    assert {"invariant": PROJECT_BACKLINKS,
            "path": "Projects/project-cygnus/README.md",
            "detail": "no page at all for a meeting backlink",
            "occurrence_id": _payload()["occurrence_id"]} in _findings(conn, vault)


def test_the_expected_note_path_comes_from_the_ledger_not_from_the_renderer(tmp_path):
    """`NOTE_FILE_NAME` is shared; its arguments are not.

    The renderer files the note under a name of its own invention and stays
    entirely self-consistent — index, backlinks and re-render all agree. The
    verifier rebuilds the canonical name from `meeting_store` facts, so it still
    knows where the note belonged.
    """
    conn = _seeded(tmp_path)
    ids = IdentityMaps.load(CONFIG_DIR)
    real = meeting_render.collect_notes

    def _misfiles(conn, *, naming, today, withhold=frozenset()):
        return [dataclasses.replace(note, file_name="wrong-name.md")
                for note in real(conn, naming=naming, today=today, withhold=withhold)]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(meeting_render, "collect_notes", _misfiles)
    try:
        vault = _render(tmp_path, conn)
        assert (vault / "Meetings" / WEEK / "wrong-name.md").is_file()
        out = verify_vault(conn, ids, vault, TODAY, semantic=True)
    finally:
        monkeypatch.undo()
    assert (out["missing"], out["unexpected"], out["differing"]) == ([], [], [])
    assert out["semantic"] == [{
        "invariant": ONE_PER_OCCURRENCE,
        "path": f"Meetings/{WEEK}/wrong-name.md",
        "detail": f"the note is not at its canonical path Meetings/{WEEK}/{NOTE}",
        "occurrence_id": _payload()["occurrence_id"],
    }]


def test_a_note_for_an_occurrence_the_ledger_dropped_is_a_vault_wide_finding(tmp_path):
    """Nobody can publish a non-current occurrence, so a finding attributed to
    one could never block anything. It belongs to the projection as a whole."""
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    note = vault / "Meetings" / WEEK / NOTE
    (note.parent / "stray.md").write_text(
        note.read_text(encoding="utf-8").replace(
            _payload()["occurrence_id"], "vc:9f3a/board-sync@2019-01-01T00:00:00Z"),
        encoding="utf-8")

    found = [f for f in _findings(conn, vault) if f["invariant"] == ONE_PER_OCCURRENCE]
    assert [f["detail"] for f in found] == [
        "a note for an occurrence the ledger does not hold as current"]
    assert found[0]["occurrence_id"] is None


# --- what a meeting is answerable for ---------------------------------------
# The Work Journal projection keeps four weeks while a person page keeps the
# whole history, so a person page always links weeks the journal no longer
# renders. Those links dangle by design and no meeting can repair them.

STALE = [
    Event(person="alex", ts="2026-07-14T09:00:00+04:00", source="gitlab",
          kind="commit", summary="fix: JWT refresh race", hash="s1",
          project="project-alpha"),
    Event(person="sam", ts="2026-07-01T09:00:00+04:00", source="gitlab",
          kind="commit", summary="feat: portal nav", hash="s2",
          project="project-beta"),
]
STALE_WEEK_LINK = "../../Work%20Journal/Week%202026-07-13-17.md"


def test_link_targets_carry_balanced_parentheses_to_any_depth():
    assert render_module._md_links("[a](b(c(d)e)f) and [g](h.md)") == [
        "b(c(d)e)f", "h.md"]
    assert render_module._md_links("[r]( x.md)") == []      # a space ends a target
    assert render_module._md_links("[u](https://x/a1)") == ["https://x/a1"]


def test_link_labels_honor_escaped_delimiters_and_backslash_parity():
    text = (r"[label \[draft\] \\ path](note.md) "
            r"\[escaped opener](fake.md) "
            r"\\[even opener](even.md) "
            r"\\\[odd opener](also-fake.md) "
            r"[two slashes\\](slashes.md)")
    assert render_module._md_links(text) == ["note.md", "even.md", "slashes.md"]


def test_activity_older_than_the_journal_window_raises_no_meeting_finding(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, STALE)
    vault = _render(tmp_path, conn)
    assert STALE_WEEK_LINK in (vault / "Person" / "Alex Rivera" / "README.md").read_text()
    assert not (vault / "Work Journal" / "Week 2026-07-13-17.md").exists()
    assert _findings(conn, vault) == []


def test_a_meeting_on_a_vault_of_older_activity_is_clean(tmp_path):
    conn = _seeded(tmp_path)
    insert_events(conn, STALE)
    vault = _render(tmp_path, conn)
    assert STALE_WEEK_LINK in (vault / "Person" / "Alex Rivera" / "README.md").read_text()
    assert _findings(conn, vault) == []


def test_a_broken_link_inside_a_note_is_charged_to_its_occurrence(tmp_path):
    conn = _seeded(tmp_path)
    insert_events(conn, STALE)
    vault = _render(tmp_path, conn)
    _edit(vault / "Meetings" / WEEK / NOTE, "[Meetings](../README.md)",
          "[Meetings](../READMEE.md)")
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [LINK_RESOLVES]
    assert found[0]["occurrence_id"] == _payload()["occurrence_id"]


def test_a_backlink_to_a_missing_note_is_charged_to_its_occurrence(tmp_path):
    conn = _seeded(tmp_path)
    insert_events(conn, STALE)
    vault = _render(tmp_path, conn)
    (vault / "Meetings" / WEEK / NOTE).unlink()
    dangling = [f for f in _findings(conn, vault) if f["invariant"] == LINK_RESOLVES]
    assert {f["path"] for f in dangling} >= {"Projects/project-atlas/README.md"}
    assert {f["occurrence_id"] for f in dangling} == {_payload()["occurrence_id"]}


def test_a_link_target_with_nested_parentheses_is_not_cut_short(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render(tmp_path, conn)
    (vault / "Docs").mkdir()
    (vault / "Docs" / "a(b(c)d).md").write_text("notes\n")
    _edit(vault / "Meetings" / WEEK / NOTE, "[Meetings](../README.md)",
          "[Meetings](../../Docs/a(b(c)d).md)")
    assert _broken(conn, vault) == set()
    _edit(vault / "Meetings" / WEEK / NOTE, "a(b(c)d).md", "a(b(c)gone).md")
    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [LINK_RESOLVES]
    assert "a(b(c)gone).md" in found[0]["detail"]   # the whole target, not a prefix


def test_a_processing_warning_reaches_the_note_as_a_code_and_nothing_else(tmp_path):
    """`detail` is producer free text and the vault is a published git remote."""
    payload = _payload()
    payload["processing_warnings"] = [
        {"code": "overlapping_speech_ambiguous", "detail": "leaked " * 20}]
    note = _note(_render(tmp_path, _seeded(tmp_path, payload)))
    assert "- overlapping_speech_ambiguous\n" in note
    assert "leaked" not in note


def test_a_bracket_in_a_link_label_never_hides_the_link_from_the_check(tmp_path):
    """A label is producer free text, and free text reaches the note verbatim.

    A link the scanner walks past is a link the check cannot verify, so the
    note would be committed and pushed carrying a link out of the vault.
    """
    payload = _payload()
    payload["summary"] = ("Agreed the plan. See [the [v2] spec]"
                          "(../../../outside-the-vault.md) for detail.")
    conn = _validated_seeded(tmp_path, payload)
    vault = _render(tmp_path, conn)

    found = _findings(conn, vault)
    assert [f["invariant"] for f in found] == [LINK_RESOLVES]
    assert "outside-the-vault.md" in found[0]["detail"]
    assert found[0]["occurrence_id"] == payload["occurrence_id"]


def test_link_labels_carry_balanced_brackets_and_an_odd_one_starts_over():
    assert render_module._md_links("[the [v2] spec](x.md)") == ["x.md"]
    assert render_module._md_links("[a [b [c] d] e](deep.md)") == ["deep.md"]
    # an opener nothing closes does not swallow the link that follows it
    assert render_module._md_links("[stray [b](c.md)") == ["c.md"]
    assert render_module._md_links("[a] (b.md) [c](d.md)") == ["d.md"]


def test_meeting_only_front_matter_values_are_quoted_yaml(tmp_path):
    """A project name such as `Ops: "Q3" #1` must stay one YAML string."""
    conn = _seeded(tmp_path)
    conn.execute("UPDATE meeting_projects SET name = ? WHERE name = ?",
                 ('Ops: "Q3" #1', "project-atlas"))
    conn.commit()
    vault = _render(tmp_path, conn)
    page = next((vault / "Projects").glob("Ops*/README.md")).read_text()
    assert page.startswith('---\nproject: "Ops: \\"Q3\\" #1"\n')
    person = next(p for p in (vault / "Person").glob("*/README.md")
                  if "person_ids:" in p.read_text())
    assert person.read_text().split("\n")[1].startswith('person_ids: "')


# --- meeting projects follow the project/area classification -----------------
# The fixture meeting discusses project-atlas, project-borealis and
# project-cygnus. Classifying them as an area, a hidden label and a count-only
# project checks each projection a meeting backlink can land on.


def _classified_ids(**overrides) -> IdentityMaps:
    from teammem.identity import _read
    projects = {
        "projects": {"project-cygnus": {"projection": "count-only"}},
        "areas": {"project-atlas": {}},
        "hidden_projects": ["project-borealis"],
    }
    projects.update(overrides)
    return IdentityMaps(_read(CONFIG_DIR, "roster"), projects)


def _render_classified(tmp_path, conn, ids) -> Path:
    vault = tmp_path / "vault"
    render_vault(conn, ids, vault, TODAY)
    return vault


def test_meeting_projects_link_to_the_folder_their_classification_renders(tmp_path):
    conn = _seeded(tmp_path)
    vault = _render_classified(tmp_path, conn, _classified_ids())
    discussed = _sections(_note(vault))["Projects discussed"]

    assert "- [project-atlas](../../Areas/project-atlas/README.md)" in discussed
    assert "- [project-cygnus](../../Projects/project-cygnus/README.md)" in discussed
    assert "project-borealis" in discussed                    # named, never linked
    assert "](../../Projects/project-borealis" not in discussed
    assert "](../../Areas/project-borealis" not in discussed

    tail = _tail()
    area = (vault / "Areas" / "project-atlas" / "README.md").read_text()
    assert area.startswith("---\narea: project-atlas\n")
    assert "## Meetings" in area and tail in area
    counts = (vault / "Projects" / "project-cygnus" / "README.md").read_text()
    assert "No commit count collected for this week." in counts
    assert "## Meetings" in counts and tail in counts
    for root in ("Projects", "Areas"):
        assert not (vault / root / "project-borealis").exists()
    assert not (vault / "Projects" / "project-atlas").exists()

    areas_index = (vault / "Areas" / "README.md").read_text()
    projects_index = (vault / "Projects" / "README.md").read_text()
    assert "## Discussed in meetings\n- [project-atlas](project-atlas/README.md)\n" in areas_index
    assert "## Discussed in meetings\n- [project-cygnus](project-cygnus/README.md)\n" in projects_index
    assert "project-borealis" not in areas_index + projects_index


def test_classified_meeting_projects_satisfy_every_meeting_invariant(tmp_path):
    conn = _seeded(tmp_path)
    ids = _classified_ids()
    vault = _render_classified(tmp_path, conn, ids)
    assert verify_meeting_semantics(conn, ids, vault) == []
    assert verify_vault(conn, ids, vault, TODAY) == {
        "missing": [], "unexpected": [], "differing": []}
    out = render_module.render_vault_verified(conn, ids, tmp_path / "verified", TODAY)
    assert out["withheld"] == {} and out["findings"] == []


def test_a_meeting_only_area_gets_an_area_page_and_no_project_page(tmp_path):
    conn = _seeded(tmp_path)
    conn.execute("UPDATE meeting_projects SET name = ? WHERE name = ?",
                 ("coordination", "project-atlas"))
    conn.commit()
    ids = _classified_ids(areas={"coordination": {}})
    vault = _render_classified(tmp_path, conn, ids)
    page = (vault / "Areas" / "coordination" / "README.md").read_text()
    assert _tail() in page
    assert not (vault / "Projects" / "coordination").exists()
    assert verify_meeting_semantics(conn, ids, vault) == []


def test_a_missing_area_backlink_fails_the_project_backlink_invariant(tmp_path):
    conn = _seeded(tmp_path)
    ids = _classified_ids()
    vault = _render_classified(tmp_path, conn, ids)
    page = vault / "Areas" / "project-atlas" / "README.md"
    text = page.read_text()
    page.write_text(text[:text.index("\n## Meetings")] + "\n")

    found = verify_meeting_semantics(conn, ids, vault)
    assert {(f["invariant"], f["path"]) for f in found} == {
        ("meeting.project.backlinks", "Areas/project-atlas/README.md")}


def test_an_event_area_page_keeps_its_activity_and_gains_the_meeting(tmp_path):
    conn = _seeded(tmp_path)
    insert_events(conn, [Event(person="alex", ts="2026-09-02T09:00:00+04:00",
                               source="slack", kind="message", summary="sync",
                               hash="a-area", project="project-atlas")])
    ids = _classified_ids()
    vault = _render_classified(tmp_path, conn, ids)
    page = (vault / "Areas" / "project-atlas" / "README.md").read_text()
    assert "1 event · 1 contributor" in page
    assert page.index("## Weeks") < page.index("## Meetings")
    assert _tail() in page
    assert verify_meeting_semantics(conn, ids, vault) == []
