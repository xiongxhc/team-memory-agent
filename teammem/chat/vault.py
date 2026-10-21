"""Bounded, read-only retrieval from a locally rendered Team Vault."""

import re
import sqlite3
import time
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from ..identity import IdentityMaps
from ..queries import week_label, week_monday
from ..render import _fname, _project_fname
from ..summarize import prepare_daily_journal
from .retrieval import _query, _rank, _tokens, open_ledger_readonly
from .state import Evidence


_MAX_RESULTS = 8
_MAX_FILES = 96
_MAX_FILE_BYTES = 256 * 1024
_MAX_TOTAL_BYTES = 1024 * 1024
_MAX_SNIPPET = 1600
_MAX_DAILIES = 128
_SEARCH_SECONDS = 3.0
_WEEK_FILE = re.compile(r"Week (\d{4}-\d{2}-\d{2})-\d{2}\.md\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class _Budget:
    def __init__(self) -> None:
        self.files = 0
        self.bytes = 0
        self.deadline = time.monotonic() + _SEARCH_SECONDS

    def read(self, root: Path, path: Path) -> str | None:
        if (self.files >= _MAX_FILES or self.bytes >= _MAX_TOTAL_BYTES
                or time.monotonic() >= self.deadline):
            return None
        try:
            if not _lexically_safe(root, path):
                return None
            resolved = path.resolve(strict=True)
            if resolved == root or root not in resolved.parents:
                return None
            stat = resolved.stat()
            if not resolved.is_file() or stat.st_size > _MAX_FILE_BYTES:
                return None
            if self.bytes + stat.st_size > _MAX_TOTAL_BYTES:
                return None
            raw = resolved.read_bytes()
            self.files += 1
            self.bytes += len(raw)
            return raw.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            return None


def _lexically_safe(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            return False
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return False
        return True
    except (OSError, ValueError):
        return False


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(
    loader: yaml.SafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    output: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in output:
            raise yaml.constructor.ConstructorError(
                None, None, "duplicate metadata key", key_node.start_mark,
            )
        output[key] = loader.construct_object(value_node, deep=deep)
    return output


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping,
)


def _frontmatter(text: str) -> tuple[dict[str, Any], str] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4, 4096)
    if end < 0:
        return None
    try:
        loaded = yaml.load(text[4:end], Loader=_UniqueLoader) or {}
    except yaml.YAMLError:
        return None
    if (not isinstance(loaded, Mapping)
            or any(not isinstance(key, str) or not key.strip() for key in loaded)):
        return None
    metadata = {key.strip().casefold(): value for key, value in loaded.items()}
    return metadata, text[end + 5:]


def _meta_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value.strip() if isinstance(value, str) and value.strip() else None


def _safe_root(vault: Mapping[str, Any]) -> Path | None:
    try:
        root = Path(vault["root"])
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            return None
        return root.resolve(strict=True)
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _url(vault: Mapping[str, Any], relative: Path, fragment: str | None = None) -> str:
    encoded_path = "/".join(quote(part, safe="") for part in relative.parts)
    encoded_ref = quote(str(vault["ref"]), safe="/")
    result = f"{str(vault['web_url']).rstrip('/')}/-/blob/{encoded_ref}/{encoded_path}"
    return result + (f"#{quote(fragment, safe='-')}" if fragment else "")


def _chunks(text: str) -> list[str]:
    output: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= _MAX_SNIPPET:
            output.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, _MAX_SNIPPET + 1)
        if cut < _MAX_SNIPPET // 3:
            cut = remaining.rfind("\n", 0, _MAX_SNIPPET + 1)
        if cut < _MAX_SNIPPET // 3:
            cut = _MAX_SNIPPET
        output.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return output


def _matches(text: str, query_text: str, metadata: str) -> tuple[int, int, int] | None:
    tokens = _tokens(query_text)
    score = _rank(tokens, text, metadata)
    return score if score[0] or not tokens else None


def _day_in_range(day: str, start: str | None, end: str | None) -> bool:
    return ((start is None or day >= start[:10])
            and (end is None or day < end[:10]))


def _daily_section(body: str, day: str) -> str | None:
    heading = re.compile(rf"^### {re.escape(day)}[ \t]*$", re.MULTILINE)
    matches = list(heading.finditer(body))
    if len(matches) != 1:
        return None
    start = matches[0].end()
    boundary = re.search(r"^(?:### |## |\*\*Activity detail\*\*)", body[start:], re.MULTILINE)
    end = len(body) if boundary is None else start + boundary.start()
    return body[start:end].strip()


