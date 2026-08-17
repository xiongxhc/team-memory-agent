"""Regenerate the team vault from the ledger.
The vault is a projection:
managed dirs (Person/, Projects/, Work Journal/, README.md) are deleted and
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
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from .identity import IdentityMaps
from .queries import (by_key, events_between, flags, ref_url,
                      week_label, week_monday, week_range)
from .store import get_summary

MANAGED = ("Person", "Projects", "Work Journal", "README.md")
MAX_WORK_LINES = 12   # work bullets per person per week (project pages, person week files)
WORK_KINDS = ("commit", "pr", "mr", "issue", "repo", "journal-highlight")
_FLAG_KEYS = frozenset({"gaps", "unmapped", "unmapped_channels", "concentration"})
_INVALID_FLAGS_MESSAGE = "invalid weekly report effective flags provenance"


def _fname(name: str) -> str:
    return name.replace("/", "-").strip() or "unnamed"


def _person_link(name: str) -> str:
    return f"[{name}](../Person/{quote(name)}/README.md)"


def _project_link(proj: str) -> str:
    return f"[{proj}](../Projects/{quote(_fname(proj))}.md)"


def _week_link(label: str, up: int = 1) -> str:
    return f"[{label}]({'../' * up}Work%20Journal/{quote(label)}.md)"


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
    vault_dir.mkdir(parents=True, exist_ok=True)
    for m in MANAGED:
        p = vault_dir / m
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    (vault_dir / "Person").mkdir()
    (vault_dir / "Projects").mkdir()
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
        return f"- 💬 {len(msgs)} messages across {len(cids)} channels{shown}\n"

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
            kinds = by_key(rs, "kind")
            detail = ", ".join(f"{len(v)} {k}" for k, v in sorted(kinds.items()))
            tally.append(f"\n### {_person_link(_fname(ids.display_name(person)))} — "
                      f"{len(rs)} events ({detail})\n")
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

    # ---- Project pages -------------------------------------------------------
    for proj, rs in sorted(by_key(all_rows, "project").items()):
        if proj == "(no project)":
            continue
        md = [f"---\nproject: {proj}\ngenerated: {today.isoformat()}\n---\n",
              f"# {proj}\n"]
        docs = [n for n in ("architecture", "summary")
                if (vault_dir / "Docs" / proj / f"{n}.md").is_file()]
        if docs:
            md.append("\n" + " · ".join(
                f"[{n.capitalize()}](../Docs/{quote(proj)}/{n}.md)"
                for n in docs) + "\n")
        for m in mondays:
            mine = [r for r in week_of[m] if (r["project"] or "(no project)") == proj]
            if not mine:
                continue
            md.append(f"\n## {_week_link(week_label(m))}\n")
            for person, prs in sorted(by_key(mine, "person").items(),
                                      key=lambda x: -len(x[1])):
                nm = _fname(ids.display_name(person))
                link = (_person_link(nm) if not person.startswith("_unmapped/")
                        else f"`{person}`")
                work = [r for r in prs if r["kind"] in WORK_KINDS]
                msgs = [r for r in prs if r["kind"] == "message"]
                md.append(f"\n### {link} — {len(prs)} events\n")
                md += [_line(r) for r in work[:MAX_WORK_LINES]]
                if len(work) > MAX_WORK_LINES:
                    md.append(f"- …and {len(work) - MAX_WORK_LINES} more work items\n")
                if msgs:
                    if not work:
                        md += _day_lines(person, msgs)
                    md.append(_msg_line(msgs))
        _write(vault_dir / "Projects" / f"{_fname(proj)}.md", "".join(md))
        files += 1

    label = week_label(mondays[0])
    (vault_dir / "README.md").write_text(
        f"# Team Vault\n\nGENERATED — do not edit Person/, Projects/, or "
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
