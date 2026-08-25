import json
from datetime import datetime, timezone
from pathlib import Path

from teammem.config import Config
from teammem.connectors.config import ConnectorSettings
from teammem.connectors.gitlab import GitLabConnector
from teammem.events import event_hash
from teammem.gitlab_collector import collect_gitlab
from teammem.identity import IdentityMaps

NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)
# Hermetic fixture dir: only .example files, so IdentityMaps.load's fallback is
# deterministic regardless of the operator's real config/roster.yaml.
CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"

PROJECTS = [{"id": 1, "path_with_namespace": "team/project-alpha"}]
BRANCHES = [{"name": "main"}]
COMMIT = {"id": "sha-abc", "author_email": "alex@example.com", "author_name": "Alex",
          "committed_date": "2026-07-14T09:00:00Z", "title": "fix: JWT refresh race",
          "web_url": "https://gitlab.internal/team/project-alpha/-/commit/sha-abc"}
MR = {"iid": 7, "state": "merged", "title": "Auth middleware fix",
      "author": {"username": "alexdev"}, "updated_at": "2026-07-14T10:00:00Z",
      "merged_at": "2026-07-14T10:00:00Z",
      "web_url": "https://gitlab.internal/team/project-alpha/-/merge_requests/7"}
ISSUE = {"iid": 31, "state": "opened", "title": "Login rate limit",
         "author": {"username": "alexdev"}, "assignee": None,
         "created_at": "2026-07-14T08:00:00Z",
         "updated_at": "2026-07-14T11:00:00Z", "closed_at": None,
         "web_url": "https://gitlab.internal/team/project-alpha/-/issues/31"}


def fake_fetch(responses):
    """responses: {path_prefix: [page1, page2, ...]}; empty list after pages run out."""
    def fetch(path, params):
        pages = responses.get(path, [])
        page = params["page"] - 1
        return pages[page] if page < len(pages) else []
    return fetch


def _collect(responses):
    responses = dict(responses)
    if "/projects/1/repository/commits" in responses:
        responses.setdefault("/projects/1/repository/branches", [BRANCHES])
    cfg = Config.load(env={"TEAMMEM_GITLAB_GROUP": "42"})
    ids = IdentityMaps.load(CONFIG_DIR)
    return collect_gitlab(cfg, ids, fake_fetch(responses), NOW)


def _collect_result(fetch):
    return GitLabConnector(fetch_json=fetch).collect(
        Config.load(env={"TEAMMEM_GITLAB_GROUP": "42"}),
        IdentityMaps.load(CONFIG_DIR),
        ConnectorSettings(name="gitlab", enabled=True, options={}),
        NOW,
    )


def test_commit_and_mr_become_events():
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/commits": [[COMMIT]],
        "/projects/1/merge_requests": [[MR]],
    })
    commit = next(e for e in events if e.kind == "commit")
    assert (commit.person, commit.project, commit.hash) == (
        "alex",
        "project-alpha",
        event_hash("commit", "1", "sha-abc"),
    )
    assert commit.source == "gitlab" and commit.summary == "fix: JWT refresh race"
    mr = next(e for e in events if e.kind == "mr")
    assert mr.person == "alex"                     # resolved via gitlab username
    assert mr.summary == "[merged] Auth middleware fix"
    assert mr.hash == event_hash("mr", "1", "7", "merged")


def test_unknown_author_is_unmapped_not_dropped():
    ghost = dict(COMMIT, id="sha-x", author_email="ghost@nowhere.com")
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/commits": [[ghost]],
        "/projects/1/merge_requests": [[]],
    })
    assert events[0].person == "_unmapped/ghost@nowhere.com"


def test_pagination_follows_full_pages():
    page1 = [dict(COMMIT, id=f"sha-{i}") for i in range(100)]   # full page → fetch next
    page2 = [dict(COMMIT, id="sha-last")]
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/commits": [page1, page2],
        "/projects/1/merge_requests": [[]],
    })
    assert len([e for e in events if e.kind == "commit"]) == 101


