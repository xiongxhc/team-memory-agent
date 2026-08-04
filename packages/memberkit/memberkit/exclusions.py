"""Local project and projected-summary exclusion rules."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence


class RuleFileError(ValueError):
    """A sanitized error while reading a local exclusion rule file."""

    def __init__(self, path: Path, line: int | None, category: str):
        self.path = path
        self.line = line
        self.category = category
        location = str(path)
        if line is not None:
            location = f"{location}: line {line}"
        super().__init__(f"{location}: {category}")


@dataclass(frozen=True)
class ExclusionRule:
    source_line: int
    kind: Literal["exact", "prefix", "regex"]
    project: str
    pattern: str | None = None
    compiled: re.Pattern[str] | None = None

    def normalized(self) -> str:
        if self.kind == "prefix":
            return f"{self.project}*"
        if self.kind == "regex":
            return f"{self.project} ~ {self.pattern}"
        return self.project

    def matches(self, event: dict) -> bool:
        project = event["project"]
        if project is None:
            return False
        if self.kind == "exact":
            return project == self.project
        if self.kind == "prefix":
            return project.startswith(self.project)
        return project == self.project and self.compiled.search(event["summary"]) is not None


@dataclass(frozen=True)
class ExclusionResult:
    included: list[dict]
    excluded_count: int
    rule_counts: tuple[int, ...]


def rules_path(workdir: Path) -> Path:
    return Path(workdir) / "exclude-projects.txt"


def _reject_controls(path: Path, source_line: int, raw: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise RuleFileError(path, source_line, "control character")


def _parse_project(path: Path, source_line: int, project: str) -> tuple[str, str]:
    if not project:
        raise RuleFileError(path, source_line, "invalid rule")
    if "*" not in project:
        return "exact", project
    if project.endswith("*") and project.count("*") == 1 and len(project) > 1:
        return "prefix", project[:-1]
    raise RuleFileError(path, source_line, "invalid project pattern")


def _parse_rule(path: Path, source_line: int, line: str) -> ExclusionRule:
    delimiter = re.search(r" +~ +", line)
    if delimiter is None:
        if line.startswith("~ ") or line.endswith(" ~"):
            raise RuleFileError(path, source_line, "invalid rule")
        kind, project = _parse_project(path, source_line, line)
        return ExclusionRule(source_line, kind, project)

    project = line[:delimiter.start()]
    pattern = line[delimiter.end():]
    if not project or not pattern:
        raise RuleFileError(path, source_line, "invalid rule")
    if "*" in project:
        raise RuleFileError(path, source_line, "invalid project pattern")
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except (re.error, OverflowError, RecursionError):
        raise RuleFileError(path, source_line, "invalid regular expression") from None
    return ExclusionRule(source_line, "regex", project, pattern, compiled)


def load_rules(path: Path) -> tuple[ExclusionRule, ...]:
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return ()
    except UnicodeDecodeError as exc:
        raise RuleFileError(path, None, "invalid UTF-8") from exc
    except OSError as exc:
        raise RuleFileError(path, None, "unreadable rules file") from exc

    rules: list[ExclusionRule] = []
    encoded_lines = text.split("\n")
    for source_line, encoded_line in enumerate(encoded_lines, start=1):
        has_lf = source_line < len(encoded_lines)
        raw = encoded_line[:-1] if has_lf and encoded_line.endswith("\r") else encoded_line
        _reject_controls(path, source_line, raw)
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rules.append(_parse_rule(path, source_line, line))
    return tuple(rules)


def apply_rules(events: Sequence[dict], rules: Sequence[ExclusionRule]) -> ExclusionResult:
    included: list[dict] = []
    counts = [0] * len(rules)
    for event in events:
        index = next((i for i, rule in enumerate(rules) if rule.matches(event)), None)
        if index is None:
            included.append(event)
        else:
            counts[index] += 1
    return ExclusionResult(included, sum(counts), tuple(counts))
