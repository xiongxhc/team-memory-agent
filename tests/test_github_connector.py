import sys
import types
from datetime import datetime, timezone
from pathlib import Path

from teammem.config import Config
from teammem.connectors.config import ConnectorSettings
from teammem.connectors.github import GitHubConnector
from teammem.identity import IdentityMaps
from teammem.store import insert_events, open_db


NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)
CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"
COMMIT = {
    "sha": "sha-abc",
    "html_url": "https://github.test/team/project-alpha/commit/sha-abc",
    "author": {"login": "alex-gh"},
    "commit": {
        "author": {
            "name": "Alex Rivera",
            "email": "alex@example.com",
            "date": "2026-07-14T09:00:00Z",
        },
        "message": "fix: JWT refresh race",
    },
}
PR = {
    "number": 7,
    "state": "closed",
    "title": "Auth middleware fix",
    "updated_at": "2026-07-14T10:00:00Z",
    "html_url": "https://github.test/pull/7",
    "user": {"login": "alex-gh"},
}


def _settings():
    return ConnectorSettings(name="github", enabled=True, options={})


def _cfg():
    return Config(github_token="test-token", since_days=7)


def _collect(fetch):
    return GitHubConnector(fetch=fetch).collect(
        _cfg(), IdentityMaps.load(CONFIG_DIR), _settings(), NOW
    )


def test_github_normalizes_mapped_commit_and_pull_request():
    """Removing typed-repository iteration or changing normalized event fields breaks this."""
    calls = []

    def fixture_fetch(path, params):
        calls.append((path, params))
        if path.endswith("/commits"):
            return [COMMIT] if params["page"] == 1 else []
        if path.endswith("/pulls"):
            return [PR] if params["page"] == 1 else []
        raise AssertionError(path)

    result = _collect(fixture_fetch)

    assert [(event.source, event.kind) for event in result.events] == [
        ("github", "commit"),
        ("github", "pr"),
    ]
    commit, pull_request = result.events
    assert (commit.person, commit.project, commit.ts, commit.summary, commit.hash) == (
        "alex", "project-alpha", "2026-07-14T09:00:00Z", "fix: JWT refresh race", "sha-abc"
    )
    assert commit.refs == (
        '{"sha": "sha-abc", "url": '
        '"https://github.test/team/project-alpha/commit/sha-abc"}'
    )
    assert (pull_request.person, pull_request.project, pull_request.ts, pull_request.summary) == (
        "alex", "project-alpha", "2026-07-14T10:00:00Z", "[closed] Auth middleware fix"
    )
    assert pull_request.refs == '{"number": 7, "url": "https://github.test/pull/7"}'
    assert pull_request.hash == "5d97791426560d2406c315b5f2ff1a70bbdef60be9681bd1ed62e2d2dd0c63cd"
    assert {path for path, _ in calls} == {
        "/repos/team/project-alpha/commits",
        "/repos/team/project-alpha/pulls",
    }


def test_github_paginates_commits_and_excludes_stale_pull_requests():
    """Dropping page traversal or the PR lookback filter breaks this."""
    commit_page = [dict(COMMIT, sha=f"sha-{number}") for number in range(100)]
    stale_pr = dict(PR, number=6, updated_at="2026-07-07T23:59:59Z")

    def fixture_fetch(path, params):
        if path.endswith("/commits"):
            return commit_page if params["page"] == 1 else [COMMIT] if params["page"] == 2 else []
        if path.endswith("/pulls"):
            return [stale_pr, PR] if params["page"] == 1 else []
        raise AssertionError(path)

    events = _collect(fixture_fetch).events

    assert len([event for event in events if event.kind == "commit"]) == 101
    assert [event.refs for event in events if event.kind == "pr"] == [
        '{"number": 7, "url": "https://github.test/pull/7"}'
    ]


def test_github_uses_since_and_idempotent_event_hashes(tmp_path):
    """Removing the commit lookback or changing stable hashes breaks this."""
    seen = {}

    def fixture_fetch(path, params):
        seen.setdefault(path, []).append(params)
        if path.endswith("/commits"):
            return [COMMIT] if params["page"] == 1 else []
        if path.endswith("/pulls"):
            return [PR] if params["page"] == 1 else []
        raise AssertionError(path)

    events = _collect(fixture_fetch).events
    conn = open_db(tmp_path / "ledger.db")

    assert seen["/repos/team/project-alpha/commits"][0] == {
        "since": "2026-07-08T00:00:00Z", "per_page": 100, "page": 1
    }
    assert seen["/repos/team/project-alpha/pulls"][0] == {
        "state": "all", "per_page": 100, "page": 1
    }
    assert insert_events(conn, events) == 2
    assert insert_events(conn, events) == 0


def test_github_http_fetch_uses_documented_bearer_headers(monkeypatch):
    """Removing the GitHub auth, media, or API-version header breaks this."""
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    class Session:
        def __init__(self):
            self.headers = {}
            self.calls = []

        def get(self, url, params, timeout):
            self.calls.append((url, params, timeout))
            return Response()

    session = Session()
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(Session=lambda: session))

    fetch = GitHubConnector().http_fetch(_cfg())

    assert fetch("/repos/team/project-alpha/commits", {"page": 1}) == []
    assert session.headers == {
        "Authorization": "Bearer test-token",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    assert session.calls == [(
        "https://api.github.com/repos/team/project-alpha/commits", {"page": 1}, 30
    )]