def test_since_and_updated_after_params_sent():
    seen = {}
    def fetch(path, params):
        seen[path] = params
        if path == "/groups/42/projects":
            return PROJECTS if params["page"] == 1 else []
        if path == "/projects/1/repository/branches":
            return BRANCHES if params["page"] == 1 else []
        return []
    cfg = Config.load(env={"TEAMMEM_GITLAB_GROUP": "42"})
    ids = IdentityMaps.load(CONFIG_DIR)
    collect_gitlab(cfg, ids, fetch, NOW)
    assert seen["/projects/1/repository/commits"]["since"] == "2026-07-08T00:00:00Z"
    assert seen["/projects/1/repository/commits"]["ref_name"] == "main"
    assert "all" not in seen["/projects/1/repository/commits"]
    assert seen["/projects/1/merge_requests"]["updated_after"] == "2026-07-08T00:00:00Z"
    assert seen["/projects/1/issues"]["updated_after"] == "2026-07-08T00:00:00Z"
    assert seen["/projects/1/repository/commits"]["per_page"] == 100
    assert seen["/groups/42/projects"]["include_subgroups"] == "true"
    assert seen["/groups/42/projects"]["with_shared"] == "false"


def test_daily_commits_include_non_default_branches():
    branch_commit = dict(
        COMMIT,
        id="sha-feature",
        title="feat: work still on a feature branch",
    )

    def fetch(path, params):
        if path == "/groups/42/projects":
            return PROJECTS if params["page"] == 1 else []
        if path == "/projects/1/repository/branches":
            return [{"name": "main"}, {"name": "feature/auth"}]
        if path == "/projects/1/repository/commits":
            if params["page"] == 1 and params.get("ref_name") == "feature/auth":
                return [branch_commit]
            return []
        return []

    result = _collect_result(fetch)

    commits = [event for event in result.events if event.kind == "commit"]
    assert [(event.hash, event.summary, event.ts) for event in commits] == [
        (
            event_hash("commit", "1", "sha-feature"),
            "feat: work still on a feature branch",
            "2026-07-14T09:00:00Z",
        ),
    ]


def test_branch_listing_and_per_branch_commits_are_paginated():
    branches_page_1 = [{"name": f"branch-{index}"} for index in range(100)]
    branches_page_2 = [{"name": "branch-last"}]
    seen = []

    def fetch(path, params):
        seen.append((path, dict(params)))
        if path == "/groups/42/projects":
            return PROJECTS if params["page"] == 1 else []
        if path == "/projects/1/repository/branches":
            return branches_page_1 if params["page"] == 1 else branches_page_2
        if path == "/projects/1/repository/commits":
            if params["ref_name"] == "branch-last" and params["page"] == 1:
                return [dict(COMMIT, id="sha-last-branch")]
            return []
        return []

    result = _collect_result(fetch)

    assert [event.summary for event in result.events if event.kind == "commit"] == [
        "fix: JWT refresh race",
    ]
    assert (
        "/projects/1/repository/branches",
        {"per_page": 100, "page": 2},
    ) in seen
    assert any(
        path == "/projects/1/repository/commits"
        and params["ref_name"] == "branch-last"
        for path, params in seen
    )


def test_same_commit_reachable_from_multiple_branches_is_emitted_once():
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/branches": [[{"name": "main"}, {"name": "release"}]],
        "/projects/1/repository/commits": [[COMMIT]],
    })

    assert len([event for event in events if event.kind == "commit"]) == 1


def test_tag_only_commit_is_not_collected():
    branch_commit = dict(COMMIT, id="sha-branch")
    tag_only_commit = dict(COMMIT, id="sha-tag-only")

    def fetch(path, params):
        if path == "/groups/42/projects":
            return PROJECTS if params["page"] == 1 else []
        if path == "/projects/1/repository/branches":
            return BRANCHES if params["page"] == 1 else []
        if path == "/projects/1/repository/commits":
            if params.get("all") == "true":
                return [branch_commit, tag_only_commit]
            return [branch_commit] if params["page"] == 1 else []
        return []

    result = _collect_result(fetch)

    assert [json.loads(event.refs)["sha"] for event in result.events
            if event.kind == "commit"] == ["sha-branch"]


