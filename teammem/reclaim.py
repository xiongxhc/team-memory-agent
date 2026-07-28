"""Re-attribute _unmapped ledger rows once roster.yaml learns an identity.

Claisam an identity AFTER ingest would otherwise double-count: person is part
of UNIQUE(person, source, hash), so the collector would insert a second row
under the new slug while the _unmapped row persists. reclaim() is the
sanctioned fix: UPDATE in place, then the collector's next run inserts 0.
"""

import sqlite3

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
                n = conn.execute("UPDATE events SET person=? WHERE person=?",
                                 (slug, person)).rowcount
        out.append((raw, slug, n))
    return out


def reclaim_channel_projects(conn: sqlite3.Connection, ids: IdentityMaps,
                             dry_run: bool = False) -> list[tuple[str, str, int]]:
    """Re-attribute project on mapped chat events AFTER ingest in place."""
    out = []
    channel_kinds = sorted(kind for kind in RESOURCE_FIELDS.values() if kind.endswith("-channel"))
    for kind in channel_kinds:
        for chat_id, project in sorted(ids.resources(kind).items()):
            where = ("source = ? AND project IS NULL"
                     " AND lower(json_extract(refs, '$.chat_id')) = ?")
            if dry_run:
                n = conn.execute(f"SELECT COUNT(*) FROM events WHERE {where}",
                                 (kind, chat_id)).fetchone()[0]
            else:
                with conn:
                    n = conn.execute(f"UPDATE events SET project = ? WHERE {where}",
                                     (project, kind, chat_id)).rowcount
            if n:
                out.append((chat_id, project, n))
    return out
