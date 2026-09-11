"""Regenerate the team vault from the ledger.
The vault is a projection:
managed dirs (Person/, Projects/, Work Journal/, Meetings/, README.md) are
deleted and rewritten on every render; anything else in the vault is never
touched.
Deterministic for a fixed (ledger, today): no wall-clock, no randomness.
Links are relative markdown links (GitLab web UI renders [[wikilinks]] as
empty repo-wiki pages; Obsidian handles relative links fine)."""

import json
import math
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

from . import meeting_render
from .identity import IdentityMaps
from .meeting_bundles import short_occurrence_id
from .meeting_store import get_meeting, list_meeting_revisions, list_meetings
from .queries import (by_key, events_between, flags, ref_url,
                      week_label, week_monday, week_range)
from .store import get_summary, ledger_read_view

MANAGED = ("Person", "Projects", "Work Journal", "Meetings", "README.md")
MAX_WORK_LINES = 12   # work bullets per person per week (project and person week files)
WORK_KINDS = ("commit", "pr", "mr", "issue", "repo", "comment", "journal-highlight")
_FLAG_KEYS = frozenset({"gaps", "unmapped", "unmapped_channels", "concentration"})
_INVALID_FLAGS_MESSAGE = "invalid weekly report effective flags provenance"
_KIND_LABELS = {
    "commit": ("commit", "commits"),
    "pr": ("PR", "PRs"),
    "mr": ("MR", "MRs"),
    "issue": ("issue", "issues"),
    "repo": ("repo", "repos"),
    "comment": ("comment", "comments"),
    "journal-highlight": ("journal highlight", "journal highlights"),
    "message": ("message", "messages"),
    "meeting": ("meeting", "meetings"),
}
_WINDOWS_DEVICE_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _fname(name: str) -> str:
    return name.replace("/", "-").replace("\\", "-").strip() or "unnamed"


def _project_fname(name: str) -> str:
    normalized = unicodedata.normalize("NFC", name)
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", normalized).rstrip(" .")
    cleaned = cleaned or "unnamed"
    if cleaned.casefold() == ".git":
        return "_git"
    if cleaned.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def _meeting_person(ids: IdentityMaps, person_id: str,
                    provider_name: str) -> tuple[str, str]:
    """(vault key, display name) for one meeting participant.

    A roster-mapped identity resolves to its slug and roster name, so a meeting
    backlink lands on the person page the ledger already renders. An identity
    the roster does not know keeps the provider's own id and name: a meeting
    carries a real display name, unlike the raw handles the event path files
    under `_unmapped/`, and every actual participant must be linkable.
    """
    slug = ids.person("feishu", person_id)
    if slug.startswith("_unmapped/"):
        return person_id, provider_name
    return slug, ids.display_name(slug)


def _person_link(name: str, up: int = 1) -> str:
    return (f"[{meeting_render.escape_md_label(name)}]"
            f"({'../' * up}Person/{quote(name)}/README.md)")


def _project_link(proj: str, up: int = 1) -> str:
    return (f"[{meeting_render.escape_md_label(proj)}]"
            f"({'../' * up}Projects/{quote(_project_fname(proj))}/README.md)")


def _week_link(label: str, up: int = 1) -> str:
    return f"[{label}]({'../' * up}Work%20Journal/{quote(label)}.md)"


def _count(noun: str, total: int) -> str:
    return f"{total} {noun if total == 1 else noun + 's'}"


def _activity_summary(rows: list[dict], separator: str = " · ") -> str:
    people = len({r["person"] for r in rows})
    return separator.join((_count("event", len(rows)), _count("contributor", people)))


def _kind_summary(rows: list[dict]) -> str:
    parts = []
    for kind, grouped in sorted(by_key(rows, "kind").items()):
        singular, plural = _KIND_LABELS.get(kind, (kind, f"{kind}s"))
        parts.append(f"{len(grouped)} {singular if len(grouped) == 1 else plural}")
    return ", ".join(parts)


def _evidence_cutoff(rows: list[dict]) -> tuple[str | None, str, str | None]:
    if not rows:
        return None, "none", None
    parsed = []
    for row in rows:
        raw = row["ts"]
        value = datetime.fromisoformat(
            f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
        )
        if value.tzinfo is None:
            return max(r["ts"][:10] for r in rows), "date", "source offset unavailable"
        parsed.append((raw, value.astimezone(timezone.utc)))
    return max(parsed, key=lambda item: item[1])[0], "instant", None


def _evidence_notice(cutoff: tuple[str | None, str, str | None]) -> str:
    value, precision, note = cutoff
    detail = f" ({precision} precision; {note})" if note else ""
    return f"Evidence through {value}{detail}."


def _validate_project_filenames(conn: sqlite3.Connection,
                                extra: dict[str, list[str]] | None = None) -> None:
    """`extra` maps a project a meeting named to the occurrences that named it,
    so a collision a producer caused costs those meetings and not the render."""
    extra = extra or {}
    seen = {"readme.md": "project index"}
    projects = conn.execute(
        "SELECT DISTINCT project FROM events WHERE project IS NOT NULL ORDER BY project"
    )
    names = sorted({row[0] for row in projects} | set(extra))
    for project in names:
        if not project or project == "(no project)":
            continue
        folder = _project_fname(project)
        key = folder.casefold()
        if folder in (".", "..") or key in seen:
            other = seen.get(key)
            message = (f"filename collision in vault render: {project!r} "
                       f"and {other!r}")
            # The later of the two names loses, so the earlier one keeps its
            # folder and only one meeting pays for the pair.
            owners = [*(extra.get(project) or extra.get(other) or ())]
            if owners:
                raise meeting_render.MeetingWithheld(owners, message)
            raise ValueError(message)
        seen[key] = project


def _line(r: dict) -> str:
    url = ref_url(r)
    ref = f" ([ref]({url}))" if url else ""
    return f"- {r['kind']} — {r['summary']}{ref}\n"


def _day_headline(text: str) -> str:
    """First-line digest of a cached day entry: its leading bold phrases,
    else the plain line truncated."""
    line = text.strip().splitlines()[0].lstrip("- ").strip()
    bolds = re.findall(r"\*\*(.+?)\*\*", line)
    if bolds:
        return " — ".join(f"**{b}**" for b in bolds[:2])
    return line if len(line) <= 140 else line[:139].rstrip() + "…"


def _msg_channel_ids(rows: list[dict]) -> set:
    chans = set()
    for r in rows:
        if r["kind"] == "message":
            try:
                refs = json.loads(r["refs"]) or {}
                cid = refs.get("channel_id") or refs.get("chat_id")
                if cid:
                    chans.add(cid)
            except (TypeError, ValueError):
                pass
    return chans


def _stored_effective_flags(summary) -> dict | None:
    """Parse a non-legacy weekly record before vault cleanup.

    Missing flags are incomplete provenance, not permission to mix an older report
    with mutable ledger flags. Legacy records retain the prior compatibility path.
    """
    if summary is None or summary.coverage_state is None:
        return None
    payload = summary.effective_flags_json
    if payload is None or payload == "" or (isinstance(payload, str) and not payload.strip()):
        return None
    if not isinstance(payload, str):
        raise ValueError(_INVALID_FLAGS_MESSAGE)
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        raise ValueError(_INVALID_FLAGS_MESSAGE) from None
    if not isinstance(parsed, dict) or set(parsed) - _FLAG_KEYS:
        raise ValueError(_INVALID_FLAGS_MESSAGE)
    _validate_flag_entries(parsed, "gaps", 1, (str,))
    _validate_flag_entries(parsed, "unmapped", 2, (str, int))
    _validate_flag_entries(parsed, "unmapped_channels", 2, (str, int))
    _validate_flag_entries(parsed, "concentration", 3, (str, str, (int, float)))
    for _, _, share in parsed.get("concentration", []):
        if isinstance(share, bool) or not math.isfinite(share) or not 0 <= share <= 1:
            raise ValueError(_INVALID_FLAGS_MESSAGE)
    return parsed


