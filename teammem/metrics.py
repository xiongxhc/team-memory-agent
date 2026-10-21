"""Immutable aggregate values used by count-only project projections."""

from dataclasses import dataclass


@dataclass(frozen=True)
class WeeklyCommitCount:
    project: str
    week_start: str
    person: str
    commit_count: int


@dataclass(frozen=True)
class CommitCountScope:
    project: str
    week_start: str
