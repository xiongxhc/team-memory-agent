import re
import traceback
from pathlib import Path

import pytest

import memberkit.exclusions as exclusions


def event(project, summary, *, title="private title", narrative="private narrative"):
    return {
        "ts": "2026-07-27T10:00:00+00:00",
        "kind": "journal-highlight",
        "summary": summary,
        "project": project,
        "refs": None,
        "title": title,
        "narrative": narrative,
    }


def test_rules_path_is_local_to_workdir(tmp_path):
    assert exclusions.rules_path(tmp_path) == tmp_path / "exclude-projects.txt"


def test_missing_rules_file_means_no_rules(tmp_path):
    assert exclusions.load_rules(tmp_path / "missing.txt") == ()


def test_rules_normalize_and_assign_first_matching_rule(tmp_path):
    path = tmp_path / "exclude-projects.txt"
    path.write_bytes(
        b"  # comment\r\n"
        b"team-memory-agent\r\n"
        b"scratch*\r\n"
        b"estidama-sdk ~ ^test(s)? passed$\r\n"
        b"estidama-sdk ~ passed\r\n"
    )
    rules = exclusions.load_rules(path)
    result = exclusions.apply_rules([
        event("team-memory-agent", "keep no source narrative"),
        event("scratch-one", "other"),
        event("estidama-sdk", "Tests Passed"),
        event(None, "Tests Passed"),
    ], rules)

    assert [rule.normalized() for rule in rules] == [
        "team-memory-agent", "scratch*",
        "estidama-sdk ~ ^test(s)? passed$", "estidama-sdk ~ passed",
    ]
    assert result.included == [event(None, "Tests Passed")]
    assert result.excluded_count == 3
    assert result.rule_counts == (1, 1, 1, 0)


@pytest.mark.parametrize(
    ("contents", "category", "line", "secret"),
    [
        (b"\xff", "invalid UTF-8", None, None),
        (b" ~ source-operand", "invalid rule", 1, "source-operand"),
        (b"source-project ~ ", "invalid rule", 1, "source-project"),
        (b"*", "invalid project pattern", 1, "*"),
        (b"foo*bar", "invalid project pattern", 1, "foo*bar"),
        (b"foo**", "invalid project pattern", 1, "foo**"),
        (b"foo* ~ match", "invalid project pattern", 1, "match"),
        (b"project ~ [secret-pattern", "invalid regular expression", 1, "secret-pattern"),
        (b"source-name\tname", "control character", 1, "source-name"),
        (b"source-name\x0bname", "control character", 1, "source-name"),
        (b"source-name\x7fname", "control character", 1, "source-name"),
        (b"good\nproject ~ [private-pattern", "invalid regular expression", 2, "private-pattern"),
    ],
)
def test_invalid_rules_are_sanitized(tmp_path, contents, category, line, secret):
    path = tmp_path / "exclude-projects.txt"
    path.write_bytes(contents)

    with pytest.raises(exclusions.RuleFileError) as raised:
        exclusions.load_rules(path)

    error = raised.value
    message = str(error)
    assert error.path == path
    assert str(path) in message
    assert category in message
    if line is None:
        assert error.line is None
        assert "line " not in message
    else:
        assert error.line == line
        assert f"line {line}" in message
    if secret is not None:
        assert secret not in message


@pytest.mark.parametrize(
    ("pattern", "compile_error", "original_message"),
    [
        pytest.param(
            "[private_regex_secret_47a9",
            re.error,
            "unterminated character set",
            id="re-error",
        ),
        pytest.param(
            "private{999999999999999999999999999999999999}",
            OverflowError,
            "the repetition number is too large",
            id="overflow-error",
        ),
        pytest.param(
            "(" * 500 + "private-nesting" + ")" * 500,
            RecursionError,
            "maximum recursion depth exceeded",
            id="recursion-error",
        ),
    ],
)
def test_regex_compile_failures_raise_sanitized_rule_file_error(
    tmp_path, pattern, compile_error, original_message,
):
    with pytest.raises(compile_error):
        re.compile(pattern, re.IGNORECASE)
    path = tmp_path / "exclude-projects.txt"
    path.write_text(f"private-project ~ {pattern}\n", encoding="utf-8")

    with pytest.raises(exclusions.RuleFileError) as raised:
        exclusions.load_rules(path)

    error = raised.value
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    assert type(error) is exclusions.RuleFileError
    assert error.path == path
    assert error.line == 1
    assert error.category == "invalid regular expression"
    assert str(error) == f"{path}: line 1: invalid regular expression"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert pattern not in formatted
    assert original_message not in formatted


def test_regex_compile_does_not_catch_unrelated_base_exception(tmp_path, monkeypatch):
    class UnrelatedCompileFailure(BaseException):
        pass

    path = tmp_path / "exclude-projects.txt"
    path.write_text("private-project ~ valid-pattern\n", encoding="utf-8")
    monkeypatch.setattr(
        exclusions.re,
        "compile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(UnrelatedCompileFailure()),
    )

    with pytest.raises(UnrelatedCompileFailure):
        exclusions.load_rules(path)


def test_project_matching_is_case_sensitive_for_exact_and_prefix(tmp_path):
    path = tmp_path / "exclude-projects.txt"
    path.write_text("exact\nprefix*\n", encoding="utf-8")
    rules = exclusions.load_rules(path)

    result = exclusions.apply_rules([
        event("exact", "one"), event("Exact", "two"),
        event("prefix-child", "three"), event("Prefix-child", "four"),
    ], rules)

    assert [item["summary"] for item in result.included] == ["two", "four"]
    assert result.rule_counts == (1, 1)


def test_regex_project_is_case_sensitive_and_summary_is_case_insensitive(tmp_path):
    path = tmp_path / "exclude-projects.txt"
    path.write_text("sdk ~ ^tests passed$", encoding="utf-8")
    rule, = exclusions.load_rules(path)

    assert rule.matches(event("sdk", "Tests Passed"))
    assert not rule.matches(event("SDK", "Tests Passed"))
    assert not rule.matches(event(None, "Tests Passed"))


def test_bare_tilde_is_a_valid_project_name(tmp_path):
    path = tmp_path / "exclude-projects.txt"
    path.write_text("~ ~ ^match$\n~", encoding="utf-8")
    rules = exclusions.load_rules(path)

    assert [rule.normalized() for rule in rules] == ["~ ~ ^match$", "~"]
    assert exclusions.apply_rules([event("~", "other"), event("~", "MATCH")], rules).rule_counts == (1, 1)


def test_duplicate_rules_keep_order_and_first_match_wins(tmp_path):
    path = tmp_path / "exclude-projects.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    result = exclusions.apply_rules([event("same", "one")], exclusions.load_rules(path))

    assert result.rule_counts == (1, 0)


def test_regex_matches_only_projected_summary_not_title_or_narrative(tmp_path):
    path = tmp_path / "exclude-projects.txt"
    path.write_text("sdk ~ sentinel", encoding="utf-8")
    rule, = exclusions.load_rules(path)

    assert not rule.matches(event("sdk", "visible summary", title="sentinel", narrative="sentinel"))
