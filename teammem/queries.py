"""Windowed aggregates over the ledger. Pure SQL + date math — no LLM, no I/O
beyond the connection. Ledger ts values are ISO-8601 strings (mixed offsets);
we compare on their lexicographic order, which is correct at day granularity
for our +04:00/Z mix — week bounds land on midnights, and events within a few
hours (worst case ~4h for +04:00 offsets) of a week edge may land in the
adjacent week. Acceptable for weekly rollups (documented tradeoff).
"""

import json
import sqlite3
from datetime import date, timedelta

from .identity import IdentityMaps


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_label(monday: date) -> str:
    friday = monday + timedelta(days=4)
    return f"Week {monday.isoformat()}-{friday:%d}"


def week_range(monday: date) -> tuple[str, str]:
    return (f"{monday.isoformat()}T00:00:00",
            f"{(monday + timedelta(days=7)).isoformat()}T00:00:00")


def events_between(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    cur = conn.execute(
        "SELECT person, project, ts, kind, summary, refs, hash, source FROM events"
        " WHERE ts >= ? AND ts < ? ORDER BY ts DESC", (start, end))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def by_key(rows: list[dict], key: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r[key] or "(no project)", []).append(r)
    return out


def ref_url(row: dict) -> str | None:
    try:
        return (json.loads(row["refs"]) or {}).get("url")
    except (TypeError, ValueError):
        return None


def flags(conn: sqlite3.Connection, monday: date, ids: IdentityMaps) -> dict:
    start, end = week_range(monday)
    prior_start, _ = week_range(monday - timedelta(weeks=4))
    this_week = events_between(conn, start, end)
    prior = events_between(conn, prior_start, start)
    active_now = {r["person"] for r in this_week}
    active_prior = {r["person"] for r in prior}
    gaps = sorted(s for s in ids.slugs() if s in active_prior and s not in active_now)
    unmapped = sorted(
        ((p, len(rs)) for p, rs in by_key(this_week, "person").items()
         if p.startswith("_unmapped/")), key=lambda x: -x[1])
    channel_counts: dict[str, int] = {}
    for r in this_week:
        if (
            r["source"]
            not in ("slack-channel", "feishu-channel", "discord-channel")
            or r["project"] not in (None, "(no project)")
        ):
            continue
        try:
            refs = json.loads(r["refs"]) or {}
            chat_id = refs.get("channel_id") or refs.get("chat_id")
        except (TypeError, ValueError):
            chat_id = None
        if chat_id:
            channel_counts[chat_id] = channel_counts.get(chat_id, 0) + 1
    unmapped_channels = sorted(
        channel_counts.items(), key=lambda item: (-item[1], item[0])
    )
    concentration = []
    for proj, rs in by_key(this_week, "project").items():
        if proj == "(no project)" or len(rs) < 10:
            continue
        top, n = max(((p, len(v)) for p, v in by_key(rs, "person").items()),
                     key=lambda x: x[1])
        share = n / len(rs)
        if share >= 0.8:
            concentration.append((proj, top, round(share, 2)))
    return {"gaps": gaps, "unmapped": unmapped,
            "unmapped_channels": unmapped_channels,
            "concentration": sorted(concentration)}
