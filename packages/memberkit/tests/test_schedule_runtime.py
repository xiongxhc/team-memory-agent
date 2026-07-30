import sqlite3
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from memberkit import bundle, schedule, schedule_windows
from memberkit.config import Config


class UsernameApi:
    def __init__(self, username="Alex"):
        self.username = username
        self.calls = 0

    def current_username(self):
        self.calls += 1
        return self.username


class ReminderRunner:
    def __init__(self, *, returncode=0, failure=None):
        self.returncode = returncode
        self.failure = failure
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if self.failure is not None:
            raise self.failure
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            b"localized secret stdout",
            b"localized secret stderr",
        )


def runtime_config(tmp_path, rows=()):
    db = tmp_path / "claude-mem.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE observations (project TEXT, title TEXT, subtitle TEXT,"
        " narrative TEXT, type TEXT, created_at TEXT, created_at_epoch INTEGER)"
    )
    connection.executemany("INSERT INTO observations VALUES (?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()
    return Config(
        member="alex",
        db=db,
        inbox_url="https://secret-token@example.invalid/team/inbox.git",
        workdir=tmp_path / "work",
        timezone=ZoneInfo("UTC"),
    )


def runtime_row(title, iso):
    instant = datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo("UTC"))
    return (
        "project-alpha",
        title,
        None,
        None,
        "feature",
        iso,
        int(instant.timestamp() * 1000),
    )


def test_windows_reminder_targets_only_current_user_with_exact_safe_argv():
    api = UsernameApi()
    runner = ReminderRunner()

    result = schedule_windows.notify_pending(
        ["2026-07-27", "2026-07-28"],
        api=api,
        runner=runner,
    )

    assert result is None
    assert api.calls == 1
    assert runner.calls == [
        (
            [
                "msg.exe",
                "Alex",
                "/TIME:60",
                (
                    "MemberKit drafts ready for review: "
                    "2026-07-27, 2026-07-28"
                ),
            ],
            {
                "check": False,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "timeout": 5,
            },
        )
    ]
    assert "*" not in runner.calls[0][0]
    assert "shell" not in runner.calls[0][1]


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (
            FileNotFoundError("missing msg.exe with secret-token"),
            "reminder.missing-executable",
        ),
        (
            PermissionError("permission denied for secret-token"),
            "reminder.permission-denied",
        ),
        (
            subprocess.TimeoutExpired(
                ["msg.exe"],
                5,
                output=b"secret-token",
                stderr=b"secret-token",
            ),
            "reminder.timeout",
        ),
    ],
)
def test_windows_reminder_process_failures_return_fixed_safe_categories(
    failure,
    category,
):
    result = schedule_windows.notify_pending(
        ["2026-07-27"],
        api=UsernameApi(),
        runner=ReminderRunner(failure=failure),
    )

    assert result == category
    assert "secret-token" not in result


def test_windows_reminder_nonzero_exit_is_nonfatal_and_output_free():
    result = schedule_windows.notify_pending(
        ["2026-07-27"],
        api=UsernameApi(),
        runner=ReminderRunner(returncode=5),
    )

    assert result == "reminder.nonzero-exit"
    assert "localized" not in result


def test_windows_reminder_username_lookup_failure_is_nonfatal_and_secret_safe():
    class FailingUsernameApi:
        def current_username(self):
            raise OSError("secret-token from GetUserNameW")

    result = schedule_windows.notify_pending(
        ["2026-07-27"],
        api=FailingUsernameApi(),
        runner=ReminderRunner(),
    )

    assert result == "reminder.unavailable"
    assert "secret-token" not in result


@pytest.mark.parametrize(
    "date",
    [
        "2026-7-27",
        "2026-07-2",
        "2026-02-30",
        " 2026-07-27",
        "2026-07-27 ",
        "2026-07-27\nsecret-token",
        "",
    ],
)
def test_windows_reminder_rejects_non_iso_dates_before_process_execution(date):
    api = UsernameApi()
    runner = ReminderRunner()

    with pytest.raises(ValueError, match="ISO YYYY-MM-DD"):
        schedule_windows.notify_pending([date], api=api, runner=runner)

    assert api.calls == 0
    assert runner.calls == []


@pytest.mark.parametrize("username", ["", "*", "Alex\n*", "Alex\0Admin"])
def test_windows_reminder_never_uses_an_empty_wildcard_or_control_target(username):
    runner = ReminderRunner()

    result = schedule_windows.notify_pending(
        ["2026-07-27"],
        api=UsernameApi(username),
        runner=runner,
    )

    assert result == "reminder.invalid-target"
    assert runner.calls == []


def test_windows_reminder_with_no_dates_has_no_native_side_effect():
    api = UsernameApi()
    runner = ReminderRunner()

    assert schedule_windows.notify_pending([], api=api, runner=runner) is None
    assert api.calls == 0
    assert runner.calls == []


