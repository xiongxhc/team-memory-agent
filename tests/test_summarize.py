import json
import subprocess
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from teammem import summarize as summarize_module
from teammem.events import Event
from teammem.store import (
    SummaryRecord,
    get_summary,
    insert_events,
    open_db,
    put_summary,
)
from teammem.queries import ReportContext, ReportState
from teammem.summarize import (
    DAILY_HASH_SCHEMA_VERSION,
    DAILY_PROMPT_VERSION,
    LEGACY_DAILY_PROMPT_VERSION,
    REPORT_PROMPT_VERSION,
    DailySummaryInput,
    daily_person_journal,
    http_llm,
    prepare_daily_journal,
    weekly_team_report,
)


def _db(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [Event(person="alex", ts="2026-07-14T09:00:00+04:00",
                               source="gitlab", kind="commit", project="project-alpha",
                               summary="fix: JWT refresh race", hash="h1")])
    return conn


def _fake_llm(calls):
    def llm(system, user):
        calls.append((system, user))
        return "- Alex fixed the JWT refresh race in [[project-alpha]]."
    return llm


def test_daily_journal_caches_and_regenerates_only_on_change(tmp_path):
    conn, calls = _db(tmp_path), []
    llm = _fake_llm(calls)
    args = dict(conn=conn, person="alex", display_name="Alex", day="2026-07-14",
                project_slugs=["project-alpha"], llm=llm, model="fake", created_ts="t1")
    t1 = daily_person_journal(**args)
    assert "JWT refresh race" in t1 and len(calls) == 1
    assert "fix: JWT refresh race" in calls[0][1]          # slice reached the prompt
    assert "Alex" in calls[0][1]                          # display name reached it
    t2 = daily_person_journal(**args)
    assert t2 == t1 and len(calls) == 1                     # unchanged day -> ZERO calls
    insert_events(conn, [Event(person="alex", ts="2026-07-14T11:00:00+04:00",
                               source="gitlab", kind="commit", project="project-alpha",
                               summary="hotfix follow-up", hash="h2")])
    daily_person_journal(**args)
    assert len(calls) == 2                                  # changed slice -> regenerate


def test_daily_journal_regenerates_when_model_changes(tmp_path):
    conn = _db(tmp_path)
    common = dict(
        conn=conn,
        person="alex",
        display_name="Alex",
        day="2026-07-14",
        project_slugs=["project-alpha"],
    )

    first = daily_person_journal(
        **common,
        llm=lambda _system, _user: "- claude summary",
        model="claude-haiku-4-5",
        created_ts="t1",
    )
    second = daily_person_journal(
        **common,
        llm=lambda _system, _user: "- codex summary",
        model="gpt-5.6-sol",
        created_ts="t2",
    )

    assert (first, second) == ("- claude summary", "- codex summary")
    assert get_summary(conn, "daily-person", "alex|2026-07-14").model == "gpt-5.6-sol"


def test_daily_journal_empty_day_returns_none_without_llm(tmp_path):
    conn, calls = _db(tmp_path), []
    out = daily_person_journal(conn=conn, person="alex", display_name="Alex",
                               day="2026-07-16", project_slugs=[], llm=_fake_llm(calls),
                               model="fake", created_ts="t1")
    assert out is None and calls == []


def test_daily_hash_and_prompt_use_only_the_complete_person_day(tmp_path):
    conn = _db(tmp_path)
    insert_events(conn, [
        Event(person="alex", ts="2026-07-14T11:00:00+04:00", source="gitlab",
              kind="commit", project="project-beta", summary="second ordered line", hash="h2"),
    ])
    before = prepare_daily_journal(
        conn, "alex", "Alex", "2026-07-14", ["project-alpha", "project-beta"]
    )
    assert before is not None
    assert before.event_count == 2
    assert before.prompt_bytes == len(before.user_prompt.encode("utf-8"))
    assert "Known project names: project-alpha, project-beta" in before.user_prompt
    assert before.user_prompt.index("fix: JWT refresh race") < before.user_prompt.index(
        "second ordered line"
    )

    insert_events(conn, [
        Event(person="sam", ts="2026-07-15T09:00:00+04:00", source="gitlab",
              kind="commit", project="project-other", summary="unrelated", hash="other"),
    ])
    after = prepare_daily_journal(
        conn,
        "alex",
        "Alex",
        "2026-07-14",
        ["project-alpha", "project-beta", "project-other"],
    )
    assert after is not None
    assert after.input_hash == before.input_hash
    assert "project-other" not in after.user_prompt
    assert after.legacy_input_hash != before.legacy_input_hash