def _validate_flag_entries(
    flags: dict, key: str, length: int, types: tuple[type | tuple[type, ...], ...]
) -> None:
    entries = flags.get(key, [])
    if not isinstance(entries, list):
        raise ValueError(_INVALID_FLAGS_MESSAGE)
    for entry in entries:
        if length == 1:
            values = (entry,)
        else:
            if not isinstance(entry, list) or len(entry) != length:
                raise ValueError(_INVALID_FLAGS_MESSAGE)
            values = entry
        if any(
            isinstance(value, bool) or not isinstance(value, expected)
            for value, expected in zip(values, types)
        ):
            raise ValueError(_INVALID_FLAGS_MESSAGE)


def render_vault(conn: sqlite3.Connection, ids: IdentityMaps, vault_dir: Path,
                 today: date, weeks: int = 4,
                 channel_names: dict | None = None, *,
                 withhold: dict[str, str] | None = None) -> dict:
    """Regenerate every managed path from the ledger.

    A meeting whose participant or project name collides with a page the vault
    already owns is left out and named in `out["withheld"]`, and the rest of the
    vault renders as it always did: a producer-chosen name costs its own
    meeting, never the daily render. `withhold` seeds that set, so a caller that
    has its own reason to leave a meeting out passes it here.
    """
    withheld = dict(withhold or {})
    while True:
        try:
            out = _render_vault(conn, ids, vault_dir, today, weeks, channel_names,
                                frozenset(withheld))
        except meeting_render.MeetingWithheld as refusal:
            fresh = [o for o in refusal.occurrence_ids if o not in withheld]
            if not fresh:                  # nothing left to withhold; a real bug
                raise ValueError(refusal.reason) from refusal
            for occurrence_id in fresh:
                withheld[occurrence_id] = refusal.reason
            continue
        out["withheld"] = withheld
        return out


