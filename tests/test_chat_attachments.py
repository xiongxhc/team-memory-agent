from pathlib import Path

import pytest

from teammem.chat.attachments import (
    AttachmentAdmissionError,
    AttachmentLimits,
    AttachmentStore,
)
from teammem.chat.state import ChatState, SessionKey


def _limits(**changes):
    values = {
        "allowed_extensions": frozenset({".pdf", ".png", ".txt"}),
        "max_files_per_request": 2,
        "max_file_bytes": 4,
        "max_request_bytes": 6,
        "raw_retention_hours": 24,
        "max_extracted_characters_per_file": 20,
    }
    values.update(changes)
    return AttachmentLimits(**values)


def _message(message_id="om-1"):
    return {"id": message_id}


def _resource(resource_id="file-1", filename="report.pdf", size=3):
    return {
        "id": resource_id,
        "filename": filename,
        "mime_type": "application/pdf",
        "size": size,
    }


def _store(tmp_path):
    state = ChatState(tmp_path / "chat.sqlite3")
    return state, AttachmentStore(tmp_path / "attachments", state)


def test_admission_persists_source_provenance_before_download_and_reopens(tmp_path):
    """Recording provenance only after a download loses a crash-safe source boundary."""
    state, store = _store(tmp_path)
    session = SessionKey("tenant", "app", "dm", "alice")

    job = store.admit_attachment(session, 0, _message(), _resource(), _limits(), now_ms=100)

    assert job.session == session
    assert job.generation == 0
    assert job.message_id == "om-1"
    assert job.resource_id == "file-1"
    assert job.filename == "report.pdf"
    assert not job.raw_path.exists()
    assert job.raw_path.name != job.filename
    store.close()
    store = AttachmentStore(tmp_path / "attachments", state)
    assert store.admit_attachment(session, 0, _message(), _resource(), _limits(), now_ms=101).id == job.id
    store.close()
    state.close()


def test_file_key_alias_and_download_signature_correct_image_placeholder_name(tmp_path):
    """A Feishu image placeholder named PNG must not make a real JPEG fail parsing."""
    state, store = _store(tmp_path)
    session = SessionKey("tenant", "app", "dm", "alice")
    resource = {
        "file_key": "feishu-file-key", "filename": "image.png",
        "mime_type": "image/jpeg", "size": 3,
    }
    job = store.admit_attachment(session, 0, _message(), resource, _limits(allowed_extensions=frozenset({".png", ".jpg"})))
    store.write_raw(job, [b"\xff\xd8\xff"])

    normalized = store.normalize_downloaded_image(job)

    assert normalized.id == job.id
    assert normalized.resource_id == "feishu-file-key"
    assert normalized.filename == "image.jpg"
    assert store.admit_attachment(session, 0, _message(), resource, normalized.limits).filename == "image.jpg"
    store.close()
    state.close()


def test_admission_enforces_shared_request_count_and_byte_budget(tmp_path):
    """Checking only each file lets one message exceed the total attachment budget."""
    state, store = _store(tmp_path)
    session = SessionKey("tenant", "app", "dm", "alice")
    limits = _limits()
    store.admit_attachment(session, 0, _message(), _resource("f-1", size=4), limits)

    with pytest.raises(AttachmentAdmissionError):
        store.admit_attachment(session, 0, _message(), _resource("f-2", size=4), limits)

    store.close()
    state.close()


@pytest.mark.parametrize("resource", [
    _resource(filename="../outside.pdf"),
    _resource(filename="report.exe"),
    _resource(size=5),
])
def test_admission_rejects_unsafe_or_unallowed_source_metadata(tmp_path, resource):
    """Using source filenames as paths or trusting an unsupported type admits hostile input."""
    state, store = _store(tmp_path)
    with pytest.raises(AttachmentAdmissionError):
        store.admit_attachment(SessionKey("tenant", "app", "dm", "alice"), 0, _message(), resource, _limits())
    store.close()
    state.close()


def test_streamed_download_rechecks_actual_byte_bounds_and_cleans_partial_file(tmp_path):
    """Trusting a declared size can retain an oversize streamed resource."""
    state, store = _store(tmp_path)
    session = SessionKey("tenant", "app", "dm", "alice")
    job = store.admit_attachment(session, 0, _message(), _resource(size=1), _limits())

    with pytest.raises(AttachmentAdmissionError):
        store.write_raw(job, [b"abc", b"de"])

    assert not job.raw_path.exists()
    store.close()
    state.close()


