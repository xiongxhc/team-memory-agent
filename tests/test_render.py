import json
from datetime import date
from pathlib import Path

import pytest

from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.render import render_vault
from teammem.store import SummaryRecord, insert_events, open_db, put_summary

CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"
TODAY = date(2026, 7, 16)


def _seed(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        Event(person="alex", ts="2026-07-14T09:00:00+04:00", source="gitlab",
              kind="commit", summary="fix: JWT refresh race", hash="a1",
              project="project-alpha", refs='{"url": "https://x/a1"}'),
        Event(person="alex", ts="2026-07-15T09:00:00+04:00", source="gitlab",
              kind="mr", summary="[merged] Auth middleware fix", hash="a2",
              project="project-alpha", refs='{"url": "https://x/a2"}'),
        Event(person="sam", ts="2026-07-01T09:00:00+04:00", source="gitlab",
              kind="commit", summary="feat: portal nav", hash="b1",
              project="project-beta", refs='{"url": "https://x/b1"}'),
        Event(person="_unmapped/x@y.z", ts="2026-07-14T10:00:00+04:00",
              source="gitlab", kind="commit", summary="mystery", hash="c1"),
    ])
    return conn


def test_render_writes_expected_tree(tmp_path):
    conn = _seed(tmp_path)
    vault = tmp_path / "vault"
    out = render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    assert out["week_label"] == "Week 2026-07-13-17"
    assert (vault / "Person" / "Alex Rivera" / "README.md").exists()
    assert (vault / "Person" / "Alex Rivera" / "Week 2026-07-13-17.md").exists()
    assert (vault / "Person" / "Sam Lee" / "README.md").exists()
    assert not (vault / "Person" / "_unmapped").exists()          # no unmapped person pages
    assert (vault / "Projects" / "project-alpha.md").exists()
    assert (vault / "Work Journal" / "Week 2026-07-13-17.md").exists()
    assert (vault / "README.md").exists()


def test_week_report_content(tmp_path):
    conn = _seed(tmp_path)
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    report = (vault / "Work Journal" / "Week 2026-07-13-17.md").read_text()
    assert "[Alex Rivera](../Person/Alex%20Rivera/README.md)" in report   # person link
    assert "2 events" in report                                   # per-person count
    assert "(https://x/a1)" in report                             # every line carries a ref
    assert "[Sam Lee](../Person/Sam%20Lee/README.md)" in report and "no activity this week" in report  # gap flag
    assert "_unmapped/x@y.z" in report                            # unmapped surfaces


def test_person_page_content(tmp_path):
    conn = _seed(tmp_path)
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    page = (vault / "Person" / "Alex Rivera" / "README.md").read_text()
    assert "slug: alex" in page
    assert "[Week 2026-07-13-17](../../Work%20Journal/Week%202026-07-13-17.md)" in page
    assert "fix: JWT refresh race" in page


def test_render_is_idempotent_and_removes_stale(tmp_path):
    conn = _seed(tmp_path)
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    stale = vault / "Person" / "Old Name.md"
    stale.write_text("stale")
    keep = vault / "Meeting Notes"; keep.mkdir(); (keep / "note.md").write_text("mine")
    first = (vault / "Person" / "Alex Rivera" / "README.md").read_bytes()
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    assert not stale.exists()                                     # managed dirs regenerated
    assert (keep / "note.md").read_text() == "mine"               # unmanaged untouched
    assert (vault / "Person" / "Alex Rivera" / "README.md").read_bytes() == first  # deterministic


def test_messages_render_as_channel_count(tmp_path):
    conn = _seed(tmp_path)
    insert_events(conn, [
        Event(person="alex", ts="2026-07-14T09:30:00+04:00", source="feishu-channel",
              kind="message", summary="deploy done", hash="f1",
              project="project-alpha", refs='{"message_id": "om_1", "chat_id": "oc_1"}'),
        Event(person="alex", ts="2026-07-14T09:31:00+04:00", source="feishu-channel",
              kind="message", summary="lgtm", hash="f2",
              project="project-alpha", refs='{"message_id": "om_2", "chat_id": "oc_1"}'),
        Event(person="alex", ts="2026-07-14T09:32:00+04:00", source="feishu-channel",
              kind="message", summary="on it", hash="f3",
              project="project-alpha", refs='{"message_id": "om_3", "chat_id": "oc_2"}'),
    ])
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    report = (vault / "Work Journal" / "Week 2026-07-13-17.md").read_text()
    assert "💬 3 messages across 2 channels" in report
    assert "fix: JWT refresh race" in report
    assert "- message —" not in report


