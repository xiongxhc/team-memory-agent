"""Synthesis engine: ledger slices -> third-person narrative, via an injected
LLM callable. Daily and weekly cache versions evolve independently."""

import json
import os
import re
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .queries import ReportContext
from .slices import (
    daily_person_event_count,
    daily_person_projects,
    daily_person_slice,
    slice_hash,
)
from .store import SummaryRecord, get_summary, put_summary

LLM = Callable[[str, str], str]

CODEX_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

_CODEX_ENV_ALLOWLIST = {
    "ALL_PROXY",
    "CODEX_HOME",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_PROXY",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
}

DAILY_PROMPT_VERSION = "2"
DAILY_HASH_SCHEMA_VERSION = "local-projects-v1"
LEGACY_DAILY_PROMPT_VERSION = "2"
LEGACY_DAILY_MIGRATION_TARGET = (
    LEGACY_DAILY_PROMPT_VERSION,
    "local-projects-v1",
)
REPORT_PROMPT_VERSION = "3"


@dataclass(frozen=True)
class PreparedDailyJournal:
    person: str
    day: str
    key: str
    user_prompt: str
    input_hash: str
    legacy_input_hash: str
    event_count: int
    prompt_bytes: int


@dataclass(frozen=True)
class DailySummaryInput:
    person: str
    day: str
    input_hash: str
    text: str


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
week's per-person daily journal entries plus deterministic flag facts.
Treat the supplied flags as ground truth: use only those flags and do not
re-derive, add, or second-guess flag findings. Provisional inputs already withhold
gap and concentration flags.
Structure, exactly these three sections:
## Shipped
## Needs attention
## Coordination-heavy / low artifact
Rules:
- Consolidate repeated commit/MR/chat evidence into team outcomes grouped by project.
- Name contributors and distinguish shipped work from in-progress coordination.
- Never rank impact by event count.
- Keep Needs attention for blockers, security findings, unresolved incidents,
  decisions requiring follow-up, and the deterministic flags supplied in the input.
