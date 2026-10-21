import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from teammem.config import Config
from teammem.connectors.config import ConnectorSettings
from teammem.connectors.github import GitHubConnector
from teammem.identity import IdentityMaps
from teammem.metrics import CommitCountScope, WeeklyCommitCount
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


def _settings(options=None):
    return ConnectorSettings(name="github", enabled=True, options=options or {})


def _cfg():
    return Config(github_token="test-token", since_days=7)


def _ids(projection):
    return IdentityMaps(
        {
            "members": {
                "alex": {
                    "emails": ["alex@example.com"],
                    "github": ["alex-gh"],
                },
                "sam": {
                    "emails": ["sam@example.com"],
                    "github": ["sam-gh"],
                },
            }
        },
        {
            "projects": {
                "project-alpha": {
                    "projection": projection,
                    "github_repos": ["team/project-alpha"],
                }
            }
        },
    )


def _collect(fetch, *, ids=None, options=None, now=NOW):
    return GitHubConnector(fetch=fetch).collect(
        _cfg(), ids or IdentityMaps.load(CONFIG_DIR), _settings(options), now
    )


def _commit(sha, login, email, date="2026-07-14T09:00:00Z"):
    return {
        **COMMIT,
        "sha": sha,
        "author": {"login": login} if login is not None else None,
        "commit": {
            **COMMIT["commit"],
            "author": {
                **COMMIT["commit"]["author"],
                "email": email,
                "date": date,
            },
        },
    }


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

    result = _collect(fixture_fetch, ids=_ids("full"))

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


def test_github_count_only_aggregates_without_commit_events_or_pull_requests(monkeypatch):
    """Constructing raw commit Events or requesting pulls breaks count-only privacy."""
    calls = []
    commits = [
        _commit("sha-alex-1", "alex-gh", "alex@example.com"),
        _commit("sha-alex-2", "alex-gh", "alex@example.com"),
        _commit("sha-sam-1", "sam-gh", "sam@example.com"),
    ]

    def fixture_fetch(path, params):
        calls.append((path, params))
        return commits if path.endswith("/commits") and params["page"] == 1 else []

    def forbidden_event(*args, **kwargs):
        raise AssertionError("count-only commits must not construct Event objects")

    monkeypatch.setattr(GitHubConnector, "_commit_event", staticmethod(forbidden_event))

    result = _collect(fixture_fetch, ids=_ids("count-only"))

    assert result.events == ()
    assert result.commit_counts == (
        WeeklyCommitCount("project-alpha", "2026-07-13", "alex", 2),
        WeeklyCommitCount("project-alpha", "2026-07-13", "sam", 1),
    )
    assert all("sha-" not in repr(row) for row in result.commit_counts)
    assert not any(path.endswith("/pulls") for path, _ in calls)


def test_github_count_only_falls_back_from_unmapped_login_to_author_email():
    """Removing email fallback misattributes commits with an absent GitHub identity."""
    commit = _commit("sha-email", None, "sam@example.com")

    def fixture_fetch(path, params):
        return [commit] if path.endswith("/commits") and params["page"] == 1 else []

    result = _collect(fixture_fetch, ids=_ids("count-only"))

    assert result.commit_counts == (
        WeeklyCommitCount("project-alpha", "2026-07-13", "sam", 1),
    )


def test_github_count_only_returns_complete_four_week_scope_with_zero_weeks():
    """Omitting empty weeks leaves stale persisted counts outside the returned scope."""
    commit = _commit("sha-current", "alex-gh", "alex@example.com")
    calls = []

    def fixture_fetch(path, params):
        calls.append((path, params))
        return [commit] if path.endswith("/commits") and params["page"] == 1 else []

    result = _collect(
        fixture_fetch,
        ids=_ids("count-only"),
    )

    assert result.commit_count_scopes == (
        CommitCountScope("project-alpha", "2026-06-22"),
        CommitCountScope("project-alpha", "2026-06-29"),
        CommitCountScope("project-alpha", "2026-07-06"),
        CommitCountScope("project-alpha", "2026-07-13"),
    )
    assert calls[0][1]["since"] == "2026-06-22T00:00:00Z"


def test_github_count_only_honors_two_week_scope_and_exact_oldest_monday():
    calls = []

    def fixture_fetch(path, params):
        calls.append((path, params))
        return []

    result = _collect(
        fixture_fetch,
        ids=_ids("count-only"),
        options={"count_weeks": 2},
    )

    assert result.commit_count_scopes == (
        CommitCountScope("project-alpha", "2026-07-06"),
        CommitCountScope("project-alpha", "2026-07-13"),
    )
    assert calls[0][1]["since"] == "2026-07-06T00:00:00Z"


@pytest.mark.parametrize("count_weeks", [0, 53, True, 2.5, "2", "four"])
def test_github_count_only_rejects_invalid_count_weeks(count_weeks):
    """Accepting an invalid aggregation window makes scope replacement unsafe."""
    with pytest.raises(ValueError, match="count_weeks.*1.*52"):
        _collect(
            lambda path, params: [],
            ids=_ids("count-only"),
            options={"count_weeks": count_weeks},
        )


