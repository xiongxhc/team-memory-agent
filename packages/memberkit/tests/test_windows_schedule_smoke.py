"""Pure safety tests for the MemberKit Windows scheduler CI smoke."""

import errno
import shutil
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


class _ParentProvisioningApi:
    def __init__(self, parent, sid):
        self.parent = Path(parent)
        self.sid = sid
        self.handle = object()
        self.calls = []

    def current_process_sid(self):
        return self.sid

    def open_file(self, path, *, directory=False, write_dac=False):
        self.calls.append(("open_file", Path(path), directory, write_dac))
        if Path(path) == self.parent and directory and not write_dac:
            failure = FileNotFoundError(errno.ENOENT, "PLANTED_EXCEPTION")
            failure.winerror = 3
            raise failure
        return self.handle

    def create_directory(self, path):
        self.calls.append(("create_directory", Path(path)))
        return self.open_file(path, directory=True, write_dac=True)

    def apply_protected_dacl(self, handle, sid, principals):
        self.calls.append(("apply_protected_dacl", handle, sid, tuple(principals)))

    def describe_handle(self, handle):
        self.calls.append(("describe_handle", handle))
        return {
            "owner_sid": self.sid,
            "dacl_protected": True,
            "file_type": "disk",
            "directory": True,
            "reparse_point": False,
            "allow_aces": [
                (self.sid, 0x10000000),
                ("S-1-5-18", 0x10000000),
                ("S-1-5-32-544", 0x10000000),
            ],
        }

    def close_handle(self, handle):
        self.calls.append(("close_handle", handle))

    def path_exists(self, path):
        self.calls.append(("path_exists", Path(path)))
        return False


class _AtomicWriteApi(_ParentProvisioningApi):
    def __init__(self, parent, sid, *, fail_candidate=False):
        super().__init__(parent, sid)
        self.records = {}
        self.fail_candidate = fail_candidate

    def open_file(self, path, *, directory=False, write_dac=False):
        path = Path(path)
        if (
            path == self.parent
            and directory
            and not write_dac
            and path not in self.records
        ):
            failure = FileNotFoundError(errno.ENOENT, "PLANTED_EXCEPTION")
            failure.winerror = 3
            raise failure
        return self.records[path]

    def create_directory(self, path):
        record = {
            "owner_sid": self.sid,
            "dacl_protected": False,
            "file_type": "disk",
            "directory": True,
            "regular": False,
            "reparse_point": False,
            "allow_aces": [],
        }
        self.records[Path(path)] = record
        return self.open_file(path, directory=True, write_dac=True)

    def create_empty_file(self, path):
        if self.fail_candidate:
            raise PermissionError(errno.EACCES, "PLANTED_CANDIDATE_SECRET")
        record = {
            "owner_sid": self.sid,
            "dacl_protected": False,
            "file_type": "disk",
            "directory": False,
            "regular": True,
            "reparse_point": False,
            "allow_aces": [],
            "data": b"",
        }
        self.records[Path(path)] = record
        return record

    def apply_protected_dacl(self, handle, sid, principals):
        handle["dacl_protected"] = True
        handle["allow_aces"] = [
            (principal, 0x10000000) for principal in principals
        ]

    def describe_handle(self, handle):
        return handle

    def close_handle(self, handle):
        pass

    def path_exists(self, path):
        return Path(path) in self.records

    def write_utf8(self, handle, data):
        handle["data"] = bytes(data)

    def flush_handle(self, handle):
        pass

    def move_file(self, candidate, destination):
        self.records[Path(destination)] = self.records.pop(Path(candidate))

    def delete_file(self, path):
        self.records.pop(Path(path), None)


