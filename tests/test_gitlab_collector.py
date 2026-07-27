from datetime import datetime, timezone
from pathlib import Path

from teammem.config import Config
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
    assert seen["/projects/1/repository/commits"]["per_page"] == 100


def test_ghost_mr_author_is_unmapped_not_crash():
    ghost_mr = dict(MR, author=None)
    events = _collect({
        "/groups/42/projects": [PROJECTS],
        "/projects/1/repository/commits": [[]],
        "/projects/1/merge_requests": [[ghost_mr]],
    })
    assert events[0].person == "_unmapped/(none)"
