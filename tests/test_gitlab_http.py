import json
import threading
import time
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from teammem.config import Config
from teammem.connectors.config import ConnectorSettings
from teammem.connectors.gitlab import GitLabConnector
from teammem.events import event_hash
from teammem.identity import IdentityMaps


@pytest.fixture
def retry_clock(monkeypatch):
    clock = SimpleNamespace(now=1784073600.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(time, "time", lambda: clock.now)
    return clock


@pytest.fixture
def http_api(tmp_path):
    api = SimpleNamespace(requests=[], reply=lambda request: (200, {}, []))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            request = (url.path, parse_qs(url.query), self.headers.get("PRIVATE-TOKEN"))
            api.requests.append(request)
            status, headers, body = api.reply(request)
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    api.cfg = Config(
        gitlab_url=f"http://127.0.0.1:{server.server_port}",
        gitlab_token="local-test-token",
        gitlab_group="42",
        db_path=tmp_path / "ledger.db",
        config_dir=tmp_path / "config",
        vault_dir=tmp_path / "vault",
    )
    api.fetch = GitLabConnector().http_fetch_json(api.cfg)
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_numeric_retry_after_preserves_request(http_api, retry_clock):
    http_api.reply = lambda request: (
        (429, {"Retry-After": "7"}, {"message": "slow down"})
        if len(http_api.requests) == 1 else (200, {}, [{"id": 9}])
    )

    assert http_api.fetch("/projects", {"page": 2, "search": "two words"}) == [{"id": 9}]
    assert retry_clock.sleeps == [7]
    assert http_api.requests == [
        ("/api/v4/projects", {"page": ["2"], "search": ["two words"]}, "local-test-token")
    ] * 2


def test_http_date_retry_after_is_honored(http_api, retry_clock):
    retry_at = formatdate(retry_clock.now + 45, usegmt=True)
    http_api.reply = lambda request: (
        (429, {"Retry-After": retry_at}, {})
        if len(http_api.requests) == 1 else (200, {}, [])
    )

    assert http_api.fetch("/projects", {}) == []
    assert retry_clock.sleeps == [45]


@pytest.mark.parametrize("header", [None, "invalid", "-1", "NaN", "Infinity"])
def test_missing_or_invalid_retry_after_uses_fallback(http_api, retry_clock, header):
    headers = {} if header is None else {"Retry-After": header}
    http_api.reply = lambda request: (
        (429, headers, {}) if len(http_api.requests) == 1 else (200, {}, [])
    )

    assert http_api.fetch("/projects", {}) == []
    assert retry_clock.sleeps == [30]


def test_past_retry_date_retries_without_delay(http_api, retry_clock):
    retry_at = formatdate(retry_clock.now - 60, usegmt=True)
    http_api.reply = lambda request: (
        (429, {"Retry-After": retry_at}, {})
        if len(http_api.requests) == 1 else (200, {}, [])
    )

    assert http_api.fetch("/projects", {}) == []
    assert sum(retry_clock.sleeps) == 0
    assert len(http_api.requests) == 2


def test_retry_after_over_budget_preserves_http_error(http_api, retry_clock):
    http_api.reply = lambda request: (429, {"Retry-After": "301"}, {"message": "limited"})

    with pytest.raises(requests.HTTPError) as error:
        http_api.fetch("/projects", {})

    assert error.value.response.status_code == 429
    assert error.value.response.json() == {"message": "limited"}
    assert retry_clock.sleeps == []
    assert len(http_api.requests) == 1


def test_retry_after_cannot_exceed_remaining_budget(http_api, retry_clock):
    http_api.reply = lambda request: (429, {"Retry-After": "200"}, {})

    with pytest.raises(requests.HTTPError) as error:
        http_api.fetch("/projects", {})

    assert error.value.response.status_code == 429
    assert retry_clock.sleeps == [200]
    assert len(http_api.requests) == 2


def test_permanent_429_is_limited_to_five_retries(http_api, retry_clock):
    http_api.reply = lambda request: (429, {"Retry-After": "0"}, {"attempt": len(http_api.requests)})

    with pytest.raises(requests.HTTPError) as error:
        http_api.fetch("/projects", {})

    assert len(http_api.requests) == 6
    assert sum(retry_clock.sleeps) == 0
    assert error.value.response.json() == {"attempt": 6}


def test_fallback_waits_double_and_stop_within_budget(http_api, retry_clock):
    http_api.reply = lambda request: (429, {}, {})

    with pytest.raises(requests.HTTPError):
        http_api.fetch("/projects", {})

    assert retry_clock.sleeps == [30, 60, 120]
    assert len(http_api.requests) == 4


@pytest.mark.parametrize("status", [401, 500])
def test_other_http_errors_are_not_retried(http_api, retry_clock, status):
    http_api.reply = lambda request: (status, {"Retry-After": "1"}, {})

    with pytest.raises(requests.HTTPError) as error:
        http_api.fetch("/projects", {})

    assert error.value.response.status_code == status
    assert len(http_api.requests) == 1
    assert retry_clock.sleeps == []


def test_collection_recovers_throttled_page_without_losing_events(http_api, retry_clock):
    def reply(request):
        path, query, _token = request
        if path == "/api/v4/groups/42/projects":
            return 200, {}, [{"id": 1, "path_with_namespace": "team/project"}]
        if path.endswith("/repository/branches"):
            return 200, {}, [{"name": "feature/holiday"}]
        if path.endswith("/repository/commits"):
            page = int(query["page"][0])
            if page == 2 and http_api.requests.count(request) == 1:
                return 429, {"Retry-After": "2"}, {}
            indexes = range(100) if page == 1 else [100]
            return 200, {}, [
                {"id": f"sha-{index}", "author_email": "alex@example.com",
                 "committed_date": "2026-07-14T09:00:00Z", "title": f"Commit {index}"}
                for index in indexes
            ]
        return 200, {}, []

    http_api.reply = reply
    ids = IdentityMaps(
        {"members": {"alex": {"emails": ["alex@example.com"]}}},
        {"projects": {"project": {"gitlab_repos": ["team/project"]}}},
    )
    result = GitLabConnector().collect(
        http_api.cfg, ids, ConnectorSettings(name="gitlab", enabled=True, options={}),
        datetime(2026, 7, 15, tzinfo=timezone.utc),
    )

    assert len(result.events) == 101
    assert {event.hash for event in result.events} == {
        event_hash("commit", "1", f"sha-{index}") for index in range(101)
    }
    assert all(event.person == "alex" and event.project == "project" for event in result.events)
    assert result.warnings == ()
    assert retry_clock.sleeps == [2]
    commit_requests = [request for request in http_api.requests if request[0].endswith("/repository/commits")]
    assert [request[1]["page"] for request in commit_requests] == [["1"], ["2"], ["2"]]
    assert commit_requests[1] == commit_requests[2]
    assert all(request[1]["ref_name"] == ["feature/holiday"] for request in commit_requests)
    assert all(request[2] == "local-test-token" for request in http_api.requests)