def test_matching_legacy_daily_cache_migrates_without_llm_call(tmp_path):
    conn, calls = _db(tmp_path), []
    prepared = prepare_daily_journal(
        conn, "alex", "Alex", "2026-07-14", ["project-alpha"]
    )
    assert prepared is not None
    put_summary(conn, SummaryRecord(
        "daily-person",
        prepared.key,
        prepared.legacy_input_hash,
        "legacy journal",
        "new-model",
        "legacy-created",
    ))

    text = daily_person_journal(
        conn=conn,
        person="alex",
        display_name="Alex",
        day="2026-07-14",
        project_slugs=["project-alpha"],
        llm=_fake_llm(calls),
        model="new-model",
        created_ts="new-created",
    )

    migrated = get_summary(conn, "daily-person", prepared.key)
    assert text == "legacy journal"
    assert calls == []
    assert migrated == SummaryRecord(
        "daily-person",
        prepared.key,
        prepared.input_hash,
        "legacy journal",
        "new-model",
        "legacy-created",
    )


def test_legacy_daily_cache_from_another_model_is_not_migrated(tmp_path):
    conn, calls = _db(tmp_path), []
    prepared = prepare_daily_journal(
        conn, "alex", "Alex", "2026-07-14", ["project-alpha"]
    )
    assert prepared is not None
    put_summary(conn, SummaryRecord(
        "daily-person",
        prepared.key,
        prepared.legacy_input_hash,
        "legacy Claude journal",
        "claude-haiku-4-5",
        "legacy-created",
    ))

    text = daily_person_journal(
        conn=conn,
        person="alex",
        display_name="Alex",
        day="2026-07-14",
        project_slugs=["project-alpha"],
        llm=_fake_llm(calls),
        model="gpt-5.6-sol",
        created_ts="new-created",
    )

    assert text == "- Alex fixed the JWT refresh race in [[project-alpha]]."
    assert len(calls) == 1
    assert get_summary(conn, "daily-person", prepared.key).model == "gpt-5.6-sol"


@pytest.mark.parametrize(
    ("version_name", "future_value"),
    [
        ("DAILY_PROMPT_VERSION", "3"),
        ("DAILY_HASH_SCHEMA_VERSION", "future-local-schema"),
    ],
)
def test_legacy_daily_cache_regenerates_for_future_daily_generations(
    tmp_path, monkeypatch, version_name, future_value
):
    import teammem.summarize as summarize

    conn, calls = _db(tmp_path), []
    legacy_prepared = prepare_daily_journal(
        conn, "alex", "Alex", "2026-07-14", ["project-alpha"]
    )
    assert legacy_prepared is not None
    put_summary(conn, SummaryRecord(
        "daily-person",
        legacy_prepared.key,
        legacy_prepared.legacy_input_hash,
        "legacy journal",
        "legacy-model",
        "legacy-created",
    ))
    monkeypatch.setattr(summarize, version_name, future_value)

    text = daily_person_journal(
        conn=conn,
        person="alex",
        display_name="Alex",
        day="2026-07-14",
        project_slugs=["project-alpha"],
        llm=_fake_llm(calls),
        model="future-model",
        created_ts="future-created",
    )

    stored = get_summary(conn, "daily-person", legacy_prepared.key)
    assert text != "legacy journal"
    assert len(calls) == 1
    assert stored.text == text
    assert stored.input_hash != legacy_prepared.legacy_input_hash
    assert stored.model == "future-model"
    assert stored.created_ts == "future-created"


def test_unverifiable_legacy_daily_cache_regenerates_safely(tmp_path):
    conn, calls = _db(tmp_path), []
    old_global_projects = ["project-alpha"]
    prepared_old = prepare_daily_journal(
        conn, "alex", "Alex", "2026-07-14", old_global_projects
    )
    assert prepared_old is not None
    put_summary(conn, SummaryRecord(
        "daily-person",
        prepared_old.key,
        prepared_old.legacy_input_hash,
        "old legacy journal",
        "legacy-model",
        "legacy-created",
    ))

    text = daily_person_journal(
        conn=conn,
        person="alex",
        display_name="Alex",
        day="2026-07-14",
        project_slugs=["project-alpha", "project-added-after-cache"],
        llm=_fake_llm(calls),
        model="new-model",
        created_ts="new-created",
    )

    current = get_summary(conn, "daily-person", prepared_old.key)
    assert text != "old legacy journal"
    assert len(calls) == 1
    assert current.input_hash == prepared_old.input_hash
    assert current.model == "new-model"
    assert current.created_ts == "new-created"


