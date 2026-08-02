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
    assert (commit.person, commit.project, commit.hash) == ("alex", "project-alpha", "sha-abc")
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
        return []
    cfg = Config.load(env={"TEAMMEM_GITLAB_GROUP": "42"})
    ids = IdentityMaps.load(CONFIG_DIR)
    collect_gitlab(cfg, ids, fetch, NOW)
    assert seen["/projects/1/repository/commits"]["since"] == "2026-07-08T00:00:00Z"
    assert seen["/projects/1/merge_requests"]["updated_after"] == "2026-07-08T00:00:00Z"
    assert seen["/projects/1/issues"]["updated_after"] == "2026-07-08T00:00:00Z"
    assert seen["/projects/1/repository/commits"]["per_page"] == 100
    assert seen["/groups/42/projects"]["include_subgroups"] == "true"
    assert seen["/groups/42/projects"]["with_shared"] == "false"


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


def test_ghost_mr_author_is_unmapped_not_crash():
    ghost_mr = dict(MR, author=None)
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/commits": [[]],
        "/projects/1/merge_requests": [[ghost_mr]],
    })
    assert events[0].person == "_unmapped/(none)"


def test_connector_preserves_legacy_gitlab_event_identities():
    closed = dict(
        ISSUE,
        state="closed",
        created_at="2026-01-01T08:00:00Z",
        closed_at="2026-07-14T15:00:00Z",
        closed_by={"username": "alexdev"},
    )
    result = GitLabConnector(fetch_json=fake_fetch({
        "/groups/42/projects": [PROJECTS],
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
         "sha-abc"),
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
