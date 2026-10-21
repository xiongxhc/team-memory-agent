import json
from pathlib import Path

import pytest

from teammem.events import Event
from teammem.identity import IdentityMaps
from teammem.reclaim import (
    reclaim,
    reclaim_channel_projects,
    reclaim_repository_projects,
)
from teammem.store import open_db, insert_events, stats

CONFIG_DIR = Path(__file__).parent / "fixtures" / "config"


def _ev(person, h):
    return Event(person=person, ts="2026-07-14T09:00:00Z", source="gitlab",
                 kind="commit", summary="x", hash=h)


def _seed(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        _ev("_unmapped/alex@example.com", "s1"),   # claimable via email
        _ev("_unmapped/alexdev", "s2"),              # claimable via gitlab username
        _ev("_unmapped/ghost@nowhere.com", "s3"),    # stays
        _ev("sam", "s4"),                           # untouched
    ])
    return conn


def test_reclaim_updates_claimable_rows(tmp_path):
    conn = _seed(tmp_path)
    got = reclaim(conn, IdentityMaps.load(CONFIG_DIR))
    assert ("alex@example.com", "alex", 1) in got
    assert ("alexdev", "alex", 1) in got
    s = stats(conn)
    assert s["by_person"]["alex"] == 2
    assert s["unmapped"] == ["_unmapped/ghost@nowhere.com"]


def test_reclaim_dry_run_writes_nothing(tmp_path):
    conn = _seed(tmp_path)
    got = reclaim(conn, IdentityMaps.load(CONFIG_DIR), dry_run=True)
    assert len(got) == 2
    assert len(stats(conn)["unmapped"]) == 3


def test_reclaim_is_idempotent(tmp_path):
    conn = _seed(tmp_path)
    reclaim(conn, IdentityMaps.load(CONFIG_DIR))
    assert reclaim(conn, IdentityMaps.load(CONFIG_DIR)) == []


def test_reclaim_conflict_is_reported_and_untouched(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [_ev("_unmapped/dup", "s1")])
    ids = IdentityMaps({"members": {
        "a": {"emails": ["dup"]},
        "b": {"gitlab": ["dup"]},
    }}, {})
    got = reclaim(conn, ids)
    assert got == [("dup", "!conflict:a|b", 0)]
    assert stats(conn)["unmapped"] == ["_unmapped/dup"]


def test_reclaim_resolves_github_identity(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [_ev("_unmapped/alex-gh", "s1")])

    got = reclaim(conn, IdentityMaps.load(CONFIG_DIR))

    assert got == [("alex-gh", "alex", 1)]
    assert stats(conn)["by_person"]["alex"] == 1


def test_reclaim_collapses_existing_unmapped_and_mapped_event_pair(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        _ev("alex", "same"),
        _ev("_unmapped/alexdev", "same"),
        _ev("_unmapped/alexdev", "claimable"),
        Event(
            person="alex",
            ts="2026-07-14T09:00:00Z",
            source="feishu-channel",
            kind="message",
            summary="same hash, different source",
            hash="claimable",
        ),
    ])

    got = reclaim(conn, IdentityMaps.load(CONFIG_DIR))

    assert got == [("alexdev", "alex", 2)]
    assert conn.execute(
        "SELECT person, source, hash FROM events ORDER BY source, hash"
    ).fetchall() == [
        ("alex", "feishu-channel", "claimable"),
        ("alex", "gitlab", "claimable"),
        ("alex", "gitlab", "same"),
    ]


def test_reclaim_dry_run_counts_duplicate_collapse_without_writing(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        _ev("alex", "same"),
        _ev("_unmapped/alexdev", "same"),
    ])

    got = reclaim(conn, IdentityMaps.load(CONFIG_DIR), dry_run=True)

    assert got == [("alexdev", "alex", 1)]
    assert conn.execute(
        "SELECT person, source, hash FROM events ORDER BY person"
    ).fetchall() == [
        ("_unmapped/alexdev", "gitlab", "same"),
        ("alex", "gitlab", "same"),
    ]


