"""Immutable, content-free progress and timing primitives."""

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TextIO, TypeAlias
from uuid import RFC_4122, UUID


Scalar: TypeAlias = str | int | float | bool | None
_RESERVED_FIELDS = frozenset({"event", "run_id", "stage"})
_ALLOWED_EVENTS = frozenset(
    {
        "journal-progress",
        "lock-wait",
        "report-progress",
        "run-end",
        "run-start",
        "stage-end",
        "stage-start",
    }
)
_ALLOWED_STAGES = frozenset(
    {
        "discord",
        "docs-sync",
        "feishu",
        "github",
        "gitlab",
        "import",
        "journal",
        "ledger",
        "lock",
        "push",
        "reclaim",
        "render",
        "report",
        "run",
        "slack",
        "snapshot",
    }
)
_COUNT_FIELDS = frozenset(
    {
        "accepted",
        "aggregate_changes",
        "aggregate_rows",
        "cached",
        "channel_rows",
        "completed",
        "events",
        "failure_count",
        "fetched",
        "identity_rows",
        "inserted",
        "llm_calls",
        "migrated",
        "pairs",
        "quarantined",
        "repository_rows",
        "total",
        "warning_count",
    }
)
_DISTRIBUTION_BASES = (
    "backend_seconds",
    "prompt_bytes",
    "prompt_events",
    "queue_wait_seconds",
)
_DISTRIBUTION_COUNT_FIELDS = frozenset(
    f"{base}_count" for base in _DISTRIBUTION_BASES
)
_DISTRIBUTION_VALUE_FIELDS = frozenset(
    f"{base}_{stat}"
    for base in _DISTRIBUTION_BASES
    for stat in ("p50", "p95", "max")
)
_STATUS_VALUES = frozenset(
    {"cached", "completed", "failed", "generated", "ok", "skipped", "waiting"}
)
_DATE_FIELDS = frozenset({"target_monday", "target_week", "week"})
_DATETIME_FIELDS = frozenset({"local_start", "started_at"})
_ALLOWED_FIELDS = (
    _COUNT_FIELDS
    | _DISTRIBUTION_COUNT_FIELDS
    | _DISTRIBUTION_VALUE_FIELDS
    | _DATE_FIELDS
    | _DATETIME_FIELDS
    | {"concurrency", "elapsed_seconds", "exit_code", "mode", "ok", "status"}
)


