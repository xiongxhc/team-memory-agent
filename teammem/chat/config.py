"""Strict configuration loading for the separately deployed chat service."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ChatConfigError(ValueError):
    """Raised when the chat configuration is incomplete or unsafe."""


@dataclass(frozen=True)
class ChatConfig:
    schema_version: int
    enabled: bool
    feishu: Mapping[str, Any]
    model: Mapping[str, Any]
    session: Mapping[str, Any]
    retrieval: Mapping[str, Any]
    attachments: Mapping[str, Any]
    access: Mapping[str, Any]
    paths: Mapping[str, Path]
    context: Mapping[str, Any] | None = None
    vault: Mapping[str, Any] | None = None


_TOP = frozenset({"schema_version", "enabled", "feishu", "model", "session", "retrieval", "attachments", "access", "paths"})
_SECTIONS = {
    "feishu": frozenset({"app_id", "expected_bot_open_id", "tenant_key", "transport", "event", "direct_messages", "group_mentions_only", "required_scopes"}),
    "model": frozenset({"provider", "name", "reasoning_effort", "max_output_tokens", "max_input_tokens", "max_requests_per_message", "max_retrieval_rounds", "store", "automatic_escalation"}),
    "session": frozenset({"direct_message_scope", "group_scope", "thread_scope", "max_context_turns", "idle_retention_days", "max_concurrent_model_calls", "automatic_team_memory_write"}),
    "retrieval": frozenset({"mode", "max_snippets", "unclassified_evidence", "count_only_projects", "hidden_projects"}),
    "attachments": frozenset({"enabled", "allowed_extensions", "max_file_bytes", "max_files_per_request", "max_request_bytes", "max_uncompressed_bytes_per_file", "max_uncompressed_bytes_per_request", "max_archive_entries_per_file", "max_archive_expansion_ratio", "max_document_pages_or_slides", "max_visual_pages_per_request", "max_decoded_image_pixels", "max_extracted_characters_per_file", "parse_timeout_seconds", "raw_retention_hours", "extracted_retention", "group_intake", "session_scope_only", "automatic_team_memory_write", "execute_active_content", "fetch_external_resources", "encrypted_files", "unsupported_files", "partial_coverage"}),
    "access": frozenset({"default", "users", "groups", "group_admins"}),
    "paths": frozenset({"credentials_env", "chat_db", "ledger_db", "source_config_dir", "attachment_dir"}),
}
_APP_ID = re.compile(r"cli_[A-Za-z0-9]+\Z")
_OPEN_ID = re.compile(r"ou_[A-Za-z0-9]{8,}\Z")


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ChatConfigError(f"{name} must be an object")
    return value


def _keys(value: dict[str, Any], expected: frozenset[str], name: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ChatConfigError(f"{name} has unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ChatConfigError(f"{name} is missing keys: {', '.join(sorted(missing))}")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChatConfigError(f"{name} must be a non-empty string")
    return value


def _integer(value: Any, name: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ChatConfigError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ChatConfigError(f"{name} must be an integer <= {maximum}")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ChatConfigError(f"{name} must be a boolean")
    return value


def _frozen_mapping(value: dict[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(value.copy())


def _validate_feishu(value: dict[str, Any], enabled: bool) -> Mapping[str, Any]:
    _keys(value, _SECTIONS["feishu"], "feishu")
    app_id = _string(value["app_id"], "feishu.app_id")
    if not _APP_ID.fullmatch(app_id):
        raise ChatConfigError("feishu.app_id is malformed")
    for name in ("expected_bot_open_id", "tenant_key"):
        identity = value[name]
        if identity is not None:
            _string(identity, f"feishu.{name}")
    if value["expected_bot_open_id"] is not None and not _OPEN_ID.fullmatch(value["expected_bot_open_id"]):
        raise ChatConfigError("feishu.expected_bot_open_id is malformed")
    if enabled:
        if not isinstance(value["expected_bot_open_id"], str) or not _OPEN_ID.fullmatch(value["expected_bot_open_id"]):
            raise ChatConfigError("enabled config requires a valid feishu.expected_bot_open_id")
        _string(value["tenant_key"], "feishu.tenant_key")
    if value["transport"] != "long_connection" or value["event"] != "im.message.receive_v1":
        raise ChatConfigError("unsupported Feishu transport or event")
    for name in ("direct_messages", "group_mentions_only"):
        _boolean(value[name], f"feishu.{name}")
    if value["group_mentions_only"] is not True:
        raise ChatConfigError("feishu.group_mentions_only must be true")
    scopes = value["required_scopes"]
    if not isinstance(scopes, list) or not scopes or any(not isinstance(scope, str) for scope in scopes):
        raise ChatConfigError("feishu.required_scopes must be a non-empty string list")
    return _frozen_mapping(value)


def _validate_model(value: dict[str, Any]) -> Mapping[str, Any]:
    _keys(value, _SECTIONS["model"], "model")
    if value["provider"] not in {"openai_responses", "codex_cli"}:
        raise ChatConfigError("chat model provider must be openai_responses or codex_cli")
    _string(value["name"], "model.name")
    if value["reasoning_effort"] not in {"low", "medium", "high"} or value["store"] is not False or value["automatic_escalation"] is not False:
        raise ChatConfigError("chat model privacy and reasoning policy is invalid")
    for name, maximum in {"max_output_tokens": 1200, "max_input_tokens": 24000, "max_requests_per_message": 3, "max_retrieval_rounds": 2}.items():
        _integer(value[name], f"model.{name}", maximum=maximum)
    return _frozen_mapping(value)


def _validate_session(value: dict[str, Any]) -> Mapping[str, Any]:
    _keys(value, _SECTIONS["session"], "session")
    if (value["direct_message_scope"], value["group_scope"], value["thread_scope"]) != ("tenant_app_user", "tenant_app_chat", "tenant_app_chat_root"):
        raise ChatConfigError("session scopes must preserve tenant and app isolation")
    for name, maximum in {"max_context_turns": 20, "idle_retention_days": 30, "max_concurrent_model_calls": 2}.items():
        _integer(value[name], f"session.{name}", maximum=maximum)
    if value["automatic_team_memory_write"] is not False:
        raise ChatConfigError("automatic team memory writes are not supported")
    return _frozen_mapping(value)


def _validate_retrieval(value: dict[str, Any]) -> Mapping[str, Any]:
    _keys(value, _SECTIONS["retrieval"], "retrieval")
    if (value["mode"], value["unclassified_evidence"], value["count_only_projects"], value["hidden_projects"]) != ("read_only_ledger", "deny", "aggregates_only", "deny"):
        raise ChatConfigError("retrieval policy must fail closed")
    _integer(value["max_snippets"], "retrieval.max_snippets", maximum=8)
    return _frozen_mapping(value)


def _validate_attachments(value: dict[str, Any]) -> Mapping[str, Any]:
    _keys(value, _SECTIONS["attachments"], "attachments")
    for name in ("enabled", "session_scope_only", "automatic_team_memory_write", "execute_active_content", "fetch_external_resources"):
        _boolean(value[name], f"attachments.{name}")
    if value["session_scope_only"] is not True or value["automatic_team_memory_write"] or value["execute_active_content"] or value["fetch_external_resources"]:
        raise ChatConfigError("attachment processing must remain isolated and inert")
    extensions = value["allowed_extensions"]
    if not isinstance(extensions, list) or not extensions or any(not isinstance(ext, str) or not re.fullmatch(r"\.[a-z0-9]+", ext) for ext in extensions):
        raise ChatConfigError("attachments.allowed_extensions must contain dotted lowercase extensions")
    budgets = {"max_file_bytes": 31_457_280, "max_files_per_request": 5, "max_request_bytes": 62_914_560, "max_uncompressed_bytes_per_file": 268_435_456, "max_uncompressed_bytes_per_request": 536_870_912, "max_archive_entries_per_file": 10_000, "max_archive_expansion_ratio": 100, "max_document_pages_or_slides": 200, "max_visual_pages_per_request": 20, "max_decoded_image_pixels": 20_000_000, "max_extracted_characters_per_file": 2_000_000, "parse_timeout_seconds": 60, "raw_retention_hours": 24}
    for name, maximum in budgets.items():
        _integer(value[name], f"attachments.{name}", maximum=maximum)
    if value["max_request_bytes"] < value["max_file_bytes"] or value["max_uncompressed_bytes_per_request"] < value["max_uncompressed_bytes_per_file"]:
        raise ChatConfigError("attachment request budgets cannot be smaller than file budgets")
    if value["extracted_retention"] != "session" or value["group_intake"] != "explicit_message_reference":
        raise ChatConfigError("attachment retention and group intake must be scoped")
    for name in ("encrypted_files", "unsupported_files"):
        if value[name] != "reject_with_explanation":
            raise ChatConfigError(f"attachments.{name} must reject safely")
    if value["partial_coverage"] != "disclose":
        raise ChatConfigError("attachments.partial_coverage must disclose limitations")
    return _frozen_mapping(value)


def _validate_access(value: dict[str, Any]) -> Mapping[str, Any]:
    _keys(value, _SECTIONS["access"], "access")
    if value["default"] != "deny":
        raise ChatConfigError("access.default must deny")
    for name in ("users", "groups", "group_admins"):
        entries = value[name]
        if not isinstance(entries, dict) or any(not isinstance(identifier, str) or not isinstance(projects, list) or any(not isinstance(project, str) or not project or project.startswith("\x00teammem-policy-v1:") for project in projects) for identifier, projects in entries.items()):
            raise ChatConfigError(f"access.{name} must map IDs to project lists")
    return _frozen_mapping(value)


def _validate_paths(value: dict[str, Any]) -> Mapping[str, Path]:
    value = dict(value)
    runtime_root = value.pop("document_runtime_root", None)
    _keys(value, _SECTIONS["paths"], "paths")
    paths = {name: Path(_string(raw, f"paths.{name}")).expanduser() for name, raw in value.items()}
    if paths["chat_db"].resolve(strict=False) == paths["ledger_db"].resolve(strict=False):
        raise ChatConfigError("paths.chat_db must not overlap paths.ledger_db")
    if runtime_root is not None:
        paths["document_runtime_root"] = Path(_string(runtime_root, "paths.document_runtime_root")).expanduser()
    return MappingProxyType(paths)


def _validate_vault(value: dict[str, Any]) -> Mapping[str, Any]:
    _keys(value, frozenset({"root", "web_url", "ref"}), "vault")
    root = Path(_string(value['root'], 'vault.root')).expanduser()
    if not root.is_absolute():
        raise ChatConfigError('vault.root must be absolute')
    url = _string(value['web_url'], 'vault.web_url').rstrip('/')
    parsed = urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ChatConfigError('vault.web_url must be a repository URL without credentials, query, or fragment')
    ref = _string(value['ref'], 'vault.ref')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', ref) or '..' in ref:
        raise ChatConfigError('vault.ref must be a branch or commit reference')
    return MappingProxyType({'root': root, 'web_url': url, 'ref': ref})


def _validate_context(value: dict[str, Any]) -> Mapping[str, Any]:
    _keys(value, frozenset({"timezone", "user_people"}), "context")
    timezone_name = _string(value["timezone"], "context.timezone")
    if timezone_name != "UTC":
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ChatConfigError("context.timezone is unknown") from exc
    people = _object(value["user_people"], "context.user_people")
    if any(
        not isinstance(identifier, str)
        or not _OPEN_ID.fullmatch(identifier)
        or not isinstance(slug, str)
        or not slug.strip()
        for identifier, slug in people.items()
    ):
        raise ChatConfigError("context.user_people must map app-scoped open IDs to roster slugs")
    return MappingProxyType({
        "timezone": timezone_name,
        "user_people": MappingProxyType(people.copy()),
    })


def load_chat_config(path: Path) -> ChatConfig:
    """Load a complete version-one chat JSON document without reading secrets."""
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ChatConfigError(f"cannot read chat config: {exc}") from exc
    document = _object(document, "chat config")
    unknown = set(document) - (_TOP | {"context", "vault"})
    missing = _TOP - set(document)
    if unknown:
        raise ChatConfigError(f"chat config has unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ChatConfigError(f"chat config is missing keys: {', '.join(sorted(missing))}")
    if document["schema_version"] != 1 or isinstance(document["schema_version"], bool):
        raise ChatConfigError("unsupported chat schema_version")
    enabled = _boolean(document["enabled"], "enabled")
    sections = {name: _object(document[name], name) for name in _SECTIONS}
    retrieval = _validate_retrieval(sections["retrieval"])
    if 'vault' in document and retrieval['max_snippets'] < 2:
        raise ChatConfigError('vault retrieval requires at least two snippets for vault and original evidence')
    return ChatConfig(
        schema_version=1,
        enabled=enabled,
        feishu=_validate_feishu(sections["feishu"], enabled),
        model=_validate_model(sections["model"]),
        session=_validate_session(sections["session"]),
        retrieval=retrieval,
        attachments=_validate_attachments(sections["attachments"]),
        access=_validate_access(sections["access"]),
        paths=_validate_paths(sections["paths"]),
        vault=None if 'vault' not in document else _validate_vault(_object(document['vault'], 'vault')),
        context=(MappingProxyType({"timezone": "UTC", "user_people": MappingProxyType({})})
                 if "context" not in document
                 else _validate_context(_object(document["context"], "context"))),
    )