def _render_vault(conn: sqlite3.Connection, ids: IdentityMaps, vault_dir: Path,
                  today: date, weeks: int, channel_names: dict | None,
                  withhold: frozenset) -> dict:
    weeks = max(1, weeks)
    channel_names = channel_names or {}
    mondays = [week_monday(today) - timedelta(weeks=i) for i in range(weeks)]
    weekly_summaries = {
        m: get_summary(conn, "weekly-team", f"team|{m.isoformat()}")
        for m in mondays
    }
    stored_flags = {
        m: _stored_effective_flags(summary)
        for m, summary in weekly_summaries.items()
    }
    naming = meeting_render.VaultNaming(
        person=lambda person_id, provider_name: _meeting_person(ids, person_id,
                                                                provider_name),
        person_folder=_fname,
        person_link=_person_link,
        project_link=_project_link,
        safe_filename=_project_fname,
    )
    # Every current occurrence, no window: Meetings/ is wiped on every render,
    # so a window would delete already-published notes from the live vault.
    notes = meeting_render.collect_notes(conn, naming=naming, today=today,
                                         withhold=withhold)
    person_meetings = meeting_render.person_backlinks(notes)
    project_meetings = meeting_render.project_backlinks(notes)
    meeting_projects = sorted(project_meetings)
    project_owners: dict[str, list[str]] = {}
    for note in notes:
        for project in note.projects:
            project_owners.setdefault(project, []).append(note.occurrence_id)
    _validate_project_filenames(conn, project_owners)
    vault_dir.mkdir(parents=True, exist_ok=True)
    for m in MANAGED:
        p = vault_dir / m
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    (vault_dir / "Person").mkdir()
    (vault_dir / "Projects").mkdir()
    (vault_dir / "Work Journal").mkdir()
    (vault_dir / "Meetings").mkdir()

    written: set = set()

    def _write(path, content):
        if path in written:
            raise ValueError(f"filename collision in vault render: {path}")
        written.add(path)
        path.write_text(content)

    week_of = {m: events_between(conn, *week_range(m)) for m in mondays}
    files = 0

    def _summary(kind: str, key: str) -> str | None:
        summary = get_summary(conn, kind, key)
        return summary.text if summary else None

    def _msg_line(msgs: list[dict]) -> str:
        cids = _msg_channel_ids(msgs)
        names = sorted(channel_names[c] for c in cids if channel_names.get(c))
        shown = ""
        if names:
            more = f", +{len(names) - 4}" if len(names) > 4 else ""
            shown = f" ({', '.join(names[:4])}{more})"
        return (f"- 💬 {_count('message', len(msgs))} across "
                f"{_count('channel', len(cids))}{shown}\n")

    def _day_lines(person: str, msgs: list[dict]) -> list[str]:
        out = []
        for d in sorted({r["ts"][:10] for r in msgs}):
            t = _summary("daily-person", f"{person}|{d}")
            if t:
                out.append(f"- {d} — {_day_headline(t)}\n")
        return out

    # ---- Work Journal: one report per rendered week -------------------------
    for m in mondays:
        rows, label = week_of[m], week_label(m)
        weekly_summary = weekly_summaries[m]
        report = weekly_summary.text if weekly_summary else None
        coverage_state = weekly_summary.coverage_state if weekly_summary else None
        provenance_incomplete = weekly_summary is not None and (
            coverage_state is not None and stored_flags[m] is None
        )
        if weekly_summary and coverage_state is not None:
            f = stored_flags[m] or {}
        else:
            f = flags(conn, m, ids)
        md = [f"---\ntitle: {label} Team\ngenerated: {today.isoformat()}\n---\n",
              f"# {label} — Team\n"]
        if report:
            if coverage_state is None:
                md.append("\n> Legacy report — exact event cutoff unknown.\n")
            elif provenance_incomplete:
                md.append(
                    "\n> Report provenance incomplete — stored effective flags unavailable.\n"
                )
            md.append("\n" + report.rstrip() + "\n")

        tally = ["\n## Appendix — activity by person\n" if report else "\n## People\n"]
        for person, rs in sorted(by_key(rows, "person").items(),
                                 key=lambda x: -len(x[1])):
            if person.startswith("_unmapped/"):
                continue
            tally.append(f"\n### {_person_link(_fname(ids.display_name(person)))} — "
                      f"{_count('event', len(rs))} ({_kind_summary(rs)})\n")
            work = [r for r in rs if r["kind"] in WORK_KINDS]
            msgs = [r for r in rs if r["kind"] == "message"]
            tally += [_line(r) for r in work[:5]]
            if msgs:
                if not work:
                    tally += _day_lines(person, msgs)
                tally.append(_msg_line(msgs))

        projects = ["\n## Projects\n"]
        prior_rows = week_of.get(m - timedelta(weeks=1)) or events_between(
            conn, *week_range(m - timedelta(weeks=1)))
        prior_proj = {k: len(v) for k, v in by_key(prior_rows, "project").items()}
        for proj, rs in sorted(by_key(rows, "project").items(),
                               key=lambda x: -len(x[1])):
            people = len({r["person"] for r in rs})
            prev = prior_proj.get(proj, 0)
            arrow = "▲" if len(rs) > prev else ("▼" if len(rs) < prev else "▬")
            link = proj if proj == "(no project)" else _project_link(proj)
            projects.append(f"- {link} — {len(rs)} events, {people} people "
                      f"(prev {prev} {arrow})\n")

        flags_md = ["\n## Flags\n"]
        for slug in ([] if coverage_state == "provisional" else f.get("gaps", [])):
            flags_md.append(f"- **Gap**: {_person_link(_fname(ids.display_name(slug)))} — active in "
                      f"prior 4 weeks, no activity this week\n")
        for person, n in f.get("unmapped", []):
            flags_md.append(f"- **Unmapped**: `{person}` ({n} events) — add to "
                      f"roster.yaml, then `teammem reclaim`\n")
        for chat_id, n in f.get("unmapped_channels", []):
            cname = channel_names.get(chat_id)
            shown = f"**{cname}** (`{chat_id}`)" if cname else f"`{chat_id}`"
            flags_md.append(f"- **Unmapped channel**: {shown} ({n} messages) — map it "
                      f"in the matching projects.yaml provider channel list\n")
        for proj, slug, share in (
            [] if coverage_state == "provisional" else f.get("concentration", [])
        ):
            flags_md.append(f"- **Concentration**: {_project_link(proj)} — {int(share * 100)}% by "
                      f"{_person_link(_fname(ids.display_name(slug)))}\n")
        if provenance_incomplete:
            flags_md.append(
                "- Stored effective flags unavailable; no current-ledger flags are shown.\n"
            )
        if coverage_state == "provisional":
            flags_md.append(
                "Gap and concentration checks are deferred until the Friday checkpoint.\n"
            )
        elif not provenance_incomplete and not (
            f.get("gaps", []) or f.get("unmapped", [])
            or f.get("unmapped_channels", []) or f.get("concentration", [])
        ):
            flags_md.append("- none\n")

        if report:
            md += flags_md + tally + projects
        else:
            md += tally + projects + flags_md
        _write(vault_dir / "Work Journal" / f"{label}.md", "".join(md))
        files += 1

    # ---- Person pages: one folder per person, one file per week -------------
    # Full ledger history, not the render window: managed dirs are wiped every
    # render, so window-scoped week files would silently delete older weeks
    # from the vault. README.md is the per-person index — the forge web UI
    # auto-renders it when the folder is opened.
    all_rows = [r for rows in week_of.values() for r in rows]
    min_ts = conn.execute("SELECT min(ts) FROM events").fetchone()[0]
    hist_mondays = list(mondays)
    if min_ts:
        first = week_monday(date.fromisoformat(min_ts[:10]))
        span = (week_monday(today) - first).days // 7 + 1
        hist_mondays = [week_monday(today) - timedelta(weeks=i)
                        for i in range(max(span, weeks))]
    hist_week_of = {m: week_of[m] if m in week_of
                    else events_between(conn, *week_range(m))
                    for m in hist_mondays}

    def _person_week_body(person: str, mine: list[dict]) -> list[str]:
        body = []
        days = sorted({r["ts"][:10] for r in mine})
        entries = [(d, _summary("daily-person", f"{person}|{d}")) for d in days]
        entries = [(d, t) for d, t in entries if t]
        for d, t in entries:
            body.append(f"\n### {d}\n{t.rstrip()}\n")
        if entries:
            body.append("\n**Activity detail**\n")
        work = [r for r in mine if r["kind"] in WORK_KINDS]
        msgs = [r for r in mine if r["kind"] == "message"]
        body += [_line(r) for r in work[:MAX_WORK_LINES]]
        if len(work) > MAX_WORK_LINES:
            body.append(f"- …and {len(work) - MAX_WORK_LINES} more work items\n")
        if msgs:
            body.append(_msg_line(msgs))
        return body

    hist_rows = [r for rows in hist_week_of.values() for r in rows]
    rendered_people: set[str] = set()
    for person, rs in sorted(by_key(hist_rows, "person").items()):
        if person.startswith("_unmapped/"):
            continue
        name = _fname(ids.display_name(person))
        pdir = vault_dir / "Person" / name
        if pdir.exists():
            raise ValueError(f"filename collision in vault render: {pdir}")
        pdir.mkdir()
        weeks_mine = [(m, [r for r in hist_week_of[m] if r["person"] == person])
                      for m in hist_mondays]
        weeks_mine = [(m, mine) for m, mine in weeks_mine if mine]

        latest_m, latest_mine = weeks_mine[0]
        md = [f"---\nslug: {person}\ngenerated: {today.isoformat()}\n---\n",
              f"# {name}\n",
              f"\n## {_week_link(week_label(latest_m), up=2)}\n"]
        md += _person_week_body(person, latest_mine)
        md.append("\n## Weeks\n")
        for m, mine in weeks_mine:
            lbl = week_label(m)
            md.append(f"- [{lbl}]({quote(lbl)}.md) — {len(mine)} events\n")
        backlinks = person_meetings.pop(name, None)
        if backlinks is not None:
            if backlinks.key != person:
                raise meeting_render.MeetingWithheld(
                    backlinks.occurrence_ids,
                    "filename collision in vault render: "
                    f"Person/{name} claimed by both {person!r} and {backlinks.key!r}")
            md.append(meeting_render.meetings_section(backlinks.bullets))
        _write(pdir / "README.md", "".join(md))
        rendered_people.add(person)
        files += 1

        for m, mine in weeks_mine:
            lbl = week_label(m)
            wmd = [f"---\nslug: {person}\nweek: {m.isoformat()}\n"
                   f"generated: {today.isoformat()}\n---\n",
                   f"# {name} — {lbl}\n",
                   f"\n[{name}](README.md) · {_week_link(lbl, up=2)}\n"]
            wmd += _person_week_body(person, mine)
            _write(pdir / f"{lbl}.md", "".join(wmd))
            files += 1

    for name, backlinks in sorted(person_meetings.items()):
        pdir = vault_dir / "Person" / name
        if pdir.exists():
            raise meeting_render.MeetingWithheld(
                backlinks.occurrence_ids,
                f"filename collision in vault render: {pdir}")
        pdir.mkdir()
        _write(pdir / "README.md", meeting_render.person_page(backlinks, today))
        files += 1

    # ---- Project pages: one folder per project, one file per active week -----
    # Match Person/: full-ledger history survives the managed-dir wipe, while
    # each folder README gives the forge UI a useful latest-week landing page.
    def _project_person_link(person: str) -> str:
        if person.startswith("_unmapped/"):
            return f"`{person}`"
        name = _fname(ids.display_name(person))
        return _person_link(name, up=2) if person in rendered_people else name

    def _project_week_body(mine: list[dict]) -> list[str]:
        body = []
        for person, prs in sorted(by_key(mine, "person").items(),
                                  key=lambda x: -len(x[1])):
            link = _project_person_link(person)
            work = [r for r in prs if r["kind"] in WORK_KINDS]
            msgs = [r for r in prs if r["kind"] == "message"]
            body.append(f"\n### {link} — {_count('event', len(prs))} "
                        f"({_kind_summary(prs)})\n")
            body += [_line(r) for r in work[:MAX_WORK_LINES]]
            if len(work) > MAX_WORK_LINES:
                body.append(f"- …and {len(work) - MAX_WORK_LINES} more work items\n")
            if msgs:
                body.append(_msg_line(msgs))
        return body

    def _project_contributors(mine: list[dict]) -> list[str]:
        body = ["\n### Contributors\n"]
        for person, prs in sorted(by_key(mine, "person").items(),
                                  key=lambda x: -len(x[1])):
            link = _project_person_link(person)
            body.append(f"- {link} — {_count('event', len(prs))} "
                        f"({_kind_summary(prs)})\n")
        return body

    cur = conn.execute(
        "SELECT person, project, ts, kind, summary, refs, hash, source FROM events"
        " WHERE project IS NOT NULL AND project != '' ORDER BY ts DESC"
    )
    cols = [column[0] for column in cur.description]
    project_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    projects_by_week: dict[date, dict[str, list[dict]]] = {}
    for row in project_rows:
        monday = week_monday(date.fromisoformat(row["ts"][:10]))
        grouped = projects_by_week.setdefault(monday, {})
        grouped.setdefault(row["project"], []).append(row)
    project_mondays = sorted(projects_by_week, reverse=True)
    project_weeks: dict[str, list[tuple[date, list[dict]]]] = {}
    for proj in sorted(by_key(project_rows, "project")):
        if proj == "(no project)":
            continue
        project_weeks[proj] = [(m, projects_by_week[m][proj])
                               for m in project_mondays
                               if proj in projects_by_week[m]]

    for proj, weeks_mine in project_weeks.items():
        pdir = vault_dir / "Projects" / _project_fname(proj)
        if pdir.name == "README.md" or pdir.exists():
            raise ValueError(f"filename collision in vault render: {pdir}")
        pdir.mkdir()
        latest_m, latest_mine = weeks_mine[0]
        latest_label = week_label(latest_m)
        md = [f"---\nproject: {proj}\ngenerated: {today.isoformat()}\n---\n",
              f"# {proj}\n"]
        docs = [n for n in ("architecture", "summary")
                if (vault_dir / "Docs" / proj / f"{n}.md").is_file()]
        if docs:
            md.append("\n" + " · ".join(
                f"[{n.capitalize()}](../../Docs/{quote(proj)}/{n}.md)"
                for n in docs) + "\n")
        latest_cutoff = _evidence_cutoff(latest_mine)
        md.append(f"\n## [{latest_label}]({quote(latest_label)}.md)\n\n"
                  f"{_activity_summary(latest_mine)}\n\n"
                  f"{_evidence_notice(latest_cutoff)}\n")
        md += _project_contributors(latest_mine)
        md.append("\n## Weeks\n")
        for m, mine in weeks_mine:
            lbl = week_label(m)
            md.append(f"- [{lbl}]({quote(lbl)}.md) — "
                      f"{_activity_summary(mine, separator=', ')}\n")
        bullets = project_meetings.pop(proj, None)
        if bullets is not None:
            md.append(meeting_render.meetings_section(bullets))
        _write(pdir / "README.md", "".join(md))
        files += 1

        for m, mine in weeks_mine:
            lbl = week_label(m)
            cutoff = _evidence_cutoff(mine)
            navigation = f"\n[{meeting_render.escape_md_label(proj)}](README.md)"
            if m in week_of:
                navigation += (f" · [{lbl} — Team]"
                               f"(../../Work%20Journal/{quote(lbl)}.md)")
            wmd = [f"---\nproject: {proj}\nweek: {m.isoformat()}\n"
                   f"evidence_through: {cutoff[0]}\n"
                   f"cutoff_precision: {cutoff[1]}\n"
                   f"generated: {today.isoformat()}\n---\n",
                   f"# {proj} — {lbl}\n",
                   f"{navigation}\n\n",
                   f"{_activity_summary(mine)}\n\n",
                   f"{_evidence_notice(cutoff)}\n"]
            wmd += _project_week_body(mine)
            _write(pdir / f"{_fname(lbl)}.md", "".join(wmd))
            files += 1

    for proj, bullets in sorted(project_meetings.items()):
        pdir = vault_dir / "Projects" / _project_fname(proj)
        if pdir.name == "README.md" or pdir.exists():
            raise meeting_render.MeetingWithheld(
                project_owners[proj],
                f"filename collision in vault render: {pdir}")
        pdir.mkdir()
        _write(pdir / "README.md", meeting_render.project_page(proj, bullets, today))
        files += 1

    current_m = mondays[0]
    current = {
        proj: rows for proj, rows in by_key(week_of[current_m], "project").items()
        if proj != "(no project)"
    }
    pmd = [f"---\ngenerated: {today.isoformat()}\n---\n",
           "# Projects\n",
           f"\n## {week_label(current_m)}\n"]
    if current:
        current_rows = [row for rows in current.values() for row in rows]
        pmd.append(f"\n{_evidence_notice(_evidence_cutoff(current_rows))}\n")
    for proj, rows in sorted(current.items(), key=lambda item: (-len(item[1]), item[0])):
        pmd.append(f"- [{meeting_render.escape_md_label(proj)}]"
                   f"({quote(_project_fname(proj))}/README.md) — "
                   f"{_activity_summary(rows)}\n")
    if not current:
        pmd.append("- No mapped project activity.\n")
    future = {proj: weeks[0] for proj, weeks in project_weeks.items()
              if weeks[0][0] > current_m}
    if future:
        pmd.append("\n## Future-dated activity\n\nCheck source timestamps.\n")
        for proj, (latest_m, latest_rows) in sorted(future.items()):
            lbl = week_label(latest_m)
            folder = quote(_project_fname(proj))
            pmd.append(f"- [{meeting_render.escape_md_label(proj)}]({folder}/README.md) — "
                       f"[{lbl}]({folder}/{quote(lbl)}.md) · "
                       f"{_activity_summary(latest_rows)}\n")
    earlier = [proj for proj in project_weeks
               if proj not in current and proj not in future]
    if earlier:
        pmd.append("\n## Earlier activity\n")
        for proj in sorted(earlier, key=lambda p: (-project_weeks[p][0][0].toordinal(), p)):
            latest_m, latest_rows = project_weeks[proj][0]
            lbl = week_label(latest_m)
            folder = quote(_project_fname(proj))
            pmd.append(f"- [{meeting_render.escape_md_label(proj)}]({folder}/README.md) — latest "
                       f"[{lbl}]({folder}/{quote(lbl)}.md) · "
                       f"{_activity_summary(latest_rows)}\n")
    if meeting_projects:
        pmd.append("\n## Discussed in meetings\n")
        for proj in meeting_projects:
            pmd.append(f"- [{meeting_render.escape_md_label(proj)}]"
                       f"({quote(_project_fname(proj))}/README.md)\n")
    _write(vault_dir / "Projects" / "README.md", "".join(pmd))
    files += 1

    for relative, content in meeting_render.meeting_files(notes, today).items():
        path = vault_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, content)
        files += 1

    label = week_label(mondays[0])
    (vault_dir / "README.md").write_text(
        f"# Team Vault\n\nGENERATED — do not edit Person/, Projects/, Work Journal/, "
        f"or Meetings/ by hand; every render regenerates them from the ledger.\n\n"
        f"- generated: {today.isoformat()}\n- weeks rendered: {weeks}\n"
        f"- events in window: {len(all_rows)}\n"
        f"- meetings: {len(notes)} — [Meetings](Meetings/README.md)\n"
        f"- current: [{label}](Work%20Journal/{quote(label)}.md)\n")
    return {"files": files + 1, "week_label": label}


