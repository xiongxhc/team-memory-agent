"""Windows Task Scheduler definition and transactional lifecycle helpers."""

from __future__ import annotations

import csv
import errno
import hashlib
import ntpath
import os
import re
import subprocess
import tempfile
import time as clock
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .windows_security import (
    _windows_filesystem_path,
    current_user_sid,
    provision_windows_state_dir,
    validate_windows_env_file,
)


_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_TAG = "{" + _NAMESPACE + "}"
_MANAGED_ERROR = "Windows schedule definition is not managed by TeamMem"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TASK_NAME = re.compile(r"\\TeamMem-Daily-[0-9a-f]{12}\Z")
_MAX_TASK_XML_BYTES = 1024 * 1024
OWNERSHIP_SOURCE = "TeamMem"
OWNERSHIP_DESCRIPTION = "TeamMem daily operator schedule"
_PRINCIPAL_ID = "Author"


@dataclass(frozen=True)
class WindowsSchedule:
    sid: str
    task_name: str
    time: str
    executable: str
    env_file: str | Path


def task_name(sid: str) -> str:
    """Create a stable non-secret root task name for one Windows user."""
    return "\\TeamMem-Daily-" + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]


def default_env_file(
    platform: str | None = None, env: Mapping[str, str] | None = None
) -> Path:
    """Expose the configuration default without importing it at module load."""
    from .config import default_env_file as configured_default

    return configured_default(platform=platform, env=env)


def _unsafe_argument(value: str) -> bool:
    return bool(_CONTROL.search(value))


def encode_arguments(arguments: Sequence[str]) -> str:
    """Encode an argv vector using the canonical Windows C runtime grammar."""
    encoded: list[str] = []
    for argument in arguments:
        if not isinstance(argument, str) or _unsafe_argument(argument):
            raise ValueError("unsafe Windows argument")
        if argument and not any(character.isspace() or character == '"' for character in argument):
            encoded.append(argument)
            continue
        pieces = ['"']
        backslashes = 0
        for character in argument:
            if character == "\\":
                backslashes += 1
                continue
            if character == '"':
                pieces.append("\\" * (backslashes * 2 + 1))
                pieces.append('"')
            else:
                pieces.append("\\" * backslashes)
                pieces.append(character)
            backslashes = 0
        pieces.append("\\" * (backslashes * 2))
        pieces.append('"')
        encoded.append("".join(pieces))
    return " ".join(encoded)