def test_reset_during_parse_cannot_restore_raw_or_derived_attachment_context(tmp_path):
    """Persisting a late parser result after reset crosses the new conversation generation."""
    state, store = _store(tmp_path)
    session = SessionKey("tenant", "app", "group", "chat-1")
    job = store.admit_attachment(session, 0, _message(), _resource(), _limits())
    store.write_raw(job, [b"pdf"])
    image = job.derived_dir / "visual-1.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    state.reset(session)

    assert store.record_result(job, {
        "filename": "report.pdf",
        "fragments": [{"text": "private chart", "locator": "page 1", "kind": "text"}],
        "images": [{"path": str(image), "locator": "page 1", "kind": "visual"}],
        "coverage": {"complete": True, "processed": ["page 1"], "omitted": [], "warnings": [], "pages_or_slides": 1, "characters": 13, "expanded_bytes": 3, "visual_pages": 1},
    }) is False
    store.invalidate_session(session)

    assert not job.raw_path.exists()
    assert not job.derived_dir.exists()
    assert store.context_fragments(session, state.generation(session)) == []
    store.close()
    state.close()


def test_raw_expiry_removes_download_but_retains_current_session_extraction(tmp_path):
    """Deleting extracted context with 24-hour raw cleanup loses valid session-local work."""
    state, store = _store(tmp_path)
    session = SessionKey("tenant", "app", "dm", "alice")
    job = store.admit_attachment(session, 0, _message(), _resource(), _limits(), now_ms=100)
    store.write_raw(job, [b"pdf"], now_ms=101)
    assert store.record_result(job, {
        "filename": "report.pdf",
        "fragments": [{"text": "中文摘要", "locator": "page 1", "kind": "ocr"}],
        "images": [],
        "coverage": {"complete": False, "processed": ["page 1"], "omitted": ["page 2"], "warnings": ["OCR"], "pages_or_slides": 2, "characters": 4, "expanded_bytes": 3, "visual_pages": 0},
    }) is True

    assert store.cleanup_expired(now_ms=job.raw_expires_at_ms + 1) == 1
    assert not job.raw_path.exists()
    fragments = store.context_fragments(session, 0)
    assert fragments == [{
        "id": f"{job.id}:fragment:0", "filename": "report.pdf", "locator": "page 1",
        "text": "中文摘要", "coverage": {"complete": False, "processed": ["page 1"], "omitted": ["page 2"], "warnings": ["OCR"], "pages_or_slides": 2, "characters": 4, "expanded_bytes": 3, "visual_pages": 0},
    }]
    assert "projects" not in fragments[0]
    store.close()
    state.close()


def test_raw_expiry_removes_derived_visuals_and_never_returns_expired_image(tmp_path, monkeypatch):
    import teammem.chat.attachments as attachments

    state, store = _store(tmp_path)
    session = SessionKey("tenant", "app", "dm", "alice")
    job = store.admit_attachment(session, 0, _message(), _resource(), _limits(), now_ms=100)
    store.write_raw(job, [b"pdf"], now_ms=101)
    image = job.derived_dir / "visual-1.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    assert store.record_result(job, {
        "filename": "report.pdf", "fragments": [{"text": "chart summary", "locator": "page 1", "kind": "text"}],
        "images": [{"path": str(image), "locator": "page 1", "kind": "visual"}],
        "coverage": {"complete": True, "processed": ["page 1"], "omitted": [], "warnings": [], "pages_or_slides": 1, "characters": 13, "expanded_bytes": 3, "visual_pages": 1},
    }) is True

    monkeypatch.setattr(attachments, "_now_ms", lambda: job.raw_expires_at_ms + 1)
    fragments = store.context_fragments(session, 0)

    assert not job.raw_path.exists()
    assert not job.derived_dir.exists()
    assert all("image_path" not in fragment for fragment in fragments)
    assert fragments[0]["text"] == "chart summary"
    assert "visual artifacts expired" in fragments[0]["coverage"]["warnings"]
    store.close()
    state.close()


def test_context_requires_exact_full_session_and_generation(tmp_path):
    """Looking up by person or chat ID alone leaks files across apps or threads."""
    state, store = _store(tmp_path)
    dm = SessionKey("tenant", "app", "dm", "alice")
    other_app = SessionKey("tenant", "other", "dm", "alice")
    job = store.admit_attachment(dm, 0, _message(), _resource(), _limits())
    store.write_raw(job, [b"pdf"])
    assert store.record_result(job, {
        "filename": "report.pdf", "fragments": [{"text": "private", "locator": "page 1", "kind": "text"}],
        "images": [], "coverage": {"complete": True, "processed": ["page 1"], "omitted": [], "warnings": [], "pages_or_slides": 1, "characters": 7, "expanded_bytes": 3, "visual_pages": 0},
    }) is True

    assert len(store.context_fragments(dm, 0)) == 1
    assert store.context_fragments(other_app, 0) == []
    assert store.context_fragments(dm, 1) == []
    store.close()
    state.close()