def _managed_files(root: Path) -> set[str]:
    found: set[str] = set()
    for m in MANAGED:
        p = root / m
        if p.is_dir():
            found |= {f.relative_to(root).as_posix()
                      for f in p.rglob("*") if f.is_file()}
        elif p.is_file():
            found.add(m)
    return found


def verify_vault(conn: sqlite3.Connection, ids: IdentityMaps, vault_dir: Path,
                 today: date, weeks: int = 4,
                 channel_names: dict | None = None, *,
                 semantic: bool = False) -> dict:
    """Re-render into a temp tree and diff managed paths against vault_dir.
    Never writes to vault_dir; detects hand-edits under managed paths and
    renderer nondeterminism.

    The diff is syntactic: it proves the vault matches what the renderer would
    write right now, and so it cannot see a renderer that is consistently
    wrong. `semantic=True` also runs `verify_meeting_semantics` and returns its
    findings under a fourth `"semantic"` key. The default is off so the
    three-key return shape every existing caller reads stays exactly as it was.
    """
    with tempfile.TemporaryDirectory() as td:
        expected_dir = Path(td) / "vault"
        docs_dir = vault_dir / "Docs"
        if docs_dir.is_dir():
            shutil.copytree(docs_dir, expected_dir / "Docs", symlinks=True)
        render_vault(conn, ids, expected_dir, today, weeks=weeks,
                     channel_names=channel_names)
        expected, actual = _managed_files(expected_dir), _managed_files(vault_dir)
        out = {
            "missing": sorted(expected - actual),
            "unexpected": sorted(actual - expected),
            "differing": sorted(
                rel for rel in expected & actual
                if (expected_dir / rel).read_bytes()
                != (vault_dir / rel).read_bytes()),
        }
    if semantic:
        out["semantic"] = verify_meeting_semantics(conn, ids, vault_dir)
    return out