def test_real_write_config_emits_exact_safe_sequence_only_on_failure(
    capsys,
):
    smoke = _smoke_module()
    config_file = Path(r"C:\PLANTED_PATH\memberkit.env")
    parent = Path(r"C:\PLANTED_PATH")
    sid = "S-1-5-21-PLANTED-SID"
    values = {"MEMBERKIT_MEMBER": "PLANTED_CONFIG_SECRET"}

    assert smoke._write_config_with_diagnostics(
        values,
        config_file=config_file,
        sid=sid,
        windows_api=_AtomicWriteApi(parent, sid),
    ) == config_file
    assert capsys.readouterr().err == ""

    with pytest.raises(RuntimeError):
        smoke._write_config_with_diagnostics(
            values,
            config_file=config_file,
            sid=sid,
            windows_api=_AtomicWriteApi(parent, sid, fail_candidate=True),
        )

    diagnostic = capsys.readouterr().err
    assert diagnostic.splitlines() == [
        (
            "memberkit.private-config stage=parent.open-existing "
            "status=missing category=file-not-found winerror=3"
        ),
        "memberkit.private-config stage=parent.create-directory status=ok",
        "memberkit.private-config stage=parent.open-write-dac status=ok",
        "memberkit.private-config stage=parent.apply-dacl status=ok",
        (
            "memberkit.private-config stage=parent.describe-handle status=ok "
            "owner_matches_current_sid=true dacl_protected=true disk=true "
            "directory=true not_reparse=true acl_parseable=true "
            "no_unapproved_read=true"
        ),
        "memberkit.private-config stage=parent.close-handle status=ok",
        "memberkit.private-config stage=destination.path-exists status=missing",
    ]
    for secret in (
        r"C:\PLANTED_PATH",
        sid,
        "PLANTED_CONFIG_SECRET",
        "PLANTED_EXCEPTION",
        "PLANTED_CANDIDATE_SECRET",
        "Traceback",
    ):
        assert secret not in diagnostic


def test_destination_path_exists_diagnostics_distinguish_ok_and_failed():
    smoke = _smoke_module()
    destination = Path(r"C:\PLANTED_PATH\memberkit.env")
    parent = Path(r"C:\PLANTED_PATH")
    sid = "S-1-5-21-PLANTED-SID"

    class ExistingApi(_ParentProvisioningApi):
        def path_exists(self, path):
            return True

    existing = smoke._PrivateConfigDiagnosticApi(
        ExistingApi(parent, sid),
        parent=parent,
        destination=destination,
        sid=sid,
    )
    assert existing.path_exists(destination) is True
    assert existing.lines() == [
        "memberkit.private-config stage=destination.path-exists status=ok"
    ]

    class FailingApi(_ParentProvisioningApi):
        def path_exists(self, path):
            raise OSError(errno.EIO, "PLANTED_PATH_EXISTS_SECRET")

    failing = smoke._PrivateConfigDiagnosticApi(
        FailingApi(parent, sid),
        parent=parent,
        destination=destination,
        sid=sid,
    )
    with pytest.raises(OSError):
        failing.path_exists(destination)
    assert failing.lines() == [
        (
            "memberkit.private-config stage=destination.path-exists "
            "status=failed category=os-error errno=5"
        )
    ]


def test_private_config_diagnostics_reduce_unsafe_handle_to_false_booleans():
    smoke = _smoke_module()
    parent = Path(r"C:\PLANTED_PATH")
    sid = "S-1-5-21-CURRENT"
    record = {
        "owner_sid": "S-1-5-21-FOREIGN-PLANTED",
        "dacl_protected": False,
        "file_type": "pipe",
        "directory": False,
        "reparse_point": True,
        "allow_aces": [("S-1-1-0", 0x80000000)],
    }

    class DescribeApi(_ParentProvisioningApi):
        def describe_handle(self, handle):
            return record

    delegate = DescribeApi(parent, sid)
    api = smoke._PrivateConfigDiagnosticApi(delegate, parent=parent, sid=sid)
    handle = api.create_directory(parent)
    api.describe_handle(handle)

    diagnostic = api.lines()[-1]
    assert diagnostic == (
        "memberkit.private-config stage=parent.describe-handle status=ok "
        "owner_matches_current_sid=false dacl_protected=false disk=false "
        "directory=false not_reparse=false acl_parseable=true "
        "no_unapproved_read=false"
    )
    for secret in (
        r"C:\PLANTED_PATH",
        "S-1-5-21-CURRENT",
        "S-1-5-21-FOREIGN-PLANTED",
        "S-1-1-0",
        "2147483648",
    ):
        assert secret not in diagnostic


def test_private_config_diagnostics_delegate_unrelated_operations_transparently():
    smoke = _smoke_module()
    parent = Path(r"C:\PLANTED_PATH")
    delegate = _ParentProvisioningApi(parent, "S-1-5-21-CURRENT")
    api = smoke._PrivateConfigDiagnosticApi(
        delegate,
        parent=parent,
        sid="S-1-5-21-CURRENT",
    )

    assert api.path_exists(Path(r"C:\OTHER-PLANTED")) is False
    assert delegate.calls == [("path_exists", Path(r"C:\OTHER-PLANTED"))]
    assert api.lines() == []