def test_same_author_and_sha_in_different_projects_have_distinct_identities():
    projects = [
        PROJECTS[0],
        {"id": 2, "path_with_namespace": "team/project-beta"},
    ]

    def fetch(path, params):
        if path == "/groups/42/projects":
            return projects if params["page"] == 1 else []
        if path in {
            "/projects/1/repository/branches",
            "/projects/2/repository/branches",
        }:
            return BRANCHES if params["page"] == 1 else []
        if path in {
            "/projects/1/repository/commits",
            "/projects/2/repository/commits",
        }:
            return [COMMIT] if params["page"] == 1 else []
        return []

    result = _collect_result(fetch)
    commits = [event for event in result.events if event.kind == "commit"]

    assert [(event.project, event.hash) for event in commits] == [
        ("project-alpha", event_hash("commit", "1", "sha-abc")),
        (None, event_hash("commit", "2", "sha-abc")),
    ]


def test_new_issue_uses_creation_time_and_attributes_to_author():
    """Using comment/update time as the opening fact fabricates activity."""
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/issues": [[ISSUE]],
    })
    issue = next(e for e in events if e.kind == "issue")
    assert (issue.person, issue.project, issue.source) == ("alex", "project-alpha", "gitlab")
    assert issue.summary == "[opened] Login rate limit"
    assert issue.ts == "2026-07-14T08:00:00Z"
    assert issue.hash == event_hash("issue", "1", "31", "opened")


def test_old_open_issue_updated_only_by_comment_emits_no_issue_fact():
    old = dict(
        ISSUE,
        created_at="2026-01-01T08:00:00Z",
        updated_at="2026-07-14T11:00:00Z",
    )

    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/issues": [[old]],
    })

    assert [event for event in events if event.kind == "issue"] == []


def test_closed_issue_attributes_to_closed_by_not_assignee():
    """Assignment does not prove who performed the provider-reported close."""
    closed = dict(ISSUE, state="closed", closed_at="2026-07-14T15:00:00Z",
                  created_at="2026-01-01T08:00:00Z",
                  author={"username": "ghost"}, assignee={"username": "samdev"},
                  closed_by={"username": "alexdev"})
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/issues": [[closed]],
    })
    assert events[0].person == "alex"
    assert events[0].ts == "2026-07-14T15:00:00Z"
    assert events[0].hash == event_hash("issue", "1", "31", "closed")


def test_issue_created_and_closed_in_window_emits_both_lifecycle_facts():
    closed = dict(
        ISSUE,
        state="closed",
        closed_at="2026-07-14T15:00:00Z",
        closed_by={"username": "samdev"},
    )
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/issues": [[closed]],
    })

    assert [(event.person, event.ts, event.summary, event.hash) for event in events] == [
        (
            "alex",
            "2026-07-14T08:00:00Z",
            "[opened] Login rate limit",
            event_hash("issue", "1", "31", "opened"),
        ),
        (
            "sam",
            "2026-07-14T15:00:00Z",
            "[closed] Login rate limit",
            event_hash("issue", "1", "31", "closed"),
        ),
    ]


def test_ghost_issue_author_is_unmapped_not_crash():
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/issues": [[dict(ISSUE, author=None)]],
    })
    assert events[0].person == "_unmapped/(none)"


def test_new_repo_becomes_attributed_event():
    """Removing repo-creation collection or creator lookup breaks this."""
    new_project = dict(PROJECTS[0], created_at="2026-07-14T08:00:00Z", creator_id=5,
                       web_url="https://gitlab.internal/team/project-alpha")
    events = _collect({
        "/groups/42/projects": [[new_project]],
        "/users/5": [{"username": "alexdev"}],
    })
    repo = next(e for e in events if e.kind == "repo")
    assert (repo.person, repo.project, repo.ts) == (
        "alex", "project-alpha", "2026-07-14T08:00:00Z")
    assert repo.summary == "[created] team/project-alpha"
    assert repo.hash == event_hash("repo", "1", "created")