# ---- Semantic verification of the meetings projection -----------------------
# `verify_vault` re-renders and diffs, which proves the vault matches what the
# renderer would write right now. It cannot see a renderer that is consistently
# wrong: a bug that always omits a project backlink survives the diff. These
# checks read the vault on disk and hold it against the ledger through
# `meeting_store`, so the renderer is never its own witness.
#
# Findings name a path and an invariant. They never carry transcript text, an
# audio path or a digest — not even when reporting that one of those leaked.

MEETING_INVARIANTS = (
    "meeting.leakage.forbidden-content",
    "meeting.link.resolves",
    "meeting.note.heading-order",
    "meeting.note.one-per-occurrence",
    "meeting.note.per-person-summaries",
    "meeting.note.revision-current",
    "meeting.person.backlinks",
    "meeting.project.backlinks",
    "meeting.week-index.membership",
)
# Individual short transcript lines ("Yes.") legitimately recur in a summary,
# so only a complete transcript may make them a probe on their own.
_MIN_LEAKED_TEXT = 24
_Probe = tuple[str, str]
_ProbeIndex = tuple[dict[str, list[_Probe]], list[_Probe]]


def _md_links(text: str) -> list[str]:
    """Every inline-link destination in `text`.

    Hand-written rather than a regular expression: a CommonMark destination may
    carry balanced parentheses to any depth, an expression can only count to a
    fixed one, and a destination cut short at the wrong parenthesis reads as a
    link to a file that does not exist.
    """
    def escaped(index: int) -> bool:
        backslashes = 0
        while index > 0 and text[index - 1] == "\\":
            backslashes += 1
            index -= 1
        return backslashes % 2 == 1

    found, cursor = [], 0
    while (opened := text.find("[", cursor)) >= 0:
        if escaped(opened):
            cursor = opened + 1
            continue
        closed = text.find("]", opened + 1)
        while closed >= 0 and escaped(closed):
            closed = text.find("]", closed + 1)
        if closed < 0:
            break
        if text[closed + 1:closed + 2] != "(":
            cursor = opened + 1
            continue
        depth, index = 1, closed + 2
        while index < len(text) and depth:
            character = text[index]
            if character.isspace():
                break
            depth += (character == "(") - (character == ")")
            index += 1
        if depth:
            cursor = closed + 2
            continue
        found.append(text[closed + 2:index - 1])
        cursor = index
    return found


def _unyaml(value: str) -> str:
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _front_matter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[4:end + 1].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = _unyaml(value.strip())
    return out


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(meeting_render.DISPLAY_TZ)


def _meeting_facts(meeting: dict, ids: IdentityMaps) -> dict:
    """What the vault must say about one meeting, read from the ledger alone.

    Derived here rather than taken from `meeting_render.collect_notes`, so a
    renderer that drops a participant or a project is caught instead of
    confirmed.
    """
    start = _instant(meeting["started_at"])
    monday = week_monday(start.date())
    week = week_label(monday)
    name = {}
    attended = []
    for participant in meeting["participants"]:
        key = _meeting_person(ids, participant["person_id"],
                              participant["display_name"])[1]
        name[participant["person_id"]] = key
        if participant["attendance"]:
            attended.append(participant["person_id"])
    spoke = {segment["speaker"]["person_id"] for segment in meeting["transcript"]
             if segment["speaker"]["kind"] == "person"}
    return {
        "title": meeting["title"],
        "revision": meeting["revision"],
        "week": week,
        # Same format as `meeting_render.collect_notes`, different arguments:
        # these come from the ledger, never from a rendered MeetingNote, so a
        # renderer that files a note under the wrong name is still caught.
        "rel": PurePosixPath(
            meeting_render.MEETINGS_DIR, week,
            meeting_render.NOTE_FILE_NAME.format(
                start=start,
                topic=_project_fname(meeting["title"]),
                short=short_occurrence_id(meeting["occurrence_id"]))),
        "person_folders": {_fname(name[pid]) for pid in attended},
        "project_folders": {_project_fname(project["name"])
                            for project in meeting["projects"]},
        "speakers": [name[entry["person_id"]] for entry in meeting["person_summaries"]
                     if entry["person_id"] in spoke and entry["person_id"] in attended],
    }


def _probe_index(values: dict[_Probe, None], probe_len: int) -> _ProbeIndex:
    indexed: dict[str, list[_Probe]] = {}
    unindexed: list[_Probe] = []
    for pair in values:
        value = pair[1]
        if not value:                             # would match every file
            continue
        if len(value) >= probe_len:
            indexed.setdefault(value[:probe_len], []).append(pair)
        else:
            unindexed.append(pair)
    return indexed, unindexed


def _compact_whitespace(text: str) -> str:
    """Remove Unicode whitespace and preserve every other code point."""
    return "".join(character for character in text if not character.isspace())


def _only_turn_scaffolding(gap: str) -> bool:
    """Only Markdown markers, list numbers or colon-terminated speaker labels.

    Applied to gaps between already matched whole turns, never spoken text.
    This also supports labels on their own lines without erasing a spoken turn
    that itself happens to end in a colon.
    """
    for line in gap.splitlines():
        line = re.sub(r"^[\s>*_`~#+-]*(?:\d+[.)]\s*)?", "", line)
        line = line.lstrip(" \t>*_`~#+-")
        line = line.rstrip(" \t>*_`~#+-")
        line = re.sub(r"^[\w][\w .'-]{0,60}(?:\*\*|__)?[:：](?:\*\*|__)?\s*$", "", line)
        if line:
            return False
    return True