def test_capturing_runner_defaults_to_the_production_bounded_runner(monkeypatch):
    smoke = _smoke_module()
    command = ["schtasks.exe", "/Query", "/TN", "\\smoke", "/XML"]

    def bounded_runner(observed_command, **kwargs):
        assert observed_command == command
        assert kwargs == {"capture_output": True, "text": False}
        return smoke.subprocess.CompletedProcess(
            observed_command,
            0,
            b"<Task/>",
            b"",
        )

    monkeypatch.setattr(windows, "_default_runner", bounded_runner)
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "smoke runner must not call unbounded subprocess.run"
        ),
    )
    runner = smoke._CapturingRunner()

    result = runner(command, capture_output=True, text=False)

    assert result.stdout == b"<Task/>"
    assert runner.last_query_xml == b"<Task/>"


def test_capturing_runner_accepts_an_injected_byte_delegate():
    smoke = _smoke_module()
    outputs = iter((b"", b"<Task/>"))

    def delegate(command, **kwargs):
        assert kwargs == {"capture_output": True, "text": False}
        return smoke.subprocess.CompletedProcess(command, 0, next(outputs), b"")

    runner = smoke._CapturingRunner(delegate)
    runner.arm()
    runner(
        ["schtasks.exe", "/Create", "/TN", "\\smoke", "/XML", "candidate.xml"],
        capture_output=True,
        text=False,
    )
    result = runner(
        ["schtasks.exe", "/Query", "/TN", "\\smoke", "/XML"],
        capture_output=True,
        text=False,
    )

    assert result.stdout == b"<Task/>"
    assert runner.candidate_xml == b"<Task/>"


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
    monkeypatch.setattr(smoke, "_require_ci", lambda: None)
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


@pytest.mark.parametrize("entrypoint", ["run_smoke", "_cleanup"])
def test_imported_smoke_mutation_entrypoints_require_github_hosted_windows(
    monkeypatch,
    entrypoint,
):
    smoke = _smoke_module()
    monkeypatch.setattr(smoke.sys, "platform", "linux")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    monkeypatch.setenv("RUNNER_OS", "Windows")
    monkeypatch.setattr(
        smoke,
        "_memberkit_executable",
        lambda: pytest.fail("guard must run before executable resolution"),
    )

    with pytest.raises(RuntimeError, match="GitHub-hosted Windows"):
        getattr(smoke, entrypoint)("123")


def _owned_cleanup(monkeypatch, tmp_path):
    smoke = _smoke_module()
    workdir = tmp_path / "work"
    state_dir = tmp_path / "state"
    config_file = tmp_path / "memberkit.env"
    sentinel = tmp_path / "owner.json"
    executable = str((tmp_path / "memberkit.exe").resolve())
    schedule_times = ("12:10", "12:20")
    monkeypatch.setattr(smoke, "_require_ci", lambda: None)
    monkeypatch.setattr(smoke, "_memberkit_executable", lambda: executable)
    monkeypatch.setattr(smoke, "_paths", lambda _suffix: (workdir, state_dir))
    monkeypatch.setattr(smoke, "_sentinel_path", lambda _suffix: sentinel)
    monkeypatch.setattr(
        smoke,
        "default_config_file",
        lambda **_kwargs: config_file,
    )
    monkeypatch.setattr(smoke, "current_user_sid", lambda: "S-1-5-21-smoke")
    smoke._write_sentinel(sentinel, "123", executable, schedule_times)
    return SimpleNamespace(
        smoke=smoke,
        workdir=workdir,
        state_dir=state_dir,
        db=smoke._database_path(state_dir),
        config_file=config_file,
        sentinel=sentinel,
        executable=executable,
        schedule_times=schedule_times,
    )


@pytest.mark.parametrize("failure_kind", ["query", "conflict", "remove"])
def test_task_cleanup_failure_retains_retry_authority(
    monkeypatch,
    tmp_path,
    failure_kind,
):
    owned = _owned_cleanup(monkeypatch, tmp_path)
    smoke = owned.smoke
    task_state = {"failure": True}

    def status(*_args):
        if not task_state["failure"]:
            return SimpleNamespace(installed=False, time=None)
        if failure_kind == "query":
            raise OSError("secret observed task")
        if failure_kind == "conflict":
            return SimpleNamespace(installed=True, time="13:00")
        return SimpleNamespace(installed=True, time=owned.schedule_times[0])

    def remove(**_kwargs):
        if task_state["failure"]:
            raise OSError("secret removal failure")
        return True

    monkeypatch.setattr(smoke, "_schedule_status", status)
    monkeypatch.setattr(smoke, "remove_schedule", remove)

    with pytest.raises(RuntimeError) as error:
        smoke._cleanup("123")

    assert "task" in str(error.value)
    assert "secret" not in str(error.value)
    assert owned.sentinel.exists()

    task_state["failure"] = False
    smoke._cleanup("123")
    assert not owned.sentinel.exists()


