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