def _finite_number(name: str, value: object, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a non-negative number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _non_negative_integer(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a non-negative integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _iso_date(name: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be an ISO date") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be an ISO date")


def _iso_datetime(name: str, value: object) -> None:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError(f"{name} must be an offset-aware ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be an offset-aware ISO datetime") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be an offset-aware ISO datetime")


def _validate_field(name: str, value: Scalar) -> None:
    if name in _COUNT_FIELDS or name in _DISTRIBUTION_COUNT_FIELDS:
        _non_negative_integer(name, value)
    elif name in _DISTRIBUTION_VALUE_FIELDS:
        _finite_number(name, value, optional=True)
    elif name == "concurrency":
        _non_negative_integer(name, value)
        if value < 1 or value > 8:
            raise ValueError("concurrency must be from 1 to 8")
    elif name == "elapsed_seconds":
        _finite_number(name, value)
    elif name == "exit_code":
        _non_negative_integer(name, value)
    elif name == "ok":
        if not isinstance(value, bool):
            raise TypeError("ok must be a boolean")
    elif name == "mode":
        if value not in {"capture-only", "full"}:
            raise ValueError("unsupported telemetry mode")
    elif name == "status":
        if value not in _STATUS_VALUES:
            raise ValueError("unsupported telemetry status")
    elif name in _DATE_FIELDS:
        _iso_date(name, value)
    elif name in _DATETIME_FIELDS:
        _iso_datetime(name, value)


@dataclass(frozen=True)
class ProgressEvent:
    event: str
    stage: str | None = None
    fields: tuple[tuple[str, Scalar], ...] = ()

    def __post_init__(self) -> None:
        if self.event not in _ALLOWED_EVENTS:
            raise ValueError(f"unsupported telemetry event: {self.event}")
        if self.stage is not None and self.stage not in _ALLOWED_STAGES:
            raise ValueError(f"unsupported telemetry stage: {self.stage}")
        if not isinstance(self.fields, tuple):
            raise TypeError("telemetry fields must be a tuple")
        seen = set()
        for item in self.fields:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("telemetry fields must contain name-value pairs")
            name, value = item
            if not isinstance(name, str) or not name:
                raise TypeError("telemetry field names must be non-empty strings")
            if name in _RESERVED_FIELDS:
                raise ValueError(f"reserved telemetry field: {name}")
            if name not in _ALLOWED_FIELDS:
                raise ValueError(f"unsupported telemetry field: {name}")
            if name in seen:
                raise ValueError(f"duplicate telemetry field: {name}")
            seen.add(name)
            if value is not None and not isinstance(
                value, (str, int, float, bool)
            ):
                raise TypeError("telemetry values must be scalar")
            _validate_field(name, value)


@dataclass(frozen=True)
class Distribution:
    count: int
    p50: float | None
    p95: float | None
    maximum: float | None

    def __post_init__(self) -> None:
        _non_negative_integer("count", self.count)
        metrics = (
            ("p50", self.p50),
            ("p95", self.p95),
            ("maximum", self.maximum),
        )
        if self.count == 0:
            if any(value is not None for _name, value in metrics):
                raise ValueError("zero-sample distribution requires null metrics")
            return
        if any(value is None for _name, value in metrics):
            raise ValueError("positive-sample distribution requires all metrics")
        for name, value in metrics:
            _finite_number(name, value)
        if not self.p50 <= self.p95 <= self.maximum:
            raise ValueError("distribution requires p50 <= p95 <= maximum")


Reporter: TypeAlias = Callable[[ProgressEvent], None]


def noop_reporter(event: ProgressEvent) -> None:
    """Accept a progress event without producing output."""


def nearest_rank(values: Sequence[float], percentile: int) -> float | None:
    """Return a nearest-rank percentile without mutating the samples."""
    if percentile < 1 or percentile > 100:
        raise ValueError("percentile must be from 1 to 100")
    if not values:
        return None
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("distribution samples must be numbers")
        if not math.isfinite(value):
            raise ValueError("distribution samples must be finite")
    ordered = sorted(values)
    rank = math.ceil(percentile * len(ordered) / 100)
    return float(ordered[rank - 1])


def distribution(values: Sequence[float]) -> Distribution:
    """Summarize a sequence using the operator-facing percentiles."""
    if not values:
        return Distribution(count=0, p50=None, p95=None, maximum=None)
    return Distribution(
        count=len(values),
        p50=nearest_rank(values, 50),
        p95=nearest_rank(values, 95),
        maximum=float(max(values)),
    )


def stream_reporter(run_id: str, stream: TextIO) -> Reporter:
    """Create a reporter that writes one deterministic JSON object per event."""
    try:
        parsed_run_id = UUID(run_id)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("run ID must be a canonical lowercase UUIDv4") from None
    if (
        parsed_run_id.version != 4
        or parsed_run_id.variant != RFC_4122
        or run_id not in {str(parsed_run_id), parsed_run_id.hex}
    ):
        raise ValueError("run ID must be a canonical lowercase UUIDv4")

    def report(event: ProgressEvent) -> None:
        if type(event) is not ProgressEvent:
            raise TypeError("reporter accepts only ProgressEvent instances")
        validated = ProgressEvent(
            event=event.event,
            stage=event.stage,
            fields=event.fields,
        )
        record: dict[str, Scalar] = {
            "event": validated.event,
            "run_id": run_id,
        }
        if validated.stage is not None:
            record["stage"] = validated.stage
        record.update(validated.fields)
        stream.write(
            json.dumps(
                record,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        stream.flush()

    return report