def test_foreign_task_and_config_are_preserved_with_retry_authority(
    monkeypatch,
    tmp_path,
):
    owned = _owned_cleanup(monkeypatch, tmp_path)
    owned.config_file.write_text("foreign-config-value", encoding="utf-8")
    monkeypatch.setattr(
        owned.smoke,
        "_schedule_status",
        lambda *_args: SimpleNamespace(installed=True, time="13:00"),
    )
    monkeypatch.setattr(
        owned.smoke,
        "read_windows_private_text",
        lambda *_args: "foreign-config-value",
    )

    with pytest.raises(RuntimeError) as error:
        owned.smoke._cleanup("123")

    assert "config" in str(error.value)
    assert "task" in str(error.value)
    assert "foreign-config-value" not in str(error.value)
    assert owned.config_file.read_text(encoding="utf-8") == "foreign-config-value"
    assert owned.sentinel.exists()


def test_config_delete_failure_retains_sentinel_and_can_retry(
    monkeypatch,
    tmp_path,
):
    owned = _owned_cleanup(monkeypatch, tmp_path)
    owned.config_file.write_text("owned", encoding="utf-8")
    monkeypatch.setattr(
        owned.smoke,
        "_schedule_status",
        lambda *_args: SimpleNamespace(installed=False, time=None),
    )
    monkeypatch.setattr(
        owned.smoke,
        "read_windows_private_text",
        lambda *_args: owned.smoke._config_text(owned.db, owned.workdir),
    )
    original_unlink = Path.unlink
    failure = {"active": True}

    def unlink(path, *args, **kwargs):
        if path == owned.config_file and failure["active"]:
            raise OSError("secret config delete failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(RuntimeError) as error:
        owned.smoke._cleanup("123")

    assert "config" in str(error.value)
    assert "secret" not in str(error.value)
    assert owned.config_file.exists()
    assert owned.sentinel.exists()

    failure["active"] = False
    owned.smoke._cleanup("123")
    assert not owned.config_file.exists()
    assert not owned.sentinel.exists()


@pytest.mark.parametrize("artifact", ["workdir", "state_dir", "db"])
def test_artifact_delete_failure_is_reported_and_cleanup_can_retry(
    monkeypatch,
    tmp_path,
    artifact,
):
    owned = _owned_cleanup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        owned.smoke,
        "_schedule_status",
        lambda *_args: SimpleNamespace(installed=False, time=None),
    )
    target = getattr(owned, artifact)
    if artifact == "db":
        target.write_text("owned database", encoding="utf-8")
    else:
        target.mkdir()
        (target / "owned").write_text("owned artifact", encoding="utf-8")
    failure = {"active": True}
    original_rmtree = shutil.rmtree
    original_unlink = Path.unlink

    def rmtree(path, *args, **kwargs):
        if Path(path) == target and failure["active"]:
            raise OSError("secret tree delete failure")
        return original_rmtree(path, *args, **kwargs)

    def unlink(path, *args, **kwargs):
        if path == target and failure["active"]:
            raise OSError("secret file delete failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(owned.smoke.shutil, "rmtree", rmtree)
    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(RuntimeError) as error:
        owned.smoke._cleanup("123")

    expected_category = "state" if artifact == "state_dir" else artifact
    assert expected_category in str(error.value)
    assert "secret" not in str(error.value)
    assert target.exists()
    assert owned.sentinel.exists()

    failure["active"] = False
    owned.smoke._cleanup("123")
    assert not target.exists()
    assert not owned.sentinel.exists()


def test_sentinel_delete_failure_is_reported_and_can_retry(
    monkeypatch,
    tmp_path,
):
    owned = _owned_cleanup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        owned.smoke,
        "_schedule_status",
        lambda *_args: SimpleNamespace(installed=False, time=None),
    )
    failure = {"active": True}
    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == owned.sentinel and failure["active"]:
            raise OSError("secret sentinel delete failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(RuntimeError) as error:
        owned.smoke._cleanup("123")

    assert "sentinel" in str(error.value)
    assert "secret" not in str(error.value)
    assert owned.sentinel.exists()

    failure["active"] = False
    owned.smoke._cleanup("123")
    assert not owned.sentinel.exists()


def test_cleanup_can_remove_a_valid_last_remaining_sentinel(
    monkeypatch,
    tmp_path,
):
    owned = _owned_cleanup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        owned.smoke,
        "_schedule_status",
        lambda *_args: SimpleNamespace(installed=False, time=None),
    )

    owned.smoke._cleanup("123")

    assert not owned.sentinel.exists()
