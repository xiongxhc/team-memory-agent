from teammem.events import Event, event_hash


def test_event_hash_deterministic():
    assert event_hash("mr", "42", "7", "merged") == event_hash("mr", "42", "7", "merged")


def test_event_hash_differs_by_any_part():
    assert event_hash("mr", "42", "7", "merged") != event_hash("mr", "42", "7", "opened")


def test_event_is_frozen_with_defaults():
    e = Event(person="alex", ts="2026-07-14T09:00:00Z", source="gitlab",
              kind="commit", summary="fix auth", hash="abc123")
    assert e.project is None and e.refs is None and e.raw is None