def test_reclaim_channel_projects_corrects_missing_and_stale_attribution(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        Event(person="alex", ts="2026-07-14T09:00:00+00:00", source="feishu-channel",
              kind="message", summary="hi", refs=json.dumps({"chat_id": "oc_new"}),
              hash="m1"),
        Event(person="alex", ts="2026-07-14T09:01:00+00:00", source="feishu-channel",
              kind="message", summary="hi2", project="stale-project",
              refs=json.dumps({"chat_id": "oc_new"}), hash="m2"),
    ])
    ids = IdentityMaps({"members": {}},
                       {"projects": {"project-alpha": {"feishu_channels": ["oc_new"]}}})
    dry = reclaim_channel_projects(conn, ids, dry_run=True)
    assert dry == [("oc_new", "project-alpha", 2)]
    assert conn.execute("SELECT COUNT(*) FROM events WHERE project='project-alpha'").fetchone()[0] == 0
    live = reclaim_channel_projects(conn, ids)
    assert live == [("oc_new", "project-alpha", 2)]
    assert conn.execute("SELECT project FROM events WHERE hash='m1'").fetchone()[0] == "project-alpha"
    assert conn.execute("SELECT project FROM events WHERE hash='m2'").fetchone()[0] == "project-alpha"
    assert reclaim_channel_projects(conn, ids) == []


def test_reclaim_channel_projects_uses_the_event_source_provider_kind(tmp_path):
    import json as _json
    from teammem.reclaim import reclaim_channel_projects

    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        Event(person="alex", ts="2026-07-14T09:00:00+00:00", source="slack-channel",
              kind="message", summary="slack", refs=_json.dumps({"chat_id": "shared"}),
              hash="slack-1"),
        Event(person="alex", ts="2026-07-14T09:01:00+00:00", source="feishu-channel",
              kind="message", summary="feishu", refs=_json.dumps({"chat_id": "shared"}),
              hash="feishu-1"),
    ])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {
            "slack-project": {"slack_channels": ["shared"]},
            "feishu-project": {"feishu_channels": ["shared"]},
        }},
    )

    assert reclaim_channel_projects(conn, ids) == [
        ("shared", "feishu-project", 1),
        ("shared", "slack-project", 1),
    ]
    assert conn.execute("SELECT project FROM events WHERE hash='slack-1'").fetchone()[0] == "slack-project"
    assert conn.execute("SELECT project FROM events WHERE hash='feishu-1'").fetchone()[0] == "feishu-project"


def test_reclaim_channel_projects_matches_uppercase_slack_channel_ids(tmp_path):
    import json as _json
    from teammem.reclaim import reclaim_channel_projects

    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        Event(person="alex", ts="2026-07-14T09:00:00+00:00", source="slack-channel",
              kind="message", summary="slack", refs=_json.dumps({"chat_id": "C0123"}),
              hash="slack-uppercase"),
    ])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {"slack-project": {"slack_channels": ["C0123"]}}},
    )

    assert reclaim_channel_projects(conn, ids) == [("c0123", "slack-project", 1)]
    assert conn.execute("SELECT project FROM events WHERE hash='slack-uppercase'").fetchone()[0] == "slack-project"


def test_reclaim_channel_projects_accepts_current_and_legacy_chat_refs(tmp_path):
    """Manual/current channel_id rows and historical chat_id rows reclaim alike."""
    import json as _json
    from teammem.reclaim import reclaim_channel_projects

    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        Event(
            person="alex",
            ts="2026-07-14T09:00:00+00:00",
            source="slack-channel",
            kind="message",
            summary="manual slack",
            refs=_json.dumps({"channel_id": "C0123"}),
            hash="slack-current",
        ),
        Event(
            person="alex",
            ts="2026-07-14T09:01:00+00:00",
            source="slack-channel",
            kind="message",
            summary="historical slack",
            refs=_json.dumps({"chat_id": "C0123"}),
            hash="slack-legacy",
        ),
        Event(
            person="alex",
            ts="2026-07-14T09:02:00+00:00",
            source="discord-channel",
            kind="message",
            summary="manual discord",
            refs=_json.dumps({"channel_id": "D0456"}),
            hash="discord-current",
        ),
        Event(
            person="alex",
            ts="2026-07-14T09:03:00+00:00",
            source="discord-channel",
            kind="message",
            summary="historical discord",
            refs=_json.dumps({"chat_id": "D0456"}),
            hash="discord-legacy",
        ),
    ])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {
            "slack-project": {"slack_channels": ["C0123"]},
            "discord-project": {"discord_channels": ["D0456"]},
        }},
    )

    assert reclaim_channel_projects(conn, ids) == [
        ("d0456", "discord-project", 2),
        ("c0123", "slack-project", 2),
    ]
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE project IS NOT NULL"
    ).fetchone()[0] == 4


