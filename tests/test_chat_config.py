import json
from pathlib import Path

import pytest

from teammem.chat.config import ChatConfigError, load_chat_config


FIXTURE = {
    "schema_version": 1,
    "enabled": False,
    "feishu": {
        "app_id": "cli_exampleapp123",
        "expected_bot_open_id": None,
        "tenant_key": None,
        "transport": "long_connection",
        "event": "im.message.receive_v1",
        "direct_messages": True,
        "group_mentions_only": True,
        "required_scopes": ["im:message.group_at_msg:readonly"],
    },
    "model": {"provider": "openai_responses", "name": "gpt-5.6-luna", "reasoning_effort": "low", "max_output_tokens": 1200, "max_input_tokens": 12000, "max_requests_per_message": 3, "max_retrieval_rounds": 2, "store": False, "automatic_escalation": False},
    "session": {"direct_message_scope": "tenant_app_user", "group_scope": "tenant_app_chat", "thread_scope": "tenant_app_chat_root", "max_context_turns": 20, "idle_retention_days": 30, "max_concurrent_model_calls": 2, "automatic_team_memory_write": False},
    "retrieval": {"mode": "read_only_ledger", "max_snippets": 8, "unclassified_evidence": "deny", "count_only_projects": "aggregates_only", "hidden_projects": "deny"},
    "attachments": {"enabled": True, "allowed_extensions": [".pdf", ".txt"], "max_file_bytes": 31457280, "max_files_per_request": 5, "max_request_bytes": 62914560, "max_uncompressed_bytes_per_file": 268435456, "max_uncompressed_bytes_per_request": 536870912, "max_archive_entries_per_file": 10000, "max_archive_expansion_ratio": 100, "max_document_pages_or_slides": 200, "max_visual_pages_per_request": 20, "max_decoded_image_pixels": 20000000, "max_extracted_characters_per_file": 2000000, "parse_timeout_seconds": 60, "raw_retention_hours": 24, "extracted_retention": "session", "group_intake": "explicit_message_reference", "session_scope_only": True, "automatic_team_memory_write": False, "execute_active_content": False, "fetch_external_resources": False, "encrypted_files": "reject_with_explanation", "unsupported_files": "reject_with_explanation", "partial_coverage": "disclose"},
    "access": {"default": "deny", "users": {}, "groups": {}, "group_admins": {}},
    "paths": {"credentials_env": "/var/lib/teammem/chat.env", "chat_db": "/var/lib/teammem/chat.sqlite3", "ledger_db": "/var/lib/teammem/ledger.sqlite3", "source_config_dir": "/etc/teammem", "attachment_dir": "/var/lib/teammem/attachments"},
}


def _write(tmp_path, change=None):
    document = json.loads(json.dumps(FIXTURE))
    if change:
        change(document)
    path = tmp_path / "chat.json"
    path.write_text(json.dumps(document))
    return path


def test_loads_complete_disabled_luna_config(tmp_path):
    config = load_chat_config(_write(tmp_path))

    assert config.enabled is False
    assert config.feishu["app_id"] == "cli_exampleapp123"
    assert config.model["name"] == "gpt-5.6-luna"
    assert config.model["store"] is False
    assert config.attachments["max_file_bytes"] == 31_457_280
    assert config.paths["chat_db"].name == "chat.sqlite3"
    assert config.context == {"timezone": "UTC", "user_people": {}}


def test_loads_optional_directory_context(tmp_path):
    config = load_chat_config(_write(
        tmp_path,
        lambda doc: doc.update(context={
            "timezone": "Asia/Dubai",
            "user_people": {"ou_exampleuser123": "alex"},
        }),
    ))

    assert config.context == {
        "timezone": "Asia/Dubai",
        "user_people": {"ou_exampleuser123": "alex"},
    }


def test_loads_optional_local_vault(tmp_path):
    config = load_chat_config(_write(tmp_path, lambda doc: doc.update(vault={
        'root':str(tmp_path/'vault'), 'web_url':'https://git.example/team/vault/', 'ref':'master'})))
    assert config.vault == {'root':tmp_path/'vault', 'web_url':'https://git.example/team/vault', 'ref':'master'}


@pytest.mark.parametrize('field,value', [
    ('root','relative/vault'), ('web_url','file:///etc'),
    ('web_url','https://user:secret@git.example/vault'),
    ('web_url','https://git.example/vault?token=secret'), ('ref','../secret'),
])
def test_rejects_unsafe_vault_configuration(tmp_path, field, value):
    vault={'root':str(tmp_path/'vault'),'web_url':'https://git.example/team/vault','ref':'master'}
    vault[field]=value
    with pytest.raises(ChatConfigError):
        load_chat_config(_write(tmp_path, lambda doc: doc.update(vault=vault)))


@pytest.mark.parametrize("context", [
    {"timezone": "Mars/Olympus", "user_people": {}},
    {"timezone": "UTC", "user_people": {"display name": "alex"}},
    {"timezone": "UTC", "user_people": {"ou_exampleuser123": ""}},
    {"timezone": "UTC", "user_people": {}, "guess_from_name": True},
])
def test_rejects_unsafe_directory_context(tmp_path, context):
    with pytest.raises(ChatConfigError):
        load_chat_config(_write(tmp_path, lambda doc: doc.update(context=context)))