def test_render_vault_weeks_zero_treated_as_one(tmp_path):
    conn = _seed(tmp_path)
    vault = tmp_path / "vault"
    out = render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY, weeks=0)
    assert out["week_label"] == "Week 2026-07-13-17"
    assert (vault / "Work Journal" / "Week 2026-07-13-17.md").exists()


def test_display_name_filename_collision_raises(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        Event(person="a", ts="2026-07-14T09:00:00+04:00", source="gitlab",
              kind="commit", summary="x", hash="h1"),
        Event(person="b", ts="2026-07-14T10:00:00+04:00", source="gitlab",
              kind="commit", summary="y", hash="h2"),
    ])
    ids = IdentityMaps({"members": {"a": {"name": "X/Y"}, "b": {"name": "X-Y"}}}, {})
    with pytest.raises(ValueError, match="filename collision"):
        render_vault(conn, ids, tmp_path / "vault", TODAY)


def _seed_summaries(conn):
    conn.executemany(
        "INSERT INTO summaries (kind, key, input_hash, text, model, created_ts)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [("daily-person", "alex|2026-07-14", "h",
          "Alex fixed the JWT refresh race in [[project-alpha]].", "fake", "t"),
         ("weekly-team", "team|2026-07-13", "h",
          "## Shipped\n- [[Alex Rivera]] closed the JWT race\n"
          "## Needs attention\n- none\n"
          "## Coordination-heavy / low artifact\n- none", "fake", "t")])
    conn.commit()


def _store_weekly_summary(
    conn,
    *,
    text: str,
    coverage_state: str | None = None,
    effective_flags: dict | None = None,
) -> None:
    put_summary(conn, SummaryRecord(
        "weekly-team",
        "team|2026-07-13",
        "weekly-hash",
        text,
        "fake",
        "t",
        evidence_cutoff=("2026-07-14T10:00:00+04:00" if coverage_state else None),
        cutoff_precision=("instant" if coverage_state else None),
        coverage_state=coverage_state,
        source_input_hash=("source-hash" if coverage_state else None),
        effective_flags_json=(
            json.dumps(effective_flags, sort_keys=True, separators=(",", ":"))
            if effective_flags is not None else None
        ),
    ))


def test_person_page_shows_day_entries_with_detail_demoted(tmp_path):
    conn = _seed(tmp_path)
    _seed_summaries(conn)
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    page = (vault / "Person" / "Alex Rivera" / "README.md").read_text()
    assert "### 2026-07-14" in page
    assert "Alex fixed the JWT refresh race" in page
    assert "**Activity detail**" in page
    assert page.index("### 2026-07-14") < page.index("**Activity detail**")


def test_weekly_page_synthesized_with_appendix_and_stable(tmp_path):
    conn = _seed(tmp_path)
    _seed_summaries(conn)
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    page = (vault / "Work Journal" / "Week 2026-07-13-17.md").read_text()
    assert "## Shipped" in page and "## Flags" in page
    assert "## Appendix — activity by person" in page
    assert page.index("## Shipped") < page.index("## Appendix — activity by person")
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)   # byte-stable
    assert (vault / "Work Journal" / "Week 2026-07-13-17.md").read_text() == page


def test_weekly_page_keeps_stored_coverage_and_flags_after_later_evidence(tmp_path):
    """Replacing stored report facts with current-ledger facts would break this."""
    conn = _seed(tmp_path)
    _store_weekly_summary(
        conn,
        text=(
            "> Provisional — event timestamps through 2026-07-14T10:00:00+04:00.\n\n"
            "## Shipped\n- JWT race fixed"
        ),
        coverage_state="provisional",
        effective_flags={
            "unmapped": [["_unmapped/x@y.z", 1]],
            "unmapped_channels": [],
        },
    )
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    insert_events(conn, [Event(
        person="alex", ts="2026-07-16T20:00:00+04:00", source="gitlab",
        kind="commit", summary="arrived after synthesis", hash="later",
        project="project-alpha",
    )])
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)

    page = (vault / "Work Journal" / "Week 2026-07-13-17.md").read_text()
    assert "> Provisional — event timestamps through 2026-07-14T10:00:00+04:00." in page
    assert "arrived after synthesis" in page
    assert "**Gap**" not in page
    assert "**Unmapped**: `_unmapped/x@y.z` (1 events)" in page
    assert "Gap and concentration checks are deferred until the Friday checkpoint." in page


