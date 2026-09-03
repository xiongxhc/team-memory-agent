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
from .store import get_summary

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
    return f"[{name}]({'../' * up}Person/{quote(name)}/README.md)"


def _project_link(proj: str, up: int = 1) -> str:
    return f"[{proj}]({'../' * up}Projects/{quote(_project_fname(proj))}/README.md)"


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
                                extra: set[str] = frozenset()) -> None:
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
            raise ValueError(
                f"filename collision in vault render: {project!r} and {seen.get(key)!r}"
            )
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
                 channel_names: dict | None = None) -> dict:
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
    notes = meeting_render.collect_notes(conn, naming=naming, today=today)
    person_meetings = meeting_render.person_backlinks(notes)
    project_meetings = meeting_render.project_backlinks(notes)
    meeting_projects = sorted(project_meetings)
    _validate_project_filenames(conn, set(meeting_projects))
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
                raise ValueError(
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
            raise ValueError(f"filename collision in vault render: {pdir}")
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
            navigation = f"\n[{proj}](README.md)"
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
            raise ValueError(f"filename collision in vault render: {pdir}")
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
        pmd.append(f"- [{proj}]({quote(_project_fname(proj))}/README.md) — "
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
            pmd.append(f"- [{proj}]({folder}/README.md) — "
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
            pmd.append(f"- [{proj}]({folder}/README.md) — latest "
                       f"[{lbl}]({folder}/{quote(lbl)}.md) · "
                       f"{_activity_summary(latest_rows)}\n")
    if meeting_projects:
        pmd.append("\n## Discussed in meetings\n")
        for proj in meeting_projects:
            pmd.append(f"- [{proj}]({quote(_project_fname(proj))}/README.md)\n")
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
# Shorter transcript lines ("Yes.") legitimately recur in a summary; matching
# them would make every vault look like a leak.
_MIN_LEAKED_TEXT = 24
# CommonMark inline links, tolerating one level of balanced parens in a target.
_MD_LINK = re.compile(r"\[[^\]]*\]\(([^()\s]*(?:\([^()]*\)[^()\s]*)*)\)")


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


def _forbidden_values(conn: sqlite3.Connection, occurrence_ids) -> list[tuple[str, str]]:
    """(kind, value) for everything that must never reach the vault.

    Every stored revision, not only the current one: a superseded revision's
    transcript is exactly as confidential as the one being rendered.
    """
    values: list[tuple[str, str]] = []
    for occurrence_id in occurrence_ids:
        for entry in list_meeting_revisions(conn, occurrence_id):
            meeting = get_meeting(conn, occurrence_id, revision=entry["revision"])
            if meeting is None:
                continue
            values += [("transcript text", segment["text"])
                       for segment in meeting["transcript"]
                       if len(segment["text"]) >= _MIN_LEAKED_TEXT]
            audio = meeting["audio"]
            if audio:
                values.append(("audio store path", audio["stored_path"]))
                values.append(("audio digest", audio["sha256"]))
                if len(audio["filename"]) >= 5:
                    values.append(("audio file name", audio["filename"]))
    return values


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

    expected = {}
    for row in list_meetings(conn):
        meeting = get_meeting(conn, row["occurrence_id"])
        if meeting is not None:                # deleted between the two reads
            expected[meeting["occurrence_id"]] = _meeting_facts(meeting, ids)

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
        listed = {unquote(t) for t in _MD_LINK.findall(page.read_text(encoding="utf-8"))
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
        listed = {unquote(t).split("/")[0] for t in _MD_LINK.findall(text)
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

    # ---- every relative link resolves inside the vault ----------------------
    root_dir = vault_dir.resolve()
    for name in sorted(_managed_files(vault_dir)):
        if not name.endswith(".md"):
            continue
        path = vault_dir / name
        seen: set[str] = set()
        for target in _MD_LINK.findall(path.read_text(encoding="utf-8")):
            target = target.split("#", 1)[0]
            if not target or target in seen or "://" in target:
                continue
            if target.startswith(("mailto:", "/", "#")):
                continue
            seen.add(target)
            resolved = (path.parent / unquote(target)).resolve()
            if not resolved.is_relative_to(root_dir) or not resolved.exists():
                report("meeting.link.resolves", name,
                       f"a relative link does not resolve inside the vault: {target}",
                       owner.get(name))

    # ---- nothing forbidden anywhere under the vault -------------------------
    forbidden = _forbidden_values(conn, sorted(expected))
    if forbidden:
        for path in sorted(vault_dir.rglob("*")):
            rel_parts = path.relative_to(vault_dir).parts
            if not path.is_file() or ".git" in rel_parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for kind in sorted({k for k, value in forbidden if value and value in text}):
                report("meeting.leakage.forbidden-content", "/".join(rel_parts),
                       f"{kind} from the ledger appears in this file")

    # An occurrence the ledger does not hold as current can never be published,
    # so a finding attributed to one could never block anything. Those, and every
    # finding nothing could attribute, belong to the projection as a whole.
    for finding in findings:
        if finding["occurrence_id"] not in expected:
            finding["occurrence_id"] = None
    return sorted(findings, key=lambda f: (f["invariant"], f["path"], f["detail"]))