def _forge_event(source, kind, event_hash, url, project=None, refs=None):
    return Event(
        person="alex",
        project=project,
        ts="2026-07-14T09:00:00+00:00",
        source=source,
        kind=kind,
        summary=f"summary {event_hash}",
        refs=refs if refs is not None else json.dumps({"url": url}),
        raw=json.dumps({"payload": event_hash}),
        hash=event_hash,
    )


def test_reclaim_repository_projects_parses_gitlab_commit_mr_repo_and_encoded_urls(
    tmp_path,
):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        _forge_event(
            "gitlab", "commit", "gl-commit",
            "https://gitlab.example/team/project-alpha/-/commit/abc",
        ),
        _forge_event(
            "gitlab", "mr", "gl-mr",
            "https://gitlab.example/team/project-alpha/-/merge_requests/7",
            project="stale-project",
        ),
        _forge_event(
            "gitlab", "repo", "gl-repo",
            "https://gitlab.example/team/project-alpha/",
        ),
        _forge_event(
            "gitlab", "commit", "gl-encoded",
            "https://gitlab.example/space%20team/project%20alpha/-/commit/def",
        ),
    ])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {
            "project-alpha": {"gitlab_repos": ["team/project-alpha"]},
            "encoded-project": {
                "gitlab_repos": ["space team/project alpha"]
            },
        }},
    )
    before = conn.execute(
        "SELECT id, person, ts, source, kind, summary, refs, raw, hash "
        "FROM events ORDER BY id"
    ).fetchall()

    assert reclaim_repository_projects(
        conn, ids, gitlab_url="https://GITLAB.EXAMPLE:443"
    ) == [
        ("space team/project alpha", "encoded-project", 1),
        ("team/project-alpha", "project-alpha", 3),
    ]
    assert conn.execute(
        "SELECT id, person, ts, source, kind, summary, refs, raw, hash "
        "FROM events ORDER BY id"
    ).fetchall() == before
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 4
    assert conn.execute(
        "SELECT project FROM events ORDER BY hash"
    ).fetchall() == [
        ("project-alpha",),
        ("encoded-project",),
        ("project-alpha",),
        ("project-alpha",),
    ]


def test_reclaim_repository_projects_parses_github_urls_and_decoded_segments(
    tmp_path,
):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        _forge_event(
            "github", "commit", "gh-commit",
            "https://github.com/team/project-alpha/commit/abc",
        ),
        _forge_event(
            "github", "pr", "gh-pr",
            "https://github.com/team/project-alpha/pull/7",
            project="stale-project",
        ),
        _forge_event(
            "github", "commit", "gh-encoded",
            "https://github.com/space%20team/project%20alpha/commit/def",
        ),
    ])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {
            "project-alpha": {"github_repos": ["team/project-alpha"]},
            "encoded-project": {
                "github_repos": ["space team/project alpha"]
            },
        }},
    )

    assert reclaim_repository_projects(conn, ids) == [
        ("space team/project alpha", "encoded-project", 1),
        ("team/project-alpha", "project-alpha", 2),
    ]
    assert conn.execute(
        "SELECT project FROM events ORDER BY hash"
    ).fetchall() == [
        ("project-alpha",),
        ("encoded-project",),
        ("project-alpha",),
    ]


def test_reclaim_repository_projects_is_dry_run_safe_and_idempotent(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        _forge_event(
            "gitlab", "commit", "missing",
            "https://gitlab.example/team/project-alpha/-/commit/abc",
        ),
        _forge_event(
            "gitlab", "mr", "stale",
            "https://gitlab.example/team/project-alpha/-/merge_requests/7",
            project="stale-project",
        ),
    ])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {
            "project-alpha": {"gitlab_repos": ["team/project-alpha"]}
        }},
    )

    expected = [("team/project-alpha", "project-alpha", 2)]
    assert reclaim_repository_projects(
        conn,
        ids,
        dry_run=True,
        gitlab_url="https://gitlab.example",
    ) == expected
    assert conn.execute(
        "SELECT project FROM events ORDER BY hash"
    ).fetchall() == [(None,), ("stale-project",)]
    assert reclaim_repository_projects(
        conn, ids, gitlab_url="https://gitlab.example"
    ) == expected
    assert reclaim_repository_projects(
        conn, ids, gitlab_url="https://gitlab.example"
    ) == []