def test_old_repo_emits_no_repo_event():
    old_project = dict(PROJECTS[0], created_at="2026-01-01T00:00:00Z", creator_id=5)
    events = _collect({"/groups/42/projects": [[old_project]]})
    assert [e for e in events if e.kind == "repo"] == []


def test_fractional_project_timestamp_at_exact_boundary_is_included():
    boundary_project = dict(
        PROJECTS[0],
        created_at="2026-07-08T00:00:00.000000Z",
        creator_id=5,
    )
    events = _collect({
        "/groups/42/projects": [[boundary_project]],
        "/users/5": [{"username": "alexdev"}],
    })

    repo = next(event for event in events if event.kind == "repo")
    assert repo.ts == "2026-07-08T00:00:00.000000Z"


def test_repo_creator_lookup_failure_defers_event_then_retries_successfully():
    new_project = dict(PROJECTS[0], created_at="2026-07-14T08:00:00Z", creator_id=9)
    attempts = 0

    def fetch(path, params):
        nonlocal attempts
        if path.startswith("/users/"):
            attempts += 1
            if attempts == 1:
                raise RuntimeError("private lookup detail")
            return {"username": "new-user"}
        if path == "/groups/42/projects":
            return [new_project] if params["page"] == 1 else []
        return []

    first = _collect_result(fetch)
    assert [event for event in first.events if event.kind == "repo"] == []
    assert first.warnings == (
        "repository creator lookup failed for team/project-alpha; creation deferred",
    )
    assert "private lookup detail" not in first.warnings[0]

    second = _collect_result(fetch)
    repos = [event for event in second.events if event.kind == "repo"]
    assert len(repos) == 1
    assert repos[0].person == "_unmapped/new-user"
    assert second.warnings == ()


def test_repo_creator_unusable_response_defers_event_with_warning():
    new_project = dict(PROJECTS[0], created_at="2026-07-14T08:00:00Z", creator_id=9)
    result = _collect_result(fake_fetch({
        "/groups/42/projects": [[new_project]],
        "/users/9": [[]],
    }))

    assert [event for event in result.events if event.kind == "repo"] == []
    assert result.warnings == (
        "repository creator lookup failed for team/project-alpha; creation deferred",
    )


def test_merged_mr_backfills_commits_older_than_window():
    """A feature-branch commit authored before the window but merged inside it
    only ever surfaces via the MR's commit list — the bounded per-branch listing
    filters it out by committed_date."""
    old_commit = dict(COMMIT, id="sha-old", committed_date="2026-06-01T09:00:00Z",
                      title="feat: branch work started long ago")
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/commits": [[]],
        "/projects/1/merge_requests": [[MR]],
        "/projects/1/merge_requests/7/commits": [[old_commit]],
    })
    commit = next(e for e in events if e.kind == "commit")
    assert (commit.person, commit.project, commit.hash) == (
        "alex",
        "project-alpha",
        event_hash("commit", "1", "sha-old"),
    )
    assert commit.ts == "2026-06-01T09:00:00Z"
    assert commit.summary == "feat: branch work started long ago"


def test_mr_backfill_skips_commits_already_in_daily_branch_listing():
    """UNIQUE(person, source, hash) would drop the duplicate at ingest anyway,
    but one collection run should not emit the same sha twice."""
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/commits": [[COMMIT]],
        "/projects/1/merge_requests": [[MR]],
        "/projects/1/merge_requests/7/commits": [[COMMIT]],
    })
    assert len([e for e in events if e.kind == "commit"]) == 1