def _ordered_turns_present(text: str, turns: tuple[re.Pattern, ...]) -> bool:
    # A spoken turn can also occur inside the next speaker label. Keep every
    # viable endpoint instead of committing to that first (possibly dead-end)
    # occurrence. Deduplication bounds state to text offsets per turn, rather
    # than exponentially many paths through repeated labels/turns.
    def placements(turn):
        start = 0
        while (match := turn.search(text, start)) is not None:
            yield match
            # finditer skips overlapping phrases (e.g. both "A A" placements
            # in "A A A"), even when the later one is the viable spoken turn.
            start = match.start() + 1

    ends = {match.end() for match in placements(turns[0])}
    for turn in turns[1:]:
        if not ends:
            return False
        ends = {match.end() for match in placements(turn)
                if any(end <= match.start()
                       and _only_turn_scaffolding(text[end:match.start()])
                       for end in ends)}
    return bool(ends)


def _forbidden_values(conn: sqlite3.Connection, occurrence_ids
                      ) -> tuple[int, _ProbeIndex, _ProbeIndex, list]:
    """A probe index of (kind, value) for everything that must never reach the vault.

    Every stored revision, not only the current one: a superseded revision's
    transcript is exactly as confidential as the one being rendered.

    Raw values retain the existing exact long-segment and audio checks. A
    separate compact domain has one complete transcript per revision with only
    Unicode whitespace removed. Keeping the domains separate lets each file be
    indexed once in the spelling appropriate to that probe. All-short revisions
    also retain ordered whole-turn patterns for Markdown/speaker gap checks.
    """
    # ~4 words of prose: long enough that vault text does not collide with the
    # start of a transcript line, short enough to index all but a stray value.
    probe_len = 32
    raw_values: dict[_Probe, None] = {}           # deduplicated, insertion order
    compact_values: dict[_Probe, None] = {}
    ordered_turns = []
    for occurrence_id in occurrence_ids:
        for entry in list_meeting_revisions(conn, occurrence_id):
            meeting = get_meeting(conn, occurrence_id, revision=entry["revision"])
            if meeting is None:
                continue
            for segment in meeting["transcript"]:
                if len(segment["text"]) >= _MIN_LEAKED_TEXT:
                    raw_values[("transcript text", segment["text"])] = None
            complete = _compact_whitespace(
                "".join(segment["text"] for segment in meeting["transcript"])
            )
            compact_values[("transcript text", complete)] = None
            texts = [_compact_whitespace(segment["text"]) for segment in meeting["transcript"]]
            if texts and all(0 < len(value) < _MIN_LEAKED_TEXT for value in texts):
                # Underscores are identifier characters, except for Markdown
                # emphasis opening/closing at an outer lexical boundary.
                # Thus _Yes_ is spoken text, while prefix_Yes_suffix is not.
                ordered_turns.append(tuple(re.compile(
                    (r"(?:(?<!\w)|(?<=\W_)|(?<=\W__)|(?<=\W___)|(?<=^_)|(?<=^__)|(?<=^___))"
                     if _word_character(value[0]) else "")
                    + r"\s*".join(re.escape(character) for character in value)
                    + (r"(?:(?!\w)|(?=_{1,3}(?:\W|$)))"
                       if _word_character(value[-1]) else "")
                ) for value in texts))
            audio = meeting["audio"]
            if audio:
                raw_values[("audio store path", audio["stored_path"])] = None
                raw_values[("audio digest", audio["sha256"])] = None
                if len(audio["filename"]) >= 5:
                    raw_values[("audio file name", audio["filename"])] = None
    return (probe_len, _probe_index(raw_values, probe_len),
            _probe_index(compact_values, probe_len), ordered_turns)


def _probe_candidates(text: str, probe_len: int, index: _ProbeIndex) -> set[_Probe]:
    indexed, unindexed = index
    candidates = set(unindexed)
    # In chunks, so a large file in the vault costs bounded memory instead of
    # one window per character all at once. Each chunk keeps the tail a probe
    # needs to be found across the seam.
    for start in range(0, max(len(text) - probe_len + 1, 1), 1 << 16):
        chunk = text[start:start + (1 << 16) + probe_len - 1]
        windows = {chunk[i:i + probe_len]
                   for i in range(len(chunk) - probe_len + 1)}
        for probe in windows & indexed.keys():
            candidates.update(indexed[probe])
    return candidates


def _compact_with_breaks(text: str) -> tuple[str, set[int]]:
    """Compact `text` and retain offsets at which whitespace was removed."""
    compact = []
    breaks = set()
    for character in text:
        if character.isspace():
            breaks.add(len(compact))
        else:
            compact.append(character)
    return "".join(compact), breaks


def _word_character(character: str) -> bool:
    return character == "_" or character.isalnum()


def _contains_compact_probe(text: str, breaks: set[int], value: str) -> bool:
    """Whether compact `text` contains a complete-transcript probe.

    Complete transcripts need lexical boundaries so `Yes` is not found in
    `Yesterday`, including when short turns combine into a longer probe.
    A whitespace break is a boundary even though compaction
    removed it. Search continues after an invalid occurrence because a later
    standalone occurrence may still be a leak.
    """
    start = text.find(value)
    while start >= 0:
        end = start + len(value)
        left_ok = (not _word_character(value[0]) or start == 0
                   or not _word_character(text[start - 1]) or start in breaks)
        right_ok = (not _word_character(value[-1]) or end == len(text)
                    or not _word_character(text[end]) or end in breaks)
        if left_ok and right_ok:
            return True
        start = text.find(value, start + 1)
    return False


def _vault_pages(vault_dir: Path, top: str) -> list[tuple[str, str, str]]:
    """(folder, relative path, text) for every `<top>/<folder>/README.md` page."""
    base = vault_dir / top
    pages = []
    for path in sorted(base.rglob("README.md")) if base.is_dir() else []:
        rel = path.relative_to(vault_dir).as_posix()
        folder = path.parent.relative_to(base).as_posix()
        if folder == ".":                      # the section index, not a page
            continue
        pages.append((folder, rel, path.read_text(encoding="utf-8")))
    return pages


def _meeting_expectations(conn: sqlite3.Connection, ids: IdentityMaps) -> dict[str, dict]:
    """What the vault must say about every current occurrence, from the ledger."""
    expected = {}
    for row in list_meetings(conn):
        meeting = get_meeting(conn, row["occurrence_id"])
        if meeting is not None:                # deleted between the two reads
            expected[meeting["occurrence_id"]] = _meeting_facts(meeting, ids)
    return expected


def _meeting_pages(expected: dict[str, dict]) -> dict[str, tuple[str, ...]]:
    """Vault page -> the occurrences answerable for the meeting content on it.

    Every page the meetings projection writes or writes into: the notes, the
    indexes above them, and the person and project pages their backlinks land
    on. A page outside this map carries nothing a meeting put there.
    """
    pages: dict[str, list[str]] = {}
    index = str(PurePosixPath(meeting_render.MEETINGS_DIR, "README.md"))
    for occurrence_id, facts in sorted(expected.items()):
        rel = facts["rel"]
        touched = [str(rel), str(rel.parent / "README.md"), index]
        touched += [str(PurePosixPath("Person", folder, "README.md"))
                    for folder in sorted(facts["person_folders"])]
        touched += [str(PurePosixPath("Projects", folder, "README.md"))
                    for folder in sorted(facts["project_folders"])]
        for page in touched:
            pages.setdefault(page, []).append(occurrence_id)
    return {page: tuple(owners) for page, owners in sorted(pages.items())}


