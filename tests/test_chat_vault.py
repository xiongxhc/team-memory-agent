import os
from datetime import date
from pathlib import Path

import pytest

from teammem.chat.vault import search_vault
from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.render import render_vault
from teammem.store import SummaryRecord, insert_events, open_db, put_summary
from teammem.summarize import prepare_daily_journal


TODAY = date(2026, 9, 16)
WEB = "https://git.example/team/vault"


def _config(tmp_path: Path) -> Path:
    source = tmp_path / "config"
    source.mkdir()
    (source / "roster.yaml").write_text(
        "members:\n  alice:\n    name: Alice Example\n"
        "  bob:\n    name: Bob Example\n"
    )
    (source / "projects.yaml").write_text(
        "projects:\n"
        "  alpha: {}\n"
        "  beta: {}\n"
        "  counts:\n    projection: count-only\n"
        "areas:\n  operations: {}\n"
        "hidden_projects: [hidden]\n"
    )
    return source


def _vault(tmp_path: Path) -> dict[str, object]:
    return {"root": tmp_path / "vault", "web_url": WEB, "ref": "main"}


def _daily(
    tmp_path: Path,
    *,
    events: list[Event] | None = None,
    text: str = "Alice shipped the alpha release.",
    stored_hash: str | None = None,
) -> tuple[Path, Path, dict[str, object]]:
    source = _config(tmp_path)
    db = tmp_path / "ledger.db"
    conn = open_db(db)
    insert_events(conn, events or [Event(
        person="alice", project="alpha", ts="2026-09-15T09:00:00+04:00",
        source="gitlab", kind="commit", summary="ship alpha", hash="one",
    )])
    prepared = prepare_daily_journal(conn, "alice", "Alice Example", "2026-09-15", [])
    assert prepared is not None
    put_summary(conn, SummaryRecord(
        "daily-person", prepared.key, stored_hash or prepared.input_hash, text,
        "test", "2026-09-16T01:00:00Z",
    ))
    vault = _vault(tmp_path)
    render_vault(conn, IdentityMaps.load(source), vault["root"], TODAY)
    conn.close()
    return db, source, vault


def _search(db, source, vault, policy, allowed, query, limit=3):
    return search_vault(
        db, source, vault, policy, frozenset(allowed), query, limit=limit,
    )


def test_returns_only_a_hash_verified_local_daily_section(tmp_path):
    """Skipping ledger and rendered-text checks could serve a stale synthesis."""
    db, source, vault = _daily(tmp_path)

    found = _search(
        db, source, vault, {"alpha": "detail"}, {"alpha"},
        {"text": "release", "person": "alice"},
    )

    assert len(found) == 1
    item = found[0]
    assert item.kind == "vault"
    assert item.title == "Alice Example — 2026-09-15"
    assert item.text == "Alice shipped the alpha release."
    assert item.project == "alpha"
    assert item.projects == frozenset({"alpha"})
    assert item.timestamp == "2026-09-16"
    assert item.url == (
        WEB + "/-/blob/main/Person/Alice%20Example/"
        "Week%202026-09-14-18.md#2026-09-15"
    )
    assert "date-prefix approximation" in item.coverage


@pytest.mark.parametrize("unsafe_project, policy, allowed", [
    (None, {"alpha": "detail"}, {"alpha"}),
    ("counts", {"alpha": "detail", "counts": "count_only"}, {"alpha", "counts"}),
    ("hidden", {"alpha": "detail", "hidden": "hidden"}, {"alpha", "hidden"}),
    ("unknown", {"alpha": "detail"}, {"alpha", "unknown"}),
])
def test_rejects_the_whole_daily_summary_when_any_source_event_is_not_detail(
    tmp_path, unsafe_project, policy, allowed,
):
    """Checking only the named project would leak mixed-scope daily prose."""
    events = [
        Event("alice", "2026-09-15T09:00:00+04:00", "gitlab", "commit", "safe", "safe", "alpha"),
        Event(
            "alice", "2026-09-15T10:00:00+04:00", "gitlab", "commit",
            "unsafe", "unsafe", unsafe_project,
        ),
    ]
    db, source, vault = _daily(tmp_path, events=events, text="Safe and unsafe synthesis.")

    assert _search(
        db, source, vault, policy, allowed,
        {"text": "unsafe", "person": "alice"},
    ) == []