def decode_arguments(command_line: str) -> list[str]:
    """Decode the Windows C runtime command line grammar without a shell."""
    if not isinstance(command_line, str) or _unsafe_argument(command_line):
        raise ValueError("unsafe Windows argument")
    values: list[str] = []
    position = 0
    length = len(command_line)
    while position < length:
        while position < length and command_line[position].isspace():
            position += 1
        if position == length:
            break
        value: list[str] = []
        quoted = False
        while position < length:
            character = command_line[position]
            if character == "\\":
                start = position
                while position < length and command_line[position] == "\\":
                    position += 1
                slashes = position - start
                if position < length and command_line[position] == '"':
                    value.append("\\" * (slashes // 2))
                    if slashes % 2:
                        value.append('"')
                    else:
                        quoted = not quoted
                    position += 1
                else:
                    value.append("\\" * slashes)
                continue
            if character == '"':
                quoted = not quoted
                position += 1
                continue
            if character.isspace() and not quoted:
                break
            value.append(character)
            position += 1
        if quoted:
            raise ValueError("unsafe Windows argument")
        values.append("".join(value))
        while position < length and command_line[position].isspace():
            position += 1
    return values


def _absolute_windows_path(value: str | Path) -> str:
    path = str(value)
    if not _windows_filesystem_path(Path(path)):
        raise ValueError("Windows schedule paths must be absolute Windows paths")
    return path


def _is_shell(path: str) -> bool:
    return ntpath.basename(path).lower() in {"cmd.exe", "powershell.exe", "pwsh.exe"}


def _valid_task_name(value: str, sid: str) -> str:
    if not _TASK_NAME.fullmatch(value) or value != task_name(sid):
        raise ValueError("Windows task name must match the current SID")
    return value


def _children(parent: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in parent if _local(child.tag) == name]


def _only(parent: ET.Element, name: str) -> ET.Element:
    values = _children(parent, name)
    if len(values) != 1:
        raise ValueError(_MANAGED_ERROR)
    return values[0]


def _exact_children(parent: ET.Element, names: Sequence[str]) -> None:
    if sorted(_local(child.tag) for child in parent) != sorted(names):
        raise ValueError(_MANAGED_ERROR)


def _required_optional_children(
    parent: ET.Element,
    required: Sequence[str],
    optional: Sequence[str],
) -> None:
    names = [_local(child.tag) for child in parent]
    if (
        any(name not in tuple(required) + tuple(optional) for name in names)
        or any(names.count(name) != 1 for name in required)
        or any(names.count(name) > 1 for name in optional)
    ):
        raise ValueError(_MANAGED_ERROR)


def _text(parent: ET.Element, name: str) -> str:
    value = _only(parent, name).text
    if value is None:
        raise ValueError(_MANAGED_ERROR)
    return value


def _local(tag: str) -> str:
    if not tag.startswith(_TAG):
        raise ValueError(_MANAGED_ERROR)
    return tag[len(_TAG):]


def _add(parent: ET.Element, name: str, value: str | None = None) -> ET.Element:
    element = ET.SubElement(parent, _TAG + name)
    if value is not None:
        element.text = value
    return element


def _normal_time(value: str) -> str:
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("schedule time must be HH:MM")
    return value


def build_task_xml(schedule: WindowsSchedule) -> bytes:
    """Build deterministic UTF-16LE+BOM XML for exactly one managed task."""
    executable = _absolute_windows_path(schedule.executable)
    env_file = _absolute_windows_path(schedule.env_file)
    if _is_shell(executable):
        raise ValueError("Windows schedule executable must not be a shell")
    name = _valid_task_name(schedule.task_name, schedule.sid)
    time = _normal_time(schedule.time)
    arguments = encode_arguments(["--env-file", env_file, "run-daily"])

    ET.register_namespace("", _NAMESPACE)
    root = ET.Element(_TAG + "Task", {"version": "1.4"})
    registration = _add(root, "RegistrationInfo")
    _add(registration, "Source", OWNERSHIP_SOURCE)
    _add(registration, "URI", name)
    _add(registration, "Description", OWNERSHIP_DESCRIPTION)
    principals = _add(root, "Principals")
    principal = ET.SubElement(
        principals, _TAG + "Principal", {"id": _PRINCIPAL_ID}
    )
    _add(principal, "UserId", schedule.sid)
    _add(principal, "LogonType", "InteractiveToken")
    _add(principal, "RunLevel", "LeastPrivilege")
    triggers = _add(root, "Triggers")
    trigger = _add(triggers, "CalendarTrigger")
    _add(trigger, "StartBoundary", f"2000-01-01T{time}:00")
    daily = _add(trigger, "ScheduleByDay")
    _add(daily, "DaysInterval", "1")
    _add(trigger, "Enabled", "true")
    settings = _add(root, "Settings")
    for key, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("Enabled", "true"),
        ("StartWhenAvailable", "true"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("WakeToRun", "false"),
        ("ExecutionTimeLimit", "PT4H"),
    ):
        _add(settings, key, value)
    actions = ET.SubElement(root, _TAG + "Actions", {"Context": _PRINCIPAL_ID})
    action = _add(actions, "Exec")
    _add(action, "Command", executable)
    _add(action, "Arguments", arguments)
    declaration = '<?xml version="1.0" encoding="UTF-16"?>\r\n'.encode("utf-16-le")
    return b"\xff\xfe" + declaration + ET.tostring(root, encoding="unicode").encode("utf-16-le")


def _xml_transport(xml: bytes) -> tuple[str, int, str]:
    if xml.startswith(b"\xef\xbb\xbf"):
        return "utf-8", 3, "utf8-bom"
    if xml.startswith(b"\xff\xfe"):
        return "utf-16-le", 2, "utf16le-bom"
    if xml.startswith(b"\xfe\xff"):
        return "utf-16-be", 2, "utf16be-bom"
    probe = xml[:64]
    while probe.startswith((b" \x00", b"\t\x00", b"\r\x00", b"\n\x00")):
        probe = probe[2:]
    if probe.startswith(b"<\x00"):
        return "utf-16-le", 0, "utf16le"
    probe = xml[:64]
    while probe.startswith((b"\x00 ", b"\x00\t", b"\x00\r", b"\x00\n")):
        probe = probe[2:]
    if probe.startswith(b"\x00<"):
        return "utf-16-be", 0, "utf16be"
    ascii_probe = xml[:64].lstrip(b" \t\r\n")
    signature = "utf8-xml" if ascii_probe.startswith(b"<") else "other"
    return "utf-8", 0, signature


def _decode_xml(xml: bytes) -> str:
    if not isinstance(xml, bytes) or not xml or len(xml) > _MAX_TASK_XML_BYTES:
        raise ValueError(_MANAGED_ERROR)
    encoding, offset, _signature = _xml_transport(xml)
    return xml[offset:].decode(encoding, errors="strict")


def _invalid() -> RuntimeError:
    return RuntimeError(_MANAGED_ERROR)


class _TaskXmlMismatch(ValueError):
    def __init__(self, category: str) -> None:
        super().__init__(_MANAGED_ERROR)
        self.category = category


def _validate_task_xml(xml: bytes, expected: WindowsSchedule) -> str:
    """Validate one definition, retaining only a fixed internal failure category."""
    try:
        source = _decode_xml(xml)
        if "<!doctype" in source.lower() or "<!entity" in source.lower():
            raise ValueError(_MANAGED_ERROR)
        root = ET.fromstring(source)
        if root.tag != _TAG + "Task" or root.attrib != {"version": "1.4"}:
            raise ValueError(_MANAGED_ERROR)
        for element in root.iter():
            _local(element.tag)
        _valid_task_name(expected.task_name, expected.sid)
        _exact_children(
            root, ["RegistrationInfo", "Principals", "Triggers", "Settings", "Actions"]
        )
        registration = _only(root, "RegistrationInfo")
        _required_optional_children(
            registration,
            ["Source", "URI", "Description"],
            ["Date", "Author"],
        )
        for name in ("Date", "Author"):
            for element in _children(registration, name):
                if (
                    element.text is None
                    or element.text != element.text.strip()
                    or list(element)
                ):
                    raise ValueError(_MANAGED_ERROR)
        principals = _only(root, "Principals")
        _exact_children(principals, ["Principal"])
        principal = _only(principals, "Principal")
        _required_optional_children(
            principal, ["UserId", "LogonType"], ["RunLevel"]
        )
        triggers = _only(root, "Triggers")
        _exact_children(triggers, ["CalendarTrigger"])
        trigger = _only(triggers, "CalendarTrigger")
        _required_optional_children(
            trigger, ["StartBoundary", "ScheduleByDay"], ["Enabled"]
        )
        _exact_children(_only(trigger, "ScheduleByDay"), ["DaysInterval"])
        settings = _only(root, "Settings")
        required_settings = (
            (
                "MultipleInstancesPolicy",
                "IgnoreNew",
                "settings.multiple-instances-policy",
            ),
            ("StartWhenAvailable", "true", "settings.start-when-available"),
            (
                "DisallowStartIfOnBatteries",
                "false",
                "settings.disallow-start-on-batteries",
            ),
            (
                "StopIfGoingOnBatteries",
                "false",
                "settings.stop-on-batteries",
            ),
            ("ExecutionTimeLimit", "PT4H", "settings.execution-time-limit"),
        )
        default_settings = (
            ("Enabled", "true", "settings.enabled"),
            (
                "RunOnlyIfNetworkAvailable",
                "false",
                "settings.network-required",
            ),
            ("WakeToRun", "false", "settings.wake-to-run"),
        )
        service_settings = ("IdleSettings", "UseUnifiedSchedulingEngine")
        _required_optional_children(
            settings,
            [key for key, _value, _category in required_settings],
            [key for key, _value, _category in default_settings]
            + list(service_settings),
        )
        idle: ET.Element | None = None
        if _children(settings, "IdleSettings"):
            idle = _only(settings, "IdleSettings")
            _exact_children(idle, ["StopOnIdleEnd", "RestartOnIdle"])
        actions = _only(root, "Actions")
        _exact_children(actions, ["Exec"])
        action = _only(actions, "Exec")
        _exact_children(action, ["Command", "Arguments"])
        for element in root.iter():
            if element is root:
                continue
            if element is principal:
                if set(element.attrib) != {"id"}:
                    raise ValueError(_MANAGED_ERROR)
            elif element is actions:
                if set(element.attrib) != {"Context"}:
                    raise ValueError(_MANAGED_ERROR)
            elif element.attrib:
                raise ValueError(_MANAGED_ERROR)
    except (ET.ParseError, UnicodeError, ValueError, TypeError):
        raise _TaskXmlMismatch("xml.structure") from None

    def check(category: str, comparison: Callable[[], bool]) -> None:
        try:
            matches = comparison()
        except (UnicodeError, ValueError, TypeError):
            matches = False
        if not matches:
            raise _TaskXmlMismatch(category)

    def text_or_default(
        parent: ET.Element, name: str, default: str
    ) -> str:
        return _text(parent, name) if _children(parent, name) else default

    check(
        "registration.source",
        lambda: _text(registration, "Source") == OWNERSHIP_SOURCE,
    )
    check(
        "registration.uri",
        lambda: _text(registration, "URI") == expected.task_name,
    )
    check(
        "registration.description",
        lambda: _text(registration, "Description") == OWNERSHIP_DESCRIPTION,
    )
    check("principal.sid", lambda: _text(principal, "UserId") == expected.sid)
    check(
        "principal.logon-type",
        lambda: _text(principal, "LogonType") == "InteractiveToken",
    )
    check(
        "principal.run-level",
        lambda: text_or_default(principal, "RunLevel", "LeastPrivilege")
        == "LeastPrivilege",
    )
    check(
        "principal.binding",
        lambda: principal.attrib["id"] == _PRINCIPAL_ID,
    )
    try:
        time = _normal_time(expected.time)
    except (ValueError, TypeError):
        raise _TaskXmlMismatch("trigger.start-boundary") from None
    check(
        "trigger.start-boundary",
        lambda: _text(trigger, "StartBoundary") == f"2000-01-01T{time}:00",
    )
    check(
        "trigger.daily-interval",
        lambda: _text(_only(trigger, "ScheduleByDay"), "DaysInterval") == "1",
    )
    check(
        "trigger.enabled",
        lambda: text_or_default(trigger, "Enabled", "true") == "true",
    )
    for key, value, category in required_settings:
        check(
            category,
            lambda key=key, value=value: _text(settings, key) == value,
        )
    for key, value, category in default_settings:
        check(
            category,
            lambda key=key, value=value: text_or_default(settings, key, value)
            == value,
        )
    if idle is not None:
        check(
            "settings.idle",
            lambda: _text(idle, "StopOnIdleEnd") == "true"
            and _text(idle, "RestartOnIdle") == "false",
        )
    if _children(settings, "UseUnifiedSchedulingEngine"):
        check(
            "settings.unified-engine",
            lambda: _text(settings, "UseUnifiedSchedulingEngine") == "false",
        )
    check(
        "action.binding",
        lambda: actions.attrib["Context"] == _PRINCIPAL_ID,
    )
    check(
        "action.command",
        lambda: _text(action, "Command")
        == _absolute_windows_path(expected.executable)
        and not _is_shell(_text(action, "Command")),
    )

    def argv_matches() -> bool:
        arguments = _text(action, "Arguments")
        expected_args = [
            "--env-file",
            _absolute_windows_path(expected.env_file),
            "run-daily",
        ]
        return (
            decode_arguments(arguments) == expected_args
            and encode_arguments(expected_args) == arguments
        )

    check("action.argv", argv_matches)
    return time


def parse_task_xml(xml: bytes, expected: WindowsSchedule) -> str:
    """Accept only a complete managed definition and expose no mismatch details."""
    try:
        return _validate_task_xml(xml, expected)
    except _TaskXmlMismatch:
        raise _invalid() from None


def task_xml_mismatch_categories(
    xml: bytes, expected: WindowsSchedule
) -> tuple[str, ...]:
    """Return the one fixed internal failure category without observed values."""
    try:
        _validate_task_xml(xml, expected)
    except _TaskXmlMismatch as mismatch:
        return (mismatch.category,)
    return ()


WindowsRunner = Callable[..., subprocess.CompletedProcess[bytes]]
_CONFLICT = "Windows schedule definition conflicts with an existing task"
_UNAVAILABLE = "Windows scheduler status is unavailable"


def _default_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, **kwargs)


def _run(runner: WindowsRunner | None, command: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Run exactly one scheduler command with byte-preserving output."""
    try:
        result = (runner or _default_runner)(
            command, capture_output=True, text=False
        )
    except (OSError, subprocess.SubprocessError) as failure:
        raise RuntimeError(_UNAVAILABLE) from failure
    if not isinstance(result.stdout, bytes) or not isinstance(result.stderr, bytes):
        raise RuntimeError(_UNAVAILABLE)
    return result


def _task_name_for(sid: str, override: str | None) -> str:
    return _valid_task_name(task_name(sid) if override is None else override, sid)


def _csv_contains_task(output: bytes, name: str) -> bool:
    """Check Task Scheduler's stable CSV task-name field, not localised text."""
    try:
        if output.startswith(b"\xef\xbb\xbf"):
            text = output.decode("utf-8-sig")
        elif output.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = output.decode("utf-16")
        else:
            # Latin-1 is a one-to-one byte mapping, so the ASCII task name
            # survives any console code page without interpreting local text.
            text = output.decode("latin-1")
        rows = csv.reader(text.splitlines())
        return any(row and row[0] == name for row in rows)
    except (UnicodeError, csv.Error):
        raise RuntimeError(_UNAVAILABLE) from None


def _query_xml(
    name: str, runner: WindowsRunner | None
) -> bytes | None:
    """Return the exact definition or classify absence through the CSV listing."""
    result = _run(runner, ["schtasks.exe", "/Query", "/TN", name, "/XML"])
    if result.returncode == 0:
        return result.stdout
    listing = _run(runner, ["schtasks.exe", "/Query", "/FO", "CSV", "/NH"])
    if listing.returncode != 0:
        raise RuntimeError(_UNAVAILABLE)
    if not _csv_contains_task(listing.stdout, name):
        return None
    raise RuntimeError(_UNAVAILABLE)


def _xml_time(xml: bytes) -> str:
    """Extract only the trigger time needed to validate an otherwise fixed task."""
    try:
        source = _decode_xml(xml)
        if "<!doctype" in source.lower() or "<!entity" in source.lower():
            raise ValueError
        root = ET.fromstring(source)
        trigger = _only(_only(root, "Triggers"), "CalendarTrigger")
        boundary = _text(trigger, "StartBoundary")
        match = re.fullmatch(r"2000-01-01T([0-2][0-9]:[0-5][0-9]):00", boundary)
        if match is None:
            raise ValueError
        return match.group(1)
    except (ET.ParseError, UnicodeError, ValueError, TypeError):
        raise RuntimeError(_CONFLICT) from None


def _valid_snapshot(
    name: str,
    sid: str,
    executable: str,
    env_file: str | Path,
    runner: WindowsRunner | None,
) -> tuple[bytes | None, WindowsSchedule | None]:
    xml = _query_xml(name, runner)
    if xml is None:
        return None, None
    expected = WindowsSchedule(
        sid=sid,
        task_name=name,
        time=_xml_time(xml),
        executable=executable,
        env_file=env_file,
    )
    try:
        parse_task_xml(xml, expected)
    except RuntimeError:
        raise RuntimeError(_CONFLICT) from None
    return xml, expected


def _state_directory(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise RuntimeError("LOCALAPPDATA is required for Windows scheduling")
    return Path(root) / "TeamMemory"


def _lock_byte(locking: Callable[[int, int, int], Any], descriptor: int, sleep: Callable[[float], None]) -> None:
    """Retry non-blocking one-byte acquisition until the owner releases it."""
    while True:
        try:
            locking(descriptor, 2, 1)  # msvcrt.LK_NBLCK
            return
        except OSError as failure:
            winerror = getattr(failure, "winerror", None)
            contended = (
                winerror == 33  # ERROR_LOCK_VIOLATION
                if winerror is not None
                else failure.errno in {errno.EACCES, errno.EDEADLK}
            )
            if not contended:
                raise
            sleep(0.05)


@contextmanager
def _native_lock(path: Path) -> Iterator[None]:
    """Lock one initialized byte for a full lifecycle transaction."""
    if os.name != "nt":
        raise RuntimeError("Windows scheduling is unavailable on this platform")
    import msvcrt

    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        _lock_byte(msvcrt.locking, handle.fileno(), clock.sleep)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _write_xml(state_dir: Path, xml: bytes) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".xml", prefix="teammem-", dir=state_dir,
        delete=False,
    ) as handle:
        handle.write(xml)
        return Path(handle.name)


def _remove_temp(path: Path) -> None:
    path.unlink()


def _cleanup_temp(path: Path | None) -> bool:
    """Remove a transaction file; success means it is demonstrably gone."""
    if path is None:
        return True
    try:
        _remove_temp(path)
    except OSError:
        if _temp_is_gone(path):
            return True
        try:
            _remove_temp(path)
        except OSError:
            return False
    return _temp_is_gone(path)


def _temp_is_gone(path: Path) -> bool:
    """Prove removal with stat; permission uncertainty is never absence."""
    try:
        path.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _create(name: str, xml_path: Path, runner: WindowsRunner | None) -> None:
    result = _run(
        runner,
        ["schtasks.exe", "/Create", "/TN", name, "/XML", str(xml_path), "/F"],
    )
    if result.returncode != 0:
        raise RuntimeError("Windows schedule installation failed")


def _delete(name: str, runner: WindowsRunner | None) -> None:
    result = _run(runner, ["schtasks.exe", "/Delete", "/TN", name, "/F"])
    if result.returncode != 0:
        raise RuntimeError("Windows schedule removal failed")


def _verify_installed(
    name: str, expected: WindowsSchedule, runner: WindowsRunner | None
) -> str:
    xml = _query_xml(name, runner)
    if xml is None:
        raise RuntimeError("Windows schedule definition conflicts with an existing task")
    try:
        return parse_task_xml(xml, expected)
    except RuntimeError:
        raise RuntimeError(_CONFLICT) from None


def _verify_absent(name: str, runner: WindowsRunner | None) -> None:
    if _query_xml(name, runner) is not None:
        raise RuntimeError("Windows schedule definition conflicts with an existing task")


def _restore(
    name: str,
    snapshot: bytes | None,
    previous: WindowsSchedule | None,
    xml_path: Path | None,
    runner: WindowsRunner | None,
) -> bool:
    try:
        if snapshot is None:
            # A failed write or a failed create can leave no definition at all.
            # Query first so a no-op rollback never turns into a false failure.
            if _query_xml(name, runner) is not None:
                _delete(name, runner)
            _verify_absent(name, runner)
        else:
            assert previous is not None
            if xml_path is None:
                _verify_installed(name, previous, runner)
                return True
            xml_path.write_bytes(snapshot)
            _create(name, xml_path, runner)
            _verify_installed(name, previous, runner)
    except (OSError, RuntimeError):
        return False
    return True


def _with_lock(
    state_dir: Path, lock_factory: Callable[[Path], Any] | None
):
    return (lock_factory or _native_lock)(state_dir / "schedule.lock")


def schedule_status(
    *,
    api: Any = None,
    runner: WindowsRunner | None = None,
    state_dir: Path | None = None,
    task_name_override: str | None = None,
    executable: str | None = None,
    env_file: str | Path | None = None,
):
    """Return only a complete, current-user TeamMem task as installed."""
    from .schedule import ScheduleStatus

    sid = current_user_sid(api)
    name = _task_name_for(sid, task_name_override)
    if executable is None or env_file is None:
        raise ValueError("Windows schedule status requires executable and environment path")
    xml, schedule = _valid_snapshot(name, sid, executable, env_file, runner)
    if xml is None:
        return ScheduleStatus(False, None, "windows", Path(name))
    assert schedule is not None
    return ScheduleStatus(True, schedule.time, "windows", Path(name))


def install_schedule(
    cfg: Any,
    hour: int,
    minute: int,
    executable: str,
    *,
    api: Any = None,
    runner: WindowsRunner | None = None,
    state_dir: Path | None = None,
    task_name_override: str | None = None,
    lock_factory: Callable[[Path], Any] | None = None,
) -> Path:
    """Install a validated task, restoring the exact prior definition on failure."""
    sid = current_user_sid(api)
    name = _task_name_for(sid, task_name_override)
    env_file = validate_windows_env_file(Path(cfg.env_file), sid, api)
    directory = provision_windows_state_dir(_state_directory(state_dir), sid, api)
    schedule = WindowsSchedule(
        sid=sid, task_name=name, time=f"{hour:02d}:{minute:02d}",
        executable=executable, env_file=env_file,
    )
    definition = build_task_xml(schedule)
    with _with_lock(directory, lock_factory):
        snapshot, previous = _valid_snapshot(
            name, sid, executable, env_file, runner
        )
        xml_path: Path | None = None
        try:
            xml_path = _write_xml(directory, definition)
            _create(name, xml_path, runner)
            if _verify_installed(name, schedule, runner) != schedule.time:
                raise RuntimeError("Windows schedule definition conflicts with an existing task")
        except (OSError, RuntimeError) as failure:
            restored = _restore(name, snapshot, previous, xml_path, runner)
            restored = _cleanup_temp(xml_path) and restored
            message = (
                "Windows schedule installation failed; previous state restored"
                if restored
                else "Windows schedule installation failed and rollback failed"
            )
            raise RuntimeError(message) from failure
        try:
            if xml_path is not None:
                _remove_temp(xml_path)
        except OSError as failure:
            restored = _restore(name, snapshot, previous, xml_path, runner)
            restored = _cleanup_temp(xml_path) and restored
            message = (
                "Windows schedule installation failed; previous state restored"
                if restored
                else "Windows schedule installation failed and rollback failed"
            )
            raise RuntimeError(message) from failure
    return Path(name)


def remove_schedule(
    *,
    api: Any = None,
    runner: WindowsRunner | None = None,
    state_dir: Path | None = None,
    task_name_override: str | None = None,
    executable: str | None = None,
    env_file: str | Path | None = None,
    lock_factory: Callable[[Path], Any] | None = None,
) -> bool:
    """Remove a managed task; this never terminates an already-running command."""
    sid = current_user_sid(api)
    name = _task_name_for(sid, task_name_override)
    if executable is None or env_file is None:
        raise ValueError("Windows schedule removal requires executable and environment path")
    directory = provision_windows_state_dir(_state_directory(state_dir), sid, api)
    with _with_lock(directory, lock_factory):
        snapshot, previous = _valid_snapshot(
            name, sid, executable, env_file, runner
        )
        if snapshot is None:
            return False
        xml_path: Path | None = None
        try:
            xml_path = _write_xml(directory, snapshot)
            _delete(name, runner)
            _verify_absent(name, runner)
        except (OSError, RuntimeError) as failure:
            restored = _restore(name, snapshot, previous, xml_path, runner)
            restored = _cleanup_temp(xml_path) and restored
            message = (
                "Windows schedule removal failed; previous state restored"
                if restored
                else "Windows schedule removal failed and rollback failed"
            )
            raise RuntimeError(message) from failure
        try:
            if xml_path is not None:
                _remove_temp(xml_path)
        except OSError as failure:
            restored = _restore(name, snapshot, previous, xml_path, runner)
            restored = _cleanup_temp(xml_path) and restored
            message = (
                "Windows schedule removal failed; previous state restored"
                if restored
                else "Windows schedule removal failed and rollback failed"
            )
            raise RuntimeError(message) from failure
    return True
