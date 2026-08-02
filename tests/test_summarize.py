import json

import pytest

from teammem.events import Event
from teammem.store import insert_events, open_db
from teammem.summarize import daily_person_journal, http_llm, weekly_team_report


def _db(tmp_path):
    conn = open_db(tmp_path / "l.db")
    insert_events(conn, [Event(person="alex", ts="2026-07-14T09:00:00+04:00",
                               source="gitlab", kind="commit", project="project-alpha",
                               summary="fix: JWT refresh race", hash="h1")])
    return conn


def _fake_llm(calls):
    def llm(system, user):
        calls.append((system, user))
        return "Alex fixed the JWT refresh race in [[project-alpha]]."
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


def test_daily_journal_empty_day_returns_none_without_llm(tmp_path):
    conn, calls = _db(tmp_path), []
    out = daily_person_journal(conn=conn, person="alex", display_name="Alex",
                               day="2026-07-16", project_slugs=[], llm=_fake_llm(calls),
                               model="fake", created_ts="t1")
    assert out is None and calls == []


def test_weekly_report_caches_on_dailies_and_flags(tmp_path):
    conn, calls = _db(tmp_path), []

    def llm(system, user):
        calls.append((system, user))
        return "## Shipped\n- JWT fix\n## Needs attention\n- none"

    dailies = [{"person": "alex", "day": "2026-07-14", "text": "Alex fixed X."}]
    flags = {"gaps": ["idle-person"], "unmapped": [], "unmapped_channels": [],
             "concentration": []}
    r1 = weekly_team_report(conn, "2026-07-13", dailies, flags, llm, "fake", "t1")
    assert "Shipped" in r1 and len(calls) == 1
    assert "Alex fixed X." in calls[0][1] and "idle-person" in calls[0][1]
    r2 = weekly_team_report(conn, "2026-07-13", dailies, flags, llm, "fake", "t2")
    assert r2 == r1 and len(calls) == 1                     # cache hit
    flags2 = dict(flags, gaps=[])
    weekly_team_report(conn, "2026-07-13", dailies, flags2, llm, "fake", "t3")
    assert len(calls) == 2                                  # changed flags -> regenerate


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
