"""Synthesis engine: ledger slices -> third-person narrative, via an injected
LLM callable. All caching is content-hash keyed (see store.get_or_make);
PROMPT_VERSION is folded into every hash so prompt edits regenerate entries."""

import json
import sqlite3
from collections.abc import Callable

from .slices import daily_person_slice, slice_hash, weekly_team_input
from .store import get_or_make

LLM = Callable[[str, str], str]

PROMPT_VERSION = "2"

DAILY_SYSTEM = """\
You write ONE person's daily work-journal entry for a company knowledge vault,
in neutral third person, in an engineering-journal house style. Output is
markdown bullet lines ONLY — no headers, no prose paragraphs, no frontmatter.

Format:
- One top-level bullet per project or workstream touched that day:
  "- **<project or topic>** — **<short headline>** — concrete details."
- Sub-bullets (4-space indent) for distinct threads under the same project.
- Be concrete: name the actual MR/commit subjects, fixes, endpoints, versions,
  and counts from the events. Merge duplicates — the same work appearing as a
  commit and a chat message is ONE fact.
- Append a status marker to a headline only when the events clearly show it:
  ✅ (merged/deployed/resolved) or 🟠 (in progress/blocked).
- Chat-only coordination that moved something concrete gets one bullet with
  what was decided or unblocked; drop pure chatter.
- A genuinely low-signal day is ONE honest bullet, e.g.
  "- Brief coordination only, no shipped output." — never inflate.
- Project and people names are plain **bold** text. NEVER use [[wikilinks]] or
  markdown links, and never invent names or facts not present in the events."""

REPORT_SYSTEM = """\
You write a weekly team report for a manager who has 20 seconds. Input: the
week's per-person daily journal entries plus deterministic flag facts (treat the
flags as ground truth — report them, do not re-derive or second-guess them).
Structure, exactly these three sections:
## Shipped
## Needs attention
## Coordination-heavy / low artifact
Rules: top-down, most important first; name people and projects as plain
**bold** text exactly matching the names used in the input — NEVER [[wikilinks]]
or markdown links (navigation links live elsewhere on the page); fold
gap/concentration/unmapped flags into "Needs attention"; be concrete and terse;
never invent facts not in the input."""


def http_llm(model: str, api_key: str, max_tokens: int) -> LLM:
    import requests

    def llm(system: str, user: str) -> str:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": max_tokens, "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=120)
        resp.raise_for_status()
        body = resp.json()
        if body.get("stop_reason") != "end_turn":
            raise ValueError(f"llm response incomplete: stop_reason={body.get('stop_reason')}")
        text = next((b["text"] for b in body["content"] if b["type"] == "text"), None)
        if text is None:
            raise ValueError("llm response has no text block")
        return text

    return llm


def claude_cli_llm(model: str, claude_bin: str = "claude") -> LLM:
    """Headless `claude -p` on the operator's subscription — no API key.
    --system-prompt replaces the default; --strict-mcp-config/--setting-sources=
    isolate the call from skills, CLAUDE.md, and MCP servers. The user prompt
    rides stdin (weekly report inputs can exceed ARG_MAX)."""
    import subprocess

    def llm(system: str, user: str) -> str:
        proc = subprocess.run(
            [claude_bin, "-p", "--model", model, "--system-prompt", system,
             "--strict-mcp-config", "--setting-sources="],
            input=user, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise ValueError(f"claude cli failed ({proc.returncode}): "
                             f"{proc.stderr.strip()[:300]}")
        text = proc.stdout.strip()
        if not text:
            raise ValueError("claude cli returned empty output")
        return text

    return llm


def daily_person_journal(conn: sqlite3.Connection, person: str, display_name: str,
                         day: str, project_slugs: list[str], llm: LLM, model: str,
                         created_ts: str) -> str | None:
    slice_text = daily_person_slice(conn, person, day)
    if not slice_text:
        return None
    user = (f"Person: {display_name} (slug: {person})\nDate: {day}\n"
            f"Known project names: {', '.join(sorted(project_slugs)) or '(none)'}\n\n"
            f"Events:\n{slice_text}")
    h = slice_hash(PROMPT_VERSION + "\n" + user)
    return get_or_make(conn, "daily-person", f"{person}|{day}", h,
                       lambda: (llm(DAILY_SYSTEM, user), model), created_ts)


def weekly_team_report(conn: sqlite3.Connection, monday_iso: str,
                       daily_texts: list[dict], flags: dict, llm: LLM,
                       model: str, created_ts: str) -> str:
    user = (f"Week of {monday_iso}.\n\nFlag facts (ground truth):\n"
            f"{json.dumps(flags, sort_keys=True)}\n\nDaily journals:\n\n"
            f"{weekly_team_input(daily_texts)}")
    h = slice_hash(PROMPT_VERSION + "\n" + user)
    return get_or_make(conn, "weekly-team", f"team|{monday_iso}", h,
                       lambda: (llm(REPORT_SYSTEM, user), model), created_ts)