@pytest.mark.parametrize("mutation", ["hash", "rendered-text"])
def test_rejects_changed_hash_or_same_day_rendered_text(tmp_path, mutation):
    """Text equality alone cannot prove the synthesis still matches its sources."""
    db, source, vault = _daily(
        tmp_path, stored_hash="wrong" if mutation == "hash" else None,
    )
    if mutation == "rendered-text":
        page = vault["root"] / "Person" / "Alice Example" / "Week 2026-09-14-18.md"
        page.write_text(page.read_text().replace(
            "Alice shipped the alpha release.", "Alice shipped altered prose.",
        ))

    assert _search(
        db, source, vault, {"alpha": "detail"}, {"alpha"},
        {"text": "Alice", "person": "alice"},
    ) == []


def test_rejects_person_page_after_current_display_name_changes(tmp_path):
    """A page at an old display-name path no longer proves the current identity."""
    db, source, vault = _daily(tmp_path)
    (source / "roster.yaml").write_text(
        "members:\n  alice:\n    name: Alice Renamed\n"
        "  bob:\n    name: Bob Example\n"
    )

    assert _search(
        db, source, vault, {"alpha": "detail"}, {"alpha"},
        {"text": "release", "person": "alice"},
    ) == []


def test_project_docs_are_project_scoped_linked_and_excluded_for_person_queries(tmp_path):
    """A docs search must not cross authorization or answer a person question with team prose."""
    source = _config(tmp_path)
    vault = _vault(tmp_path)
    for project, marker in (("alpha", "alpha sentinel"), ("beta", "beta sentinel")):
        directory = vault["root"] / "Docs" / project
        directory.mkdir(parents=True)
        (directory / "architecture.md").write_text(
            f"---\nproject: \"{project.title()} Product\"\n"
            "date updated: 2026-09-12\ntags:\n  - architecture\n  - system-design\n"
            f"---\n# Architecture\n\n{'background ' * 190}\n\n{marker}\n"
        )
    db = tmp_path / "ledger.db"
    open_db(db).close()

    found = _search(
        db, source, vault, {"alpha": "detail", "beta": "detail"}, {"alpha"},
        {"text": "sentinel", "project": "alpha"},
    )

    assert len(found) == 1
    assert found[0].text.endswith("alpha sentinel")
    assert not found[0].text.startswith("# Architecture")
    assert found[0].project == "alpha"
    assert found[0].projects == frozenset({"alpha"})
    assert found[0].timestamp == "2026-09-12"
    assert found[0].title == "alpha — Architecture"
    assert found[0].url == WEB + "/-/blob/main/Docs/alpha/architecture.md"
    assert _search(
        db, source, vault, {"alpha": "detail"}, {"alpha"},
        {"text": "sentinel", "person": "bob"},
    ) == []


def test_explicit_architecture_query_prefers_document_over_weekly_mentions(tmp_path):
    """Repeated weekly mentions must not displace the architecture overview."""
    source = _config(tmp_path)
    db = tmp_path / "ledger.db"
    open_db(db).close()
    vault = _vault(tmp_path)
    docs = vault["root"] / "Docs" / "alpha"
    docs.mkdir(parents=True)
    (docs / "architecture.md").write_text(
        "---\nproject: Alpha Product\nlast_scanned: 2026-09-11\n"
        "tags:\n  - architecture\n---\n# Architecture\n\n"
        "System overview and boundaries.\n\n" + "deep detail " * 200
    )
    project = vault["root"] / "Projects" / "alpha"
    project.mkdir(parents=True)
    (project / "Week 2026-09-14-18.md").write_text(
        "---\nproject: alpha\nweek: 2026-09-14\ngenerated: 2026-09-16\n---\n"
        "# alpha — Week 2026-09-14-18\n\n" + "architecture activity " * 80
    )

    found = _search(
        db, source, vault, {"alpha": "detail"}, {"alpha"},
        {"text": "architecture", "project": "alpha"},
    )

    assert found[0].id == "vault:Docs/alpha/architecture.md:0"
    assert found[0].text.startswith("# Architecture\n\nSystem overview")
    assert found[0].timestamp == "2026-09-11"
    assert "last-scanned timestamp" in found[0].coverage


