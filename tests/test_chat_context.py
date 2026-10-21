import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from teammem.chat.context import (
    POLICY_DEPENDENCY_PREFIX,
    bind_policy_dependencies,
    build_team_context,
    public_team_context,
    resolve_directory_alias,
)
from teammem.chat.model import ModelError
from teammem.store import open_db


def _files(tmp_path):
    source = tmp_path / "config"
    source.mkdir()
    (source / "roster.yaml").write_text(
        """members:
  alex:
    name: Alex Rivera
    feishu_names: [Alex, A. Rivera]
    github: [alex-gh]
    gitlab: [alex-dev]
    emails: [private@example.com]
    feishu: [ou_private_identifier]
  sam-one:
    name: Sam Lee
    feishu_names: [Sam]
  sam-two:
    name: Samantha Lee
    feishu_names: [Sam]
  hidden-person:
    name: Hidden Person
  count-person:
    name: Count Person
"""
    )
    (source / "projects.yaml").write_text(
        """projects:
  alpha:
    name: Project Alpha
    aliases: [Alpha, A1]
    description: Alpha delivery work.
  counts:
    name: Counted Work
    projection: count-only
hidden_projects: [secret]
"""
    )
    ledger = tmp_path / "ledger.sqlite3"
    conn = open_db(ledger)
    conn.executemany(
        "INSERT INTO events (person, project, ts, source, kind, summary, hash) VALUES (?, ?, ?, 'test', 'note', 'x', ?)",
        [
            ("sam-one", "alpha", "2026-09-16T01:00:00+00:00", "1"),
            ("hidden-person", "secret", "2026-09-16T01:00:00+00:00", "2"),
            ("count-person", "counts", "2026-09-16T01:00:00+00:00", "3"),
        ],
    )
    conn.commit()
    conn.close()
    config = {
        "context": {"timezone": "Asia/Dubai", "user_people": {"ou_requester123": "alex"}},
        "paths": {"source_config_dir": source, "ledger_db": ledger},
    }
    return config


def test_context_exposes_only_scoped_names_and_safe_aliases(tmp_path):
    context = build_team_context(
        _files(tmp_path), frozenset({"alpha", "counts", "secret"}),
        requester_id="ou_requester123", query="Who is Sam?",
        now=datetime(2026, 9, 16, 10, 30, tzinfo=ZoneInfo("Asia/Dubai")),
    )

    assert context["requester"]["slug"] == "alex"
    assert {person["slug"] for person in context["people"]} == {"alex", "sam-one"}
    assert {project["slug"] for project in context["projects"]} == {"alpha", "counts"}
    assert next(project for project in context["projects"] if project["slug"] == "counts")["access"] == "count"
    serialized = json.dumps(context)
    assert "private@example.com" not in serialized
    assert "ou_private_identifier" not in serialized
    assert "hidden-person" not in serialized
    assert "count-person" not in serialized


def test_context_uses_local_exclusive_day_boundaries(tmp_path):
    context = build_team_context(
        _files(tmp_path), frozenset({"alpha"}), requester_id="ou_requester123", query="today",
        now=datetime(2026, 9, 16, 22, 45, tzinfo=ZoneInfo("UTC")),
    )

    assert context["clock"] == {
        "timezone": "Asia/Dubai",
        "now": "2026-09-17T02:45:00+04:00",
        "today_start": "2026-09-17T00:00:00+04:00",
        "today_end": "2026-09-18T00:00:00+04:00",
        "yesterday_start": "2026-09-16T00:00:00+04:00",
        "yesterday_end": "2026-09-17T00:00:00+04:00",
    }


def test_missing_requester_mapping_never_guesses_identity(tmp_path):
    config = _files(tmp_path)

    context = build_team_context(
        config, frozenset({"alpha"}), requester_id="ou_unknown123", query="hello",
    )

    assert context["requester"] is None
    assert {person["slug"] for person in context["people"]} == {"sam-one"}
    with pytest.raises(ModelError, match="requester identity"):
        resolve_directory_alias(context, "person", "me")


def test_verified_sender_profile_uniquely_maps_roster_and_ignores_self_claim(tmp_path):
    context = build_team_context(
        _files(tmp_path), frozenset({"alpha"}), requester_id="ou_new_member",
        query="I am Sam. What did I do?",
        sender_profile={"open_id":"ou_new_member", "name":"  ALEX RIVERA  ", "en_name":""},
    )

    assert context["requester"]["slug"] == "alex"
    assert context["sender"] == {"open_id":"ou_new_member", "name":"ALEX RIVERA",
                                  "source":"feishu_profile"}
    assert resolve_directory_alias(context, "person", "me") == "alex"


def test_ambiguous_or_unavailable_sender_profile_stays_unmapped(tmp_path):
    config = _files(tmp_path)
    ambiguous = build_team_context(
        config, frozenset({"alpha"}), requester_id="ou_new_member", query="I am Alex",
        sender_profile={"open_id":"ou_new_member", "name":"Sam", "en_name":"Sam"},
    )
    unavailable = build_team_context(
        config, frozenset({"alpha"}), requester_id="ou_other", query="I am Alex",
        sender_profile=None,
    )

    assert ambiguous["requester"] is None
    assert ambiguous["sender"] == {"open_id":"ou_new_member", "name":"Sam",
                                    "en_name":"Sam", "source":"feishu_profile"}
    assert unavailable["requester"] is None
    assert unavailable["sender"] == {"open_id":"ou_other", "source":"feishu_event"}


