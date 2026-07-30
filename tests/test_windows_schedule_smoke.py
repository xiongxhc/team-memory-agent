"""Pure safety tests for the Windows CI scheduler smoke-test time selection."""

from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


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
    xml = b"""\
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

    shape = smoke._safe_task_shape(xml)

    for structural_name in ("RegistrationInfo", "Date", "Author", "Principal", "id", "Actions", "Context"):
        assert structural_name in shape
    for secret_value in ("runneradmin", "S-1-5-21-secret", "C:\\secret", "--env-file"):
        assert secret_value not in shape


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
