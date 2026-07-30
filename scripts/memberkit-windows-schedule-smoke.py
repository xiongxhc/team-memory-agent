"""Exercise one disposable MemberKit Task Scheduler definition in Windows CI."""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import memberkit.schedule_windows as schedule_windows
import memberkit.windows_security as windows_security
from memberkit.config import default_config_file, load, write_config
from memberkit.schedule import install_schedule, remove_schedule, schedule_status
from memberkit.schedule_windows import (
    WindowsSchedule,
    _decode_xml,
    task_name,
    task_xml_mismatch_categories,
)
from memberkit.windows_security import current_user_sid, read_windows_private_text


_SAFE_SUFFIX = re.compile(r"[A-Za-z0-9_-]+\Z")
_FIRST_TRIGGER_DELAY = timedelta(minutes=10)
_REPLACEMENT_TRIGGER_DELAY = timedelta(minutes=20)
_ROLLOVER_WAIT_SECONDS = 21 * 60
_STRUCTURE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_MAX_SHAPE_ELEMENTS = 128
_MAX_SHAPE_CHARACTERS = 4096
_SENTINEL_VERSION = 1
_SMOKE_MEMBER = "memberkit-ci-smoke"
_SMOKE_INBOX = "file:///memberkit-windows-schedule-smoke-disabled"
_DIAGNOSTIC_PREFIX = "memberkit.private-config"
_FAILURE_CATEGORIES = (
    (FileNotFoundError, "file-not-found"),
    (FileExistsError, "file-exists"),
    (PermissionError, "permission"),
    (OSError, "os-error"),
    (ValueError, "value-error"),
    (RuntimeError, "runtime-error"),
)
_CLEANUP_ERRORS = (
    OSError,
    RuntimeError,
    subprocess.SubprocessError,
    ValueError,
)


