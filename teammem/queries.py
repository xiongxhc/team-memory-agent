"""Windowed aggregates over the ledger. Pure SQL + date math — no LLM, no I/O
beyond the connection. Ledger ts values are ISO-8601 strings (mixed offsets);
we compare on their lexicographic order, which is correct at day granularity
for our +04:00/Z mix — week bounds land on midnights, and events within a few
hours (worst case ~4h for +04:00 offsets) of a week edge may land in the
adjacent week. Acceptable for weekly rollups (documented tradeoff).
"""

import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .identity import IdentityMaps


@dataclass(frozen=True)
class ReportState:
    target_monday: date
    coverage_state: str
    evidence_cutoff: str | None
    cutoff_precision: str
    cutoff_note: str | None


@dataclass(frozen=True)
class ReportContext:
    state: ReportState
    effective_flags: dict


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
         if p.startswith("_unmapped/")), key=lambda x: (-x[1], x[0]))
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


def report_context(
    conn: sqlite3.Connection,
    target_monday: date,
    operator_date: date,
    ids: IdentityMaps,
    included_person_days: set[tuple[str, str]],
) -> ReportContext:
    current_week = target_monday == week_monday(operator_date)
    coverage_state = (
        "provisional"
        if current_week and operator_date.weekday() < 4
        else "friday-checkpoint"
    )
    effective_flags = deepcopy(flags(conn, target_monday, ids))
    if coverage_state == "provisional":
        effective_flags.pop("gaps", None)
        effective_flags.pop("concentration", None)

    evidence_cutoff, cutoff_precision, cutoff_note = _evidence_cutoff(
        conn, included_person_days
    )
    return ReportContext(
        state=ReportState(
            target_monday=target_monday,
            coverage_state=coverage_state,
            evidence_cutoff=evidence_cutoff,
            cutoff_precision=cutoff_precision,
            cutoff_note=cutoff_note,
        ),
        effective_flags=effective_flags,
    )


def _evidence_cutoff(
    conn: sqlite3.Connection,
    included_person_days: set[tuple[str, str]],
) -> tuple[str | None, str, str | None]:
    if not included_person_days:
        return None, "none", None

    rows = [
        (person, timestamp)
        for person, timestamp in conn.execute("SELECT person, ts FROM events")
        if (person, timestamp[:10]) in included_person_days
    ]
    if not rows:
        return None, "none", None

    latest_day = max(timestamp[:10] for _, timestamp in rows)
    parsed = [
        (timestamp, _parse_timestamp(timestamp))
        for _, timestamp in rows
    ]
    if any(value.tzinfo is None for _, value in parsed):
        return latest_day, "date", "some source timestamps omit offsets"

    latest_timestamp, _ = max(
        parsed,
        key=lambda item: item[1].astimezone(timezone.utc),
    )
    return latest_timestamp, "instant", None


def _parse_timestamp(timestamp: str) -> datetime:
    if timestamp.endswith("Z"):
        timestamp = f"{timestamp[:-1]}+00:00"
    return datetime.fromisoformat(timestamp)
