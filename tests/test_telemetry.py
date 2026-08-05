import io
import json
import math
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from teammem.telemetry import (
    Distribution,
    ProgressEvent,
    distribution,
    nearest_rank,
    noop_reporter,
    stream_reporter,
)


RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
COMPACT_RUN_ID = "123e4567e89b42d3a456426614174000"


class FlushTrackingStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flush_count = 0

    def flush(self):
        self.flush_count += 1
        super().flush()


def test_progress_values_are_immutable_and_noop_accepts_them():
    event = ProgressEvent("stage-start", stage="journal")

    noop_reporter(event)

    with pytest.raises(FrozenInstanceError):
        event.stage = "report"


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], (0, None, None, None)),
        ([4.5], (1, 4.5, 4.5, 4.5)),
        ([1.0, 9.0], (2, 1.0, 9.0, 9.0)),
    ],
)
def test_distribution_defines_zero_one_and_two_sample_percentiles(values, expected):
    result = distribution(values)
    assert (result.count, result.p50, result.p95, result.maximum) == expected


def test_nearest_rank_sorts_without_mutating_the_input():
    values = [8.0, 2.0, 5.0, 1.0]

    assert nearest_rank(values, 50) == 2.0
    assert nearest_rank(values, 95) == 8.0
    assert values == [8.0, 2.0, 5.0, 1.0]


@pytest.mark.parametrize("percentile", [0, 101])
def test_nearest_rank_rejects_percentiles_outside_definition(percentile):
    with pytest.raises(ValueError, match="^percentile must be from 1 to 100$"):
        nearest_rank([1.0], percentile)


def test_stream_reporter_writes_stable_sorted_json_and_flushes_every_line():
    stream = FlushTrackingStream()
    report = stream_reporter(RUN_ID, stream)

    report(
        ProgressEvent(
            "stage-end",
            stage="journal",
            fields=(("pairs", 4), ("cached", 2), ("elapsed_seconds", 1.25)),
        )
    )
    report(ProgressEvent("run-end", fields=(("ok", True),)))

    assert stream.getvalue().splitlines() == [
        '{"cached":2,"elapsed_seconds":1.25,"event":"stage-end",'
        f'"pairs":4,"run_id":"{RUN_ID}","stage":"journal"}}',
        f'{{"event":"run-end","ok":true,"run_id":"{RUN_ID}"}}',
    ]
    assert stream.flush_count == 2
    assert json.loads(stream.getvalue().splitlines()[0])["cached"] == 2


@pytest.mark.parametrize(
    "field_name",
    [
        "reference",
        "my_summary",
        "event_text",
        "payload",
        "arbitrary_name",
        "prompt",
        "refs",
        "secret",
        "event_body",
    ],
)
def test_progress_events_reject_every_field_outside_the_allowed_schema(field_name):
    with pytest.raises(ValueError, match="unsupported telemetry field"):
        ProgressEvent("journal-progress", fields=((field_name, "private"),))


def test_progress_events_allow_the_approved_task_4_and_7_schema():
    event = ProgressEvent(
        "journal-progress",
        stage="journal",
        fields=(
            ("pairs", 12),
            ("cached", 3),
            ("migrated", 2),
            ("llm_calls", 7),
            ("concurrency", 2),
            ("completed", 4),
            ("total", 7),
            ("prompt_events_count", 12),
            ("prompt_events_p50", 4.0),
            ("prompt_events_p95", 9.0),
            ("prompt_events_max", 10.0),
            ("prompt_bytes_count", 12),
            ("prompt_bytes_p50", 2048.0),
            ("prompt_bytes_p95", 8192.0),
            ("prompt_bytes_max", 9000.0),
            ("queue_wait_seconds_count", 7),
            ("queue_wait_seconds_p50", 0.1),
            ("queue_wait_seconds_p95", 0.4),
            ("queue_wait_seconds_max", 0.5),
            ("backend_seconds_count", 7),
            ("backend_seconds_p50", 5.0),
            ("backend_seconds_p95", 8.0),
            ("backend_seconds_max", 9.0),
            ("elapsed_seconds", 10.0),
        ),
    )
    assert event.fields[-1] == ("elapsed_seconds", 10.0)

    connector = ProgressEvent(
        "stage-end",
        stage="gitlab",
        fields=(("fetched", 10), ("inserted", 3), ("elapsed_seconds", 0.2)),
    )
    assert connector.fields[:2] == (("fetched", 10), ("inserted", 3))

    run = ProgressEvent(
        "run-end",
        stage="run",
        fields=(
            ("mode", "capture-only"),
            ("local_start", "2026-08-05T18:20:00+04:00"),
            ("ok", True),
            ("exit_code", 0),
        ),
    )
    assert run.fields[-1] == ("exit_code", 0)

    weekly = ProgressEvent(
        "report-progress",
        stage="report",
        fields=(("target_week", "2026-08-03"), ("status", "generated")),
    )
    assert weekly.fields == (
        ("target_week", "2026-08-03"),
        ("status", "generated"),
    )