@pytest.mark.parametrize(
    "change",
    [
        lambda doc: doc.__setitem__("unexpected", True),
        lambda doc: doc["model"].__setitem__("temperature", 0),
        lambda doc: doc["attachments"].__setitem__("unbounded", True),
    ],
)
def test_rejects_unknown_keys_at_every_config_level(tmp_path, change):
    with pytest.raises(ChatConfigError, match="unknown"):
        load_chat_config(_write(tmp_path, change))


@pytest.mark.parametrize(
    "change",
    [
        lambda doc: doc.update(enabled=True),
        lambda doc: doc["feishu"].update(expected_bot_open_id="not an open id", tenant_key="tenant"),
        lambda doc: doc["feishu"].update(expected_bot_open_id="ou_bot", tenant_key=None),
        lambda doc: doc["feishu"].update(app_id="collector-app"),
    ],
)
def test_enabled_config_requires_valid_distinct_bot_identity(tmp_path, change):
    with pytest.raises(ChatConfigError):
        load_chat_config(_write(tmp_path, change))


@pytest.mark.parametrize(
    "change",
    [
        lambda doc: doc["model"].__setitem__("max_output_tokens", 0),
        lambda doc: doc["session"].__setitem__("idle_retention_days", -1),
        lambda doc: doc["attachments"].__setitem__("max_request_bytes", 1),
        lambda doc: doc["attachments"].__setitem__("allowed_extensions", ["pdf"]),
    ],
)
def test_rejects_invalid_budgets_and_attachment_limits(tmp_path, change):
    with pytest.raises(ChatConfigError):
        load_chat_config(_write(tmp_path, change))


def test_public_engine_accepts_private_model_selection_with_supported_effort(tmp_path):
    config = load_chat_config(_write(
        tmp_path,
        lambda doc: doc["model"].update(name="gpt-private-selection", reasoning_effort="high"),
    ))

    assert config.model["name"] == "gpt-private-selection"
    assert config.model["reasoning_effort"] == "high"


def test_accepts_codex_cli_provider_without_changing_model_limits(tmp_path):
    config = load_chat_config(_write(
        tmp_path,
        lambda doc: doc["model"].__setitem__("provider", "codex_cli"),
    ))

    assert config.model["provider"] == "codex_cli"


def test_accepts_24000_input_tokens_but_rejects_more(tmp_path):
    config = load_chat_config(_write(
        tmp_path,
        lambda doc: doc["model"].update(max_input_tokens=24_000),
    ))
    assert config.model["max_input_tokens"] == 24_000

    with pytest.raises(ChatConfigError):
        load_chat_config(_write(
            tmp_path,
            lambda doc: doc["model"].update(max_input_tokens=24_001),
        ))


@pytest.mark.parametrize(
    "change",
    [
        lambda doc: doc["feishu"].__setitem__("group_mentions_only", False),
        lambda doc: doc["attachments"].__setitem__("session_scope_only", False),
        lambda doc: doc["model"].__setitem__("max_requests_per_message", 4),
        lambda doc: doc["model"].__setitem__("max_retrieval_rounds", 3),
        lambda doc: doc["session"].__setitem__("max_concurrent_model_calls", 3),
        lambda doc: doc["attachments"].__setitem__("max_file_bytes", 31_457_281),
    ],
)
def test_rejects_config_that_exceeds_chat_safety_caps(tmp_path, change):
    with pytest.raises(ChatConfigError):
        load_chat_config(_write(tmp_path, change))


def test_rejects_chat_database_that_overlaps_ledger(tmp_path):
    ledger = tmp_path / "ledger.sqlite3"

    with pytest.raises(ChatConfigError, match="chat_db"):
        load_chat_config(_write(
            tmp_path,
            lambda doc: doc["paths"].update(chat_db=str(ledger), ledger_db=str(ledger)),
        ))


def test_accepts_explicit_group_administrators_separate_from_project_grants(tmp_path):
    config = load_chat_config(_write(
        tmp_path,
        lambda doc: doc["access"].update(group_admins={"group-chat": ["new-app-user"]}),
    ))

    assert config.access["group_admins"] == {"group-chat": ["new-app-user"]}


def test_rejects_reserved_policy_dependency_as_project_grant(tmp_path):
    with pytest.raises(ChatConfigError, match="project lists"):
        load_chat_config(_write(
            tmp_path,
            lambda doc: doc["access"].update(users={
                "ou_exampleuser123": ["\x00teammem-policy-v1:collision"],
            }),
        ))


def test_vault_requires_room_for_original_evidence(tmp_path):
    def change(doc):
        doc['vault'] = {'root':str(tmp_path/'vault'),'web_url':'https://git.example/team/vault','ref':'main'}
        doc['retrieval']['max_snippets'] = 1
    with pytest.raises(ChatConfigError, match='at least two snippets'):
        load_chat_config(_write(tmp_path, change))