class _PrivateConfigDiagnosticApi:
    """Value-free CI diagnostics around the real Windows filesystem API."""

    def __init__(
        self,
        delegate: Any,
        *,
        parent: Path,
        sid: str,
        destination: Path | None = None,
    ) -> None:
        self._delegate = delegate
        self._parent = Path(parent)
        self._destination = (
            None if destination is None else Path(destination)
        )
        self._sid = sid
        self._parent_handles: set[int] = set()
        self._events: list[dict[str, object]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def _start(self, stage: str) -> dict[str, object]:
        event: dict[str, object] = {"stage": stage}
        self._events.append(event)
        return event

    @staticmethod
    def _finish(
        event: dict[str, object],
        status: str,
        failure: Exception | None = None,
    ) -> None:
        event["status"] = status
        if failure is None:
            return
        event["category"] = next(
            (
                category
                for exception, category in _FAILURE_CATEGORIES
                if isinstance(failure, exception)
            ),
            "other",
        )
        for name in ("winerror", "errno"):
            value = getattr(failure, name, None)
            if type(value) is int:
                event[name] = value
                break

    def _call(
        self,
        stage: str,
        action: Callable[[], Any],
        *,
        missing: bool = False,
        event: dict[str, object] | None = None,
    ) -> Any:
        event = self._start(stage) if event is None else event
        try:
            result = action()
        except Exception as failure:
            status = "missing" if missing and isinstance(
                failure, FileNotFoundError
            ) else "failed"
            self._finish(event, status, failure)
            raise
        self._finish(event, "ok")
        return result

    @staticmethod
    def _delegate_open(
        opener: Callable[..., Any],
        path: Path,
        directory: bool,
        write_dac: bool,
    ) -> Any:
        kwargs = {"directory": directory}
        if write_dac:
            kwargs["write_dac"] = True
        return opener(path, **kwargs)

    def _open_parent(
        self,
        opener: Callable[..., Any],
        path: Path,
        *,
        directory: bool,
        write_dac: bool,
    ) -> Any:
        handle = self._call(
            "parent.open-write-dac" if write_dac else "parent.open-existing",
            lambda: self._delegate_open(opener, path, directory, write_dac),
            missing=not write_dac,
        )
        self._parent_handles.add(id(handle))
        return handle

    def open_file(
        self,
        path: Path,
        *,
        directory: bool = False,
        write_dac: bool = False,
    ) -> Any:
        if directory and Path(path) == self._parent:
            return self._open_parent(
                self._delegate.open_file,
                Path(path),
                directory=directory,
                write_dac=write_dac,
            )
        return self._delegate_open(
            self._delegate.open_file, Path(path), directory, write_dac
        )

    def create_directory(self, path: Path) -> Any:
        if Path(path) != self._parent:
            return self._delegate.create_directory(path)

        event = self._start("parent.create-directory")
        instance_opener = getattr(self._delegate, "__dict__", {}).get("open_file")
        original_opener = self._delegate.open_file

        def observed_opener(
            opened_path: Path,
            *,
            directory: bool = False,
            write_dac: bool = False,
        ) -> Any:
            if directory and Path(opened_path) == self._parent:
                return self._open_parent(
                    original_opener,
                    Path(opened_path),
                    directory=directory,
                    write_dac=write_dac,
                )
            return self._delegate_open(
                original_opener, Path(opened_path), directory, write_dac
            )

        setattr(self._delegate, "open_file", observed_opener)
        try:
            handle = self._call(
                "parent.create-directory",
                lambda: self._delegate.create_directory(path),
                event=event,
            )
        except FileExistsError as failure:
            self._finish(event, "exists", failure)
            raise
        else:
            self._parent_handles.add(id(handle))
            return handle
        finally:
            if instance_opener is not None:
                setattr(self._delegate, "open_file", instance_opener)
            else:
                delattr(self._delegate, "open_file")

    def apply_protected_dacl(
        self,
        handle: Any,
        sid: str,
        principals: Any,
    ) -> None:
        if id(handle) not in self._parent_handles:
            return self._delegate.apply_protected_dacl(handle, sid, principals)
        self._call(
            "parent.apply-dacl",
            lambda: self._delegate.apply_protected_dacl(handle, sid, principals),
        )

    def describe_handle(self, handle: Any) -> dict[str, Any]:
        if id(handle) not in self._parent_handles:
            return self._delegate.describe_handle(handle)
        event = self._start("parent.describe-handle")
        record = self._call(
            "parent.describe-handle",
            lambda: self._delegate.describe_handle(handle),
            event=event,
        )
        aces = record.get("allow_aces")
        event.update(
            owner_matches_current_sid=record.get("owner_sid") == self._sid,
            dacl_protected=record.get("dacl_protected") is True,
            disk=record.get("file_type") in {"disk", 1},
            directory=record.get("directory") is True,
            not_reparse=record.get("reparse_point") is False,
            acl_parseable=aces is not None,
            no_unapproved_read=aces is not None
            and not windows_security._unapproved_read_grant(aces, self._sid),
        )
        return record

    def close_handle(self, handle: Any) -> None:
        if id(handle) not in self._parent_handles:
            return self._delegate.close_handle(handle)
        self._call(
            "parent.close-handle",
            lambda: self._delegate.close_handle(handle),
        )
        self._parent_handles.remove(id(handle))

    def path_exists(self, path: Path) -> bool:
        if self._destination is None or Path(path) != self._destination:
            return self._delegate.path_exists(path)
        event = self._start("destination.path-exists")
        exists = self._call(
            "destination.path-exists",
            lambda: self._delegate.path_exists(path),
            event=event,
        )
        self._finish(event, "ok" if exists else "missing")
        return exists

    def lines(self) -> list[str]:
        def render(value: object) -> str:
            return str(value).lower() if type(value) is bool else str(value)

        return [
            _DIAGNOSTIC_PREFIX
            + " "
            + " ".join(f"{key}={render(value)}" for key, value in event.items())
            for event in self._events
        ]


def _write_config_with_diagnostics(
    values: dict[str, str],
    *,
    config_file: Path,
    sid: str,
    windows_api: Any = None,
) -> Path:
    delegate = (
        windows_security.NativeWindowsApi()
        if windows_api is None
        else windows_api
    )
    parent = Path(ntpath.dirname(str(config_file)))
    diagnostic_api = _PrivateConfigDiagnosticApi(
        delegate,
        parent=parent,
        destination=config_file,
        sid=sid,
    )
    try:
        return write_config(
            values,
            config_file=config_file,
            platform="win32",
            windows_api=diagnostic_api,
        )
    except Exception:
        for line in diagnostic_api.lines():
            print(line, file=sys.stderr)
        raise


def _structure_name(qualified: str) -> str:
    local = qualified.rsplit("}", 1)[-1]
    return local if _STRUCTURE_NAME.fullmatch(local) else "<unknown>"


def _xml_signature(xml: bytes) -> str:
    if xml.startswith(b"\xff\xfe"):
        return "utf16-le-bom"
    if xml.startswith(b"\xfe\xff"):
        return "utf16-be-bom"
    if xml.startswith(b"\xef\xbb\xbf"):
        return "utf8-bom"
    if xml.startswith(b"<\x00?\x00"):
        return "utf16-le"
    if xml.startswith(b"\x00<\x00?"):
        return "utf16-be"
    if xml.startswith(b"<?"):
        return "utf8"
    return "other"


def _safe_task_shape(xml: bytes) -> str:
    """Summarize XML structure without exposing element or attribute values."""
    sample = xml[:128]
    profile = (
        f"signature={_xml_signature(xml)};length={len(xml)};"
        f"zero-even={sample[0::2].count(0)};"
        f"zero-odd={sample[1::2].count(0)}"
    )
    try:
        root = ET.fromstring(_decode_xml(xml))
    except (ET.ParseError, UnicodeError, ValueError, TypeError):
        return profile + ";tags=<unparseable-task-xml>"

    entries: list[str] = []
    truncated = False

    def visit(element: ET.Element, parents: tuple[str, ...]) -> None:
        nonlocal truncated
        if len(entries) >= _MAX_SHAPE_ELEMENTS:
            truncated = True
            return
        path = parents + (_structure_name(element.tag),)
        attributes = sorted(_structure_name(name) for name in element.attrib)
        suffix = f"[attrs={','.join(attributes)}]" if attributes else ""
        entries.append("/".join(path) + suffix)
        for child in element:
            visit(child, path)
            if truncated:
                break

    visit(root, ())
    if truncated:
        entries.append("<truncated>")
    return (profile + ";tags=" + ";".join(entries))[:_MAX_SHAPE_CHARACTERS]


class _CapturingRunner:
    """Capture queried XML for value-free failure diagnostics."""

    def __init__(
        self,
        delegate: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
    ) -> None:
        self._delegate = (
            schedule_windows._default_runner if delegate is None else delegate
        )
        self.last_query_xml: bytes | None = None
        self.candidate_xml: bytes | None = None
        self._armed = False
        self._created = False

    def arm(self) -> None:
        self.candidate_xml = None
        self._armed = True
        self._created = False

    def __call__(
        self,
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        result = self._delegate(command, **kwargs)
        successful_query = (
            result.returncode == 0
            and "/Query" in command
            and "/XML" in command
            and isinstance(result.stdout, bytes)
        )
        if successful_query:
            self.last_query_xml = result.stdout
        if self._armed and result.returncode == 0 and "/Create" in command:
            self._created = True
        if self._armed and self._created and "/Query" in command and "/XML" in command:
            if successful_query:
                self.candidate_xml = result.stdout
            self._armed = False
        return result


def _report_task_shape(
    runner: _CapturingRunner,
    expected: WindowsSchedule | None,
) -> None:
    definition = (
        runner.candidate_xml
        if runner.candidate_xml is not None
        else runner.last_query_xml
    )
    if definition is None:
        return
    print(
        "Queried Task Scheduler XML shape: " + _safe_task_shape(definition),
        file=sys.stderr,
    )
    if expected is not None:
        categories = task_xml_mismatch_categories(definition, expected)
        print("Mismatch categories: " + ",".join(categories), file=sys.stderr)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument(
        "--suffix",
        default=os.environ.get("GITHUB_RUN_ID", ""),
        help="GitHub run ID used only for disposable paths and ownership",
    )
    return parser.parse_args()


def _require_ci() -> None:
    if (
        sys.platform != "win32"
        or os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or os.environ.get("RUNNER_OS") != "Windows"
    ):
        raise RuntimeError(
            "MemberKit scheduler smoke requires GitHub-hosted Windows Actions"
        )


def _memberkit_executable() -> str:
    command = shutil.which("memberkit.exe")
    if not command:
        raise RuntimeError("an absolute memberkit.exe is required")
    resolved = Path(command).resolve()
    if not resolved.is_absolute() or resolved.name.lower() != "memberkit.exe":
        raise RuntimeError("an absolute memberkit.exe is required")
    return str(resolved)


def _future_schedule_times(now: datetime) -> tuple[str, str] | None:
    first = now + _FIRST_TRIGGER_DELAY
    replacement = now + _REPLACEMENT_TRIGGER_DELAY
    if first.date() != now.date() or replacement.date() != now.date():
        return None
    return first.strftime("%H:%M"), replacement.strftime("%H:%M")


def _select_future_schedule_times() -> tuple[str, str]:
    deadline = time.monotonic() + _ROLLOVER_WAIT_SECONDS
    while True:
        selected = _future_schedule_times(datetime.now().astimezone())
        if selected is not None:
            return selected
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("smoke.time-window-unavailable")
        time.sleep(min(60, remaining))


def _paths(suffix: str) -> tuple[Path, Path]:
    if not suffix or _SAFE_SUFFIX.fullmatch(suffix) is None:
        raise ValueError("smoke suffix must contain only letters, digits, _ and -")
    root = os.environ.get("RUNNER_TEMP")
    if not root:
        raise RuntimeError("RUNNER_TEMP is required on GitHub Actions")
    base = Path(root)
    return (
        base / f"memberkit-windows-smoke-work-{suffix}",
        base / f"memberkit-windows-smoke-state-{suffix}",
    )


def _sentinel_path(suffix: str) -> Path:
    workdir, _state_dir = _paths(suffix)
    return workdir.parent / f"memberkit-windows-smoke-owner-{suffix}.json"


def _sentinel_payload(
    suffix: str,
    executable: str,
    schedule_times: tuple[str, str],
) -> dict[str, object]:
    return {
        "version": _SENTINEL_VERSION,
        "run_id": suffix,
        "executable": executable,
        "schedule_times": list(schedule_times),
    }


def _write_sentinel(
    path: Path,
    suffix: str,
    executable: str,
    schedule_times: tuple[str, str],
) -> None:
    text = json.dumps(
        _sentinel_payload(suffix, executable, schedule_times),
        sort_keys=True,
        separators=(",", ":"),
    )
    created = False
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            created = True
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise


def _read_sentinel(
    path: Path,
    suffix: str,
    executable: str,
) -> tuple[str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or set(data) != {
        "version",
        "run_id",
        "executable",
        "schedule_times",
    }:
        return None
    times = data.get("schedule_times")
    if (
        data.get("version") != _SENTINEL_VERSION
        or data.get("run_id") != suffix
        or data.get("executable") != executable
        or not isinstance(times, list)
        or len(times) != 2
        or not all(
            isinstance(value, str)
            and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value)
            for value in times
        )
    ):
        return None
    return times[0], times[1]


def _config_values(db: Path, workdir: Path) -> dict[str, str]:
    return {
        "MEMBERKIT_MEMBER": _SMOKE_MEMBER,
        "MEMBERKIT_INBOX_URL": _SMOKE_INBOX,
        "MEMBERKIT_DB": str(db),
        "MEMBERKIT_WORKDIR": str(workdir),
    }


def _config_text(db: Path, workdir: Path) -> str:
    values = _config_values(db, workdir)
    return "".join(f"{name}={value}\n" for name, value in values.items())


def _create_empty_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE observations ("
            "project TEXT, title TEXT, subtitle TEXT, narrative TEXT, "
            "type TEXT, created_at TEXT, created_at_epoch INTEGER)"
        )
        connection.commit()
    finally:
        connection.close()


def _database_path(state_dir: Path) -> Path:
    return state_dir.with_name(state_dir.name + "-claude-mem.db")


def _schedule_status(
    executable: str,
    runner: _CapturingRunner,
    state_dir: Path,
):
    return schedule_status(
        platform="win32",
        windows_runner=runner,
        windows_state_dir=state_dir,
        windows_executable=executable,
    )


def _task_expected(
    sid: str,
    executable: str,
    schedule_time: str,
) -> WindowsSchedule:
    return WindowsSchedule(
        sid=sid,
        task_name=task_name(sid),
        time=schedule_time,
        executable=executable,
    )


def _default_config_is_absent() -> bool:
    return not os.path.lexists(default_config_file(platform="win32"))


def run_smoke(suffix: str) -> None:
    _require_ci()
    executable = _memberkit_executable()
    workdir, state_dir = _paths(suffix)
    sentinel = _sentinel_path(suffix)
    config_file = default_config_file(platform="win32")
    sid = current_user_sid()
    runner = _CapturingRunner()
    expected: WindowsSchedule | None = None

    if not _default_config_is_absent():
        raise RuntimeError("precondition.config-present")
    initial = _schedule_status(executable, runner, state_dir)
    if initial.installed:
        raise RuntimeError("precondition.task-present")

    install_time, replace_time = _select_future_schedule_times()
    db = _database_path(state_dir)
    if any(
        os.path.lexists(path)
        for path in (workdir, state_dir, db, sentinel)
    ):
        raise RuntimeError("precondition.smoke-path-present")
    _write_sentinel(
        sentinel,
        suffix,
        executable,
        (install_time, replace_time),
    )

    workdir.mkdir(parents=False, exist_ok=False)
    _create_empty_database(db)
    _write_config_with_diagnostics(
        _config_values(db, workdir),
        config_file=config_file,
        sid=sid,
    )
    config = load(
        env={"APPDATA": os.environ["APPDATA"]},
        platform="win32",
    )
    if config_file != default_config_file(platform="win32"):
        raise RuntimeError("config.path-mismatch")

    try:
        expected = _task_expected(sid, executable, install_time)
        runner.arm()
        install_schedule(
            config,
            time=install_time,
            platform="win32",
            executable=executable,
            windows_runner=runner,
            windows_state_dir=state_dir,
        )
        installed = _schedule_status(executable, runner, state_dir)
        if not installed.installed or installed.time != install_time:
            raise RuntimeError("task.install-validation")

        expected = _task_expected(sid, executable, replace_time)
        runner.arm()
        install_schedule(
            config,
            time=replace_time,
            platform="win32",
            executable=executable,
            windows_runner=runner,
            windows_state_dir=state_dir,
        )
        replaced = _schedule_status(executable, runner, state_dir)
        if not replaced.installed or replaced.time != replace_time:
            raise RuntimeError("task.replace-validation")

        if not remove_schedule(
            platform="win32",
            windows_runner=runner,
            windows_state_dir=state_dir,
            windows_executable=executable,
        ):
            raise RuntimeError("task.remove-validation")
        if _schedule_status(executable, runner, state_dir).installed:
            raise RuntimeError("task.remove-validation")

        if any(workdir.iterdir()):
            raise RuntimeError("schedule.action-executed")
    except RuntimeError:
        _report_task_shape(runner, expected)
        raise


def _path_absent(path: Path) -> bool:
    try:
        return not os.path.lexists(path)
    except OSError:
        return False


def _remove_file(path: Path) -> bool:
    if _path_absent(path):
        return True
    try:
        path.unlink()
    except OSError:
        pass
    return _path_absent(path)


def _remove_tree(path: Path) -> bool:
    if _path_absent(path):
        return True
    try:
        shutil.rmtree(path)
    except OSError:
        pass
    return _path_absent(path)


def _cleanup(suffix: str) -> None:
    _require_ci()
    executable = _memberkit_executable()
    workdir, state_dir = _paths(suffix)
    sentinel = _sentinel_path(suffix)
    config_file = default_config_file(platform="win32")
    db = _database_path(state_dir)
    schedule_times = _read_sentinel(sentinel, suffix, executable)
    if schedule_times is None:
        runner = _CapturingRunner()
        config_present = not _path_absent(config_file)
        try:
            task_present = _schedule_status(
                executable,
                runner,
                state_dir,
            ).installed
        except _CLEANUP_ERRORS:
            _report_task_shape(runner, None)
            task_present = True
        artifacts_present = any(
            not _path_absent(path)
            for path in (workdir, state_dir, db, sentinel)
        )
        if config_present or task_present or artifacts_present:
            raise RuntimeError("cleanup.ownership-sentinel")
        return

    runner = _CapturingRunner()
    failures: set[str] = set()

    try:
        status = _schedule_status(executable, runner, state_dir)
    except _CLEANUP_ERRORS:
        _report_task_shape(runner, None)
        failures.add("task")
    else:
        if status.installed and status.time not in schedule_times:
            _report_task_shape(runner, None)
            failures.add("task")
        elif status.installed:
            try:
                remove_schedule(
                    platform="win32",
                    windows_runner=runner,
                    windows_state_dir=state_dir,
                    windows_executable=executable,
                )
            except _CLEANUP_ERRORS:
                _report_task_shape(runner, None)
                failures.add("task")
            else:
                try:
                    removed = _schedule_status(executable, runner, state_dir)
                except _CLEANUP_ERRORS:
                    _report_task_shape(runner, None)
                    failures.add("task")
                else:
                    if removed.installed:
                        _report_task_shape(runner, None)
                        failures.add("task")

    if not _path_absent(config_file):
        try:
            sid = current_user_sid()
            exact_config = (
                read_windows_private_text(config_file, sid)
                == _config_text(db, workdir)
            )
        except _CLEANUP_ERRORS:
            exact_config = False
        if exact_config:
            if not _remove_file(config_file):
                failures.add("config")
        else:
            failures.add("config")

    for category, path, remover in (
        ("workdir", workdir, _remove_tree),
        ("state", state_dir, _remove_tree),
        ("db", db, _remove_file),
    ):
        if not remover(path):
            failures.add(category)

    if failures:
        raise RuntimeError("cleanup.failure:" + ",".join(sorted(failures)))

    if not _remove_file(sentinel):
        raise RuntimeError("cleanup.failure:sentinel")


def main() -> int:
    args = _arguments()
    _require_ci()
    if args.cleanup_only:
        _cleanup(args.suffix)
        return 0
    try:
        run_smoke(args.suffix)
    finally:
        _cleanup(args.suffix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
