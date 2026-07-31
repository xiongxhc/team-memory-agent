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


def test_open_issue_attributes_to_author():
    """Removing issue collection or its author attribution breaks this."""
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/issues": [[ISSUE]],
    })
    issue = next(e for e in events if e.kind == "issue")
    assert (issue.person, issue.project, issue.source) == ("alex", "project-alpha", "gitlab")
    assert issue.summary == "[opened] Login rate limit"
    assert issue.ts == "2026-07-14T11:00:00Z"
    assert issue.hash == event_hash("issue", "1", "31", "opened")


def test_closed_issue_attributes_to_assignee():
    """Closing is the assignee's work; author attribution here breaks this."""
    closed = dict(ISSUE, state="closed", closed_at="2026-07-14T15:00:00Z",
                  author={"username": "ghost"}, assignee={"username": "alexdev"})
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/issues": [[closed]],
    })
    assert events[0].person == "alex"
    assert events[0].ts == "2026-07-14T15:00:00Z"
    assert events[0].hash == event_hash("issue", "1", "31", "closed")


def test_closed_unassigned_issue_falls_back_to_author():
    closed = dict(ISSUE, state="closed", closed_at="2026-07-14T15:00:00Z")
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/issues": [[closed]],
    })
    assert events[0].person == "alex"


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


def test_repo_creator_lookup_failure_still_emits_event():
    """A deleted creator account must not lose the repo fact."""
    new_project = dict(PROJECTS[0], created_at="2026-07-14T08:00:00Z", creator_id=9)
    def fetch(path, params):
        if path.startswith("/users/"):
            raise RuntimeError("404")
        if path == "/groups/42/projects":
            return [new_project] if params["page"] == 1 else []
        return []
    cfg = Config.load(env={"TEAMMEM_GITLAB_GROUP": "42"})
    ids = IdentityMaps.load(CONFIG_DIR)
    events = collect_gitlab(cfg, ids, fetch, NOW)
    repo = next(e for e in events if e.kind == "repo")
    assert repo.person == "_unmapped/(none)"


def test_ghost_mr_author_is_unmapped_not_crash():
    ghost_mr = dict(MR, author=None)
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/commits": [[]],
        "/projects/1/merge_requests": [[ghost_mr]],
    })
    assert events[0].person == "_unmapped/(none)"


def test_connector_preserves_legacy_gitlab_event_identities():
    result = GitLabConnector(fetch_json=fake_fetch({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/commits": [[COMMIT]],
        "/projects/1/merge_requests": [[MR]],
        "/projects/1/issues": [[ISSUE]],
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
    ]