def verify_meeting_semantics(conn: sqlite3.Connection, ids: IdentityMaps,
                             vault_dir: Path) -> list[dict]:
    """Check the rendered vault against the meeting invariants.

    Returns a sorted list of `{"invariant", "path", "detail", "occurrence_id"}`
    findings; an empty list means the vault satisfies every invariant in
    `MEETING_INVARIANTS`. Reads the vault and the ledger and writes nothing.

    `occurrence_id` is the current occurrence a finding is the fault of, or
    `None` when it is the fault of the projection as a whole — a missing index,
    a stray note the ledger no longer holds, leaked content that could have come
    from any revision. A caller publishing one occurrence may skip another
    occurrence's findings; it may never skip an unattributed one.
    """
    findings: list[dict] = []

    def report(invariant: str, path, detail: str, occurrence_id: str | None = None) -> None:
        findings.append({"invariant": invariant, "path": str(path), "detail": detail,
                         "occurrence_id": occurrence_id})

    expected = _meeting_expectations(conn, ids)

    # ---- one canonical file per current occurrence --------------------------
    one = "meeting.note.one-per-occurrence"
    root = vault_dir / meeting_render.MEETINGS_DIR
    on_disk: dict[str, list[tuple[PurePosixPath, str, dict]]] = {}
    for path in sorted(root.rglob("*.md")) if root.is_dir() else []:
        if path.name == "README.md":
            continue
        rel = PurePosixPath(path.relative_to(vault_dir).as_posix())
        text = path.read_text(encoding="utf-8")
        front = _front_matter(text)
        occurrence_id = front.get("occurrence_id")
        if not occurrence_id:
            report(one, rel, "the note declares no occurrence_id in its front matter")
            continue
        on_disk.setdefault(occurrence_id, []).append((rel, text, front))

    canonical: dict[str, tuple[PurePosixPath, str, dict]] = {}
    for occurrence_id, entries in sorted(on_disk.items()):
        facts = expected.get(occurrence_id)
        if facts is None:
            for rel, _, _ in entries:
                # Attributed to an occurrence that is not current, so the
                # normalisation below turns it into a vault-wide finding.
                report(one, rel,
                       "a note for an occurrence the ledger does not hold as current",
                       occurrence_id)
            continue
        chosen = next((e for e in entries if e[0] == facts["rel"]), None)
        if chosen is None:
            chosen = entries[0]
            report(one, chosen[0], f"the note is not at its canonical path {facts['rel']}",
                   occurrence_id)
        for rel, _, _ in entries:
            if rel != chosen[0]:
                report(one, rel, "a duplicate of a note already rendered for this occurrence",
                       occurrence_id)
        canonical[occurrence_id] = chosen
    for occurrence_id, facts in sorted(expected.items()):
        if occurrence_id not in canonical:
            report(one, facts["rel"], "no canonical note for a current occurrence",
                   occurrence_id)

    # ---- per-note structure -------------------------------------------------
    for occurrence_id, (rel, text, front) in sorted(canonical.items()):
        facts = expected[occurrence_id]
        lines = text.splitlines()
        if [l for l in lines if l.startswith("# ")] != [f"# {facts['title']}"]:
            report("meeting.note.heading-order", rel,
                   "the title heading is not the meeting topic", occurrence_id)
        sections = tuple(l[3:].strip() for l in lines if l.startswith("## "))
        if sections != meeting_render.NOTE_HEADINGS:
            report("meeting.note.heading-order", rel,
                   "the sections are not the nine canonical headings in order",
                   occurrence_id)

        summaries = [l[4:].strip() for l in lines if l.startswith("### ")]
        for who in [w for w in summaries if w not in facts["speakers"]]:
            report("meeting.note.per-person-summaries", rel,
                   f"a per-person summary for a participant with no reconciled speech: {who}",
                   occurrence_id)
        for who in [w for w in facts["speakers"] if w not in summaries]:
            report("meeting.note.per-person-summaries", rel,
                   f"no per-person summary for a participant who spoke: {who}",
                   occurrence_id)
        if sorted(summaries) == sorted(facts["speakers"]) and summaries != facts["speakers"]:
            report("meeting.note.per-person-summaries", rel,
                   "the per-person summaries are not in the meeting's own order",
                   occurrence_id)

        if front.get("revision") != str(facts["revision"]):
            report("meeting.note.revision-current", rel,
                   f"the note renders revision {front.get('revision')}; "
                   f"the current revision is {facts['revision']}", occurrence_id)

    # ---- weekly index membership -------------------------------------------
    index_invariant = "meeting.week-index.membership"
    # Which occurrence owns each canonical note, for the findings that name one.
    owner = {str(rel): occurrence_id
             for occurrence_id, (rel, _, _) in canonical.items()}
    by_week: dict[PurePosixPath, set[str]] = {}
    for rel, _, _ in canonical.values():
        by_week.setdefault(rel.parent, set()).add(rel.name)
    for parent, names in sorted(by_week.items()):
        rel = parent / "README.md"
        page = vault_dir / rel
        if not page.is_file():
            report(index_invariant, rel, "the week has notes but no index")
            continue
        listed = {unquote(t) for t in _md_links(page.read_text(encoding="utf-8"))
                  if not t.startswith("../")}
        for name in sorted(names - listed):
            report(index_invariant, rel, f"the week index omits a canonical note: {name}",
                   owner.get(str(parent / name)))
        for name in sorted(listed - names):
            report(index_invariant, rel,
                   f"the week index lists a file that is not a canonical note: {name}")

    top_rel = PurePosixPath(meeting_render.MEETINGS_DIR, "README.md")
    if not (vault_dir / top_rel).is_file():
        if by_week:
            report(index_invariant, top_rel, "the meetings index is missing")
    else:
        text = (vault_dir / top_rel).read_text(encoding="utf-8")
        listed = {unquote(t).split("/")[0] for t in _md_links(text)
                  if t.endswith("/README.md") and not t.startswith("../")}
        weeks = {parent.name for parent in by_week}
        for week in sorted(weeks - listed):
            report(index_invariant, top_rel, f"the meetings index omits a week: {week}")
        for week in sorted(listed - weeks):
            report(index_invariant, top_rel,
                   f"the meetings index lists a week with no canonical note: {week}")

    # ---- all-and-only backlinks --------------------------------------------
    for top, key, invariant, absent, present in (
        ("Person", "person_folders", "meeting.person.backlinks",
         "the page of an actual participant does not link the meeting",
         "the page of someone who did not attend links the meeting"),
        ("Projects", "project_folders", "meeting.project.backlinks",
         "the page of a discussed project does not link the meeting",
         "the page of a project the meeting did not discuss links it"),
    ):
        pages = _vault_pages(vault_dir, top)
        folders = {folder for folder, _, _ in pages}
        for occurrence_id, (rel, _, _) in sorted(canonical.items()):
            tail = f"{quote(rel.parent.name)}/{quote(rel.name)}"
            wanted = expected[occurrence_id][key]
            for folder, page_rel, text in pages:
                links = tail in text
                if folder in wanted and not links:
                    report(invariant, page_rel, absent, occurrence_id)
                elif folder not in wanted and links:
                    report(invariant, page_rel, present, occurrence_id)
            for folder in sorted(wanted - folders):
                report(invariant, PurePosixPath(top, folder, "README.md"),
                       "no page at all for a meeting backlink", occurrence_id)

    # ---- every link a meeting is answerable for resolves --------------------
    # Scoped to the meeting notes and the meeting links on the pages they
    # backlink into. A person page keeps full history while the Work Journal
    # keeps four weeks, so its older week links dangle by design; those are no
    # meeting's fault, no meeting can repair them, and a finding no occurrence
    # owns blocks every publication, so this checker does not raise them.
    root_dir = vault_dir.resolve()
    prefix = meeting_render.MEETINGS_DIR + "/"
    by_rel = {str(facts["rel"]): occurrence_id
              for occurrence_id, facts in expected.items()}
    by_short = {short_occurrence_id(occurrence_id): occurrence_id
                for occurrence_id in expected}
    for name, owners in sorted(_meeting_pages(expected).items()):
        path = vault_dir / name
        if not path.is_file():        # a page that is missing is another finding
            continue
        inside = name.startswith(prefix)
        seen: set[str] = set()
        for target in _md_links(path.read_text(encoding="utf-8")):
            target = target.split("#", 1)[0]
            if not target or target in seen or "://" in target:
                continue
            if target.startswith(("mailto:", "/", "#")):
                continue
            seen.add(target)
            resolved = (path.parent / unquote(target)).resolve()
            within = resolved.is_relative_to(root_dir)
            rel = resolved.relative_to(root_dir).as_posix() if within else None
            # Who the link belongs to: the occurrence whose note it names, else
            # the sole occupant of the page it sits on.
            blamed = by_rel.get(rel)
            if blamed is None and rel is not None and rel.startswith(prefix):
                stem = PurePosixPath(rel).stem
                if stem.endswith("]") and "[" in stem:
                    blamed = by_short.get(stem.rsplit("[", 1)[1][:-1])
            if blamed is None:
                if not inside:
                    continue          # not a link any meeting put there
                blamed = owner.get(name) or (owners[0] if len(owners) == 1 else None)
            if not within or not resolved.exists():
                report("meeting.link.resolves", name,
                       f"a relative link does not resolve inside the vault: {target}",
                       blamed)

    # ---- nothing forbidden anywhere under the vault -------------------------
    probe_len, raw_index, compact_index, ordered_turns = _forbidden_values(conn, sorted(expected))
    if any(raw_index) or any(compact_index):
        for path in sorted(vault_dir.rglob("*")):
            rel_parts = path.relative_to(vault_dir).parts
            if not path.is_file() or ".git" in rel_parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            leaked = {
                kind for kind, value in _probe_candidates(text, probe_len, raw_index)
                if value in text
            }
            if any(compact_index):
                compact, breaks = _compact_with_breaks(text)
                leaked.update(
                    kind for kind, value in _probe_candidates(
                        compact, probe_len, compact_index
                    ) if _contains_compact_probe(compact, breaks, value)
                )
            if any(_ordered_turns_present(text, turns) for turns in ordered_turns):
                leaked.add("transcript text")
            for kind in sorted(leaked):
                report("meeting.leakage.forbidden-content", "/".join(rel_parts),
                       f"{kind} from the ledger appears in this file")

    # An occurrence the ledger does not hold as current can never be published,
    # so a finding attributed to one could never block anything. Those, and every
    # finding nothing could attribute, belong to the projection as a whole.
    for finding in findings:
        if finding["occurrence_id"] not in expected:
            finding["occurrence_id"] = None
    return sorted(findings, key=lambda f: (f["invariant"], f["path"], f["detail"]))