def test_reclaim_repository_projects_ignores_invalid_unmapped_and_loose_matches(
    tmp_path,
):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        _forge_event("gitlab", "commit", "malformed-json", None, refs="{"),
        _forge_event("gitlab", "commit", "missing-url", None, refs="{}"),
        _forge_event(
            "gitlab", "commit", "non-string-url", None,
            refs=json.dumps({"url": 42}),
        ),
        _forge_event(
            "gitlab", "commit", "relative-url",
            "/team/foo/-/commit/abc",
        ),
        _forge_event(
            "github", "commit", "unrelated-host",
            "https://gitlab.example/team/foo/commit/abc",
        ),
        _forge_event(
            "gitlab", "commit", "unmapped",
            "https://gitlab.example/team/not-mapped/-/commit/abc",
            project="keep-me",
        ),
        _forge_event(
            "gitlab", "commit", "loose-substring",
            "https://gitlab.example/team/foo-bar/-/commit/abc",
        ),
        _forge_event(
            "slack-channel", "message", "not-forge",
            "https://gitlab.example/team/foo/-/commit/abc",
        ),
    ])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {"foo": {"gitlab_repos": ["team/foo"],
                                "github_repos": ["team/foo"]}}},
    )

    assert reclaim_repository_projects(
        conn, ids, gitlab_url="https://gitlab.example"
    ) == []
    assert dict(conn.execute("SELECT hash, project FROM events")) == {
        "loose-substring": None,
        "malformed-json": None,
        "missing-url": None,
        "non-string-url": None,
        "not-forge": None,
        "relative-url": None,
        "unmapped": "keep-me",
        "unrelated-host": None,
    }


def test_reclaim_repository_projects_skips_gitlab_without_authoritative_origin(
    tmp_path,
):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [_forge_event(
        "gitlab", "commit", "gitlab-without-origin",
        "https://gitlab.example/team/project-alpha/-/commit/abc",
    )])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {
            "project-alpha": {"gitlab_repos": ["team/project-alpha"]}
        }},
    )

    assert reclaim_repository_projects(conn, ids) == []
    assert conn.execute(
        "SELECT project FROM events WHERE hash = 'gitlab-without-origin'"
    ).fetchone()[0] is None


def test_reclaim_repository_projects_trusts_current_and_historical_origins_only(
    tmp_path,
):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [
        _forge_event(
            "gitlab", "commit", "current-origin",
            "https://gitlab-current.example/team/project-alpha/-/commit/current",
        ),
        _forge_event(
            "gitlab", "commit", "historical-origin",
            "https://GITLAB-HISTORY.EXAMPLE:443/team/project-alpha/-/commit/old",
        ),
        _forge_event(
            "gitlab", "commit", "unrelated-origin",
            "https://unrelated.example/team/project-alpha/-/commit/unrelated",
            project="keep-me",
        ),
    ])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {
            "project-alpha": {"gitlab_repos": ["team/project-alpha"]}
        }},
    )

    assert reclaim_repository_projects(
        conn,
        ids,
        gitlab_url="https://gitlab-current.example",
        reclaim_origins=["https://gitlab-history.example"],
    ) == [("team/project-alpha", "project-alpha", 2)]
    assert dict(conn.execute("SELECT hash, project FROM events")) == {
        "current-origin": "project-alpha",
        "historical-origin": "project-alpha",
        "unrelated-origin": "keep-me",
    }
    assert reclaim_repository_projects(
        conn,
        ids,
        gitlab_url="https://gitlab-current.example",
        reclaim_origins=["https://gitlab-history.example"],
    ) == []


