import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from teammem.config import Config
from teammem.connectors.config import ConnectorSettings
from teammem.connectors.gitlab import GitLabConnector
from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.services import collect_connector
from teammem.store import insert_events, open_db


CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"
NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)
OPENED_HASH = "907928208df1c752749c3bea14cd056955dde724d7df620eaf597e7ac9c4beed"
CLOSED_HASH = "789bb2e605c8237773c1eb4c159aa90a6ad895365f1b86de054cb1cc1a23ccf8"
REPO_HASH = "b3d2e000d5a70c05dca0e5d74ec9745d4dc6a399b38078c36cd4956075cba31a"
PROJECT = {
    "id": 1,
    "path_with_namespace": "team/project-alpha",
}


def _cfg(tmp_path):
    return Config.load(env={
        "TEAMMEM_DB": str(tmp_path / "ledger.db"),
        "TEAMMEM_CONFIG_DIR": str(CONFIG_DIR),
        "TEAMMEM_GITLAB_GROUP": "42",
    })


def _fetch(project, *, issues=(), users=None):
    users = users or {}

    def fetch(path, params):
        if path == "/groups/42/projects":
            return [project] if params["page"] == 1 else []
        if path == "/projects/1/issues":
            return list(issues) if params["page"] == 1 else []
        if path.startswith("/users/"):
            return users[path]
        return []

    return fetch


def _collect(cfg, conn, fetch):
    return collect_connector(
        "gitlab",
        cfg,
        IdentityMaps.load(CONFIG_DIR),
        ConnectorSettings("gitlab", True, {}),
        NOW,
        connector=GitLabConnector(fetch_json=fetch),
        conn=conn,
        emit=False,
    )


def _rows(conn):
    return conn.execute(
        "SELECT person, project, ts, source, kind, summary, refs, raw, hash "
        "FROM events ORDER BY id"
    ).fetchall()


def _base_issue_event(issue):
    """Return the row shape written by GitLab collection at base 6f37d12."""
    worker = (
        (issue.get("assignee") or {}) if issue["state"] == "closed" else {}
    ) or issue.get("author") or {}
    username = worker.get("username", "")
    person = IdentityMaps.load(CONFIG_DIR).person("gitlab", username)
    state_hash = CLOSED_HASH if issue["state"] == "closed" else OPENED_HASH
    return Event(
        person=person,
        project="project-alpha",
        ts=issue.get("closed_at") or issue["updated_at"],
        source="gitlab",
        kind="issue",
        summary=f"[{issue['state']}] {issue['title']}",
        refs=json.dumps({"iid": issue["iid"], "url": issue.get("web_url")}),
        raw=json.dumps(issue),
        hash=state_hash,
    )


def test_closed_issue_upgrade_replaces_prior_assignee_row(tmp_path):
    issue = {
        "iid": 31,
        "state": "closed",
        "title": "Login rate limit",
        "author": {"username": "alexdev"},
        "assignee": {"username": "samdev"},
        "closed_by": {"username": "alexdev"},
        "created_at": "2026-01-01T08:00:00Z",
        "updated_at": "2026-07-14T15:00:00Z",
        "closed_at": "2026-07-14T15:00:00Z",
        "web_url": "https://gitlab.internal/team/project-alpha/-/issues/31",
    }
    cfg = _cfg(tmp_path)
    conn = open_db(cfg.db_path)
    insert_events(conn, [_base_issue_event(issue)])

    result = _collect(cfg, conn, _fetch(PROJECT, issues=[issue]))

    assert result.inserted == 0
    assert _rows(conn) == [(
        "alex",
        "project-alpha",
        "2026-07-14T15:00:00Z",
        "gitlab",
        "issue",
        "[closed] Login rate limit",
        json.dumps({"iid": 31, "url": issue["web_url"]}),
        json.dumps(issue),
        CLOSED_HASH,
    )]