- Be concrete, terse, and grounded only in the input; never invent facts.
- Present the most important outcomes first. Use plain **bold** people and project
  names exactly matching the input; NEVER use [[wikilinks]] or markdown links."""


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


def _cli_failure_detail(stderr: str, stdout: str) -> str:
    """Return a compact, deterministic diagnostic from CLI output."""
    excerpts = []
    for label, stream in (("stderr", stderr), ("stdout", stdout)):
        text = re.sub(r"[\s\x00-\x1f\x7f]+", " ", stream).strip()
        if text:
            excerpts.append(f"{label}: {text[:140]}")
    return " | ".join(excerpts) or "(no output)"


def claude_cli_llm(model: str, claude_bin: str = "claude") -> LLM:
    """Headless `claude -p` on the operator's subscription — no API key.
    --system-prompt replaces the default; --strict-mcp-config/--setting-sources=
    isolate the call from skills, CLAUDE.md, and MCP servers. The user prompt
    rides stdin (weekly report inputs can exceed ARG_MAX)."""
    def llm(system: str, user: str) -> str:
        proc = subprocess.run(
            [claude_bin, "-p", "--model", model, "--system-prompt", system,
             "--strict-mcp-config", "--setting-sources="],
            input=user, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            detail = _cli_failure_detail(proc.stderr, proc.stdout)
            raise ValueError(f"claude cli failed ({proc.returncode}): {detail}")
        text = proc.stdout.strip()
        if not text:
            raise ValueError("claude cli returned empty output")
        return text

    return llm


def _scrubbed_llm_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in _CODEX_ENV_ALLOWLIST
    }


def codex_cli_llm(
    model: str,
    *,
    reasoning_effort: str = "high",
    codex_bin: str = "codex",
) -> LLM:
    """Run one confined Codex synthesis call using the operator's login."""

    def llm(system: str, user: str) -> str:
        with tempfile.TemporaryDirectory(prefix="teammem-codex-") as temporary:
            directory = Path(temporary)
            schema_path = directory / "text-schema.json"
            output_path = directory / "response.json"
            schema_path.write_text(json.dumps(CODEX_TEXT_SCHEMA))
            command = [
                codex_bin,
                "exec",
                "--model",
                model,
                "--config",
                f"developer_instructions={json.dumps(system)}",
                "--config",
                f'model_reasoning_effort="{reasoning_effort}"',
                "--disable",
                "shell_tool",
                "--disable",
                "multi_agent",
                "--config",
                "tools.view_image=false",
                "--config",
                'web_search="disabled"',
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--json",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            try:
                process = subprocess.run(
                    command,
                    input=user,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    env=_scrubbed_llm_env(),
                    cwd=temporary,
                )
            except subprocess.TimeoutExpired as error:
                raise ValueError("codex cli timed out after 600s") from error
            if process.returncode != 0:
                detail = _cli_failure_detail(process.stderr, process.stdout)
                raise ValueError(
                    f"codex cli failed ({process.returncode}): {detail}"
                )
            try:
                body = json.loads(output_path.read_text())
                text = body["text"]
                if not isinstance(text, str) or not text.strip():
                    raise ValueError
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("codex cli returned invalid output") from error
            return text.strip()

    return llm


def _daily_user_prompt(
    person: str,
    display_name: str,
    day: str,
    projects: list[str],
    slice_text: str,
) -> str:
    return (
        f"Person: {display_name} (slug: {person})\nDate: {day}\n"
        f"Known project names: {', '.join(sorted(projects)) or '(none)'}\n\n"
        f"Events:\n{slice_text}"
    )


def prepare_daily_journal(
    conn: sqlite3.Connection,
    person: str,
    display_name: str,
    day: str,
    legacy_project_slugs: list[str],
) -> PreparedDailyJournal | None:
    slice_text = daily_person_slice(conn, person, day)
    if not slice_text:
        return None
    local_projects = daily_person_projects(conn, person, day)
    user_prompt = _daily_user_prompt(
        person, display_name, day, local_projects, slice_text
    )
    canonical_input = json.dumps(
        {
            "daily_hash_schema_version": DAILY_HASH_SCHEMA_VERSION,
            "daily_prompt_version": DAILY_PROMPT_VERSION,
            "day": day,
            "display_name": display_name,
            "events": slice_text,
            "person": person,
            "projects": local_projects,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    legacy_user_prompt = _daily_user_prompt(
        person, display_name, day, legacy_project_slugs, slice_text
    )
    return PreparedDailyJournal(
        person=person,
        day=day,
        key=f"{person}|{day}",
        user_prompt=user_prompt,
        input_hash=slice_hash(canonical_input),
        legacy_input_hash=slice_hash(
            LEGACY_DAILY_PROMPT_VERSION + "\n" + legacy_user_prompt
        ),
        event_count=daily_person_event_count(conn, person, day),
        prompt_bytes=len(user_prompt.encode("utf-8")),
    )


def daily_cache_status(
    conn: sqlite3.Connection,
    prepared: PreparedDailyJournal,
    model: str | None = None,
) -> tuple[str, str | None]:
    existing = get_summary(conn, "daily-person", prepared.key)
    if existing is None:
        return "miss", None
    if (
        existing.input_hash == prepared.input_hash
        and (model is None or existing.model == model)
    ):
        return "cached", existing.text
    migration_target_is_current = (
        DAILY_PROMPT_VERSION,
        DAILY_HASH_SCHEMA_VERSION,
    ) == LEGACY_DAILY_MIGRATION_TARGET
    if (
        migration_target_is_current
        and existing.input_hash == prepared.legacy_input_hash
        and (model is None or existing.model == model)
    ):
        put_summary(conn, replace(existing, input_hash=prepared.input_hash))
        return "migrated", existing.text
    return "miss", None


def put_daily_journal(
    conn: sqlite3.Connection,
    prepared: PreparedDailyJournal,
    text: str,
    model: str,
    created_ts: str,
) -> None:
    put_summary(
        conn,
        SummaryRecord(
            kind="daily-person",
            key=prepared.key,
            input_hash=prepared.input_hash,
            text=text,
            model=model,
            created_ts=created_ts,
        ),
    )


def daily_person_journal(conn: sqlite3.Connection, person: str, display_name: str,
                         day: str, project_slugs: list[str], llm: LLM, model: str,
                         created_ts: str) -> str | None:
    prepared = prepare_daily_journal(
        conn, person, display_name, day, project_slugs
    )
    if prepared is None:
        return None
    status, cached_text = daily_cache_status(conn, prepared, model)
    if status != "miss":
        return cached_text
    text = llm(DAILY_SYSTEM, prepared.user_prompt)
    put_daily_journal(conn, prepared, text, model, created_ts)
    return text


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _daily_summary_sort_key(
    daily: DailySummaryInput,
) -> tuple[str, str, str, str]:
    return daily.person, daily.day, daily.input_hash, daily.text


def _weekly_input(dailies: Sequence[DailySummaryInput]) -> str:
    return "\n\n".join(
        f"## {daily.person} — {daily.day}\n{daily.text}"
        for daily in sorted(dailies, key=_daily_summary_sort_key)
    )


def _coverage_line(context: ReportContext) -> str:
    state = context.state
    if state.cutoff_precision == "instant":
        coverage = f"event timestamps through {state.evidence_cutoff}"
    elif state.cutoff_precision == "date":
        coverage = f"event dates through {state.evidence_cutoff}"
    else:
        coverage = "exact event cutoff unavailable"
    if state.cutoff_note:
        coverage += f"; {state.cutoff_note}"
    if state.coverage_state == "provisional":
        return f"> Provisional — {coverage}."
    return (
        f"> Friday checkpoint — {coverage}; "
        "later evidence reconciles on the next full run."
    )


def weekly_team_report(
    conn: sqlite3.Connection,
    *,
    monday_iso: str,
    dailies: Sequence[DailySummaryInput],
    context: ReportContext,
    llm: LLM,
    model: str,
    created_ts: str,
) -> SummaryRecord:
    effective_flags_json = _canonical_json(context.effective_flags)
    ordered_dailies = sorted(dailies, key=_daily_summary_sort_key)
    source_input_hash = slice_hash(_canonical_json({
        "dailies": [
            [daily.person, daily.day, daily.input_hash, daily.text]
            for daily in ordered_dailies
        ],
        "effective_flags": context.effective_flags,
    }))
    state = context.state
    input_hash = slice_hash(_canonical_json({
        "coverage_state": state.coverage_state,
        "cutoff_note": state.cutoff_note,
        "cutoff_precision": state.cutoff_precision,
        "evidence_cutoff": state.evidence_cutoff,
        "report_prompt_version": REPORT_PROMPT_VERSION,
        "source_input_hash": source_input_hash,
        "target_monday": state.target_monday.isoformat(),
    }))
    key = f"team|{monday_iso}"
    existing = get_summary(conn, "weekly-team", key)
    if (
        existing is not None
        and existing.input_hash == input_hash
        and existing.model == model
    ):
        return existing

    user = (
        f"Week of {monday_iso}.\n"
        f"Report state: {state.coverage_state}\n"
        f"Evidence cutoff: {state.evidence_cutoff or '(none)'}\n"
        f"Cutoff precision: {state.cutoff_precision}\n"
        f"Cutoff note: {state.cutoff_note or '(none)'}\n\n"
        "Effective flag facts (ground truth):\n"
        f"{effective_flags_json}\n\n"
        "Daily journals:\n\n"
        f"{_weekly_input(ordered_dailies)}"
    )
    narrative = llm(REPORT_SYSTEM, user)
    record = SummaryRecord(
        kind="weekly-team",
        key=key,
        input_hash=input_hash,
        text=f"{_coverage_line(context)}\n\n{narrative}",
        model=model,
        created_ts=created_ts,
        evidence_cutoff=state.evidence_cutoff,
        cutoff_precision=state.cutoff_precision,
        coverage_state=state.coverage_state,
        source_input_hash=source_input_hash,
        effective_flags_json=effective_flags_json,
    )
    put_summary(conn, record)
    return record
