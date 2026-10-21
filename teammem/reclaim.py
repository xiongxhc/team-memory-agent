"""Re-attribute _unmapped ledger rows once roster.yaml learns an identity.

Claisam an identity AFTER ingest would otherwise double-count: person is part
of UNIQUE(person, source, hash), so the collector would insert a second row
under the new slug while the _unmapped row persists. reclaim() is the
sanctioned fix: UPDATE in place, then the collector's next run inserts 0.
"""

import json
import sqlite3
from collections.abc import Iterable
from urllib.parse import unquote, urlsplit

from .connectors.config import Origin, normalize_http_origin
from .identity import IDENTITY_FIELDS, IdentityMaps, RESOURCE_FIELDS

_KINDS = IDENTITY_FIELDS


def reclaim(conn: sqlite3.Connection, ids: IdentityMaps,
            dry_run: bool = False) -> list[tuple[str, str, int]]:
    out = []
    unmapped = [r[0] for r in conn.execute(
        "SELECT DISTINCT person FROM events WHERE person LIKE '_unmapped/%'")]
    for person in unmapped:
        raw = person.removeprefix("_unmapped/")
        hits = {s for s in (ids.person(k, raw) for k in _KINDS)
                if not s.startswith("_unmapped/")}
        if not hits:
            continue
        if len(hits) > 1:
            out.append((raw, "!conflict:" + "|".join(sorted(hits)), 0))
            continue
        slug = hits.pop()
        if dry_run:
            n = conn.execute("SELECT COUNT(*) FROM events WHERE person=?",
                             (person,)).fetchone()[0]
        else:
            with conn:
                deleted = conn.execute(
                    "DELETE FROM events WHERE person=? AND EXISTS ("
                    "SELECT 1 FROM events AS mapped"
                    " WHERE mapped.person=? AND mapped.source=events.source"
                    " AND mapped.hash=events.hash)",
                    (person, slug),
                ).rowcount
                updated = conn.execute("UPDATE events SET person=? WHERE person=?",
                                       (slug, person)).rowcount
                n = deleted + updated
        out.append((raw, slug, n))
    return out


def reclaim_channel_projects(conn: sqlite3.Connection, ids: IdentityMaps,
                             dry_run: bool = False) -> list[tuple[str, str, int]]:
    """Re-attribute project on mapped chat events AFTER ingest in place."""
    out = []
    channel_kinds = sorted(kind for kind in RESOURCE_FIELDS.values() if kind.endswith("-channel"))
    for kind in channel_kinds:
        for chat_id, project in sorted(ids.resources(kind).items()):
            normalized_chat_id = chat_id.lower()
            where = ("source = ? AND project IS NOT ?"
                     " AND lower(coalesce("
                     "json_extract(refs, '$.channel_id'),"
                     "json_extract(refs, '$.chat_id')"
                     ")) = ?")
            if dry_run:
                n = conn.execute(f"SELECT COUNT(*) FROM events WHERE {where}",
                                 (kind, project, normalized_chat_id)).fetchone()[0]
            else:
                with conn:
                    n = conn.execute(f"UPDATE events SET project = ? WHERE {where}",
                                     (project, kind, project, normalized_chat_id)).rowcount
            if n:
                out.append((normalized_chat_id, project, n))
    return out


def _repository_path(
    source: str,
    url: str,
    gitlab_origins: frozenset[Origin],
) -> str | None:
    origin = normalize_http_origin(url)
    if origin is None:
        return None
    parsed = urlsplit(url)

    path = unquote(parsed.path).strip("/")
    if source == "github":
        if origin[1] != "github.com":
            return None
        segments = path.split("/")
        if len(segments) < 2 or not all(segments[:2]):
            return None
        return "/".join(segments[:2])

    if origin not in gitlab_origins:
        return None
    repository = path.split("/-/", 1)[0]
    segments = repository.split("/")
    if len(segments) < 2 or not all(segments):
        return None
    return repository


def reclaim_repository_projects(
    conn: sqlite3.Connection,
    ids: IdentityMaps,
    dry_run: bool = False,
    gitlab_url: str | None = None,
    reclaim_origins: Iterable[str] = (),
) -> list[tuple[str, str, int]]:
    """Re-attribute forge projects using explicit trusted GitLab origins."""
    gitlab_origins: set[Origin] = set()
    if gitlab_url:
        current_origin = normalize_http_origin(gitlab_url)
        if current_origin is not None:
            gitlab_origins.add(current_origin)
    if isinstance(reclaim_origins, (str, bytes)):
        raise ValueError("invalid GitLab reclaim origin configuration")
    for configured_origin in reclaim_origins:
        origin = normalize_http_origin(configured_origin, origin_only=True)
        if origin is None:
            raise ValueError("invalid GitLab reclaim origin configuration")
        gitlab_origins.add(origin)
    trusted_gitlab_origins = frozenset(gitlab_origins)
    mapped = {}
    for source, kind in (("github", "github-repo"), ("gitlab", "gitlab-repo")):
        for repository, project in ids.resources(kind).items():
            mapped[(source, repository.lower())] = (repository, project)

    changes = []
    counts: dict[tuple[str, str], int] = {}
    rows = conn.execute(
        "SELECT id, source, project, refs FROM events "
        "WHERE source IN ('github', 'gitlab')"
    ).fetchall()
    for event_id, source, current_project, refs in rows:
        try:
            parsed_refs = json.loads(refs)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed_refs, dict):
            continue
        url = parsed_refs.get("url")
        if not isinstance(url, str):
            continue
        repository = _repository_path(source, url, trusted_gitlab_origins)
        if repository is None:
            continue
        target = mapped.get((source, repository.lower()))
        if target is None:
            continue
        canonical_repository, project = target
        if current_project == project:
            continue
        changes.append((project, event_id))
        key = (canonical_repository, project)
        counts[key] = counts.get(key, 0) + 1

    if not dry_run and changes:
        with conn:
            conn.executemany(
                "UPDATE events SET project = ? WHERE id = ?",
                changes,
            )
    return [
        (repository, project, count)
        for (repository, project), count in sorted(counts.items())
    ]