def _daily_evidence(
    conn: sqlite3.Connection,
    source_config_dir: Path,
    root: Path,
    vault: Mapping[str, Any],
    project_policy: Mapping[str, str],
    allowed_projects: frozenset[str],
    text: str,
    start: str | None,
    end: str | None,
    person: str,
    project: str | None,
    budget: _Budget,
) -> list[tuple[tuple[int, int, int], Evidence]]:
    try:
        identities = IdentityMaps.load(source_config_dir)
    except (OSError, TypeError, ValueError):
        return []
    if person not in identities.slugs():
        return []
    display_name = identities.display_name(person)
    prefix = f"{person}|"
    rows = conn.execute(
        "SELECT key, input_hash, text FROM summaries "
        "WHERE kind = 'daily-person' AND substr(key, 1, ?) = ? "
        "ORDER BY key DESC LIMIT ?",
        (len(prefix), prefix, _MAX_DAILIES),
    ).fetchall()
    ranked: list[tuple[tuple[int, int, int], Evidence]] = []
    for stored in rows:
        if time.monotonic() >= budget.deadline:
            break
        if not stored["key"].startswith(prefix):
            continue
        day = stored["key"][len(prefix):]
        if not _DATE.fullmatch(day) or not _day_in_range(day, start, end):
            continue
        footprint = conn.execute(
            "SELECT DISTINCT project FROM events "
            "WHERE person = ? AND substr(ts, 1, 10) = ? ORDER BY project",
            (person, day),
        ).fetchall()
        dependencies = frozenset(row["project"] for row in footprint
                                 if row["project"] is not None)
        if (not footprint or len(dependencies) != len(footprint)
                or any(value not in allowed_projects
                       or project_policy.get(value) != "detail"
                       for value in dependencies)
                or (project is not None and dependencies != {project})):
            continue
        prepared = prepare_daily_journal(conn, person, display_name, day, [])
        if prepared is None or prepared.input_hash != stored["input_hash"]:
            continue
        label = week_label(week_monday(date.fromisoformat(day)))
        relative = Path("Person") / _fname(display_name) / f"{label}.md"
        page = budget.read(root, root / relative)
        parsed = _frontmatter(page) if page is not None else None
        if parsed is None:
            continue
        metadata, body = parsed
        generated = _meta_text(metadata.get("generated"))
        if (_meta_text(metadata.get("slug")) != person
                or _meta_text(metadata.get("week")) != label[5:15]
                or generated is None or not _DATE.fullmatch(generated)):
            continue
        section = _daily_section(body, day)
        if section is None or section != stored["text"].strip():
            continue
        score = _matches(
            section, text,
            f"{display_name} {person} {day} {' '.join(dependencies)}",
        )
        if score is None:
            continue
        chunks = _chunks(section)
        coverage = (
            "Verified generated daily journal; source event dates use the stored "
            "timestamp date-prefix approximation; bounded excerpt up to 1600 characters"
            + (" and additional text may be omitted" if len(chunks) > 1 else "")
            + "; the local vault page may be stale until the next render."
        )
        for index, chunk in enumerate(chunks):
            ranked.append((score, Evidence(
                id=f"vault:person:{person}:{day}:{index}",
                project=sorted(dependencies)[0], projects=dependencies,
                timestamp=generated, text=chunk,
                url=_url(vault, relative, day), person=person, kind="vault",
                title=f"{display_name} — {day}", coverage=coverage,
            )))
    return ranked


def _page_timestamp(
    metadata: Mapping[str, Any],
    path: Path,
) -> tuple[str, str] | None:
    fields = (
        ("date updated", "date-updated timestamp"),
        ("date_updated", "date-updated timestamp"),
        ("updated", "date-updated timestamp"),
        ("last_scanned", "last-scanned timestamp"),
        ("generated", "generated timestamp"),
    )
    for key, provenance in fields:
        value = _meta_text(metadata.get(key))
        if value:
            try:
                date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                )
            except ValueError:
                return None
            return value, provenance
    try:
        value = datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc,
        ).date().isoformat()
        return value, (
            "local file modification time; this is file time, not "
            "source-updated time"
        )
    except OSError:
        return None


def _week_intersects(path: Path, start: str | None, end: str | None) -> bool:
    match = _WEEK_FILE.fullmatch(path.name)
    if match is None:
        return True
    monday = date.fromisoformat(match.group(1))
    after_start = start is None or monday + timedelta(days=7) > date.fromisoformat(start[:10])
    before_end = end is None or monday < date.fromisoformat(end[:10])
    return after_start and before_end


def _project_candidates(
    root: Path,
    project: str,
    start: str | None,
    end: str | None,
) -> list[tuple[Path, str]]:
    name = _project_fname(project)
    candidates: list[tuple[Path, str]] = []
    for directory, field in ((root / "Projects" / name, "project"),
                             (root / "Areas" / name, "area")):
        weeks: list[Path] = []
        if _lexically_safe(root, directory):
            try:
                for index, path in enumerate(directory.iterdir()):
                    if index >= _MAX_FILES:
                        break
                    if (not path.is_symlink() and _WEEK_FILE.fullmatch(path.name)
                            and _week_intersects(path, start, end)):
                        weeks.append(path)
            except OSError:
                pass
        weeks.sort(reverse=True)
        candidates.extend((path, field) for path in weeks)
        if start is None and end is None:
            candidates.append((directory / "README.md", field))
    if start is None and end is None:
        docs = root / "Docs" / name
        candidates.extend((docs / filename, "docs")
                          for filename in ("architecture.md", "summary.md"))
    return candidates