def test_legacy_weekly_page_warns_when_exact_cutoff_is_not_stored(tmp_path):
    """Removing the explicit legacy warning would overstate old report coverage."""
    conn = _seed(tmp_path)
    _store_weekly_summary(conn, text="## Shipped\n- historical report")
    vault = tmp_path / "vault"

    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)

    page = (vault / "Work Journal" / "Week 2026-07-13-17.md").read_text()
    assert "> Legacy report — exact event cutoff unknown." in page
    assert "event timestamps through" not in page


def test_provisional_weekly_page_hides_stored_gap_and_concentration(tmp_path):
    """Rendering partial-week absence claims before Friday would break this."""
    conn = _seed(tmp_path)
    _store_weekly_summary(
        conn,
        text="## Shipped\n- rolling report",
        coverage_state="provisional",
        effective_flags={
            "gaps": ["sam"],
            "unmapped": [["_unmapped/x@y.z", 1]],
            "unmapped_channels": [["oc_orphan", 2]],
            "concentration": [["project-alpha", "alex", 0.9]],
        },
    )
    vault = tmp_path / "vault"

    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)

    page = (vault / "Work Journal" / "Week 2026-07-13-17.md").read_text()
    assert "**Gap**" not in page
    assert "**Concentration**" not in page
    assert "**Unmapped**: `_unmapped/x@y.z` (1 events)" in page
    assert "**Unmapped channel**: `oc_orphan` (2 messages)" in page
    assert "Gap and concentration checks are deferred until the Friday checkpoint." in page


def test_friday_weekly_page_renders_stored_gap_and_concentration_stably(tmp_path):
    """Dropping checkpoint flag facts or making output unstable would break this."""
    conn = _seed(tmp_path)
    _store_weekly_summary(
        conn,
        text=(
            "> Friday checkpoint — event timestamps through 2026-07-17T18:30:00+04:00; "
            "later evidence reconciles on the next full run.\n\n## Shipped\n- checkpoint report"
        ),
        coverage_state="friday-checkpoint",
        effective_flags={
            "gaps": ["sam"],
            "unmapped": [],
            "unmapped_channels": [],
            "concentration": [["project-alpha", "alex", 0.9]],
        },
    )
    vault = tmp_path / "vault"

    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    first = (vault / "Work Journal" / "Week 2026-07-13-17.md").read_bytes()
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    page = (vault / "Work Journal" / "Week 2026-07-13-17.md").read_text()

    assert "**Gap**: [Sam Lee]" in page
    assert "**Concentration**: [project-alpha]" in page
    assert "Gap and concentration checks are deferred" not in page
    assert (vault / "Work Journal" / "Week 2026-07-13-17.md").read_bytes() == first


def _managed_vault_bytes(vault: Path) -> dict[str, bytes]:
    contents = {
        "Person/existing.md": b"person before render\n",
        "Projects/existing.md": b"project before render\n",
        "Work Journal/existing.md": b"journal before render\n",
        "README.md": b"root before render\n",
    }
    for relative, content in contents.items():
        path = vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return {relative: (vault / relative).read_bytes() for relative in contents}


@pytest.mark.parametrize(
    "effective_flags",
    [
        pytest.param("not-json", id="malformed-json"),
        pytest.param("[]", id="non-object"),
        pytest.param('{"gaps":[1]}', id="malformed-entry"),
        pytest.param('{"unexpected":[]}', id="unknown-key"),
    ],
)
def test_invalid_nonlegacy_flags_preserve_existing_managed_vault(tmp_path, effective_flags):
    """Parsing stored flags after cleanup would erase this vault on bad provenance."""
    conn = _seed(tmp_path)
    _store_weekly_summary(
        conn,
        text="## Shipped\n- stored report",
        coverage_state="friday-checkpoint",
        effective_flags={},
    )
    conn.execute(
        "UPDATE summaries SET effective_flags_json = ? WHERE kind = 'weekly-team'",
        (effective_flags,),
    )
    conn.commit()
    vault = tmp_path / "vault"
    before = _managed_vault_bytes(vault)

    with pytest.raises(ValueError, match="invalid weekly report effective flags provenance"):
        render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)

    assert {
        relative: (vault / relative).read_bytes() for relative in before
    } == before