def test_document_without_date_labels_timestamp_as_local_file_time(tmp_path):
    """Filesystem mtime must not be described as source document freshness."""
    source = _config(tmp_path)
    db = tmp_path / "ledger.db"
    open_db(db).close()
    vault = _vault(tmp_path)
    docs = vault["root"] / "Docs" / "alpha"
    docs.mkdir(parents=True)
    summary = docs / "summary.md"
    summary.write_text("# Summary\n\nmtime sentinel\n")
    os.utime(summary, (0, 0))

    found = _search(
        db, source, vault, {"alpha": "detail"}, {"alpha"},
        {"text": "mtime sentinel", "project": "alpha"},
    )

    assert found[0].timestamp == "1970-01-01"
    assert "local file modification time" in found[0].coverage
    assert "not source-updated time" in found[0].coverage


def test_daily_project_and_date_filters_apply_to_all_dependencies(tmp_path):
    """A requested project must be the daily summary's only dependency."""
    events = [
        Event("alice", "2026-09-15T09:00:00+04:00", "gitlab", "commit", "alpha", "a", "alpha"),
        Event("alice", "2026-09-15T10:00:00+04:00", "gitlab", "commit", "beta", "b", "beta"),
    ]
    db, source, vault = _daily(tmp_path, events=events, text="Cross-project work.")
    policy = {"alpha": "detail", "beta": "detail"}

    assert _search(
        db, source, vault, policy, {"alpha", "beta"},
        {"text": "work", "person": "alice", "project": "alpha"},
    ) == []
    assert _search(
        db, source, vault, policy, {"alpha", "beta"},
        {"text": "work", "person": "alice", "start": "2026-09-16T00:00:00Z"},
    ) == []


def test_long_daily_summary_is_split_into_bounded_chunks(tmp_path):
    """Returning one unbounded synthesis would bypass the retrieval context budget."""
    text = "\n\n".join(f"Paragraph {index}: " + "x" * 380 for index in range(8))
    db, source, vault = _daily(tmp_path, text=text)

    found = _search(
        db, source, vault, {"alpha": "detail"}, {"alpha"},
        {"text": "Paragraph", "person": "alice"}, limit=3,
    )

    assert len(found) == 2
    assert all(len(item.text) <= 1600 for item in found)
    assert [item.id.rsplit(":", 1)[-1] for item in found] == ["0", "1"]
    assert "bounded excerpt" in found[0].coverage


def test_oversize_known_document_is_skipped(tmp_path):
    """A known filename still cannot bypass the per-file read budget."""
    source = _config(tmp_path)
    db = tmp_path / "ledger.db"
    open_db(db).close()
    vault = _vault(tmp_path)
    docs = vault["root"] / "Docs" / "alpha"
    docs.mkdir(parents=True)
    (docs / "architecture.md").write_text("oversize sentinel " + "x" * (256 * 1024))

    assert _search(
        db, source, vault, {"alpha": "detail"}, {"alpha"},
        {"text": "oversize", "project": "alpha"},
    ) == []


def test_symlinks_path_escapes_and_unknown_files_are_never_searched(tmp_path):
    """Walking arbitrary vault paths would let local files masquerade as generated pages."""
    source = _config(tmp_path)
    db = tmp_path / "ledger.db"
    open_db(db).close()
    vault = _vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "README.md").write_text(
        "---\nproject: alpha\ngenerated: 2026-09-16\n---\nescape sentinel\n"
    )
    projects = vault["root"] / "Projects"
    projects.mkdir(parents=True)
    (projects / "alpha").symlink_to(outside, target_is_directory=True)
    docs = vault["root"] / "Docs" / "alpha"
    docs.mkdir(parents=True)
    (docs / "notes.md").write_text("unknown sentinel")

    assert _search(
        db, source, vault, {"alpha": "detail"}, {"alpha"},
        {"text": "sentinel", "project": "alpha"},
    ) == []
