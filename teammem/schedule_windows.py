"""Pure Windows Task Scheduler definition helpers.

Lifecycle calls to ``schtasks.exe`` intentionally live in the next layer; this
module only defines the identity, command line, and XML security contract.
"""

from __future__ import annotations

import hashlib
import ntpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .windows_security import _windows_filesystem_path, current_user_sid


_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_TAG = "{" + _NAMESPACE + "}"
_MANAGED_ERROR = "Windows schedule definition is not managed by TeamMem"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TASK_NAME = re.compile(r"\\TeamMem-Daily-[0-9a-f]{12}\Z")
OWNERSHIP_SOURCE = "TeamMem"
OWNERSHIP_DESCRIPTION = "TeamMem daily operator schedule"


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
    principal = _add(principals, "Principal")
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
    actions = _add(root, "Actions")
    action = _add(actions, "Exec")
    _add(action, "Command", executable)
    _add(action, "Arguments", arguments)
    declaration = '<?xml version="1.0" encoding="UTF-16"?>\r\n'.encode("utf-16-le")
    return b"\xff\xfe" + declaration + ET.tostring(root, encoding="unicode").encode("utf-16-le")


def _decode_xml(xml: bytes) -> str:
    if xml.startswith(b"\xff\xfe"):
        return xml[2:].decode("utf-16-le")
    if xml.startswith(b"\xfe\xff"):
        return xml[2:].decode("utf-16-be")
    return xml.decode("utf-8")


def _invalid() -> RuntimeError:
    return RuntimeError(_MANAGED_ERROR)


def parse_task_xml(xml: bytes, expected: WindowsSchedule) -> str:
    """Accept only the complete semantic definition generated by TeamMem."""
    try:
        source = _decode_xml(xml)
        if "<!doctype" in source.lower() or "<!entity" in source.lower():
            raise ValueError(_MANAGED_ERROR)
        root = ET.fromstring(source)
        if root.tag != _TAG + "Task" or root.attrib != {"version": "1.4"}:
            raise ValueError(_MANAGED_ERROR)
        for element in root.iter():
            _local(element.tag)
            if element is not root and element.attrib:
                raise ValueError(_MANAGED_ERROR)
        _valid_task_name(expected.task_name, expected.sid)
        _exact_children(
            root, ["RegistrationInfo", "Principals", "Triggers", "Settings", "Actions"]
        )
        registration = _only(root, "RegistrationInfo")
        _exact_children(registration, ["Source", "URI", "Description"])
        if (
            _text(registration, "Source") != OWNERSHIP_SOURCE
            or _text(registration, "URI") != expected.task_name
            or _text(registration, "Description") != OWNERSHIP_DESCRIPTION
        ):
            raise ValueError(_MANAGED_ERROR)
        principals = _only(root, "Principals")
        _exact_children(principals, ["Principal"])
        principal = _only(principals, "Principal")
        _exact_children(principal, ["UserId", "LogonType", "RunLevel"])
        if (
            _text(principal, "UserId") != expected.sid
            or _text(principal, "LogonType") != "InteractiveToken"
            or _text(principal, "RunLevel") != "LeastPrivilege"
        ):
            raise ValueError(_MANAGED_ERROR)
        triggers = _only(root, "Triggers")
        _exact_children(triggers, ["CalendarTrigger"])
        trigger = _only(triggers, "CalendarTrigger")
        _exact_children(trigger, ["StartBoundary", "ScheduleByDay", "Enabled"])
        time = _normal_time(expected.time)
        if (
            _text(trigger, "StartBoundary") != f"2000-01-01T{time}:00"
            or _text(_only(trigger, "ScheduleByDay"), "DaysInterval") != "1"
            or _text(trigger, "Enabled") != "true"
        ):
            raise ValueError(_MANAGED_ERROR)
        _exact_children(_only(trigger, "ScheduleByDay"), ["DaysInterval"])
        settings = _only(root, "Settings")
        setting_values = (
            ("MultipleInstancesPolicy", "IgnoreNew"),
            ("Enabled", "true"),
            ("StartWhenAvailable", "true"),
            ("DisallowStartIfOnBatteries", "false"),
            ("StopIfGoingOnBatteries", "false"),
            ("RunOnlyIfNetworkAvailable", "false"),
            ("WakeToRun", "false"),
            ("ExecutionTimeLimit", "PT4H"),
        )
        _exact_children(settings, [key for key, _value in setting_values])
        for key, value in setting_values:
            if _text(settings, key) != value:
                raise ValueError(_MANAGED_ERROR)
        actions = _only(root, "Actions")
        _exact_children(actions, ["Exec"])
        action = _only(actions, "Exec")
        _exact_children(action, ["Command", "Arguments"])
        executable = _text(action, "Command")
        env_file = _absolute_windows_path(expected.env_file)
        if (
            executable != _absolute_windows_path(expected.executable)
            or _is_shell(executable)
        ):
            raise ValueError(_MANAGED_ERROR)
        arguments = _text(action, "Arguments")
        expected_args = ["--env-file", env_file, "run-daily"]
        if decode_arguments(arguments) != expected_args or encode_arguments(expected_args) != arguments:
            raise ValueError(_MANAGED_ERROR)
        return time
    except (ET.ParseError, UnicodeError, ValueError, TypeError):
        raise _invalid() from None