def test_partial_nonlegacy_provenance_never_uses_later_ledger_flags(tmp_path):
    """Falling back to current flags beside an older report would break this."""
    conn = _seed(tmp_path)
    _store_weekly_summary(
        conn,
        text=(
            "> Provisional — event timestamps through 2026-07-14T10:00:00+04:00.\n\n"
            "## Shipped\n- stored report"
        ),
        coverage_state="provisional",
        effective_flags=None,
    )
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    insert_events(conn, [Event(
        person="_unmapped/late@y.z", ts="2026-07-16T20:00:00+04:00",
        source="gitlab", kind="commit", summary="late unmapped evidence",
        hash="late-unmapped",
    )])
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)

    page = (vault / "Work Journal" / "Week 2026-07-13-17.md").read_text()
    assert "> Report provenance incomplete — stored effective flags unavailable." in page
    assert "Stored effective flags unavailable; no current-ledger flags are shown." in page
    assert "Gap and concentration checks are deferred until the Friday checkpoint." in page
    assert "**Gap**" not in page
    assert "**Unmapped**" not in page
    assert "**Concentration**" not in page
    assert "late@y.z" not in page


def test_render_without_summaries_falls_back_to_m2_layout(tmp_path):
    conn = _seed(tmp_path)
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    page = (vault / "Work Journal" / "Week 2026-07-13-17.md").read_text()
    assert "## People" in page and "## Appendix" not in page


def test_weekly_appendix_message_only_person_gets_day_headlines(tmp_path):
    import json as _json
    conn = _seed(tmp_path)
    _seed_summaries(conn)
    insert_events(conn, [
        Event(person="sam", ts="2026-07-14T11:00:00+04:00", source="feishu-channel",
              kind="message", summary="nav feedback", hash="m1",
              project="project-beta", refs=_json.dumps({"chat_id": "oc_up"})),
        Event(person="alex", ts="2026-07-14T11:01:00+04:00", source="feishu-channel",
              kind="message", summary="deploy done", hash="m2",
              project="project-alpha", refs=_json.dumps({"chat_id": "oc_wj"})),
    ])
    conn.execute(
        "INSERT INTO summaries (kind, key, input_hash, text, model, created_ts)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("daily-person", "sam|2026-07-14", "h",
         "- **Portal** — **nav redesign review** — feedback consolidated;"
         " next iteration planned.", "fake", "t"))
    conn.commit()
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    page = (vault / "Work Journal" / "Week 2026-07-13-17.md").read_text()
    headline = "- 2026-07-14 — **Portal** — **nav redesign review**\n"
    sam = page[page.index("### [Sam Lee]"):]
    assert headline in sam                                       # message-only: day headline
    assert sam.index(headline) < sam.index("💬 1 messages")     # above the count line
    assert "Alex fixed the JWT refresh race" not in page        # has work lines: no headline


def test_project_page_message_only_person_gets_day_headlines(tmp_path):
    import json as _json
    conn = _seed(tmp_path)
    insert_events(conn, [Event(
        person="sam", ts="2026-07-14T11:00:00+04:00", source="feishu-channel",
        kind="message", summary="nav feedback", hash="m1",
        project="project-beta", refs=_json.dumps({"chat_id": "oc_up"}))])
    conn.execute(
        "INSERT INTO summaries (kind, key, input_hash, text, model, created_ts)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("daily-person", "sam|2026-07-14", "h",
         "- Coordinated portal nav rollout with QA.", "fake", "t"))
    conn.commit()
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    page = (vault / "Projects" / "project-beta.md").read_text()
    assert "- 2026-07-14 — Coordinated portal nav rollout with QA.\n" in page


def test_project_page_links_docs_when_present(tmp_path):
    conn = _seed(tmp_path)
    vault = tmp_path / "vault"
    docs = vault / "Docs" / "project-alpha"
    docs.mkdir(parents=True)
    (docs / "architecture.md").write_text("# arch")
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    page = (vault / "Projects" / "project-alpha.md").read_text()
    assert "[Architecture](../Docs/project-alpha/architecture.md)" in page
    assert "](../Docs/project-alpha/summary.md)" not in page          # only existing files
    assert (docs / "architecture.md").exists()                     # Docs/ survives render


def test_message_line_names_channels_when_cache_present(tmp_path):
    import json as _json
    conn = _seed(tmp_path)
    insert_events(conn, [Event(
        person="alex", ts="2026-07-14T10:00:00+00:00", source="feishu-channel",
        kind="message", summary="hi", refs=_json.dumps({"chat_id": "oc_pm"}),
        hash="mm1")])
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY,
                 channel_names={"oc_pm": "PM. Share"})
    page = (vault / "Person" / "Alex Rivera" / "README.md").read_text()
    assert "💬 1 messages across 1 channels (PM. Share)" in page