def test_reset_cleans_derived_files_even_after_raw_expiry(tmp_path):
    """Assuming an expired raw path still exists leaves derived visuals after forget."""
    state, store = _store(tmp_path)
    session = SessionKey("tenant", "app", "dm", "alice")
    job = store.admit_attachment(session, 0, _message(), _resource(), _limits(), now_ms=100)
    store.write_raw(job, [b"pdf"], now_ms=101)
    image = job.derived_dir / "visual-1.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    assert store.record_result(job, {
        "filename": "report.pdf", "fragments": [],
        "images": [{"path": str(image), "locator": "page 1", "kind": "visual"}],
        "coverage": {"complete": True, "processed": ["page 1"], "omitted": [], "warnings": [], "pages_or_slides": 1, "characters": 0, "expanded_bytes": 3, "visual_pages": 1},
    }) is True
    store.cleanup_expired(now_ms=job.raw_expires_at_ms + 1)
    state.forget(session)

    store.invalidate_session(session)

    assert not job.derived_dir.exists()
    store.close()
    state.close()


def test_periodic_cleanup_removes_idle_or_reset_generation_without_service_callback(tmp_path):
    """Only expiring raw files leaves old extracted visuals after session invalidation."""
    state, store = _store(tmp_path)
    session = SessionKey("tenant", "app", "dm", "alice")
    job = store.admit_attachment(session, 0, _message(), _resource(), _limits())
    store.write_raw(job, [b"pdf"])
    image = job.derived_dir / "visual-1.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    assert store.record_result(job, {
        "filename": "report.pdf", "fragments": [],
        "images": [{"path": str(image), "locator": "page 1", "kind": "visual"}],
        "coverage": {"complete": True, "processed": ["page 1"], "omitted": [], "warnings": [], "pages_or_slides": 1, "characters": 0, "expanded_bytes": 3, "visual_pages": 1},
    }) is True
    state.reset(session)

    store.cleanup_expired()

    assert not job.derived_dir.exists()
    store.close()
    state.close()


def test_model_selector_keeps_relevant_bilingual_text_and_same_file_visuals(tmp_path):
    """Sending every fragment or dropping its chart image defeats bounded visual answers."""
    state, store = _store(tmp_path)
    session = SessionKey("tenant", "app", "dm", "alice")
    job = store.admit_attachment(session, 0, _message("om-chart"), _resource("chart", "chart.pdf"), _limits())
    store.write_raw(job, [b"pdf"])
    image = job.derived_dir / "visual-1.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    assert store.record_result(job, {
        "filename": "chart.pdf",
        "fragments": [{"text": "项目路线图", "locator": "page 1", "kind": "text"}],
        "images": [{"path": str(image), "locator": "page 1", "kind": "visual"}],
        "coverage": {"complete": False, "processed": ["page 1"], "omitted": ["page 2"], "warnings": [], "pages_or_slides": 2, "characters": 5, "expanded_bytes": 3, "visual_pages": 1},
    }) is True
    other = store.admit_attachment(session, 0, _message("om-other"), _resource("other", "other.txt"), _limits())
    store.write_raw(other, [b"txt"])
    assert store.record_result(other, {
        "filename": "other.txt",
        "fragments": [{"text": "unrelated", "locator": "line 1", "kind": "text"}],
        "images": [],
        "coverage": {"complete": True, "processed": ["line 1"], "omitted": [], "warnings": [], "pages_or_slides": None, "characters": 9, "expanded_bytes": 3, "visual_pages": 0},
    }) is True

    selected = store.select_context(session, 0, "路线图", limit=3)

    assert len(selected) == 3
    assert selected[0]["text"] == "项目路线图"
    assert any(item.get("image_path") == str(image.resolve()) for item in selected)
    assert sum("coverage" in item for item in selected if item["filename"] == "chart.pdf") == 1
    assert selected[0]["coverage"]["context_omitted_fragments"] == 0
    store.close()
    state.close()


def test_model_selector_caps_large_session_context_at_one_hundred_fragments(tmp_path):
    """Allowing every retained fragment into a model turn defeats the input budget."""
    state, store = _store(tmp_path)
    session = SessionKey("tenant", "app", "dm", "alice")
    for index in range(101):
        job = store.admit_attachment(
            session, 0, _message(f"om-{index}"), _resource(f"file-{index}", f"file-{index}.txt"), _limits(),
        )
        store.write_raw(job, [b"txt"])
        text = "needle result" if index == 100 else f"ordinary result {index}"
        assert store.record_result(job, {
            "filename": job.filename,
            "fragments": [{"text": text, "locator": "line 1", "kind": "text"}],
            "images": [],
            "coverage": {"complete": True, "processed": ["line 1"], "omitted": [], "warnings": [], "pages_or_slides": None, "characters": len(text), "expanded_bytes": 3, "visual_pages": 0},
        }) is True

    selected = store.select_context(session, 0, "needle")

    assert len(selected) == 100
    assert selected[0]["text"] == "needle result"
    store.close()
    state.close()