@pytest.mark.parametrize(
    "reclaim_origins",
    [
        ["https://gitlab-history.example:invalid"],
        ["https://gitlab-history.example/group"],
        ["https://gitlab-history.example", "not-an-origin"],
    ],
)
def test_reclaim_repository_projects_rejects_malformed_historical_origins_before_mutation(
    tmp_path, reclaim_origins
):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [_forge_event(
        "gitlab", "commit", "historical-origin",
        "https://gitlab-history.example/team/project-alpha/-/commit/old",
        project="stale-project",
    )])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {
            "project-alpha": {"gitlab_repos": ["team/project-alpha"]}
        }},
    )
    before = conn.execute("SELECT * FROM events ORDER BY id").fetchall()

    with pytest.raises(ValueError, match="invalid GitLab reclaim origin"):
        reclaim_repository_projects(
            conn,
            ids,
            gitlab_url="https://gitlab-current.example",
            reclaim_origins=reclaim_origins,
        )

    assert conn.execute("SELECT * FROM events ORDER BY id").fetchall() == before


def test_reclaim_repository_projects_preserves_three_argument_dry_run(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [_forge_event(
        "github", "commit", "three-argument-call",
        "https://github.com/team/project-alpha/commit/abc",
    )])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {
            "project-alpha": {"github_repos": ["team/project-alpha"]}
        }},
    )

    assert reclaim_repository_projects(conn, ids, True) == [
        ("team/project-alpha", "project-alpha", 1)
    ]
    assert conn.execute(
        "SELECT project FROM events WHERE hash = 'three-argument-call'"
    ).fetchone()[0] is None


@pytest.mark.parametrize(
    "url",
    [
        "\x00https://gitlab.example/team/project-alpha/-/commit/abc",
        "https://gitlab.example:/team/project-alpha/-/commit/abc",
        "https://gitlab.example/team/project-alpha/-/commit/abc?",
        "https://gitlab.example/team/project-alpha/-/commit/abc#",
    ],
)
def test_reclaim_repository_projects_rejects_malformed_event_origins_before_mutation(
    tmp_path, url
):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [_forge_event(
        "gitlab",
        "commit",
        "malformed-event-origin",
        url,
        project="stale-project",
    )])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {
            "project-alpha": {"gitlab_repos": ["team/project-alpha"]}
        }},
    )
    before = conn.execute("SELECT * FROM events ORDER BY id").fetchall()

    assert reclaim_repository_projects(
        conn, ids, gitlab_url="https://gitlab.example"
    ) == []
    assert conn.execute("SELECT * FROM events ORDER BY id").fetchall() == before


@pytest.mark.parametrize(
    "url",
    [
        "https://unrelated.example/team/project-alpha/-/commit/abc",
        "http://gitlab.example/team/project-alpha/-/commit/abc",
        "https://gitlab.example:444/team/project-alpha/-/commit/abc",
    ],
)
def test_reclaim_repository_projects_requires_exact_gitlab_origin(tmp_path, url):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [_forge_event(
        "gitlab", "commit", "wrong-origin", url,
    )])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {
            "project-alpha": {"gitlab_repos": ["team/project-alpha"]}
        }},
    )

    assert reclaim_repository_projects(
        conn, ids, gitlab_url="https://gitlab.example"
    ) == []
    assert conn.execute(
        "SELECT project FROM events WHERE hash = 'wrong-origin'"
    ).fetchone()[0] is None


@pytest.mark.parametrize(
    ("source", "url", "gitlab_url"),
    [
        (
            "github",
            "https://github.com:not-a-port/team/project-alpha/commit/abc",
            None,
        ),
        (
            "gitlab",
            "https://gitlab.example:not-a-port/team/project-alpha/-/commit/abc",
            "https://gitlab.example",
        ),
        (
            "gitlab",
            "https://gitlab.example/team/project-alpha/-/commit/abc",
            "https://gitlab.example:not-a-port",
        ),
    ],
)
def test_reclaim_repository_projects_ignores_malformed_ports(
    tmp_path, source, url, gitlab_url
):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [_forge_event(
        source, "commit", "malformed-port", url,
    )])
    ids = IdentityMaps(
        {"members": {}},
        {"projects": {
            "project-alpha": {
                "github_repos": ["team/project-alpha"],
                "gitlab_repos": ["team/project-alpha"],
            }
        }},
    )

    assert reclaim_repository_projects(
        conn, ids, gitlab_url=gitlab_url
    ) == []
    assert conn.execute(
        "SELECT project FROM events WHERE hash = 'malformed-port'"
    ).fetchone()[0] is None