def test_progress_events_reject_free_form_strings_even_on_allowed_fields():
    with pytest.raises(TypeError, match="pairs must be a non-negative integer"):
        ProgressEvent("journal-progress", fields=(("pairs", "PRIVATE"),))
    with pytest.raises(ValueError, match="unsupported telemetry status"):
        ProgressEvent("report-progress", fields=(("status", "PRIVATE"),))
    with pytest.raises(ValueError, match="ISO date"):
        ProgressEvent("report-progress", fields=(("target_week", "PRIVATE"),))


@pytest.mark.parametrize(
    ("event", "stage"),
    [("private event text", "journal"), ("stage-start", "private stage text")],
)
def test_progress_event_and_stage_are_constrained_identifiers(event, stage):
    with pytest.raises(ValueError):
        ProgressEvent(event, stage=stage)


@pytest.mark.parametrize(
    "run_id",
    [
        "private run identifier with spaces",
        "PRIVATE-CONTENT",
        "deadbeefdeadbeefdeadbeefdeadbeef",
        "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        "123e4567-e89b-42d3-7456-426614174000",
        "123E4567-E89B-42D3-A456-426614174000",
    ],
)
def test_stream_reporter_rejects_free_form_run_ids(run_id):
    with pytest.raises(ValueError, match="run ID"):
        stream_reporter(run_id, io.StringIO())


@pytest.mark.parametrize("run_id", [RUN_ID, COMPACT_RUN_ID])
def test_stream_reporter_accepts_canonical_lowercase_uuid4_forms(run_id):
    stream = io.StringIO()
    stream_reporter(run_id, stream)(ProgressEvent("run-start"))
    assert json.loads(stream.getvalue())["run_id"] == run_id


def test_stream_reporter_rejects_event_like_objects_before_serializing():
    stream = io.StringIO()
    event_like = SimpleNamespace(
        event="event_text",
        stage="private-stage",
        fields=(("payload", "PRIVATE-CONTENT"),),
    )

    with pytest.raises(TypeError, match="ProgressEvent"):
        stream_reporter(RUN_ID, stream)(event_like)

    assert stream.getvalue() == ""


def test_stream_reporter_revalidates_mutated_progress_events():
    stream = io.StringIO()
    event = ProgressEvent("stage-end", stage="journal")
    object.__setattr__(event, "fields", (("payload", "PRIVATE-CONTENT"),))

    with pytest.raises(ValueError, match="unsupported telemetry field"):
        stream_reporter(RUN_ID, stream)(event)

    assert stream.getvalue() == ""


def test_progress_events_allow_only_scalar_values():
    with pytest.raises(TypeError, match="telemetry values must be scalar"):
        ProgressEvent("journal-progress", fields=(("completed", [1, 2]),))


def test_progress_events_reject_duplicate_or_reserved_field_names():
    with pytest.raises(ValueError, match="duplicate telemetry field"):
        ProgressEvent("stage-end", fields=(("cached", 1), ("cached", 2)))
    with pytest.raises(ValueError, match="reserved telemetry field"):
        ProgressEvent("stage-end", fields=(("run_id", "replacement"),))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_progress_events_reject_non_finite_metrics(value):
    with pytest.raises(ValueError, match="finite"):
        ProgressEvent("stage-end", fields=(("elapsed_seconds", value),))


def test_stream_reporter_revalidates_non_finite_mutated_events():
    event = ProgressEvent("stage-end", fields=(("elapsed_seconds", 1.0),))
    object.__setattr__(event, "fields", (("elapsed_seconds", math.nan),))

    with pytest.raises(ValueError, match="finite"):
        stream_reporter(RUN_ID, io.StringIO())(event)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_distributions_reject_non_finite_samples_and_values(value):
    with pytest.raises(ValueError, match="finite"):
        distribution([value])
    with pytest.raises(ValueError, match="finite"):
        Distribution(count=1, p50=value, p95=1.0, maximum=1.0)


def test_zero_sample_distribution_requires_null_metrics():
    with pytest.raises(ValueError, match="zero-sample distribution"):
        Distribution(count=0, p50=1.0, p95=2.0, maximum=3.0)


@pytest.mark.parametrize(
    ("p50", "p95", "maximum"),
    [(None, 1.0, 1.0), (1.0, None, 1.0), (1.0, 1.0, None)],
)
def test_positive_sample_distribution_requires_all_metrics(p50, p95, maximum):
    with pytest.raises(ValueError, match="positive-sample distribution"):
        Distribution(count=1, p50=p50, p95=p95, maximum=maximum)


@pytest.mark.parametrize(
    ("p50", "p95", "maximum"),
    [(2.0, 1.0, 3.0), (1.0, 3.0, 2.0)],
)
def test_distribution_metrics_must_be_ordered(p50, p95, maximum):
    with pytest.raises(ValueError, match="p50 <= p95 <= maximum"):
        Distribution(count=2, p50=p50, p95=p95, maximum=maximum)