def test_github_count_only_uses_utc_week_for_the_same_instant_in_any_offset():
    utc_now = datetime(2026, 7, 13, 0, 30, tzinfo=timezone.utc)
    offset_now = datetime(
        2026,
        7,
        12,
        20,
        30,
        tzinfo=timezone(-timedelta(hours=4)),
    )
    results = []
    requests = []

    for now in (utc_now, offset_now):
        calls = []

        def fixture_fetch(path, params):
            calls.append((path, params))
            return []

        results.append(
            _collect(
                fixture_fetch,
                ids=_ids("count-only"),
                options={"count_weeks": 2},
                now=now,
            )
        )
        requests.append(calls[0][1])

    expected_scopes = (
        CommitCountScope("project-alpha", "2026-07-06"),
        CommitCountScope("project-alpha", "2026-07-13"),
    )
    assert tuple(result.commit_count_scopes for result in results) == (
        expected_scopes,
        expected_scopes,
    )
    assert requests[0]["since"] == "2026-07-06T00:00:00Z"
    assert requests[1]["since"] == requests[0]["since"]


def test_github_full_collection_formats_offset_now_as_a_utc_request_timestamp():
    calls = []
    offset_now = datetime(
        2026,
        7,
        12,
        20,
        30,
        tzinfo=timezone(-timedelta(hours=4)),
    )

    def fixture_fetch(path, params):
        calls.append((path, params))
        return []

    _collect(fixture_fetch, ids=_ids("full"), now=offset_now)

    commit_request = next(params for path, params in calls if path.endswith("/commits"))
    assert commit_request["since"] == "2026-07-06T00:30:00Z"


def test_github_count_only_ignores_and_warns_on_out_of_scope_response_weeks():
    commits = [
        _commit("sha-current", "alex-gh", "alex@example.com"),
        _commit(
            "sha-before-window",
            "sam-gh",
            "sam@example.com",
            "2026-06-21T23:59:59Z",
        ),
        _commit(
            "sha-after-window",
            "sam-gh",
            "sam@example.com",
            "2026-07-20T00:00:00Z",
        ),
    ]

    def fixture_fetch(path, params):
        return commits if path.endswith("/commits") and params["page"] == 1 else []

    result = _collect(fixture_fetch, ids=_ids("count-only"))

    assert result.commit_counts == (
        WeeklyCommitCount("project-alpha", "2026-07-13", "alex", 1),
    )
    assert result.warnings == (
        "github count-only response for project-alpha has UTC week 2026-06-15 "
        "outside requested replacement scopes; ignored",
        "github count-only response for project-alpha has UTC week 2026-07-20 "
        "outside requested replacement scopes; ignored",
    )
    assert not any("sha-" in warning for warning in result.warnings)


def test_github_paginates_commits_and_excludes_stale_pull_requests():
    """Dropping page traversal or the PR lookback filter breaks this."""
    commit_page = [dict(COMMIT, sha=f"sha-{number}") for number in range(100)]
    stale_pr = dict(PR, number=6, updated_at="2026-07-07T23:59:59Z")

    def fixture_fetch(path, params):
        if path.endswith("/commits"):
            return commit_page if params["page"] == 1 else [COMMIT] if params["page"] == 2 else []
        if path.endswith("/pulls"):
            return [PR, stale_pr] if params["page"] == 1 else []
        raise AssertionError(path)

    events = _collect(fixture_fetch).events

    assert len([event for event in events if event.kind == "commit"]) == 101
    assert [event.refs for event in events if event.kind == "pr"] == [
        '{"number": 7, "url": "https://github.test/pull/7"}'
    ]


def test_github_stops_sorted_pull_pagination_after_second_page_crosses_lookback():
    """Sorted PR pages stop at the first stale item while retaining the boundary."""
    calls = []
    first_page = [
        dict(PR, number=number, updated_at="2026-07-14T10:00:00Z")
        for number in range(100, 200)
    ]
    second_page = [
        dict(PR, number=200, updated_at="2026-07-09T10:00:00Z"),
        dict(PR, number=201, updated_at="2026-07-08T00:00:00Z"),
        *[
            dict(PR, number=number, updated_at="2026-07-07T23:59:59Z")
            for number in range(202, 300)
        ],
    ]

    def fixture_fetch(path, params):
        if path.endswith("/commits"):
            return []
        calls.append(params)
        if params["page"] == 1:
            return first_page
        if params["page"] == 2:
            return second_page
        raise AssertionError("lookback crossing must prevent a third PR page")

    pull_requests = [
        event for event in _collect(fixture_fetch).events if event.kind == "pr"
    ]

    assert len(pull_requests) == 102
    assert pull_requests[-1].ts == "2026-07-08T00:00:00Z"
    assert calls == [
        {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
            "per_page": 100,
            "page": 1,
        },
        {
            "state": "all",
            "sort": "updated",
            "direction": "desc",
            "per_page": 100,
            "page": 2,
        },
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
        "state": "all",
        "sort": "updated",
        "direction": "desc",
        "per_page": 100,
        "page": 1,
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