@pytest.mark.parametrize(
    ("platform", "backend_name", "runner_key"),
    [
        ("darwin", "macos", "runner"),
        ("win32", "windows", "runner"),
    ],
)
def test_reminder_facade_lazily_dispatches_only_the_selected_backend(
    monkeypatch,
    platform,
    backend_name,
    runner_key,
):
    calls = []
    backend = types.SimpleNamespace(
        notify_pending=lambda dates, **kwargs: (
            calls.append((dates, kwargs)) or "reminder.result"
        )
    )
    imports = []

    def load_backend(name):
        imports.append(name)
        return backend

    monkeypatch.setattr(schedule, "_load_backend", load_backend)
    result = schedule._notify_pending(
        ["2026-07-27"],
        platform=platform,
        macos_runner="macos-runner",
        windows_api="windows-api",
        windows_runner="windows-runner",
    )

    assert result == "reminder.result"
    assert imports == [backend_name]
    expected_kwargs = (
        {runner_key: "macos-runner"}
        if platform == "darwin"
        else {"api": "windows-api", runner_key: "windows-runner"}
    )
    assert calls == [(["2026-07-27"], expected_kwargs)]


def test_reminder_facade_skips_unsupported_platform_without_import(monkeypatch):
    monkeypatch.setattr(
        schedule,
        "_load_backend",
        lambda _name: (_ for _ in ()).throw(
            AssertionError("unsupported notification must not import a backend")
        ),
    )

    assert schedule._notify_pending(["2026-07-27"], platform="linux") is None


def test_bounded_log_normalizes_records_and_retains_one_capped_backup(tmp_path):
    path = tmp_path / "schedule.log"

    for index in range(12):
        schedule._append_bounded_log(
            path,
            f"record-{index}\r\nnext\0field\tvalue",
            max_bytes=48,
        )

    backup = Path(f"{path}.1")
    assert path.exists()
    assert backup.exists()
    assert path.stat().st_size <= 48
    assert backup.stat().st_size <= 48
    assert not Path(f"{path}.2").exists()
    for retained in (path, backup):
        text = retained.read_text(encoding="utf-8")
        assert "\r" not in text
        assert "\0" not in text
        assert len(text.splitlines()) == 1


def test_bounded_log_truncates_multibyte_record_without_splitting_utf8(tmp_path):
    path = tmp_path / "schedule.err"

    schedule._append_bounded_log(path, "é" * 100, max_bytes=17)

    assert path.stat().st_size <= 17
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_windows_scheduled_run_keeps_positional_contract_and_logs_safe_success(
    tmp_path,
    monkeypatch,
):
    cfg = runtime_config(
        tmp_path,
        [runtime_row("secret event summary", "2026-07-27T12:00:00")],
    )
    observed = {}

    def notify(dates, **kwargs):
        observed["notification"] = (dates, kwargs)
        return None

    monkeypatch.setattr(schedule, "_notify_pending", notify)
    sys.modules.pop("memberkit.push", None)

    pending = schedule.scheduled_run(
        cfg,
        datetime(2026, 7, 28, 18, 0),
        True,
        ZoneInfo("UTC"),
        platform="win32",
        windows_api="windows-api",
        windows_runner="windows-runner",
    )

    assert pending == ["2026-07-27"]
    assert observed == {
        "notification": (
            ["2026-07-27"],
            {
                "platform": "win32",
                "macos_runner": None,
                "windows_api": "windows-api",
                "windows_runner": "windows-runner",
            },
        )
    }
    log = (cfg.workdir / "schedule.log").read_text(encoding="utf-8")
    assert "2026-07-28T18:00:00+00:00" in log
    assert "2026-07-27" in log
    assert "alex" not in log
    assert "secret-token" not in log
    assert "secret event summary" not in log
    assert "journal" not in log
    assert "memberkit.push" not in sys.modules


def test_windows_scheduled_run_logs_fixed_reminder_failure_and_returns_dates(
    tmp_path,
    monkeypatch,
):
    cfg = runtime_config(
        tmp_path,
        [runtime_row("private event", "2026-07-27T12:00:00")],
    )
    monkeypatch.setattr(
        schedule,
        "_notify_pending",
        lambda _dates, **_kwargs: "reminder.nonzero-exit",
    )

    pending = schedule.scheduled_run(
        cfg,
        datetime(2026, 7, 28, 18, 0),
        platform="win32",
    )

    assert pending == ["2026-07-27"]
    error = (cfg.workdir / "schedule.err").read_text(encoding="utf-8")
    assert "phase=reminder" in error
    assert "reminder.nonzero-exit" in error
    assert "private event" not in error
    assert "secret-token" not in error


def test_windows_scheduled_run_logs_exception_class_then_reraises_draft_failure(
    tmp_path,
    monkeypatch,
):
    cfg = runtime_config(tmp_path)

    def fail_draft(*_args, **_kwargs):
        raise RuntimeError("secret-token https://private.invalid")

    monkeypatch.setattr(bundle, "draft", fail_draft)

    with pytest.raises(RuntimeError, match="secret-token"):
        schedule.scheduled_run(
            cfg,
            datetime(2026, 7, 28, 18, 0),
            False,
            ZoneInfo("UTC"),
            platform="win32",
        )

    error = (cfg.workdir / "schedule.err").read_text(encoding="utf-8")
    assert "phase=draft" in error
    assert "RuntimeError" in error
    assert "secret-token" not in error
    assert "private.invalid" not in error