def test_merged_mr_backfills_unseen_in_window_commit_from_deleted_branch():
    in_window = dict(
        COMMIT,
        id="sha-deleted-branch",
        title="feat: original work from deleted source branch",
    )
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/branches": [BRANCHES],
        "/projects/1/repository/commits": [[]],
        "/projects/1/merge_requests": [[MR]],
        "/projects/1/merge_requests/7/commits": [[in_window]],
    })

    commits = [event for event in events if event.kind == "commit"]
    assert [(json.loads(event.refs)["sha"], event.hash) for event in commits] == [
        (
            "sha-deleted-branch",
            event_hash("commit", "1", "sha-deleted-branch"),
        ),
    ]


def test_mr_backfill_preserves_distinct_squash_and_original_commit_shas():
    squash = dict(COMMIT, id="sha-squash", title="feat: squashed result")
    original = dict(
        COMMIT,
        id="sha-original",
        committed_date="2026-06-01T09:00:00Z",
        title="feat: original branch commit",
    )
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/commits": [[squash]],
        "/projects/1/merge_requests": [[MR]],
        "/projects/1/merge_requests/7/commits": [[original]],
    })

    assert {event.hash for event in events if event.kind == "commit"} == {
        event_hash("commit", "1", "sha-squash"),
        event_hash("commit", "1", "sha-original"),
    }


def _paths_fetched(responses):
    seen = []
    inner = fake_fetch(responses)
    def fetch(path, params):
        seen.append(path)
        return inner(path, params)
    return fetch, seen


def test_unmerged_mr_does_not_fetch_commits():
    opened = dict(MR, state="opened", merged_at=None)
    fetch, seen = _paths_fetched({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/merge_requests": [[opened]],
    })
    cfg = Config.load(env={"TEAMMEM_GITLAB_GROUP": "42"})
    collect_gitlab(cfg, IdentityMaps.load(CONFIG_DIR), fetch, NOW)
    assert "/projects/1/merge_requests/7/commits" not in seen


def test_mr_merged_before_window_does_not_fetch_commits():
    """An old merged MR bumped by a comment re-enters the listing via
    updated_after; its commits were already collectable when it merged."""
    stale = dict(MR, merged_at="2026-01-05T10:00:00Z")
    fetch, seen = _paths_fetched({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/merge_requests": [[stale]],
    })
    cfg = Config.load(env={"TEAMMEM_GITLAB_GROUP": "42"})
    collect_gitlab(cfg, IdentityMaps.load(CONFIG_DIR), fetch, NOW)
    assert "/projects/1/merge_requests/7/commits" not in seen


def test_mr_merged_at_exact_lookback_boundary_backfills_commits():
    boundary = dict(MR, merged_at="2026-07-08T00:00:00.000000Z")
    fetch, seen = _paths_fetched({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/merge_requests": [[boundary]],
        "/projects/1/merge_requests/7/commits": [[]],
    })

    cfg = Config.load(env={"TEAMMEM_GITLAB_GROUP": "42"})
    collect_gitlab(cfg, IdentityMaps.load(CONFIG_DIR), fetch, NOW)

    assert "/projects/1/merge_requests/7/commits" in seen


def test_mr_commit_backfill_uses_pagination():
    page1 = [dict(COMMIT, id=f"sha-mr-{index}") for index in range(100)]
    page2 = [dict(COMMIT, id="sha-mr-last")]
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/commits": [[]],
        "/projects/1/merge_requests": [[MR]],
        "/projects/1/merge_requests/7/commits": [page1, page2],
    })

    assert len([event for event in events if event.kind == "commit"]) == 101


def test_collect_mr_commits_option_disables_backfill():
    fetch, seen = _paths_fetched({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/merge_requests": [[MR]],
        "/projects/1/merge_requests/7/commits": [[COMMIT]],
    })
    result = GitLabConnector(fetch_json=fetch).collect(
        Config.load(env={"TEAMMEM_GITLAB_GROUP": "42"}),
        IdentityMaps.load(CONFIG_DIR),
        ConnectorSettings(name="gitlab", enabled=True,
                          options={"collect_mr_commits": False}),
        NOW,
    )
    assert "/projects/1/merge_requests/7/commits" not in seen
    assert [e for e in result.events if e.kind == "commit"] == []


