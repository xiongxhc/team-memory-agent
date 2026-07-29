import json

from memberkit.state import DraftState, event_fingerprint


DATE = "2026-07-27"


def _event(summary: str) -> dict:
    return {
        "ts": f"{DATE}T10:00:00",
        "kind": "journal-highlight",
        "summary": summary,
        "project": "project-alpha",
        "refs": None,
    }


def test_refresh_adds_unseen_without_restoring_removed_pending_event(tmp_path):
    state = DraftState(tmp_path / "state.json")
    private = _event("private")
    first = state.refresh(DATE, [private], current=None)
    assert first == [private]

    public = _event("public")
    refreshed = state.refresh(DATE, [private, public], current={"events": []})

    assert refreshed == [public]
    saved = state.snapshot()
    assert event_fingerprint(private, DATE) in saved["excluded"]


def test_refresh_current_records_removed_even_when_return_filters_approved(tmp_path):
    kept, removed = _event("kept"), _event("removed")
    kept_fingerprint = event_fingerprint(kept, DATE)
    removed_fingerprint = event_fingerprint(removed, DATE)
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "approved": [kept_fingerprint],
        "excluded": [],
        "pending": {DATE: [removed_fingerprint]},
    }))
    state = DraftState(path)

    output = state.refresh(DATE, discovered=[], current={"events": [kept]})

    assert output == []
    assert removed_fingerprint in state.snapshot()["excluded"]


def test_record_push_marks_included_approved_and_missing_excluded(tmp_path):
    state = DraftState(tmp_path / "state.json")
    kept, removed = _event("kept"), _event("removed")
    state.refresh(DATE, [kept, removed], current=None)

    state.record_push(DATE, [kept])

    saved = state.snapshot()
    assert event_fingerprint(kept, DATE) in saved["approved"]
    assert event_fingerprint(removed, DATE) in saved["excluded"]
    assert DATE not in saved["pending"]


def test_approved_and_excluded_events_do_not_reappear(tmp_path):
    state = DraftState(tmp_path / "state.json")
    kept, removed = _event("kept"), _event("removed")
    state.refresh(DATE, [kept, removed], current=None)
    state.record_push(DATE, [kept])

    assert state.refresh(DATE, [kept, removed], current=None) == []


def test_duplicate_events_preserve_multiplicity_through_redaction_and_push(tmp_path):
    state = DraftState(tmp_path / "state.json")
    duplicate = _event("same observation")

    assert state.refresh(DATE, [duplicate, duplicate], current=None) == [
        duplicate, duplicate
    ]
    assert state.snapshot()["pending"][DATE].count(
        event_fingerprint(duplicate, DATE)
    ) == 2

    assert state.refresh(DATE, [], current={"events": [duplicate]}) == [duplicate]
    assert state.snapshot()["excluded"].count(
        event_fingerprint(duplicate, DATE)
    ) == 1

    state.record_push(DATE, [duplicate])

    assert state.refresh(DATE, [duplicate, duplicate], current=None) == []


def test_repeated_push_does_not_accumulate_duplicate_approvals(tmp_path):
    state = DraftState(tmp_path / "state.json")
    duplicate = _event("same observation")
    state.refresh(DATE, [duplicate, duplicate], current=None)

    state.record_push(DATE, [duplicate, duplicate])
    state.record_push(DATE, [duplicate, duplicate])

    assert state.snapshot()["approved"].count(
        event_fingerprint(duplicate, DATE)
    ) == 2


def test_push_records_manual_duplicate_beyond_pending_multiplicity(tmp_path):
    state = DraftState(tmp_path / "state.json")
    duplicate = _event("same observation")
    state.refresh(DATE, [duplicate, duplicate], current=None)

    state.record_push(DATE, [duplicate, duplicate, duplicate])
    assert state.snapshot()["approved"].count(
        event_fingerprint(duplicate, DATE)
    ) == 3
    state.record_push(DATE, [duplicate, duplicate, duplicate])

    assert state.snapshot()["approved"].count(
        event_fingerprint(duplicate, DATE)
    ) == 3
    assert state.refresh(DATE, [duplicate, duplicate, duplicate], current=None) == []