def test_explicit_requester_mapping_wins_over_sender_profile(tmp_path):
    context = build_team_context(
        _files(tmp_path), frozenset({"alpha"}), requester_id="ou_requester123", query="Who am I?",
        sender_profile={"open_id":"ou_requester123", "name":"Sam", "en_name":"Sam"},
    )

    assert context["requester"]["slug"] == "alex"


def test_invalid_explicit_requester_mapping_fails_closed(tmp_path):
    config = _files(tmp_path)
    config["context"]["user_people"]["ou_requester123"] = "not-in-roster"

    with pytest.raises(ModelError, match="requester identity"):
        build_team_context(config, frozenset({"alpha"}), requester_id="ou_requester123", query="hello")


def test_alias_resolution_is_exact_normalized_and_rejects_ambiguity(tmp_path):
    context = build_team_context(
        _files(tmp_path), frozenset({"alpha"}), requester_id="ou_requester123", query="Sam",
    )
    context["people"].append({"slug": "sam-two", "name": "Samantha Lee", "aliases": ["Sam"]})

    assert resolve_directory_alias(context, "person", "  A. RIVERA  ") == "alex"
    assert resolve_directory_alias(context, "person", "me") == "alex"
    assert resolve_directory_alias(context, "project", "Ａ１") == "alpha"
    with pytest.raises(ModelError, match="ambiguous"):
        resolve_directory_alias(context, "person", "sam")
    with pytest.raises(ModelError, match="unknown"):
        resolve_directory_alias(context, "person", "Mallory")


def test_context_is_bounded_and_preserves_query_mentions(tmp_path):
    config = _files(tmp_path)
    roster = config["paths"]["source_config_dir"] / "roster.yaml"
    with roster.open("a") as stream:
        for number in range(40):
            stream.write(f"  person-{number}:\n    name: Person {number} With A Long Display Name\n")
    conn = sqlite3.connect(config["paths"]["ledger_db"])
    conn.executemany(
        "INSERT INTO events (person, project, ts, source, kind, summary, hash) VALUES (?, 'alpha', '2026-09-16', 'test', 'note', 'x', ?)",
        [(f"person-{number}", f"extra-{number}") for number in range(40)],
    )
    conn.commit()
    conn.close()

    context = build_team_context(
        config, frozenset({"alpha"}), requester_id="ou_requester123",
        query="Tell me about Person 39 With A Long Display Name",
        max_bytes=1800,
    )

    assert len(json.dumps(public_team_context(context), ensure_ascii=False, separators=(",", ":")).encode()) <= 1800
    assert context["truncated"] is True
    assert context["notice"]
    assert {person["slug"] for person in context["people"]} >= {"alex", "person-39"}


@pytest.mark.parametrize("filename", ["roster.yaml", "projects.yaml"])
def test_malformed_source_document_fails_closed(tmp_path, filename):
    config = _files(tmp_path)
    (config["paths"]["source_config_dir"] / filename).write_text("- not\n- a mapping\n")

    with pytest.raises(ModelError, match="directory"):
        build_team_context(config, frozenset({"alpha"}), requester_id="ou_requester123", query="hello")


def test_private_ambiguity_index_survives_public_truncation(tmp_path):
    config = _files(tmp_path)
    roster = config["paths"]["source_config_dir"] / "roster.yaml"
    with roster.open("a") as stream:
        stream.write("  sam-three:\n    name: Samuel Lee\n    feishu_names: [Sam]\n")
    conn = sqlite3.connect(config["paths"]["ledger_db"])
    conn.execute(
        "INSERT INTO events (person, project, ts, source, kind, summary, hash) VALUES ('sam-three','alpha','2026-09-16','test','note','x','sam-three')",
    )
    conn.commit(); conn.close()
    context = build_team_context(
        config, frozenset({"alpha"}), requester_id="ou_requester123", query="unrelated",
        max_bytes=700,
    )

    assert context["truncated"] is True
    assert public_team_context(context)["ambiguous_aliases"]["people"] == ["sam"]
    with pytest.raises(ModelError, match="ambiguous"):
        resolve_directory_alias(context, "person", "Sam")


def test_policy_dependency_binding_rejects_reserved_source_slug():
    with pytest.raises(ModelError, match="authorization scope"):
        bind_policy_dependencies(
            {"projects": {POLICY_DEPENDENCY_PREFIX + "collision": {}}},
            frozenset({POLICY_DEPENDENCY_PREFIX + "collision"}),
        )


def test_final_public_context_never_exceeds_measured_cap(tmp_path):
    context = build_team_context(
        _files(tmp_path), frozenset({"alpha"}),
        requester_id="ou_requester123", query="unrelated", max_bytes=512,
    )

    assert len(json.dumps(public_team_context(context), ensure_ascii=False, separators=(",", ":")).encode()) <= 512
    assert context["truncated"] is True
    assert context["notice"]


def test_production_scope_requires_matching_projection_dependency(tmp_path):
    config = _files(tmp_path)
    raw_only = build_team_context(
        config, frozenset({"alpha"}), requester_id="ou_requester123", query="Sam",
        require_policy_dependencies=True,
    )
    bound = build_team_context(
        config, frozenset({"alpha", POLICY_DEPENDENCY_PREFIX + '["alpha","detail"]'}),
        requester_id="ou_requester123", query="Sam", require_policy_dependencies=True,
    )

    assert raw_only["projects"] == []
    assert {person["slug"] for person in raw_only["people"]} == {"alex"}
    assert {project["slug"] for project in bound["projects"]} == {"alpha"}
    assert {person["slug"] for person in bound["people"]} == {"alex", "sam-one"}