def test_failed_mr_commit_lookup_warns_without_losing_other_events_and_retries():
    old_commit = dict(
        COMMIT,
        id="sha-old",
        committed_date="2026-06-01T09:00:00Z",
        title="feat: older branch work",
    )
    attempts = 0

    def fetch(path, params):
        nonlocal attempts
        if path == "/groups/42/projects":
            return PROJECTS if params["page"] == 1 else []
        if path == "/projects/1/repository/branches":
            return BRANCHES if params["page"] == 1 else []
        if path == "/projects/1/repository/commits":
            return [COMMIT] if params["page"] == 1 else []
        if path == "/projects/1/merge_requests":
            return [MR] if params["page"] == 1 else []
        if path == "/projects/1/merge_requests/7/commits":
            attempts += 1
            if attempts == 1:
                raise RuntimeError("token=super-secret upstream detail")
            return [old_commit] if params["page"] == 1 else []
        if path == "/projects/1/issues":
            return [ISSUE] if params["page"] == 1 else []
        return []

    first = _collect_result(fetch)

    assert {event.kind for event in first.events} == {"commit", "mr", "issue"}
    assert [event.hash for event in first.events if event.kind == "commit"] == [
        event_hash("commit", "1", "sha-abc"),
    ]
    assert first.warnings == (
        "merge request commit lookup failed for team/project-alpha !7; backfill deferred",
    )
    assert "super-secret" not in first.warnings[0]

    second = _collect_result(fetch)
    assert {event.hash for event in second.events if event.kind == "commit"} == {
        event_hash("commit", "1", "sha-abc"),
        event_hash("commit", "1", "sha-old"),
    }
    assert second.warnings == ()


def test_ghost_mr_author_is_unmapped_not_crash():
    ghost_mr = dict(MR, author=None)
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/commits": [[]],
        "/projects/1/merge_requests": [[ghost_mr]],
    })
    assert events[0].person == "_unmapped/(none)"


def test_connector_preserves_non_commit_gitlab_identities():
    closed = dict(
        ISSUE,
        state="closed",
        created_at="2026-01-01T08:00:00Z",
        closed_at="2026-07-14T15:00:00Z",
        closed_by={"username": "alexdev"},
    )
    result = GitLabConnector(fetch_json=fake_fetch({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/branches": [BRANCHES],
        "/projects/1/repository/commits": [[COMMIT]],
        "/projects/1/merge_requests": [[MR]],
        "/projects/1/issues": [[ISSUE, closed]],
    })).collect(
        Config.load(env={"TEAMMEM_GITLAB_GROUP": "42"}),
        IdentityMaps.load(CONFIG_DIR),
        ConnectorSettings(name="gitlab", enabled=True, options={}),
        NOW,
    )
    assert [(event.source, event.kind, event.refs, event.hash) for event in result.events] == [
        ("gitlab", "commit",
         '{"sha": "sha-abc", "url": "https://gitlab.internal/team/project-alpha/-/commit/sha-abc"}',
         event_hash("commit", "1", "sha-abc")),
        ("gitlab", "mr",
         '{"iid": 7, "url": "https://gitlab.internal/team/project-alpha/-/merge_requests/7"}',
         "b56227665acb0f91946d18838c871e0cd076abdbcfade0e3bc52ba25d107c767"),
        ("gitlab", "issue",
         '{"iid": 31, "url": "https://gitlab.internal/team/project-alpha/-/issues/31"}',
         "907928208df1c752749c3bea14cd056955dde724d7df620eaf597e7ac9c4beed"),
        ("gitlab", "issue",
         '{"iid": 31, "url": "https://gitlab.internal/team/project-alpha/-/issues/31"}',
         "789bb2e605c8237773c1eb4c159aa90a6ad895365f1b86de054cb1cc1a23ccf8"),
    ]


MR_NOTE = {"id": 900, "body": "LGTM, one nit on the quota check", "system": False,
           "author": {"username": "alexdev"},
           "created_at": "2026-07-14T10:30:00Z"}
