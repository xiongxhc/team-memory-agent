"""Deterministic grants for the TeamMem chat boundary."""

from collections.abc import Mapping
from typing import Any

from teammem.identity import IdentityMaps

from .state import SessionKey


class AccessDenied(PermissionError):
    """The sender or conversation has no enabled chat grant."""


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _projects(projects: Any) -> frozenset[str] | None:
    if not isinstance(projects, (list, tuple, set, frozenset)):
        return None
    if any(not isinstance(project, str) or not project for project in projects):
        return None
    return frozenset(projects)


def _enabled_user(users: Any, sender: str) -> frozenset[str]:
    if not isinstance(users, Mapping):
        raise AccessDenied("unknown chat identity")
    if sender not in users:
        raise AccessDenied("unknown chat identity")
    projects = _projects(users[sender])
    if projects is None:
        raise AccessDenied("invalid chat grant")
    return projects


def authorize(config: Mapping[str, Any] | Any, key: SessionKey, sender: str) -> frozenset[str]:
    """Return the evidence scope for an explicitly listed user and chat.

    User keys are the new bot application's scoped IDs.  Group answers are only
    permitted when both the user and that group have a grant, so a private DM
    grant can never widen a shared conversation.
    """
    access = _field(config, "access")
    if _field(access, "default") != "deny" or not isinstance(sender, str) or not sender:
        raise AccessDenied("chat access denied")
    users = _field(access, "users")
    user_projects = _enabled_user(users, sender)

    if key.kind == "dm":
        if _field(_field(config, "feishu", {}), "direct_messages", True) is not True:
            raise AccessDenied("direct messages are disabled")
        if key.owner != sender:
            raise AccessDenied("DM sender does not own session")
        return user_projects
    if key.kind not in {"group", "thread"}:
        raise AccessDenied("unknown chat session")

    groups = _field(access, "groups")
    if not isinstance(groups, Mapping):
        raise AccessDenied("unknown group")
    if key.owner not in groups:
        raise AccessDenied("unknown group")
    group_projects = _projects(groups[key.owner])
    if group_projects is None:
        raise AccessDenied("invalid group grant")
    return user_projects & group_projects


def project_policy_from_source_config(source_config: Mapping[str, Any]) -> dict[str, str]:
    """Translate validated operator projection settings into retrieval policy.

    This accepts only the source `projects.yaml` object; it never inspects ledger
    rows, whose project text is untrusted for authorization and publication policy.
    """
    if not isinstance(source_config, Mapping):
        raise ValueError("source project configuration must be a mapping")
    document = dict(source_config)
    # Keep the existing source-config validation and collision rules authoritative.
    identities = IdentityMaps({}, document)
    projects = document.get("projects") or {}
    areas = document.get("areas") or {}
    hidden = document.get("hidden_projects") or []
    if not isinstance(projects, Mapping) or not isinstance(areas, Mapping) or not isinstance(hidden, list):
        raise ValueError("invalid source project configuration")

    policy: dict[str, str] = {}
    for slug in (*projects, *areas, *hidden):
        if not isinstance(slug, str) or not slug:
            raise ValueError("project slugs must be non-empty strings")
        projection = identities.projection(slug)
        if projection in {"full", "area"}:
            policy[slug] = "detail"
        elif projection == "count-only":
            policy[slug] = "count_only"
        elif projection == "hidden":
            policy[slug] = "hidden"
        else:
            raise ValueError(f"unrecognized projection for {slug!r}")
    return policy