# ---- Rendering for a caller that will publish -------------------------------


def _blame_findings(conn: sqlite3.Connection, ids: IdentityMaps,
                    findings: list[dict], withheld: dict[str, str]
                    ) -> dict[str, list[dict]]:
    """The occurrences that could be withheld to clear these findings.

    `verify_meeting_semantics` attributes a finding only when one current
    occurrence is at fault, and reports leakage against the projection as a
    whole because a leaked value could come from any stored revision. For
    withholding, the file a finding names is enough: a note belongs to the
    occurrence its short id spells, and a person or project page belongs to the
    occurrences whose backlinks reach it.
    """
    blamed: dict[str, list[dict]] = {}
    shorts: dict[str, str] | None = None
    pages: dict[str, tuple[str, ...]] | None = None
    for finding in findings:
        path = finding["path"]
        owners = [finding["occurrence_id"]] if finding["occurrence_id"] else []
        if not owners and path.startswith(meeting_render.MEETINGS_DIR + "/"):
            if shorts is None:
                shorts = {short_occurrence_id(row["occurrence_id"]): row["occurrence_id"]
                          for row in list_meetings(conn)}
            stem = PurePosixPath(path).stem
            if stem.endswith("]") and "[" in stem:
                owners = [o for o in (shorts.get(stem.rsplit("[", 1)[1][:-1]),) if o]
        if not owners:
            if pages is None:
                pages = _meeting_pages(_meeting_expectations(conn, ids))
            owners = list(pages.get(path, ()))
        for occurrence_id in owners:
            if occurrence_id not in withheld:
                blamed.setdefault(occurrence_id, []).append(finding)
    return blamed


def render_vault_verified(conn: sqlite3.Connection, ids: IdentityMaps,
                          vault_dir: Path, today: date, weeks: int = 4,
                          channel_names: dict | None = None) -> dict:
    """Render and verify one coherent ledger view, returning its byte manifest."""
    with ledger_read_view(conn) as view:
        return _render_vault_verified(view, ids, vault_dir, today, weeks, channel_names)


def _render_vault_verified(conn: sqlite3.Connection, ids: IdentityMaps,
                           vault_dir: Path, today: date, weeks: int,
                           channel_names: dict | None) -> dict:
    """Render for a caller that is going to commit and push the result.

    `render_vault` on its own leaves meeting content in the vault that no check
    has read, and the daily pipeline commits and pushes whatever it finds there.
    Refusing would be worse than the disease: one bad note would stop the vault
    publishing anything. So the offending occurrence is withheld instead — left
    out of the projection and named — while every other page renders exactly as
    it always did, and is committed and pushed as it always was. The occurrence
    stays retryable: nothing is deleted from the ledger, and the next render
    files it as soon as a corrected revision lands.

    Returns `render_vault`'s dict, plus `withheld` (occurrence -> what it failed),
    `withheld_findings` (the structured semantic findings behind those entries),
    and `findings`: the findings no occurrence could be withheld for. Those name
    vault content the meetings projection does not own and no render can remove,
    so a caller that pushes must not.
    """
    withheld: dict[str, str] = {}
    withheld_findings: dict[str, list[dict]] = {}
    while True:
        out = render_vault(conn, ids, vault_dir, today, weeks=weeks,
                           channel_names=channel_names, withhold=withheld)
        withheld = out["withheld"]
        findings = verify_meeting_semantics(conn, ids, vault_dir)
        blamed = _blame_findings(conn, ids, findings, withheld)
        if blamed:
            withheld_findings.update(blamed)
            withheld.update({
                occurrence_id: "; ".join(
                    f"{finding['invariant']} on {finding['path']}"
                    for finding in occurrence_findings
                )
                for occurrence_id, occurrence_findings in blamed.items()
            })
            continue
        out["withheld_findings"] = withheld_findings
        out["findings"] = [f for f in findings
                           if f["occurrence_id"] not in withheld]
        # The cooperative vault lock spans verification, this manifest and Git.
        # Git must reproduce every verified file, including companion pages,
        # with identical bytes after ignore rules, clean filters and hooks.
        out["verified_manifest"] = {
            path.relative_to(vault_dir).as_posix(): path.read_bytes()
            for path in sorted(vault_dir.rglob("*"))
            if ".git" not in path.relative_to(vault_dir).parts and path.is_file()
        }
        return out
