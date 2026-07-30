"""Exercise one disposable MemberKit Task Scheduler definition in Windows CI."""

from __future__ import annotations

import argparse
import json
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
from typing import Any

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

    def __init__(self) -> None:
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
        result = subprocess.run(command, **kwargs)
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


def _delete_task(name: str) -> None:
    """Delete an already revalidated smoke task without exposing native output."""
    subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", name, "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


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
    write_config(_config_values(db, workdir), platform="win32")
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


def _cleanup(suffix: str) -> None:
    executable = _memberkit_executable()
    workdir, state_dir = _paths(suffix)
    sentinel = _sentinel_path(suffix)
    schedule_times = _read_sentinel(sentinel, suffix, executable)
    if schedule_times is None:
        runner = _CapturingRunner()
        config_present = os.path.lexists(default_config_file(platform="win32"))
        try:
            task_present = _schedule_status(
                executable,
                runner,
                state_dir,
            ).installed
        except RuntimeError:
            _report_task_shape(runner, None)
            task_present = True
        if config_present or task_present:
            raise RuntimeError("cleanup.ownership-sentinel")
        return

    config_file = default_config_file(platform="win32")
    db = _database_path(state_dir)
    sid = current_user_sid()
    runner = _CapturingRunner()
    conflicts: list[str] = []
    try:
        try:
            status = _schedule_status(executable, runner, state_dir)
        except RuntimeError:
            _report_task_shape(runner, None)
            conflicts.append("task")
        else:
            if status.installed and status.time not in schedule_times:
                _report_task_shape(runner, None)
                conflicts.append("task")
            elif status.installed:
                try:
                    remove_schedule(
                        platform="win32",
                        windows_runner=runner,
                        windows_state_dir=state_dir,
                        windows_executable=executable,
                    )
                except RuntimeError:
                    _report_task_shape(runner, None)
                    conflicts.append("task")

        if os.path.lexists(config_file):
            try:
                exact_config = (
                    read_windows_private_text(config_file, sid)
                    == _config_text(db, workdir)
                )
            except (OSError, RuntimeError, ValueError):
                exact_config = False
            if exact_config:
                try:
                    config_file.unlink()
                except OSError:
                    conflicts.append("config")
            else:
                conflicts.append("config")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)
        try:
            db.unlink()
        except OSError:
            pass
        try:
            sentinel.unlink()
        except OSError:
            pass
        try:
            config_file.parent.rmdir()
        except OSError:
            pass

    if conflicts:
        raise RuntimeError("cleanup.conflict:" + ",".join(sorted(set(conflicts))))


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