def test_daily_and_report_prompt_versions_invalidate_only_their_cache_kind(
    tmp_path, monkeypatch
):
    import teammem.summarize as summarize

    conn, daily_calls, report_calls = _db(tmp_path), [], []
    daily_args = dict(
        conn=conn,
        person="alex",
        display_name="Alex",
        day="2026-07-14",
        project_slugs=["project-alpha"],
        llm=_fake_llm(daily_calls),
        model="daily-model",
        created_ts="t1",
    )
    report_args = dict(
        conn=conn,
        monday_iso="2026-07-13",
        dailies=[DailySummaryInput("alex", "2026-07-14", "daily-hash", "daily")],
        context=_report_context(),
        llm=_fake_llm(report_calls),
        model="report-model",
        created_ts="t1",
    )
    daily_person_journal(**daily_args)
    weekly_team_report(**report_args)

    monkeypatch.setattr(summarize, "REPORT_PROMPT_VERSION", "report-next")
    daily_person_journal(**daily_args)
    weekly_team_report(**report_args)
    assert len(daily_calls) == 1
    assert len(report_calls) == 2

    monkeypatch.setattr(summarize, "DAILY_PROMPT_VERSION", "daily-next")
    daily_person_journal(**daily_args)
    weekly_team_report(**report_args)
    assert len(daily_calls) == 2
    assert len(report_calls) == 2


def test_daily_version_constants_are_explicit_and_legacy_is_pinned():
    assert DAILY_PROMPT_VERSION == "2"
    assert DAILY_HASH_SCHEMA_VERSION == "local-projects-v1"
    assert LEGACY_DAILY_PROMPT_VERSION == "2"


def test_tied_events_have_identical_prompt_and_hash_across_insertion_orders(
    tmp_path
):
    tied_events = [
        Event(
            person="alex",
            ts="2026-07-14T09:00:00+04:00",
            source="gitlab",
            kind="commit",
            project="project-beta",
            summary="same timestamp kind and summary",
            hash="hash-beta",
        ),
        Event(
            person="alex",
            ts="2026-07-14T09:00:00+04:00",
            source="gitlab",
            kind="commit",
            project="project-alpha",
            summary="same timestamp kind and summary",
            hash="hash-alpha",
        ),
    ]

    prepared = []
    for name, events in (("forward", tied_events), ("reverse", tied_events[::-1])):
        conn = open_db(tmp_path / f"{name}.db")
        insert_events(conn, events)
        item = prepare_daily_journal(
            conn,
            "alex",
            "Alex",
            "2026-07-14",
            ["project-alpha", "project-beta"],
        )
        assert item is not None
        prepared.append(item)

    assert prepared[0].user_prompt == prepared[1].user_prompt
    assert prepared[0].input_hash == prepared[1].input_hash
    event_text = prepared[0].user_prompt.split("Events:\n", 1)[1]
    assert event_text.index("project-alpha") < event_text.index("project-beta")


def _report_context(
    *,
    coverage_state="provisional",
    evidence_cutoff="2026-07-14T09:00:00+04:00",
    cutoff_precision="instant",
    cutoff_note=None,
    effective_flags=None,
):
    return ReportContext(
        state=ReportState(
            target_monday=date(2026, 7, 13),
            coverage_state=coverage_state,
            evidence_cutoff=evidence_cutoff,
            cutoff_precision=cutoff_precision,
            cutoff_note=cutoff_note,
        ),
        effective_flags=(
            {"unmapped": [], "unmapped_channels": []}
            if effective_flags is None
            else effective_flags
        ),
    )


