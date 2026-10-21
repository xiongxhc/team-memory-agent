"""Build a bounded, authorization-scoped directory for one chat turn."""

import json
import re
import sqlite3
import time
import unicodedata
from collections.abc import Callable, Mapping
from datetime import datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .access import project_policy_from_source_config
from .model import ModelError
from .retrieval import open_ledger_readonly


_DIRECTORY_TIMEOUT_SECONDS = 0.25
_NOTICE = "Directory truncated."
POLICY_DEPENDENCY_PREFIX = "\x00teammem-policy-v1:"


def _field(config: Any, section: str) -> Any:
    return config[section] if isinstance(config, Mapping) else getattr(config, section)


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _aliases(slug: str, definition: Mapping[str, Any], fields: tuple[str, ...]) -> list[str]:
    primary = definition.get("name") or slug
    candidates = []
    for field in fields:
        candidates.extend(_strings(definition.get(field)))
    seen = {_normalize(slug), _normalize(primary) if isinstance(primary, str) else ""}
    result = []
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        normalized = _normalize(candidate)
        if normalized not in seen:
            seen.add(normalized)
            result.append(candidate.strip())
    return result


def _mentioned(item: Mapping[str, Any], query: str) -> bool:
    normalized_query = _normalize(query)
    for value in (item["slug"], item["name"], *item.get("aliases", [])):
        alias = _normalize(value)
        if alias and re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized_query):
            return True
    return False


def _json_size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def policy_dependency(slug: str, mode: str) -> str:
    if not isinstance(slug, str) or not slug or slug.startswith(POLICY_DEPENDENCY_PREFIX):
        raise ValueError("invalid project slug for policy dependency")
    if mode not in {"detail", "count_only"}:
        raise ValueError("invalid policy dependency mode")
    return POLICY_DEPENDENCY_PREFIX + json.dumps([slug, mode], separators=(",", ":"))


def bind_policy_dependencies(
    projects_source: Mapping[str, Any], authorization: frozenset[str],
) -> frozenset[str]:
    """Bind raw grants to their current visible projection mode."""
    if any(not isinstance(slug, str) or slug.startswith(POLICY_DEPENDENCY_PREFIX) for slug in authorization):
        raise ModelError("The project authorization scope is invalid.")
    try:
        policy = project_policy_from_source_config(projects_source)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ModelError("The project authorization scope is invalid.") from exc
    if any(slug.startswith(POLICY_DEPENDENCY_PREFIX) for slug in policy):
        raise ModelError("The project authorization scope is invalid.")
    result = set()
    for slug in authorization:
        mode = policy.get(slug)
        if mode in {"detail", "count_only"}:
            result.update((slug, policy_dependency(slug, mode)))
    return frozenset(result)