def test_push_reconciles_a_readded_excluded_duplicate(tmp_path):
    state = DraftState(tmp_path / "state.json")
    duplicate = _event("same observation")
    state.refresh(DATE, [duplicate, duplicate], current=None)
    state.refresh(DATE, [], current={"events": [duplicate]})

    state.record_push(DATE, [duplicate, duplicate])

    fingerprint = event_fingerprint(duplicate, DATE)
    saved = state.snapshot()
    assert saved["approved"].count(fingerprint) == 2
    assert saved["excluded"].count(fingerprint) == 0
    assert state.refresh(DATE, [duplicate, duplicate, duplicate], current=None) == [
        duplicate
    ]


def test_push_excludes_missing_pending_duplicate_without_repeat_drift(tmp_path):
    state = DraftState(tmp_path / "state.json")
    duplicate = _event("same observation")
    state.refresh(DATE, [duplicate, duplicate], current=None)

    state.record_push(DATE, [duplicate])
    state.record_push(DATE, [duplicate])

    fingerprint = event_fingerprint(duplicate, DATE)
    saved = state.snapshot()
    assert saved["approved"].count(fingerprint) == 1
    assert saved["excluded"].count(fingerprint) == 1
    assert state.refresh(DATE, [duplicate, duplicate], current=None) == []


def test_push_adds_new_pending_duplicate_to_existing_approvals(tmp_path):
    duplicate = _event("same observation")
    fingerprint = event_fingerprint(duplicate, DATE)
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "approved": [fingerprint, fingerprint],
        "excluded": [],
        "pending": {DATE: [fingerprint]},
    }))
    state = DraftState(path)

    state.record_push(DATE, [duplicate])

    assert state.snapshot()["approved"].count(fingerprint) == 3
    assert state.refresh(DATE, [duplicate, duplicate, duplicate], current=None) == []


def test_pending_bundle_multiplicity_is_additive_to_existing_approvals(tmp_path):
    duplicate = _event("same observation")
    fingerprint = event_fingerprint(duplicate, DATE)
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "approved": [fingerprint, fingerprint],
        "excluded": [],
        "pending": {DATE: [fingerprint]},
    }))
    state = DraftState(path)

    state.record_push(DATE, [duplicate, duplicate, duplicate])

    assert state.snapshot()["approved"].count(fingerprint) == 5
    assert state.refresh(DATE, [duplicate] * 6, current=None) == [duplicate]


def test_untracked_push_readds_excluded_duplicates_once(tmp_path):
    duplicate = _event("same observation")
    fingerprint = event_fingerprint(duplicate, DATE)
    first_path = tmp_path / "first-state.json"
    first_path.write_text(json.dumps({
        "approved": [], "excluded": [fingerprint], "pending": {},
    }))
    state = DraftState(first_path)

    state.record_push(DATE, [duplicate])

    saved = state.snapshot()
    assert saved["approved"].count(fingerprint) == 1
    assert saved["excluded"].count(fingerprint) == 0

    expanded_path = tmp_path / "expanded-state.json"
    expanded_path.write_text(json.dumps({
        "approved": [fingerprint, fingerprint],
        "excluded": [fingerprint], "pending": {},
    }))
    expanded_state = DraftState(expanded_path)
    expanded_state.record_push(DATE, [duplicate, duplicate, duplicate])
    expanded = expanded_state.snapshot()
    assert expanded["approved"].count(fingerprint) == 3
    assert expanded["excluded"].count(fingerprint) == 0

    unchanged_path = tmp_path / "unchanged-state.json"
    unchanged_path.write_text(json.dumps({
        "approved": [fingerprint, fingerprint],
        "excluded": [fingerprint], "pending": {},
    }))
    unchanged_state = DraftState(unchanged_path)
    unchanged_state.record_push(DATE, [duplicate])
    unchanged = unchanged_state.snapshot()
    assert unchanged["approved"].count(fingerprint) == 2
    assert unchanged["excluded"].count(fingerprint) == 1