ISSUE_NOTE = {"id": 901, "body": "Repro confirmed on staging", "system": False,
              "author": {"username": "alexdev"},
              "created_at": "2026-07-14T11:30:00Z"}


def test_mr_and_issue_comments_become_events():
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/merge_requests": [[MR]],
        "/projects/1/issues": [[ISSUE]],
        "/projects/1/merge_requests/7/notes": [[MR_NOTE]],
        "/projects/1/issues/31/notes": [[ISSUE_NOTE]],
    })
    comments = [e for e in events if e.kind == "comment"]
    assert len(comments) == 2
    mr_c = next(c for c in comments if c.summary.startswith("[!7]"))
    assert mr_c.person == "alex"
    assert mr_c.summary == "[!7] LGTM, one nit on the quota check"
    assert mr_c.ts == "2026-07-14T10:30:00Z"
    assert mr_c.hash == event_hash("comment", "1", "900")
    assert json.loads(mr_c.refs)["url"] == MR["web_url"] + "#note_900"
    issue_c = next(c for c in comments if c.summary.startswith("[#31]"))
    assert issue_c.summary == "[#31] Repro confirmed on staging"
    assert issue_c.hash == event_hash("comment", "1", "901")


def test_system_bot_and_stale_notes_not_collected():
    system_note = dict(MR_NOTE, id=902, system=True, body="changed milestone")
    bot_note = dict(MR_NOTE, id=903, author={"username": "fgbot"})
    stale_note = dict(MR_NOTE, id=904, created_at="2026-06-01T00:00:00Z")
    result = GitLabConnector(fetch_json=fake_fetch({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/merge_requests": [[MR]],
        "/projects/1/merge_requests/7/notes": [[system_note, bot_note, stale_note]],
    })).collect(
        Config.load(env={"TEAMMEM_GITLAB_GROUP": "42"}),
        IdentityMaps.load(CONFIG_DIR),
        ConnectorSettings(name="gitlab", enabled=True,
                          options={"exclude_note_authors": ["fgbot"]}),
        NOW,
    )
    assert [e for e in result.events if e.kind == "comment"] == []


def test_comment_summary_is_capped_at_120_chars():
    long_note = dict(MR_NOTE, id=905, body="x" * 200)
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/merge_requests": [[MR]],
        "/projects/1/merge_requests/7/notes": [[long_note]],
    })
    comment = next(e for e in events if e.kind == "comment")
    assert comment.summary == "[!7] " + "x" * 119 + "…"


def test_failed_note_lookup_warns_without_losing_other_events():
    def fetch(path, params):
        if path.endswith("/notes"):
            raise RuntimeError("boom")
        return fake_fetch({
            "/groups/42/projects": [PROJECTS],
            "/projects/1/merge_requests": [[dict(MR, state="opened", merged_at=None)]],
        })(path, params)
    result = GitLabConnector(fetch_json=fetch).collect(
        Config.load(env={"TEAMMEM_GITLAB_GROUP": "42"}),
        IdentityMaps.load(CONFIG_DIR),
        ConnectorSettings(name="gitlab", enabled=True, options={}),
        NOW,
    )
    assert [e.kind for e in result.events] == ["mr"]
    assert any("comment lookup failed" in w and "!7" in w for w in result.warnings)


def test_multiline_comment_body_collapses_to_one_line():
    noisy = dict(MR_NOTE, id=906,
                 body="LGTM overall.\n\n- fix the quota check\n- add a test\n")
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/merge_requests": [[MR]],
        "/projects/1/merge_requests/7/notes": [[noisy]],
    })
    comment = next(e for e in events if e.kind == "comment")
    assert comment.summary == "[!7] LGTM overall. - fix the quota check - add a test"


def test_empty_comment_body_is_skipped():
    empty = dict(MR_NOTE, id=907, body="   \n  ")
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/merge_requests": [[MR]],
        "/projects/1/merge_requests/7/notes": [[empty]],
    })
    assert [e for e in events if e.kind == "comment"] == []
