"""Regenerate the team vault from the ledger.
The vault is a projection:
managed dirs (Person/, Projects/, Areas/, Work Journal/, README.md) are deleted and
rewritten on every render; anything else in the vault is never touched.
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
from pathlib import Path
from urllib.parse import quote

from .identity import IdentityMaps
from .queries import (by_key, events_between, flags, ref_url,
                      week_label, week_monday, week_range)
from .store import get_summary, weekly_commit_counts

MANAGED = ("Person", "Projects", "Areas", "Work Journal", "README.md")
MAX_WORK_LINES = 12   # work bullets per person per week (project and person week files)
WORK_KINDS = ("commit", "pr", "mr", "issue", "repo", "comment", "journal-highlight")
_FLAG_KEYS = frozenset({"gaps", "unmapped", "unmapped_channels", "concentration"})
_INVALID_FLAGS_MESSAGE = "invalid weekly report effective flags provenance"
_NO_COMMIT_COUNTS = "No commit count collected for this week."
_WEEKLY_REPORT_SECTIONS = (
    "Shipped",
    "Needs attention",
    "Coordination-heavy / low artifact",
)
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


def _weekly_report_blocks(text: str) -> dict[str, list[str]] | None:
    lines = text.splitlines()
    if any(re.match(r"^ {0,3}(?:```|~~~)", line) for line in lines):
        return None
    headings = [
        (i, line[3:].rstrip())
        for i, line in enumerate(lines)
        if line.startswith("## ")
    ]
    if [heading for _, heading in headings] != list(_WEEKLY_REPORT_SECTIONS):
        return None

    sections = {}
    for section_index, (line_index, heading) in enumerate(headings):
        end = (
            headings[section_index + 1][0]
            if section_index + 1 < len(headings)
            else len(lines)
        )
        blocks: list[str] = []
        current: list[str] = []

        def finish_block() -> None:
            while current and not current[-1].strip():
                current.pop()
            if current:
                blocks.append("\n".join(current))
                current.clear()

        for line in lines[line_index + 1:end]:
            if not line.strip():
                if current and current[0].startswith("- "):
                    current.append(line)
                else:
                    finish_block()
            elif line.startswith("- ") and current:
                finish_block()
                current = [line]
            elif (
                current
                and current[0].startswith("- ")
                and not line[0].isspace()
                and not current[-1].strip()
            ):
                finish_block()
                current = [line]
            else:
                current.append(line)
        finish_block()
        sections[heading] = blocks
    return sections


def _project_weekly_brief(summary, project: str, raw_cutoff: str) -> list[str]:
    if (
        summary is None
        or summary.evidence_cutoff is None
        or summary.cutoff_precision not in {"instant", "date"}
        or summary.coverage_state not in {"provisional", "friday-checkpoint"}
    ):
        return []
    sections = _weekly_report_blocks(summary.text)
    if sections is None:
        return []
    canonical = unicodedata.normalize("NFC", project).casefold()

    def attributes_project(block: str) -> bool:
        for bold in re.findall(r"\*\*([^*\n]+)\*\*", block):
            label = unicodedata.normalize("NFC", bold).casefold().strip()
            parenthetical = [
                part.strip() for part in re.findall(r"\(([^()]*)\)", label)
            ]
            if canonical in parenthetical or label == canonical:
                return True
            if (
                "-" in canonical
                and label.startswith(f"{canonical} ")
                and label.endswith(":")
            ):
                return True
        return False

    matched = {
        heading: [block for block in blocks if attributes_project(block)]
        for heading, blocks in sections.items()
    }
    if not any(matched.values()):
        return []

    state_label = {
        "provisional": "Provisional",
        "friday-checkpoint": "Friday checkpoint",
    }[summary.coverage_state]
    body = [
        "\n## Weekly brief\n",
        f"\n> {state_label} summary evidence through "
        f"{summary.evidence_cutoff} ({summary.cutoff_precision} precision); "
        f"raw activity evidence through {raw_cutoff}.\n",
    ]
    for heading in _WEEKLY_REPORT_SECTIONS:
        if matched[heading]:
            body.append(f"\n### {heading}\n\n")
            body.append("\n\n".join(matched[heading]) + "\n")
    return body


def _person_link(name: str, up: int = 1) -> str:
    return f"[{name}]({'../' * up}Person/{quote(name)}/README.md)"


def _markdown_text(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|])", r"\\\1", text)


def _project_link(proj: str, ids: IdentityMaps) -> str:
    projection = ids.projection(proj)
    if projection == "hidden":
        return _markdown_text(proj)
    root = "Areas" if projection == "area" else "Projects"
    return f"[{proj}](../{root}/{quote(_project_fname(proj))}/README.md)"


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


def _validate_projection_filenames(
    conn: sqlite3.Connection, ids: IdentityMaps
) -> None:
    slugs = set(ids.project_slugs()) | set(ids.area_slugs())
    slugs.update({
        project
        for (project,) in conn.execute(
            "SELECT DISTINCT project FROM events "
            "WHERE project IS NOT NULL ORDER BY project"
        )
        if project and project != "(no project)"
    })
    slugs.update(
        project
        for (project,) in conn.execute(
            "SELECT DISTINCT project FROM weekly_commit_counts ORDER BY project"
        )
        if ids.projection(project) == "count-only"
    )
    seen_by_root = {
        "Projects": {"readme.md": "project index"},
        "Areas": {"readme.md": "area index"},
    }
    for project in sorted(slugs):
        if not project or project == "(no project)":
            continue
        projection = ids.projection(project)
        if projection == "hidden":
            continue
        root = "Areas" if projection == "area" else "Projects"
        seen = seen_by_root[root]
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
    _validate_projection_filenames(conn, ids)
    vault_dir.mkdir(parents=True, exist_ok=True)
    for m in MANAGED:
        p = vault_dir / m
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    (vault_dir / "Person").mkdir()
    (vault_dir / "Projects").mkdir()
    (vault_dir / "Areas").mkdir()
    (vault_dir / "Work Journal").mkdir()

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
            link = proj if proj == "(no project)" else _project_link(proj, ids)
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
            flags_md.append(f"- **Concentration**: {_project_link(proj, ids)} — {int(share * 100)}% by "
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

    # ---- Project and area pages: one folder per item, one file per week ------
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

    def _event_weeks(rows: list[dict]) -> dict[str, list[tuple[date, list[dict]]]]:
        rows_by_week: dict[date, dict[str, list[dict]]] = {}
        for row in rows:
            monday = week_monday(date.fromisoformat(row["ts"][:10]))
            grouped = rows_by_week.setdefault(monday, {})
            grouped.setdefault(row["project"], []).append(row)
        row_mondays = sorted(rows_by_week, reverse=True)
        return {
            proj: [
                (monday, rows_by_week[monday][proj])
                for monday in row_mondays
                if proj in rows_by_week[monday]
            ]
            for proj in sorted(by_key(rows, "project"))
            if proj != "(no project)"
        }

    cur = conn.execute(
        "SELECT person, project, ts, kind, summary, refs, hash, source FROM events"
        " WHERE project IS NOT NULL AND project != '' ORDER BY ts DESC"
    )
    cols = [column[0] for column in cur.description]
    classified_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    project_rows = [
        row for row in classified_rows
        if ids.projection(row["project"]) in {"full", "unclassified"}
    ]
    area_rows = [
        row for row in classified_rows
        if ids.projection(row["project"]) == "area"
    ]
    project_weeks = _event_weeks(project_rows)
    area_weeks = _event_weeks(area_rows)
    for proj in ids.project_slugs():
        if ids.projection(proj) == "full":
            project_weeks.setdefault(proj, [])
    for area in ids.area_slugs():
        area_weeks.setdefault(area, [])

    count_dates: dict[str, set[date]] = {}
    for row in classified_rows:
        if ids.projection(row["project"]) == "count-only":
            count_dates.setdefault(row["project"], set()).add(
                week_monday(date.fromisoformat(row["ts"][:10]))
            )
    for proj, week_start in conn.execute(
        "SELECT DISTINCT project, week_start FROM weekly_commit_counts "
        "ORDER BY project, week_start DESC"
    ):
        if ids.projection(proj) == "count-only":
            count_dates.setdefault(proj, set()).add(date.fromisoformat(week_start))
    collected_count_dates = {
        proj: set(project_dates)
        for proj, project_dates in count_dates.items()
    }
    for proj in ids.project_slugs():
        if ids.projection(proj) == "count-only":
            count_dates.setdefault(proj, set()).add(mondays[0])
    count_weeks = {
        proj: [
            (monday, weekly_commit_counts(conn, proj, monday.isoformat()))
            for monday in sorted(project_dates, reverse=True)
        ]
        for proj, project_dates in sorted(count_dates.items())
    }
    count_index_weeks = {
        proj: [
            (monday, counts)
            for monday, counts in weeks_mine
            if monday in collected_count_dates.get(proj, set())
        ]
        for proj, weeks_mine in count_weeks.items()
    }

    def _render_event_pages(
        root_name: str,
        frontmatter_name: str,
        item_weeks: dict[str, list[tuple[date, list[dict]]]],
    ) -> None:
        nonlocal files
        for proj, weeks_mine in item_weeks.items():
            pdir = vault_dir / root_name / _project_fname(proj)
            if pdir.name == "README.md" or pdir.exists():
                raise ValueError(f"filename collision in vault render: {pdir}")
            pdir.mkdir()
            if not weeks_mine:
                md = [
                    f"---\n{frontmatter_name}: {proj}\n"
                    f"generated: {today.isoformat()}\n---\n",
                    f"# {proj}\n\n",
                    f"No activity has been collected for this {frontmatter_name}.\n",
                ]
                _write(pdir / "README.md", "".join(md))
                files += 1
                continue
            latest_m, latest_mine = weeks_mine[0]
            latest_label = week_label(latest_m)
            md = [
                f"---\n{frontmatter_name}: {proj}\n"
                f"generated: {today.isoformat()}\n---\n",
                f"# {proj}\n",
            ]
            docs = [
                name for name in ("architecture", "summary")
                if (vault_dir / "Docs" / proj / f"{name}.md").is_file()
            ]
            if docs:
                md.append("\n" + " · ".join(
                    f"[{name.capitalize()}](../../Docs/{quote(proj)}/{name}.md)"
                    for name in docs
                ) + "\n")
            latest_cutoff = _evidence_cutoff(latest_mine)
            md.append(f"\n## [{latest_label}]({quote(latest_label)}.md)\n\n"
                      f"{_activity_summary(latest_mine)}\n\n"
                      f"{_evidence_notice(latest_cutoff)}\n")
            md += _project_contributors(latest_mine)
            md.append("\n## Weeks\n")
            for monday, mine in weeks_mine:
                label = week_label(monday)
                md.append(f"- [{label}]({quote(label)}.md) — "
                          f"{_activity_summary(mine, separator=', ')}\n")
            _write(pdir / "README.md", "".join(md))
            files += 1

            for monday, mine in weeks_mine:
                label = week_label(monday)
                cutoff = _evidence_cutoff(mine)
                navigation = f"\n[{proj}](README.md)"
                if monday in week_of:
                    navigation += (f" · [{label} — Team]"
                                   f"(../../Work%20Journal/{quote(label)}.md)")
                wmd = [
                    f"---\n{frontmatter_name}: {proj}\nweek: {monday.isoformat()}\n"
                    f"evidence_through: {cutoff[0]}\n"
                    f"cutoff_precision: {cutoff[1]}\n"
                    f"generated: {today.isoformat()}\n---\n",
                    f"# {proj} — {label}\n",
                    f"{navigation}\n\n",
                    f"{_activity_summary(mine)}\n\n",
                    f"{_evidence_notice(cutoff)}\n",
                ]
                if root_name == "Projects":
                    wmd += _project_weekly_brief(
                        weekly_summaries.get(monday), proj, cutoff[0]
                    )
                wmd += _project_week_body(mine)
                _write(pdir / f"{_fname(label)}.md", "".join(wmd))
                files += 1

    def _commit_summary(counts: list, separator: str = " · ") -> str:
        if not counts:
            return _NO_COMMIT_COUNTS
        return separator.join((
            _count("commit", sum(count.commit_count for count in counts)),
            _count("contributor", len({count.person for count in counts})),
        ))

    def _commit_body(counts: list) -> list[str]:
        if not counts:
            return [f"\n{_NO_COMMIT_COUNTS}\n"]
        ordered = sorted(
            counts,
            key=lambda count: (
                -count.commit_count,
                ids.display_name(count.person),
                count.person,
            ),
        )
        body = [
            f"\n{_commit_summary(ordered)}\n\n",
            "| Contributor | Commits |\n",
            "|---|---:|\n",
        ]
        body.extend(
            f"| {_markdown_text(ids.display_name(count.person))} "
            f"| {count.commit_count} |\n"
            for count in ordered
        )
        return body

    def _render_count_pages() -> None:
        nonlocal files
        for proj, weeks_mine in count_weeks.items():
            pdir = vault_dir / "Projects" / _project_fname(proj)
            if pdir.name == "README.md" or pdir.exists():
                raise ValueError(f"filename collision in vault render: {pdir}")
            pdir.mkdir()
            latest_m, latest_counts = weeks_mine[0]
            latest_label = week_label(latest_m)
            md = [
                f"---\nproject: {proj}\ngenerated: {today.isoformat()}\n---\n",
                f"# {proj}\n",
                f"\n## [{latest_label}]({quote(latest_label)}.md)\n",
            ]
            md += _commit_body(latest_counts)
            md.append("\n## Weeks\n")
            for monday, counts in weeks_mine:
                label = week_label(monday)
                md.append(f"- [{label}]({quote(label)}.md) — "
                          f"{_commit_summary(counts, separator=', ')}\n")
            _write(pdir / "README.md", "".join(md))
            files += 1

            for monday, counts in weeks_mine:
                label = week_label(monday)
                navigation = f"\n[{proj}](README.md)"
                if monday in week_of:
                    navigation += (f" · [{label} — Team]"
                                   f"(../../Work%20Journal/{quote(label)}.md)")
                wmd = [
                    f"---\nproject: {proj}\nweek: {monday.isoformat()}\n"
                    f"generated: {today.isoformat()}\n---\n",
                    f"# {proj} — {label}\n",
                    f"{navigation}\n",
                ]
                wmd += _commit_body(counts)
                _write(pdir / f"{_fname(label)}.md", "".join(wmd))
                files += 1

    def _render_index(
        root_name: str,
        event_item_weeks: dict[str, list[tuple[date, list[dict]]]],
        count_item_weeks: dict[str, list[tuple[date, list]]] | None = None,
    ) -> None:
        nonlocal files
        count_item_weeks = count_item_weeks or {}
        all_item_weeks = {**event_item_weeks, **count_item_weeks}
        current_m = mondays[0]
        current = {
            proj: next(
                (mine for monday, mine in weeks_mine if monday == current_m),
                None,
            )
            for proj, weeks_mine in all_item_weeks.items()
        }
        current = {proj: mine for proj, mine in current.items() if mine is not None}

        def item_summary(proj: str, rows: list, separator: str = " · ") -> str:
            if proj in count_item_weeks:
                return _commit_summary(rows, separator=separator)
            return _activity_summary(rows, separator=separator)

        def item_size(proj: str, rows: list) -> int:
            if proj in count_item_weeks:
                return sum(count.commit_count for count in rows)
            return len(rows)

        md = [
            f"---\ngenerated: {today.isoformat()}\n---\n",
            f"# {root_name}\n",
            f"\n## {week_label(current_m)}\n",
        ]
        current_event_rows = [
            row
            for proj, rows in current.items()
            if proj not in count_item_weeks
            for row in rows
        ]
        if current_event_rows:
            md.append(f"\n{_evidence_notice(_evidence_cutoff(current_event_rows))}\n")
        for proj, rows in sorted(
            current.items(),
            key=lambda item: (-item_size(*item), item[0]),
        ):
            md.append(f"- [{proj}]({quote(_project_fname(proj))}/README.md) — "
                      f"{item_summary(proj, rows)}\n")
        if not current:
            noun = "project" if root_name == "Projects" else "area"
            md.append(f"- No mapped {noun} activity.\n")

        future = {
            proj: weeks_mine[0]
            for proj, weeks_mine in all_item_weeks.items()
            if weeks_mine and weeks_mine[0][0] > current_m
        }
        if future:
            md.append("\n## Future-dated activity\n\nCheck source timestamps.\n")
            for proj, (latest_m, latest_rows) in sorted(future.items()):
                label = week_label(latest_m)
                folder = quote(_project_fname(proj))
                md.append(f"- [{proj}]({folder}/README.md) — "
                          f"[{label}]({folder}/{quote(label)}.md) · "
                          f"{item_summary(proj, latest_rows)}\n")
        earlier = [
            proj for proj in all_item_weeks
            if all_item_weeks[proj]
            and proj not in current and proj not in future
        ]
        if earlier:
            md.append("\n## Earlier activity\n")
            for proj in sorted(
                earlier,
                key=lambda item: (-all_item_weeks[item][0][0].toordinal(), item),
            ):
                latest_m, latest_rows = all_item_weeks[proj][0]
                label = week_label(latest_m)
                folder = quote(_project_fname(proj))
                md.append(f"- [{proj}]({folder}/README.md) — latest "
                          f"[{label}]({folder}/{quote(label)}.md) · "
                          f"{item_summary(proj, latest_rows)}\n")
        configured_empty = sorted(
            proj for proj, weeks_mine in all_item_weeks.items()
            if not weeks_mine
        )
        if configured_empty:
            md.append("\n## Configured, no collected activity\n")
            for proj in configured_empty:
                md.append(
                    f"- [{proj}]({quote(_project_fname(proj))}/README.md)\n"
                )
        _write(vault_dir / root_name / "README.md", "".join(md))
        files += 1

    _render_event_pages("Projects", "project", project_weeks)
    _render_count_pages()
    _render_event_pages("Areas", "area", area_weeks)
    _render_index("Projects", project_weeks, count_index_weeks)
    _render_index("Areas", area_weeks)

    label = week_label(mondays[0])
    (vault_dir / "README.md").write_text(
        f"# Team Vault\n\nGENERATED — do not edit Person/, Projects/, Areas/, or "
        f"Work Journal/ by hand; every render regenerates them from the ledger.\n\n"
        f"- generated: {today.isoformat()}\n- weeks rendered: {weeks}\n"
        f"- events in window: {len(all_rows)}\n"
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
                 channel_names: dict | None = None) -> dict:
    """Re-render into a temp tree and diff managed paths against vault_dir.
    Never writes to vault_dir; detects hand-edits under managed paths and
    renderer nondeterminism."""
    with tempfile.TemporaryDirectory() as td:
        expected_dir = Path(td) / "vault"
        docs_dir = vault_dir / "Docs"
        if docs_dir.is_dir():
            shutil.copytree(docs_dir, expected_dir / "Docs", symlinks=True)
        render_vault(conn, ids, expected_dir, today, weeks=weeks,
                     channel_names=channel_names)
        expected, actual = _managed_files(expected_dir), _managed_files(vault_dir)
        return {
            "missing": sorted(expected - actual),
            "unexpected": sorted(actual - expected),
            "differing": sorted(
                rel for rel in expected & actual
                if (expected_dir / rel).read_bytes()
                != (vault_dir / rel).read_bytes()),
        }
