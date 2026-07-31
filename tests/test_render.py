from datetime import date
from pathlib import Path

import pytest

from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.render import render_vault
from teammem.store import open_db, insert_events

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
    assert (vault / "Person" / "Alex Rivera.md").exists()
    assert (vault / "Person" / "Sam Lee.md").exists()
    assert not (vault / "Person" / "_unmapped").exists()          # no unmapped person pages
    assert (vault / "Projects" / "project-alpha.md").exists()
    assert (vault / "Work Journal" / "Week 2026-07-13-17.md").exists()
    assert (vault / "README.md").exists()


def test_week_report_content(tmp_path):
    conn = _seed(tmp_path)
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    report = (vault / "Work Journal" / "Week 2026-07-13-17.md").read_text()
    assert "[Alex Rivera](../Person/Alex%20Rivera.md)" in report   # person link
    assert "2 events" in report                                   # per-person count
    assert "(https://x/a1)" in report                             # every line carries a ref
    assert "[Sam Lee](../Person/Sam%20Lee.md)" in report and "no activity this week" in report  # gap flag
    assert "_unmapped/x@y.z" in report                            # unmapped surfaces


def test_person_page_content(tmp_path):
    conn = _seed(tmp_path)
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    page = (vault / "Person" / "Alex Rivera.md").read_text()
    assert "slug: alex" in page
    assert "[Week 2026-07-13-17](../Work%20Journal/Week%202026-07-13-17.md)" in page
    assert "fix: JWT refresh race" in page


def test_render_is_idempotent_and_removes_stale(tmp_path):
    conn = _seed(tmp_path)
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    stale = vault / "Person" / "Old Name.md"
    stale.write_text("stale")
    keep = vault / "Meeting Notes"; keep.mkdir(); (keep / "note.md").write_text("mine")
    first = (vault / "Person" / "Alex Rivera.md").read_bytes()
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    assert not stale.exists()                                     # managed dirs regenerated
    assert (keep / "note.md").read_text() == "mine"               # unmanaged untouched
    assert (vault / "Person" / "Alex Rivera.md").read_bytes() == first  # deterministic


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


def test_person_page_shows_day_entries_with_detail_demoted(tmp_path):
    conn = _seed(tmp_path)
    _seed_summaries(conn)
    vault = tmp_path / "vault"
    render_vault(conn, IdentityMaps.load(CONFIG_DIR), vault, TODAY)
    page = (vault / "Person" / "Alex Rivera.md").read_text()
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
    page = (vault / "Person" / "Alex Rivera.md").read_text()
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
        vault / "Person" / "Alex Rivera.md"
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

    person = (vault / "Person" / "Alex Rivera.md").read_text()
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

    page = (vault / "Person" / "Alex Rivera.md").read_text()
    assert "💬 2 messages across 2 channels (Feishu, Slack)" in page
