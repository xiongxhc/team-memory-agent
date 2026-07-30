"""Pure safety tests for the MemberKit Windows scheduler CI smoke."""

from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

import memberkit.schedule_windows as windows


def _smoke_module():
    path = (
        Path(__file__).parents[3]
        / "scripts"
        / "memberkit-windows-schedule-smoke.py"
    )
    spec = spec_from_file_location("memberkit_windows_schedule_smoke", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_smoke_times_are_ten_and_twenty_minutes_ahead_on_same_date():
    smoke = _smoke_module()

    assert smoke._future_schedule_times(
        datetime(2026, 7, 30, 12, 0, 30)
    ) == ("12:10", "12:20")


def test_smoke_times_refuse_a_window_that_crosses_midnight():
    smoke = _smoke_module()

    assert smoke._future_schedule_times(datetime(2026, 7, 30, 23, 41)) is None


def test_smoke_requires_github_hosted_windows(monkeypatch):
    smoke = _smoke_module()
    monkeypatch.setattr(smoke.sys, "platform", "win32")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("RUNNER_OS", "Windows")

    smoke._require_ci()


@pytest.mark.parametrize(
    ("platform", "github_actions", "runner_environment", "runner_os"),
    [
        ("linux", "true", "github-hosted", "Windows"),
        ("win32", None, "github-hosted", "Windows"),
        ("win32", "true", "self-hosted", "Windows"),
        ("win32", "true", "github-hosted", "Linux"),
    ],
)
def test_smoke_rejects_every_non_github_hosted_windows_shape(
    monkeypatch,
    platform,
    github_actions,
    runner_environment,
    runner_os,
):
    smoke = _smoke_module()
    monkeypatch.setattr(smoke.sys, "platform", platform)
    for name, value in (
        ("GITHUB_ACTIONS", github_actions),
        ("RUNNER_ENVIRONMENT", runner_environment),
        ("RUNNER_OS", runner_os),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError, match="GitHub-hosted Windows"):
        smoke._require_ci()


def test_smoke_resolves_only_an_absolute_memberkit_exe(monkeypatch, tmp_path):
    smoke = _smoke_module()
    executable = tmp_path / "memberkit.exe"
    executable.touch()
    monkeypatch.setattr(smoke.shutil, "which", lambda name: str(executable))

    assert smoke._memberkit_executable() == str(executable.resolve())


@pytest.mark.parametrize("resolved", [None, "memberkit", r"C:\tools\memberkit"])
def test_smoke_rejects_missing_relative_or_non_exe_memberkit(
    monkeypatch, resolved
):
    smoke = _smoke_module()
    monkeypatch.setattr(smoke.shutil, "which", lambda _name: resolved)

    with pytest.raises(RuntimeError, match="absolute memberkit.exe"):
        smoke._memberkit_executable()


def test_smoke_task_shape_reports_structure_without_values():
    smoke = _smoke_module()
    text = """\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4">
  <RegistrationInfo>
    <Date>2026-07-30T08:13:39</Date>
    <Author>CI\\runneradmin</Author>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-21-observed-secret</UserId>
    </Principal>
  </Principals>
  <Actions Context="Author">
    <Exec>
      <Command>C:\\secret\\memberkit.exe</Command>
      <Arguments>scheduled-run</Arguments>
    </Exec>
  </Actions>
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
    for secret_value in (
        "runneradmin",
        "S-1-5-21-observed-secret",
        r"C:\secret",
        "scheduled-run",
    ):
        assert secret_value not in shape


def test_smoke_mismatch_report_contains_categories_but_no_values(capsys):
    smoke = _smoke_module()
    expected = windows.WindowsSchedule(
        sid="S-1-5-21-expected-secret",
        task_name=windows.task_name("S-1-5-21-expected-secret"),
        time="17:30",
        executable=r"C:\expected-secret\memberkit.exe",
    )
    text = windows.build_task_xml(expected)[2:].decode("utf-16-le")
    text = text.replace(
        "<UserId>S-1-5-21-expected-secret</UserId>",
        "<UserId>S-1-5-21-observed-secret</UserId>",
        1,
    )
    runner = smoke._CapturingRunner()
    runner.candidate_xml = b"\xef\xbb\xbf" + text.encode("utf-8")

    smoke._report_task_shape(runner, expected)

    diagnostic = capsys.readouterr().err
    assert "Mismatch categories: principal.sid" in diagnostic
    for secret in (
        "S-1-5-21-expected-secret",
        "S-1-5-21-observed-secret",
        r"C:\expected-secret",
        "17:30",
    ):
        assert secret not in diagnostic


def test_smoke_sentinel_creation_preserves_a_preexisting_owner(tmp_path):
    smoke = _smoke_module()
    sentinel = tmp_path / "owner.json"
    sentinel.write_text("foreign-owner", encoding="utf-8")

    with pytest.raises(FileExistsError):
        smoke._write_sentinel(
            sentinel,
            "123",
            str((tmp_path / "memberkit.exe").resolve()),
            ("12:10", "12:20"),
        )

    assert sentinel.read_text(encoding="utf-8") == "foreign-owner"


def test_cleanup_only_is_idempotent_when_no_smoke_artifacts_exist(
    monkeypatch,
    tmp_path,
):
    smoke = _smoke_module()
    workdir = tmp_path / "work"
    state_dir = tmp_path / "state"
    config_file = tmp_path / "memberkit.env"
    executable = str((tmp_path / "memberkit.exe").resolve())
    monkeypatch.setattr(smoke, "_memberkit_executable", lambda: executable)
    monkeypatch.setattr(smoke, "_paths", lambda _suffix: (workdir, state_dir))
    monkeypatch.setattr(smoke, "_sentinel_path", lambda _suffix: tmp_path / "owner")
    monkeypatch.setattr(
        smoke,
        "default_config_file",
        lambda **_kwargs: config_file,
    )
    monkeypatch.setattr(
        smoke,
        "_schedule_status",
        lambda *_args: SimpleNamespace(installed=False, time=None),
    )

    smoke._cleanup("123")

    assert not workdir.exists()
    assert not state_dir.exists()
    assert not config_file.exists()