def public_team_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Remove authorization bookkeeping before the directory reaches a model."""
    fields = ("requester", "sender", "people", "projects", "clock",
              "ambiguous_aliases", "truncated", "notice")
    return {key: context[key] for key in fields if key in context}


def has_policy_dependencies(projects: frozenset[str]) -> bool:
    """Reject legacy scoped content that predates projection-mode binding."""
    raw = {item for item in projects if not item.startswith(POLICY_DEPENDENCY_PREFIX)}
    bound = {item for item in projects if item.startswith(POLICY_DEPENDENCY_PREFIX)}
    if not raw:
        return not bound
    for slug in raw:
        if not any(token == policy_dependency(slug, mode) for token in bound for mode in ("detail", "count_only")):
            return False
    return True


def _clock(timezone_name: str, now: datetime | None) -> dict[str, str]:
    zone = timezone.utc if timezone_name == "UTC" else ZoneInfo(timezone_name)
    current = datetime.now(zone) if now is None else now.astimezone(zone)
    today = datetime.combine(current.date(), day_time.min, tzinfo=zone)
    tomorrow = today + timedelta(days=1)
    yesterday = today - timedelta(days=1)
    return {
        "timezone": timezone_name,
        "now": current.isoformat(),
        "today_start": today.isoformat(),
        "today_end": tomorrow.isoformat(),
        "yesterday_start": yesterday.isoformat(),
        "yesterday_end": today.isoformat(),
    }


def _visible_people(ledger_path: Path, detail_projects: tuple[str, ...]) -> set[str]:
    if not detail_projects:
        return set()
    placeholders = ",".join("?" for _ in detail_projects)
    try:
        with open_ledger_readonly(ledger_path) as conn:
            deadline = time.monotonic() + _DIRECTORY_TIMEOUT_SECONDS
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
            try:
                rows = conn.execute(
                    f"SELECT DISTINCT person FROM events WHERE project IN ({placeholders}) LIMIT 10000",
                    detail_projects,
                ).fetchall()
            finally:
                conn.set_progress_handler(None, 0)
    except sqlite3.Error as exc:
        raise ModelError("The team directory could not be loaded safely.") from exc
    return {str(row[0]) for row in rows}


def build_team_context(
    config: Any,
    authorization: frozenset[str],
    *,
    requester_id: str,
    query: str,
    sender_profile: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    max_bytes: int = 8000,
    measure_bytes: Callable[[Mapping[str, Any]], int] | None = None,
    require_policy_dependencies: bool = False,
) -> dict[str, Any]:
    """Read current source files and ledger state into a scoped model data object."""
    context_config = _field(config, "context")
    paths = _field(config, "paths")
    if not isinstance(context_config, Mapping):
        raise ModelError("The requester identity directory is not configured.")
    mappings = context_config.get("user_people")
    requester_slug = mappings.get(requester_id) if isinstance(mappings, Mapping) else None
    if requester_slug is not None and (not isinstance(requester_slug, str) or not requester_slug):
        raise ModelError("The requester identity could not be verified.")
    if not isinstance(authorization, frozenset):
        raise ModelError("The directory authorization scope is invalid.")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 512:
        raise ValueError("max_bytes must be an integer of at least 512")

    source_dir = Path(paths["source_config_dir"])
    try:
        roster = yaml.safe_load((source_dir / "roster.yaml").read_text()) or {}
        projects_source = yaml.safe_load((source_dir / "projects.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ModelError("The team directory could not be loaded safely.") from exc
    if not isinstance(roster, Mapping) or not isinstance(projects_source, Mapping):
        raise ModelError("The team directory could not be loaded safely.")
    members = roster.get("members") or {}
    if not isinstance(members, Mapping) or (requester_slug is not None and requester_slug not in members):
        raise ModelError("The requester identity could not be verified.")
    if not isinstance(requester_id, str) or not requester_id or len(requester_id) > 200:
        raise ModelError("The requester identity could not be verified.")
    sender = {"open_id": requester_id, "source": "feishu_event"}
    profile_names = []
    if isinstance(sender_profile, Mapping) and sender_profile.get("open_id") == requester_id:
        for field in ("name", "en_name"):
            value = sender_profile.get(field)
            if isinstance(value, str) and value.strip():
                bounded = value.strip()[:200]
                sender[field] = bounded
                profile_names.append(bounded)
        sender["source"] = "feishu_profile"
    if requester_slug is None and profile_names:
        matches = set()
        for slug, definition in members.items():
            if not isinstance(slug, str) or not isinstance(definition, Mapping):
                continue
            roster_names = []
            name = definition.get("name")
            if isinstance(name, str) and name.strip():
                roster_names.append(name)
            roster_names.extend(_strings(definition.get("feishu_names")))
            if any(_normalize(profile) == _normalize(roster_name)
                   for profile in profile_names for roster_name in roster_names):
                matches.add(slug)
        if len(matches) == 1:
            requester_slug = next(iter(matches))
    try:
        policy = project_policy_from_source_config(projects_source)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ModelError("The team directory could not be loaded safely.") from exc
    raw_authorization = frozenset(
        project for project in authorization
        if isinstance(project, str) and not project.startswith(POLICY_DEPENDENCY_PREFIX)
    )
    def admitted(project: str, mode: str) -> bool:
        return not require_policy_dependencies or policy_dependency(project, mode) in authorization
    detail_projects = tuple(sorted(
        project for project in raw_authorization
        if policy.get(project) == "detail" and admitted(project, "detail")
    ))
    count_projects = tuple(sorted(
        project for project in raw_authorization
        if policy.get(project) == "count_only" and admitted(project, "count_only")
    ))
    visible = _visible_people(Path(paths["ledger_db"]), detail_projects)
    if requester_slug is not None:
        visible.add(requester_slug)

    people = []
    for slug in sorted(visible):
        definition = members.get(slug)
        if not isinstance(definition, Mapping):
            continue
        name = definition.get("name") or slug
        if not isinstance(name, str) or not name.strip():
            name = slug
        people.append({
            "slug": slug,
            "name": name.strip(),
            "aliases": _aliases(slug, definition, ("feishu_names", "gitlab", "github")),
        })
    requester = next((person for person in people if person["slug"] == requester_slug), None)
    if requester_slug is not None and requester is None:
        raise ModelError("The requester identity could not be verified.")

    project_definitions = {}
    for section in ("projects", "areas"):
        values = projects_source.get(section) or {}
        if isinstance(values, Mapping):
            project_definitions.update(values)
    projects = []
    for slug in (*detail_projects, *count_projects):
        definition = project_definitions.get(slug) or {}
        if not isinstance(definition, Mapping):
            definition = {}
        name = definition.get("name") or slug
        if not isinstance(name, str) or not name.strip():
            name = slug
        description = definition.get("description") or ""
        if not isinstance(description, str):
            description = ""
        projects.append({
            "slug": slug,
            "name": name.strip(),
            "aliases": _aliases(slug, definition, ("aliases",)),
            "description": description.strip()[:240],
            "access": "detail" if slug in detail_projects else "count",
        })

    def ambiguous(items: list[Mapping[str, Any]]) -> list[str]:
        owners: dict[str, set[str]] = {}
        for item in items:
            for value in (item["slug"], item["name"], *item.get("aliases", [])):
                owners.setdefault(_normalize(value), set()).add(item["slug"])
        return sorted(alias for alias, slugs in owners.items() if len(slugs) > 1)

    dependencies = []
    for slug in detail_projects:
        dependencies.extend((slug, policy_dependency(slug, "detail")))
    for slug in count_projects:
        dependencies.extend((slug, policy_dependency(slug, "count_only")))
    ambiguity = {"people": ambiguous(people), "projects": ambiguous(projects)}
    result: dict[str, Any] = {
        "requester": requester,
        "sender": sender,
        "people": [] if requester is None else [requester],
        "projects": [],
        "clock": _clock(str(context_config["timezone"]), now),
        "truncated": False,
        "_scope": {"detail_projects": list(detail_projects), "count_projects": list(count_projects)},
        "_project_dependencies": dependencies,
        "_ambiguous_aliases": ambiguity,
    }
    if ambiguity["people"] or ambiguity["projects"]:
        result["ambiguous_aliases"] = ambiguity
    measure = _json_size if measure_bytes is None else measure_bytes
    if not callable(measure):
        raise ValueError("measure_bytes must be callable")
    while measure(public_team_context(result)) > max_bytes:
        if "en_name" in sender:
            sender.pop("en_name")
        elif "name" in sender:
            sender.pop("name")
        else:
            break
    aliases_compacted = False
    while (measure(public_team_context(result)) > max_bytes and requester is not None
           and requester["aliases"]):
        requester["aliases"].pop()
        aliases_compacted = True
    if aliases_compacted:
        result["truncated"] = True
        result["notice"] = _NOTICE
    if measure(public_team_context(result)) > max_bytes and requester is not None:
        result["people"] = []
        result["truncated"] = True
        result["notice"] = _NOTICE
    if measure(public_team_context(result)) > max_bytes:
        raise ModelError("The authorized directory scope is too large to load safely.")

    candidates = []
    for project in projects:
        candidates.append((0 if _mentioned(project, query) else 1, 0, "projects", project))
    for person in people:
        if requester_slug is None or person["slug"] != requester_slug:
            candidates.append((0 if _mentioned(person, query) else 1, 1, "people", person))
    candidates.sort(key=lambda item: (item[0], item[1], _normalize(item[3]["name"]), item[3]["slug"]))
    omitted = False
    for _, _, section, item in candidates:
        result[section].append(item)
        bounded = public_team_context(result)
        bounded["truncated"] = True
        bounded["notice"] = _NOTICE
        if measure(bounded) > max_bytes:
            result[section].pop()
            omitted = True
    if omitted:
        result["truncated"] = True
        result["notice"] = _NOTICE
        while measure(public_team_context(result)) > max_bytes and requester is not None and requester["aliases"]:
            requester["aliases"].pop()
        if measure(public_team_context(result)) > max_bytes:
            raise ModelError("The authorized directory scope is too large to load safely.")
    return result


def resolve_directory_alias(context: Mapping[str, Any], kind: str, value: str) -> str:
    """Resolve one exact scoped alias without guessing or fuzzy matching."""
    if kind not in {"person", "project"} or not isinstance(value, str) or not value.strip():
        raise ModelError("The directory reference is unknown.")
    normalized = _normalize(value)
    if kind == "person" and normalized in {"me", "myself", "self"}:
        requester = context.get("requester")
        if isinstance(requester, Mapping) and isinstance(requester.get("slug"), str):
            return requester["slug"]
        raise ModelError("The requester identity could not be verified.")
    section = "people" if kind == "person" else "projects"
    matches = set()
    ambiguous = context.get("_ambiguous_aliases")
    ambiguous_values = ambiguous.get(section, []) if isinstance(ambiguous, Mapping) else []
    if normalized in ambiguous_values:
        raise ModelError(f"The {kind} reference is ambiguous in the authorized directory.")
    for item in context.get(section, []):
        if not isinstance(item, Mapping):
            continue
        values = (item.get("slug"), item.get("name"), *(item.get("aliases") or []))
        if any(isinstance(candidate, str) and _normalize(candidate) == normalized for candidate in values):
            matches.add(item.get("slug"))
    matches.discard(None)
    if not matches:
        raise ModelError(f"The {kind} reference is unknown in the authorized directory.")
    if len(matches) != 1:
        raise ModelError(f"The {kind} reference is ambiguous in the authorized directory.")
    return next(iter(matches))