def _project_evidence(
    root: Path,
    vault: Mapping[str, Any],
    projects: tuple[str, ...],
    text: str,
    start: str | None,
    end: str | None,
    budget: _Budget,
) -> list[tuple[tuple[int, int, int], Evidence]]:
    ranked: list[tuple[tuple[int, int, int], Evidence]] = []
    seen: set[Path] = set()
    for project in projects:
        for path, field in _project_candidates(root, project, start, end):
            if path in seen or time.monotonic() >= budget.deadline:
                continue
            seen.add(path)
            page = budget.read(root, path)
            if page is None:
                continue
            parsed = _frontmatter(page)
            if field != "docs":
                if parsed is None or _meta_text(parsed[0].get(field)) != project:
                    continue
                metadata, body = parsed
                match = _WEEK_FILE.fullmatch(path.name)
                if (match is not None
                        and _meta_text(metadata.get("week")) != match.group(1)):
                    continue
            elif parsed is None:
                metadata, body = {}, page
            else:
                metadata, body = parsed
            body = body.strip()
            chunks = _chunks(body)
            heading = next((line[2:].strip() for line in body.splitlines()
                            if line.startswith("# ")), None)
            if field == "docs" and heading:
                heading = f"{project} — {heading}"
            relative = path.relative_to(root)
            page_time = _page_timestamp(metadata, path)
            if page_time is None:
                continue
            timestamp, time_provenance = page_time
            query_tokens = _tokens(text)
            explicit_document_topic = (
                field == "docs" and path.stem.casefold() in query_tokens
            )
            for index, excerpt in enumerate(chunks):
                score = _matches(excerpt, text, f"{project} {path.name} {heading or ''}")
                if score is None:
                    continue
                if explicit_document_topic:
                    score = (
                        score[0] + 1000 + (50 if index == 0 else 0),
                        score[1],
                        score[2],
                    )
                weekly = _WEEK_FILE.fullmatch(path.name) is not None
                ranked.append((score, Evidence(
                    id="vault:" + relative.as_posix() + f":{index}",
                    project=project, projects=frozenset({project}),
                    timestamp=timestamp, text=excerpt, url=_url(vault, relative),
                    kind="vault", title=heading or f"{project} — {path.stem}",
                    coverage=(
                        "Local Team Vault project page; bounded excerpt up to 1600 "
                        f"characters; page time uses {time_provenance}; content "
                        "may be stale until the page is refreshed."
                        + (" This is weekly prose and does not establish an "
                           "exact daily fact."
                           if weekly else "")
                    ),
                )))
    return ranked


def search_vault(
    db_path: Path,
    source_config_dir: Path,
    vault: Mapping[str, Any],
    project_policy: Mapping[str, str],
    allowed_projects: frozenset[str],
    query: Mapping[str, Any],
    limit: int = 3,
) -> list[Evidence]:
    """Search only known, local vault projections within the caller's detail scope."""
    if (isinstance(limit, bool) or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_RESULTS):
        raise ValueError("limit must be between 1 and 8")
    if not isinstance(project_policy, Mapping):
        raise ValueError("project policy must be a mapping")
    if (not isinstance(allowed_projects, frozenset)
            or any(not isinstance(value, str) or not value
                   for value in allowed_projects)):
        raise ValueError("allowed projects must be a frozenset of slugs")
    text, start, end, person, project = _query(query)
    root = _safe_root(vault)
    if root is None:
        return []
    detail = tuple(sorted(value for value in allowed_projects
                          if project_policy.get(value) == "detail"))
    if project is not None:
        detail = tuple(value for value in detail if value == project)
    if not detail:
        return []
    budget = _Budget()
    try:
        with open_ledger_readonly(db_path) as conn:
            conn.set_progress_handler(
                lambda: int(time.monotonic() >= budget.deadline), 100,
            )
            ranked = (_daily_evidence(
                conn, Path(source_config_dir), root, vault, project_policy,
                allowed_projects, text, start, end, person, project, budget,
            ) if person is not None else _project_evidence(
                root, vault, detail, text, start, end, budget,
            ))
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return []
    def sort_key(item: tuple[tuple[int, int, int], Evidence]) -> tuple[Any, ...]:
        identifier = item[1].id
        base, separator, suffix = identifier.rpartition(":")
        chunk = int(suffix) if separator and suffix.isdigit() else 0
        return item[0], item[1].timestamp, base or identifier, -chunk

    ranked.sort(key=sort_key, reverse=True)
    return [item for _, item in ranked[:limit]]