def test_github_pull_requests_render_as_work_items(tmp_path):
    conn = _seed(tmp_path)
    insert_events(conn, [Event(
        person="alex",
        ts="2026-07-14T12:00:00+00:00",
        source="github",
        kind="pr",
        summary="[open] Add provider-neutral runner",
        refs='{"url": "https://github.test/pull/7"}',
        hash="pr-7",
        project="project-alpha",
    )])

    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)

    assert "[open] Add provider-neutral runner" in (
        vault / "Person" / "Alex Rivera" / "README.md"
    ).read_text()


def test_gitlab_issues_and_repos_render_as_work_items(tmp_path):
    conn = _seed(tmp_path)
    insert_events(conn, [
        Event(person="alex", ts="2026-07-14T12:00:00+00:00", source="gitlab",
              kind="issue", summary="[closed] Login rate limit",
              refs='{"iid": 31, "url": "https://gitlab.test/issues/31"}',
              hash="issue-31-closed", project="project-alpha"),
        Event(person="alex", ts="2026-07-14T08:00:00+00:00", source="gitlab",
              kind="repo", summary="[created] team/project-alpha",
              refs='{"id": 1, "url": "https://gitlab.test/team/project-alpha"}',
              hash="repo-1-created", project="project-alpha"),
    ])

    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)

    person = (vault / "Person" / "Alex Rivera" / "README.md").read_text()
    assert "[closed] Login rate limit" in person
    assert "[created] team/project-alpha" in person
    project = (vault / "Projects" / "project-alpha.md").read_text()
    assert "[closed] Login rate limit" in project


def test_channel_id_refs_are_counted_with_legacy_chat_id_refs(tmp_path):
    conn = _seed(tmp_path)
    insert_events(conn, [
        Event(
            person="alex",
            ts="2026-07-14T10:00:00+00:00",
            source="slack-channel",
            kind="message",
            summary="one",
            refs='{"channel_id": "C1"}',
            hash="slack-1",
        ),
        Event(
            person="alex",
            ts="2026-07-14T10:01:00+00:00",
            source="feishu-channel",
            kind="message",
            summary="two",
            refs='{"chat_id": "oc_1"}',
            hash="feishu-1",
        ),
    ])

    vault = tmp_path / "vault"
    render_vault(
        conn,
        IdentityMaps.load(CONFIG_DIR),
        vault,
        TODAY,
        channel_names={"C1": "Slack", "oc_1": "Feishu"},
    )

    page = (vault / "Person" / "Alex Rivera" / "README.md").read_text()
    assert "💬 2 messages across 2 channels (Feishu, Slack)" in page


def test_person_week_files_and_index(tmp_path):
    """Removing the per-week person files or the README week index breaks this."""
    conn = _seed(tmp_path)
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    readme = (vault / "Person" / "Alex Rivera" / "README.md").read_text()
    assert "## Weeks" in readme
    assert "- [Week 2026-07-13-17](Week%202026-07-13-17.md) — 2 events" in readme
    week = (vault / "Person" / "Alex Rivera" / "Week 2026-07-13-17.md").read_text()
    assert "# Alex Rivera — Week 2026-07-13-17" in week
    assert "[Alex Rivera](README.md)" in week
    assert "(../../Work%20Journal/Week%202026-07-13-17.md)" in week
    assert "fix: JWT refresh race" in week


def test_person_history_renders_beyond_window(tmp_path):
    """Week files older than the render window must survive the managed wipe."""
    conn = _seed(tmp_path)
    insert_events(conn, [Event(
        person="alex", ts="2026-05-05T09:00:00+04:00", source="gitlab",
        kind="commit", summary="ancient work", hash="old1",
        project="project-alpha", refs='{"url": "https://x/old1"}')])
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    old_week = vault / "Person" / "Alex Rivera" / "Week 2026-05-04-08.md"
    assert old_week.exists() and "ancient work" in old_week.read_text()
    assert not (vault / "Work Journal" / "Week 2026-05-04-08.md").exists()


def test_person_week_activity_detail_capped(tmp_path):
    conn = _seed(tmp_path)
    insert_events(conn, [Event(
        person="alex", ts="2026-07-14T10:00:00+04:00", source="gitlab",
        kind="commit", summary=f"bulk {i}", hash=f"bulk{i}",
        project="project-alpha") for i in range(13)])
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    week = (vault / "Person" / "Alex Rivera" / "Week 2026-07-13-17.md").read_text()
    assert week.count("- commit —") + week.count("- mr —") == 12
    assert "…and 3 more work items" in week
