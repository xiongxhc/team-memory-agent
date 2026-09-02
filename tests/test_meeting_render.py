"""The canonical meeting note and its backlinks.

Every meeting in this file is imported through `meeting_store.import_meeting_revision`
from the frozen `teammem-meeting/v1` fixtures, so what is rendered is what the
store actually holds. The vault must carry the summary note and its backlinks and
never the transcript or the audio.
"""

import copy
import json
import os
import re
import time
from datetime import date
from pathlib import Path

import pytest

from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.meeting_bundles import canonical_artifact_hash, short_occurrence_id
from teammem.meeting_store import StagedAudio, import_meeting_revision
from teammem.render import MANAGED, render_vault, verify_vault
from teammem.store import insert_events, open_db

CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"
FIXTURES = Path(__file__).parent / "fixtures" / "meetings"
COMPLETE = FIXTURES / "teammem-meeting-v1.complete.json"

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
    )
    return occurrence_id


def _seeded(tmp_path, *payloads, name="l.db"):
    conn = open_db(tmp_path / name)
    for payload in payloads or (_payload(),):
        _import(conn, payload)
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
    assert page.startswith("---\nproject: project-atlas\n")
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


def test_a_dot_segment_display_name_cannot_escape_the_person_directory(tmp_path):
    """Provider display names are untrusted; `..` must never become a folder."""
    payload = _payload()
    payload["participants"][0]["display_name"] = ".."
    conn = _seeded(tmp_path, payload)
    vault = tmp_path / "vault"
    with pytest.raises(ValueError, match="filename collision in vault render"):
        render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    assert not (vault / "Meetings").exists()      # refused before anything was written


def test_an_unrostered_participant_cannot_take_over_a_rostered_person_page(tmp_path):
    """Two different people must never be merged onto one Person folder."""
    payload = _payload()
    payload["participants"][0]["display_name"] = "Alex Rivera"   # ou_ada, not the roster alex
    conn = _seeded(tmp_path, payload)
    insert_events(conn, [Event(person="alex", ts="2026-09-02T09:00:00+04:00",
                               source="gitlab", kind="commit", summary="fix: a thing",
                               hash="a1", project="project-alpha")])
    with pytest.raises(ValueError, match="filename collision in vault render"):
        render_vault(conn, IdentityMaps.load(CONFIG_DIR), tmp_path / "vault", TODAY)