def test_weekly_report_sends_the_approved_management_prompt_contract(tmp_path):
    conn = open_db(tmp_path / "report.db")
    calls = []

    def llm(system, user):
        calls.append((system, user))
        return "report"

    weekly_team_report(
        conn,
        monday_iso="2026-07-13",
        dailies=[DailySummaryInput(
            "alex", "2026-07-14", "daily-hash", "Alex shipped X."
        )],
        context=_report_context(),
        llm=llm,
        model="fake",
        created_ts="t1",
    )

    expected_system = """\
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
    assert calls[0][0] == expected_system
    assert "Alex shipped X." in calls[0][1]
    assert REPORT_PROMPT_VERSION == "3"


def test_weekly_report_regenerates_when_model_changes(tmp_path):
    conn = open_db(tmp_path / "report-model.db")
    common = dict(
        conn=conn,
        monday_iso="2026-07-13",
        dailies=[DailySummaryInput(
            "alex", "2026-07-14", "daily-hash", "Alex shipped X."
        )],
        context=_report_context(),
    )

    first = weekly_team_report(
        **common,
        llm=lambda _system, _user: "claude report",
        model="claude-sonnet-5",
        created_ts="t1",
    )
    second = weekly_team_report(
        **common,
        llm=lambda _system, _user: "codex report",
        model="gpt-5.6-sol",
        created_ts="t2",
    )

    assert first.text.endswith("claude report")
    assert second.text.endswith("codex report")
    assert second.model == "gpt-5.6-sol"


def test_weekly_report_stores_prompt_identity_and_complete_provenance(tmp_path):
    conn, calls = _db(tmp_path), []

    def llm(system, user):
        calls.append((system, user))
        return "## Shipped\n- JWT fix\n## Needs attention\n- none"

    dailies = [
        DailySummaryInput("sam", "2026-07-15", "sam-hash", "Sam shipped Y."),
        DailySummaryInput("alex", "2026-07-14", "alex-hash", "Alex fixed X."),
    ]
    flags = {"unmapped": [["unknown", 1]], "unmapped_channels": []}
    context = _report_context(
        cutoff_note="source note",
        effective_flags=flags,
    )

    record = weekly_team_report(
        conn,
        monday_iso="2026-07-13",
        dailies=dailies,
        context=context,
        llm=llm,
        model="fake",
        created_ts="t1",
    )

    assert REPORT_PROMPT_VERSION == "3"
    assert len(calls) == 1
    user = calls[0][1]
    assert "Report state: provisional" in user
    assert "Evidence cutoff: 2026-07-14T09:00:00+04:00" in user
    assert "Cutoff precision: instant" in user
    assert "Cutoff note: source note" in user
    assert json.dumps(flags, sort_keys=True, separators=(",", ":")) in user
    assert user.index("Alex fixed X.") < user.index("Sam shipped Y.")
    assert record == get_summary(conn, "weekly-team", "team|2026-07-13")
    assert record.text == (
        "> Provisional — event timestamps through "
        "2026-07-14T09:00:00+04:00; source note.\n\n"
        "## Shipped\n- JWT fix\n## Needs attention\n- none"
    )
    assert record.model == "fake"
    assert record.created_ts == "t1"
    assert record.evidence_cutoff == "2026-07-14T09:00:00+04:00"
    assert record.cutoff_precision == "instant"
    assert record.coverage_state == "provisional"
    assert record.source_input_hash
    assert record.effective_flags_json == json.dumps(
        flags, sort_keys=True, separators=(",", ":")
    )


def test_weekly_source_identity_includes_daily_hash_and_is_order_stable(tmp_path):
    conn, calls = _db(tmp_path), []
    llm = _fake_llm(calls)
    context = _report_context()
    first_dailies = [
        DailySummaryInput("sam", "2026-07-15", "sam-hash", "same sam text"),
        DailySummaryInput("alex", "2026-07-14", "alex-hash", "same alex text"),
    ]

    first = weekly_team_report(
        conn,
        monday_iso="2026-07-13",
        dailies=first_dailies,
        context=context,
        llm=llm,
        model="fake",
        created_ts="t1",
    )
    reordered = weekly_team_report(
        conn,
        monday_iso="2026-07-13",
        dailies=list(reversed(first_dailies)),
        context=context,
        llm=llm,
        model="fake",
        created_ts="t2",
    )
    changed_hash = weekly_team_report(
        conn,
        monday_iso="2026-07-13",
        dailies=[
            DailySummaryInput("sam", "2026-07-15", "sam-hash", "same sam text"),
            DailySummaryInput(
                "alex", "2026-07-14", "alex-hash-changed", "same alex text"
            ),
        ],
        context=context,
        llm=llm,
        model="fake",
        created_ts="t3",
    )

    assert reordered == first
    assert len(calls) == 2
    assert changed_hash.source_input_hash != first.source_input_hash
    assert changed_hash.input_hash != first.input_hash


def test_weekly_source_identity_sorts_the_complete_daily_tuple(tmp_path):
    dailies = [
        DailySummaryInput("alex", "2026-07-14", "hash-b", "second text"),
        DailySummaryInput("alex", "2026-07-14", "hash-a", "first text"),
    ]
    records = []
    prompts = []
    for name, ordered in (("forward", dailies), ("reverse", dailies[::-1])):
        conn = open_db(tmp_path / f"{name}.db")

        def llm(_system, user):
            prompts.append(user)
            return "report"

        records.append(weekly_team_report(
            conn,
            monday_iso="2026-07-13",
            dailies=ordered,
            context=_report_context(),
            llm=llm,
            model="fake",
            created_ts="t1",
        ))

    assert records[0].source_input_hash == records[1].source_input_hash
    assert records[0].input_hash == records[1].input_hash
    assert prompts[0] == prompts[1]


def test_weekly_coverage_state_changes_only_final_weekly_identity(tmp_path):
    conn, calls = _db(tmp_path), []
    dailies = [DailySummaryInput("alex", "2026-07-14", "daily-hash", "daily")]
    provisional = weekly_team_report(
        conn,
        monday_iso="2026-07-13",
        dailies=dailies,
        context=_report_context(),
        llm=_fake_llm(calls),
        model="fake",
        created_ts="t1",
    )
    checkpoint = weekly_team_report(
        conn,
        monday_iso="2026-07-13",
        dailies=dailies,
        context=_report_context(coverage_state="friday-checkpoint"),
        llm=_fake_llm(calls),
        model="fake",
        created_ts="t2",
    )

    assert len(calls) == 2
    assert checkpoint.source_input_hash == provisional.source_input_hash
    assert checkpoint.input_hash != provisional.input_hash
    assert checkpoint.text.startswith("> Friday checkpoint — ")


def _fake_post(body):
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return body

    def post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResp()

    return post, captured


def test_http_llm_extracts_text_and_sends_minimal_body(monkeypatch):
    post, captured = _fake_post({
        "stop_reason": "end_turn",
        "content": [{"type": "thinking", "thinking": ""},
                    {"type": "text", "text": "narrative"}],
    })
    monkeypatch.setattr("requests.post", post)
    llm = http_llm("fake-model", "sk-test", 512)
    assert llm("sys", "user") == "narrative"
    assert set(captured["json"].keys()) == {"model", "max_tokens", "system", "messages"}
    assert captured["headers"]["x-api-key"] == "sk-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"


def test_http_llm_raises_on_truncated_response(monkeypatch):
    post, _ = _fake_post({
        "stop_reason": "max_tokens",
        "content": [{"type": "text", "text": "cut off mid-sent"}],
    })
    monkeypatch.setattr("requests.post", post)
    llm = http_llm("fake-model", "sk-test", 512)
    with pytest.raises(ValueError, match="stop_reason"):
        llm("sys", "user")


def test_http_llm_raises_on_text_free_response(monkeypatch):
    post, _ = _fake_post({
        "stop_reason": "end_turn",
        "content": [{"type": "thinking", "thinking": "just thinking, no answer"}],
    })
    monkeypatch.setattr("requests.post", post)
    llm = http_llm("fake-model", "sk-test", 512)
    with pytest.raises(ValueError):
        llm("sys", "user")


def _stub_claude(tmp_path, script):
    stub = tmp_path / "claude"
    stub.write_text(f"#!/bin/sh\n{script}\n")
    stub.chmod(0o755)
    return str(stub)


def test_claude_cli_llm_passes_flags_and_returns_stdout(tmp_path):
    from teammem.summarize import claude_cli_llm
    stub = _stub_claude(tmp_path, 'echo "$@" > args.txt; cat > stdin.txt; printf -- "- **X** — ok"')
    import os
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        llm = claude_cli_llm("daily-summary-model", claude_bin=stub)
        assert llm("SYS RULES", "the events") == "- **X** — ok"
    finally:
        os.chdir(cwd)
    args = (tmp_path / "args.txt").read_text()
    assert "-p" in args and "--model daily-summary-model" in args
    assert "SYS RULES" in args and "--setting-sources=" in args
    assert (tmp_path / "stdin.txt").read_text() == "the events"   # user prompt via stdin


def test_claude_cli_llm_raises_on_failure_and_empty_output(tmp_path):
    from teammem.summarize import claude_cli_llm
    bad = _stub_claude(tmp_path, "cat > /dev/null; echo boom >&2; exit 3")
    with pytest.raises(ValueError, match="boom"):
        claude_cli_llm("m", claude_bin=bad)("s", "u")
    empty = _stub_claude(tmp_path / "sub", "cat > /dev/null") if (tmp_path / "sub").mkdir() is None else None
    with pytest.raises(ValueError, match="empty"):
        claude_cli_llm("m", claude_bin=empty)("s", "u")


def test_claude_cli_failure_surfaces_stdout_detail(monkeypatch):
    """The CLI reports bad-model/usage errors on stdout with empty stderr."""
    import subprocess
    import types

    import pytest

    from teammem.summarize import claude_cli_llm

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=1, stdout="There's an issue with the selected model\n", stderr=""))
    with pytest.raises(ValueError, match="issue with the selected model"):
        claude_cli_llm("some-model")("system", "user")


def test_claude_cli_failure_includes_bounded_excerpts_from_both_streams(monkeypatch):
    """Diagnostics retain both streams without allowing terminal output to sprawl."""
    import subprocess
    import types

    from teammem.summarize import claude_cli_llm

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        returncode=1,
        stderr="warning:\n" + ("unrelated diagnostic " * 40),
        stdout="selected model is unavailable\tfor this subscription\n",
    ))
    with pytest.raises(ValueError) as exc_info:
        claude_cli_llm("some-model")("system", "user")

    detail = str(exc_info.value).split(": ", 1)[1]
    assert detail.startswith("stderr: warning: unrelated diagnostic")
    assert "stdout: selected model is unavailable for this subscription" in detail
    assert "\n" not in detail and "\t" not in detail
    assert len(detail) <= 300


def test_codex_cli_llm_uses_confined_structured_exec_and_scrubs_credentials(
    monkeypatch,
):
    seen = {}
    monkeypatch.setenv("HOME", "/home/operator")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CODEX_HOME", "/home/operator/.codex")
    monkeypatch.setenv("TEAMMEM_GITLAB_TOKEN", "gitlab-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("MY_PASSWORD", "password-secret")
    monkeypatch.setenv("DATABASE_URL", "postgres://database-secret")
    monkeypatch.setenv("SENTRY_DSN", "https://sentry-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")

    def fake_run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        schema_path = Path(command[command.index("--output-schema") + 1])
        output_path = Path(command[command.index("--output-last-message") + 1])
        assert json.loads(schema_path.read_text()) == {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
        output_path.write_text('{"text":"- **project-alpha** — shipped"}')
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = summarize_module.codex_cli_llm(
        "gpt-5.6-sol",
        reasoning_effort="medium",
        codex_bin="/opt/codex",
    )("TRUSTED SYSTEM CONTRACT", "untrusted events")

    assert result == "- **project-alpha** — shipped"
    command = seen["command"]
    assert command[:4] == ["/opt/codex", "exec", "--model", "gpt-5.6-sol"]
    assert 'model_reasoning_effort="medium"' in command
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[-1] == "-"
    pairs = list(zip(command, command[1:]))
    assert ("--disable", "shell_tool") in pairs
    assert ("--disable", "multi_agent") in pairs
    assert ("--config", "tools.view_image=false") in pairs
    assert ("--config", 'web_search="disabled"') in pairs
    developer_setting = next(
        value for value in command if value.startswith("developer_instructions=")
    )
    assert "TRUSTED SYSTEM CONTRACT" in developer_setting
    assert seen["kwargs"]["input"] == "untrusted events"
    assert seen["kwargs"]["cwd"] == str(Path(
        command[command.index("--output-last-message") + 1]
    ).parent)
    child_env = seen["kwargs"]["env"]
    assert child_env["HOME"] == "/home/operator"
    assert child_env["CODEX_HOME"] == "/home/operator/.codex"
    for key in (
        "TEAMMEM_GITLAB_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "MY_PASSWORD",
        "DATABASE_URL",
        "SENTRY_DSN",
        "SSH_AUTH_SOCK",
    ):
        assert key not in child_env


@pytest.mark.parametrize(
    "payload", ["not-json", "{}", '{"text":""}', '{"text":3}']
)
def test_codex_cli_llm_rejects_malformed_or_empty_output(monkeypatch, payload):
    def fake_run(command, **_kwargs):
        Path(command[command.index("--output-last-message") + 1]).write_text(payload)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="codex cli returned invalid output"):
        summarize_module.codex_cli_llm("gpt-5.6-sol")("system", "user")


def test_codex_cli_llm_reports_nonzero_and_timeout(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2,
            stdout="account limit reached",
            stderr="network warning",
        ),
    )
    with pytest.raises(ValueError, match="network warning.*account limit reached"):
        summarize_module.codex_cli_llm("gpt-5.6-sol")("system", "user")

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("codex", 600)
        ),
    )
    with pytest.raises(ValueError, match="timed out after 600s"):
        summarize_module.codex_cli_llm("gpt-5.6-sol")("system", "user")


@pytest.mark.parametrize('text', [
    '',
    'I need more information to write a meaningful journal entry.\n- Could you provide the message?',
    'Looking at the events, here is the journal:\n- Work completed.',
    '- Brief coordination only.\nCo-Authored-By: Claude <noreply@example.com>',
    '- Could you provide more details about what Alex worked on?',
])
def test_invalid_daily_output_does_not_replace_good_cache(tmp_path, text):
    conn = _db(tmp_path)
    previous = SummaryRecord('daily-person', 'alex|2026-07-14', 'old-hash',
                             '- Existing grounded journal.', 'old-model', 'old')
    put_summary(conn, previous)
    with pytest.raises(ValueError, match='journal'):
        daily_person_journal(conn, 'alex', 'Alex', '2026-07-14', [],
                             lambda _s, _u: text, 'new-model', 'new')
    assert get_summary(conn, 'daily-person', previous.key) == previous


def test_daily_output_preserves_quoted_request_and_source_signature(tmp_path):
    conn = _db(tmp_path)
    text = ('- **Review** — Alex asked the owner: “Could you provide more details?”\n'
            '    - Removed the literal `Co-Authored-By:` trailer from a commit.')
    result = daily_person_journal(conn, 'alex', 'Alex', '2026-07-14', [],
                                  lambda _s, _u: text, 'model', 'new')
    assert result == text
    assert get_summary(conn, 'daily-person', 'alex|2026-07-14').text == text


def test_daily_output_accepts_wrapped_bullets_without_changing_content(tmp_path):
    conn = _db(tmp_path)
    text = ('- **Review** — Alex requested a deployment check,\n'
            '  with acceptance still pending.\n'
            '    - Captured the literal source response:\n'
            '      > Could you provide more details?')
    result = daily_person_journal(conn, 'alex', 'Alex', '2026-07-14', [],
                                  lambda _s, _u: text, 'model', 'new')
    assert result == text


@pytest.mark.parametrize('text', [
    '- **Support** — Alex recorded the customer message: “After deployment,\n  I cannot sign in to the portal.”',
    '- **Review** — Alex reviewed the commit subject “Remove the\n  Co-Authored-By: trailer from generated commits”.',
])
def test_daily_output_preserves_source_quotes_wrapped_across_lines(tmp_path, text):
    conn = _db(tmp_path)
    result = daily_person_journal(conn, 'alex', 'Alex', '2026-07-14', [],
                                  lambda _s, _u: text, 'model', 'new')
    assert result == text


@pytest.mark.parametrize('text', ['', ' \n ', '## Shipped\n## Needs attention',
    'Could you provide the source journals?', '## Shipped\n\nPlease share more context.',
    '- I cannot write a grounded report without evidence.', 'Co-Authored-By: Model'])
def test_weekly_generation_quality_rejects_empty_or_model_conversation(text):
    with pytest.raises(ValueError, match='invalid weekly report'):
        summarize_module.validate_weekly_report(text)


@pytest.mark.parametrize('text', [
    'Released the portal update.',
    '## Shipped\n- Alex released the portal.\n\n## Needs attention\nNone recorded.',
    '## Needs attention\n- Alex recorded the customer message: “After deployment,\n  I cannot sign in to the portal.”',
    '## Needs attention\n> Could you provide a screenshot?\n\nAlex is following up.',
    '- Alex reviewed “Remove the\n  Co-Authored-By: trailer from generated commits”.',
])
def test_weekly_generation_quality_preserves_report_styles_and_quoted_sources(text):
    summarize_module.validate_weekly_report(text)


def test_weekly_generation_guard_does_not_revalidate_legacy_cache_reads(tmp_path):
    from dataclasses import replace
    conn = open_db(tmp_path / 'legacy-report.db')
    kwargs = dict(conn=conn, monday_iso='2026-07-13',
                  dailies=[DailySummaryInput('alex', '2026-07-14', 'daily-hash', '- Shipped.')],
                  context=_report_context(), model='model', created_ts='now')
    current = weekly_team_report(**kwargs, llm=lambda _s, _u: 'Released the portal.')
    legacy = replace(current, text='## Historical cached layout')
    put_summary(conn, legacy)
    result = weekly_team_report(**kwargs, llm=lambda *_: pytest.fail('cache hit must not call model'))
    assert result == legacy


def _codex_rate_error(message="exceeded retry limit, last status: 429 Too Many Requests"):
    return "\n".join(json.dumps(row) for row in [
        {"type": "thread.started", "thread_id": "test-thread"},
        {"type": "item.completed", "item": {
            "type": "error", "message": "Under-development features enabled: skip_host_skill_discovery"}},
        {"type": "error", "message": message},
        {"type": "turn.failed", "error": {"message": message}},
    ])


def test_codex_terminal_error_precedes_startup_warning():
    detail = summarize_module._cli_failure_detail("", _codex_rate_error())
    assert "429 Too Many Requests" in detail
    assert "Under-development" not in detail
    assert "thread.started" not in detail
    assert len(detail) <= 300


def test_codex_structured_quota_code_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(summarize_module.time, "sleep", lambda _delay: pytest.fail("unexpected retry"))

    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stderr="", stdout=json.dumps({
            "type": "turn.failed", "error": {
                "code": "insufficient_quota", "message": "429 Too Many Requests"}}))

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="insufficient_quota"):
        summarize_module.codex_cli_llm("same-model")("system", "user")
    assert len(calls) == 1


def test_codex_rate_limit_retries_with_backoff_then_succeeds(monkeypatch):
    calls, waits = [], []
    monkeypatch.setattr(summarize_module.time, "sleep", waits.append)
    monkeypatch.setattr(summarize_module.random, "uniform", lambda _low, _high: 2.0)

    def run(command, **kwargs):
        calls.append(kwargs)
        output = Path(command[command.index("--output-last-message") + 1])
        assert not output.exists()
        if len(calls) < 3:
            output.write_text('{"text":"stale failed response"}')
            return SimpleNamespace(returncode=1, stderr="", stdout=_codex_rate_error())
        output.write_text('{"text":"- recovered"}')
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(subprocess, "run", run)
    assert summarize_module.codex_cli_llm("same-model")("system", "user") == "- recovered"
    assert len(calls) == 3
    assert waits == [62.0, 122.0]
    assert all(call["input"] == "user" for call in calls)


def test_codex_exhausted_rate_limit_defers_remaining_batch(monkeypatch):
    calls, waits = [], []
    monkeypatch.setattr(summarize_module.time, "sleep", waits.append)
    monkeypatch.setattr(summarize_module.random, "uniform", lambda _low, _high: 0.0)

    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stderr="", stdout=_codex_rate_error())

    monkeypatch.setattr(subprocess, "run", run)
    llm = summarize_module.codex_cli_llm("same-model")
    with pytest.raises(ValueError, match="429 Too Many Requests"):
        llm("system", "first journal")
    for prompt in ("next journal", "historical reconciliation"):
        with pytest.raises(ValueError, match="rate limit.*batch"):
            llm("system", prompt)
    assert len(calls) == 3
    assert waits == [60.0, 120.0]


@pytest.mark.parametrize("message", [
    "401 Unauthorized", "403 Forbidden", "invalid model", "429 insufficient_quota",
    "429 You exceeded your current quota, please check your plan and billing details",
    "429 You have hit your usage limit",
    "429 usage_limit_reached", "429 organization_usage_limit_exceeded",
])
def test_codex_nontransient_errors_are_not_retried(monkeypatch, message):
    calls = []
    monkeypatch.setattr(summarize_module.time, "sleep", lambda _delay: pytest.fail("unexpected retry"))

    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stderr="", stdout=_codex_rate_error(message))

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="codex cli failed"):
        summarize_module.codex_cli_llm("same-model")("system", "user")
    assert len(calls) == 1


def test_codex_success_after_rate_limit_requires_fresh_output(monkeypatch):
    calls = []
    monkeypatch.setattr(summarize_module.time, "sleep", lambda _delay: None)

    def run(command, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            Path(command[command.index("--output-last-message") + 1]).write_text('{"text":"stale"}')
            return SimpleNamespace(returncode=1, stderr="", stdout=_codex_rate_error())
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ValueError, match="invalid output"):
        summarize_module.codex_cli_llm("same-model")("system", "user")
    assert len(calls) == 2


def test_codex_waiting_retry_stops_when_another_call_exhausts_backend(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, local

    waiting, release, worker = Event(), Event(), local()
    calls = []

    def run(*args, **kwargs):
        worker.prompt = kwargs["input"]
        calls.append(worker.prompt)
        return SimpleNamespace(returncode=1, stderr="", stdout=_codex_rate_error())

    def sleep(_delay):
        if worker.prompt == "waiting journal":
            waiting.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(summarize_module.time, "sleep", sleep)
    llm = summarize_module.codex_cli_llm("same-model")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(llm, "system", "waiting journal")
        assert waiting.wait(timeout=5)
        second = pool.submit(llm, "system", "exhausting journal")
        try:
            with pytest.raises(ValueError, match="429 Too Many Requests"):
                second.result(timeout=5)
        finally:
            release.set()
        with pytest.raises(ValueError, match="rate limit.*batch"):
            first.result(timeout=5)
    assert calls == ["waiting journal"] + ["exhausting journal"] * 3