def test_repo_upgrade_replaces_base_unmapped_creator_row(tmp_path):
    project = {
        **PROJECT,
        "created_at": "2026-07-14T08:00:00Z",
        "creator_id": 5,
        "web_url": "https://gitlab.internal/team/project-alpha",
    }
    cfg = _cfg(tmp_path)
    conn = open_db(cfg.db_path)
    insert_events(conn, [Event(
        person="_unmapped/(none)",
        project="project-alpha",
        ts=project["created_at"],
        source="gitlab",
        kind="repo",
        summary="[created] team/project-alpha",
        refs=json.dumps({"id": 1, "url": project["web_url"]}),
        raw=json.dumps(project),
        hash=REPO_HASH,
    )])

    result = _collect(
        cfg,
        conn,
        _fetch(project, users={"/users/5": {"username": "alexdev"}}),
    )

    assert result.inserted == 0
    assert _rows(conn) == [(
        "alex",
        "project-alpha",
        "2026-07-14T08:00:00Z",
        "gitlab",
        "repo",
        "[created] team/project-alpha",
        json.dumps({"id": 1, "url": project["web_url"]}),
        json.dumps(project),
        REPO_HASH,
    )]


def test_open_issue_upgrade_repairs_base_updated_at_timestamp(tmp_path):
    issue = {
        "iid": 31,
        "state": "opened",
        "title": "Login rate limit",
        "author": {"username": "alexdev"},
        "assignee": None,
        "created_at": "2026-01-01T08:00:00Z",
        "updated_at": "2026-07-14T11:00:00Z",
        "closed_at": None,
        "web_url": "https://gitlab.internal/team/project-alpha/-/issues/31",
    }
    cfg = _cfg(tmp_path)
    conn = open_db(cfg.db_path)
    insert_events(conn, [_base_issue_event(issue)])

    result = _collect(cfg, conn, _fetch(PROJECT, issues=[issue]))

    assert result.fetched == 0
    assert result.inserted == 0
    assert _rows(conn) == [(
        "alex",
        "project-alpha",
        "2026-01-01T08:00:00Z",
        "gitlab",
        "issue",
        "[opened] Login rate limit",
        json.dumps({"iid": 31, "url": issue["web_url"]}),
        json.dumps(issue),
        OPENED_HASH,
    )]


def test_out_of_lookback_open_issue_does_not_backfill_fresh_ledger(tmp_path):
    issue = {
        "iid": 31,
        "state": "opened",
        "title": "Login rate limit",
        "author": {"username": "alexdev"},
        "assignee": None,
        "created_at": "2026-01-01T08:00:00Z",
        "updated_at": "2026-07-14T11:00:00Z",
        "closed_at": None,
        "web_url": "https://gitlab.internal/team/project-alpha/-/issues/31",
    }
    cfg = _cfg(tmp_path)
    conn = open_db(cfg.db_path)

    result = _collect(cfg, conn, _fetch(PROJECT, issues=[issue]))

    assert result.fetched == 0
    assert result.inserted == 0
    assert _rows(conn) == []


def test_gitlab_reconciliation_rolls_back_replacement_on_insert_failure(tmp_path):
    issue = {
        "iid": 31,
        "state": "closed",
        "title": "Login rate limit",
        "author": {"username": "alexdev"},
        "assignee": {"username": "samdev"},
        "closed_by": {"username": "alexdev"},
        "created_at": "2026-01-01T08:00:00Z",
        "updated_at": "2026-07-14T15:00:00Z",
        "closed_at": "2026-07-14T15:00:00Z",
        "web_url": "https://gitlab.internal/team/project-alpha/-/issues/31",
    }
    cfg = _cfg(tmp_path)
    conn = open_db(cfg.db_path)
    legacy = _base_issue_event(issue)
    insert_events(conn, [legacy])
    conn.execute(
        "CREATE TRIGGER fail_authoritative_gitlab_row "
        "BEFORE INSERT ON events WHEN NEW.person = 'alex' "
        "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
    )

    with pytest.raises(Exception, match="forced failure"):
        _collect(cfg, conn, _fetch(PROJECT, issues=[issue]))

    assert _rows(conn) == [(
        legacy.person,
        legacy.project,
        legacy.ts,
        legacy.source,
        legacy.kind,
        legacy.summary,
        legacy.refs,
        legacy.raw,
        legacy.hash,
    )]
