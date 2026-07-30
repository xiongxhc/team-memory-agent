"""Pure safety tests for the Windows CI scheduler smoke-test time selection."""

from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import subprocess

import pytest

import teammem.schedule_windows as windows


def _smoke_module():
    path = Path(__file__).parents[1] / "scripts" / "windows-schedule-smoke.py"
    spec = spec_from_file_location("windows_schedule_smoke", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_times_are_future_and_on_the_current_local_date():
    smoke = _smoke_module()
    now = datetime(2026, 7, 29, 12, 0, 30)

    assert smoke._future_schedule_times(now) == ("12:10", "12:20")


def test_smoke_times_refuse_to_install_when_the_window_crosses_midnight():
    smoke = _smoke_module()
    now = datetime(2026, 7, 29, 23, 41)

    assert smoke._future_schedule_times(now) is None


def test_smoke_task_shape_reports_structure_without_values():
    smoke = _smoke_module()
    text = """\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4">
  <RegistrationInfo>
    <Date>2026-07-30T08:13:39</Date>
    <Author>CI\\runneradmin</Author>
  </RegistrationInfo>
  <Principals><Principal id="Author"><UserId>S-1-5-21-secret</UserId></Principal></Principals>
  <Actions Context="Author"><Exec><Command>C:\\secret\\teammem.exe</Command>
    <Arguments>--env-file C:\\secret\\hub.env run-daily</Arguments></Exec></Actions>
</Task>
"""
    xml = b"\xef\xbb\xbf" + text.encode("utf-8")

    shape = smoke._safe_task_shape(xml)

    for structural_name in (
        "signature=utf8-bom",
        "RegistrationInfo",
        "Date",
        "Author",
        "Principal",
        "id",
        "Actions",
        "Context",
    ):
        assert structural_name in shape
    for secret_value in ("runneradmin", "S-1-5-21-secret", "C:\\secret", "--env-file"):
        assert secret_value not in shape


def test_smoke_unparseable_shape_reports_only_bounded_byte_profile():
    smoke = _smoke_module()

    shape = smoke._safe_task_shape(b"\x00\xffsecret-value")

    assert "signature=other" in shape
    assert "length=14" in shape
    assert "zero-even=" in shape
    assert "zero-odd=" in shape
    assert "secret-value" not in shape


def test_smoke_mismatch_report_contains_only_safe_categories(capsys):
    smoke = _smoke_module()
    schedule = windows.WindowsSchedule(
        sid="S-1-5-21-expected-secret",
        task_name=windows.task_name("S-1-5-21-expected-secret"),
        time="18:20",
        executable=r"C:\expected-secret\teammem.exe",
        env_file=r"C:\expected-secret\hub.env",
    )
    text = windows.build_task_xml(schedule)[2:].decode("utf-16-le")
    text = text.replace(
        "<UserId>S-1-5-21-expected-secret</UserId>",
        "<UserId>S-1-5-21-observed-secret</UserId>",
        1,
    )
    runner = smoke._CapturingRunner()
    runner.last_query_xml = windows.build_task_xml(schedule)
    runner.candidate_xml = b"\xef\xbb\xbf" + text.encode("utf-8")

    smoke._report_task_shape(runner, schedule)

    candidate_diagnostic = capsys.readouterr().err
    runner.last_query_xml = runner.candidate_xml
    runner.candidate_xml = None
    smoke._report_task_shape(runner, schedule)
    fallback_diagnostic = capsys.readouterr().err

    for diagnostic in (candidate_diagnostic, fallback_diagnostic):
        assert "Mismatch categories: principal.sid" in diagnostic
    for secret in (
        "S-1-5-21-expected-secret",
        "S-1-5-21-observed-secret",
        r"C:\expected-secret",
    ):
        assert secret not in candidate_diagnostic
        assert secret not in fallback_diagnostic


def test_smoke_capture_keeps_replacement_candidate_through_rollback(monkeypatch):
    smoke = _smoke_module()
    snapshot = b"snapshot"
    first_candidate = b"first-candidate"
    replacement_candidate = b"replacement-candidate"
    rollback = b"rollback"
    outputs = iter(
        (
            snapshot,
            b"",
            first_candidate,
            first_candidate,
            b"",
            replacement_candidate,
            b"",
            rollback,
        )
    )

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, next(outputs), b"")

    monkeypatch.setattr(smoke.subprocess, "run", fake_run)
    runner = smoke._CapturingRunner()

    runner.arm()
    runner(["schtasks.exe", "/Query", "/XML"])
    runner(["schtasks.exe", "/Create", "/XML", "first.xml"])
    runner(["schtasks.exe", "/Query", "/XML"])
    runner.arm()
    runner(["schtasks.exe", "/Query", "/XML"])
    runner(["schtasks.exe", "/Create", "/XML", "replacement.xml"])
    runner(["schtasks.exe", "/Query", "/XML"])
    runner(["schtasks.exe", "/Create", "/XML", "rollback.xml"])
    runner(["schtasks.exe", "/Query", "/XML"])

    assert runner.candidate_xml == replacement_candidate
    assert runner.last_query_xml == rollback


def test_smoke_allows_only_github_hosted_runners(monkeypatch):
    smoke = _smoke_module()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")

    smoke._require_ci()


@pytest.mark.parametrize(
    "github_actions, runner_environment",
    [
        ("true", "self-hosted"),
        ("true", None),
        (None, "github-hosted"),
    ],
)
def test_smoke_rejects_non_github_hosted_runners(
    monkeypatch, github_actions, runner_environment
):
    smoke = _smoke_module()
    if github_actions is None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    else:
        monkeypatch.setenv("GITHUB_ACTIONS", github_actions)
    if runner_environment is None:
        monkeypatch.delenv("RUNNER_ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("RUNNER_ENVIRONMENT", runner_environment)

    with pytest.raises(RuntimeError, match="GitHub-hosted"):
        smoke._require_ci()
