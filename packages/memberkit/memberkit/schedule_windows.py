"""Canonical Windows Task Scheduler definitions for MemberKit."""

from __future__ import annotations

import hashlib
import ntpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .schedule import ScheduleStatus
from .windows_security import (
    _is_absolute_windows_filesystem_path,
    current_user_sid,
)


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_NUMERIC_ENTITY = re.compile(r"&#(?:[0-9]+|[xX][0-9a-fA-F]+);")
_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_TAG = "{" + _NAMESPACE + "}"
_TASK_NAME = re.compile(r"\\TeamMem-MemberKit-Daily-[0-9a-f]{12}\Z")
_MAX_TASK_XML_BYTES = 1024 * 1024
_MANAGED_ERROR = "Windows schedule definition is not managed by TeamMem MemberKit"
_PRINCIPAL_ID = "Author"
OWNERSHIP_SOURCE = "TeamMem-MemberKit"
OWNERSHIP_DESCRIPTION = "TeamMem MemberKit daily draft reminder"


@dataclass(frozen=True)
class WindowsSchedule:
    sid: str
    task_name: str
    time: str
    executable: str


def task_name(sid: str) -> str:
    """Create a stable task name for one SID without exposing the SID."""
    return (
        "\\TeamMem-MemberKit-Daily-"
        + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]
    )


def _unsafe_argument(value: str) -> bool:
    return bool(_CONTROL.search(value))


def encode_arguments(arguments: Sequence[str]) -> str:
    """Encode an argument vector using the canonical Windows C runtime grammar."""
    encoded: list[str] = []
    for argument in arguments:
        if not isinstance(argument, str) or _unsafe_argument(argument):
            raise ValueError("unsafe Windows argument")
        if argument and not any(
            character.isspace() or character == '"' for character in argument
        ):
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
    """Decode the Windows C runtime command-line grammar without a shell."""
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
    if not _is_absolute_windows_filesystem_path(Path(path)):
        raise ValueError("Windows schedule executable must be an absolute Windows path")
    return path


def _is_shell(path: str) -> bool:
    return ntpath.basename(path).lower() in {
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
    }


def _canonical_memberkit_executable(value: str | Path) -> str:
    path = _absolute_windows_path(value)
    if "/" in path or _CONTROL.search(path) or ntpath.normpath(path) != path:
        raise ValueError(
            "Windows schedule executable must use a canonical memberkit.exe path"
        )

    drive, tail = ntpath.splitdrive(path)
    components = [component for component in tail.split("\\") if component]
    if drive.startswith("\\\\"):
        components = drive[2:].split("\\") + components
    invalid_characters = set('<>"|?*:')
    reserved = re.compile(
        r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z",
        re.IGNORECASE,
    )
    if (
        not components
        or any(
            component in {".", ".."}
            or component.endswith((".", " "))
            or any(character in invalid_characters for character in component)
            or reserved.fullmatch(component)
            for component in components
        )
        or ntpath.basename(path) != "memberkit.exe"
    ):
        raise ValueError(
            "Windows schedule executable must use a canonical memberkit.exe path"
        )
    return path


def _valid_task_name(value: str, sid: str) -> str:
    if not _TASK_NAME.fullmatch(value) or value != task_name(sid):
        raise ValueError("Windows task name must match the current SID")
    return value


def _normal_time(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[01][0-9]|2[0-3]):[0-5][0-9]",
        value,
    ):
        raise ValueError("schedule time must be HH:MM")
    return value


def _add(
    parent: ET.Element,
    name: str,
    value: str | None = None,
) -> ET.Element:
    element = ET.SubElement(parent, _TAG + name)
    if value is not None:
        element.text = value
    return element


def build_task_xml(schedule: WindowsSchedule) -> bytes:
    """Build one deterministic UTF-16LE Task Scheduler definition."""
    executable = _absolute_windows_path(schedule.executable)
    if _is_shell(executable):
        raise ValueError("Windows schedule executable must not be a shell")
    executable = _canonical_memberkit_executable(executable)
    name = _valid_task_name(schedule.task_name, schedule.sid)
    time = _normal_time(schedule.time)
    arguments = encode_arguments(["scheduled-run"])

    ET.register_namespace("", _NAMESPACE)
    root = ET.Element(_TAG + "Task", {"version": "1.4"})
    registration = _add(root, "RegistrationInfo")
    _add(registration, "Source", OWNERSHIP_SOURCE)
    _add(registration, "URI", name)
    _add(registration, "Description", OWNERSHIP_DESCRIPTION)

    principals = _add(root, "Principals")
    principal = ET.SubElement(
        principals,
        _TAG + "Principal",
        {"id": _PRINCIPAL_ID},
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
        ("UseUnifiedSchedulingEngine", "true"),
    ):
        _add(settings, key, value)

    actions = ET.SubElement(
        root,
        _TAG + "Actions",
        {"Context": _PRINCIPAL_ID},
    )
    action = _add(actions, "Exec")
    _add(action, "Command", executable)
    _add(action, "Arguments", arguments)

    declaration = (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'.encode("utf-16-le")
    )
    document = ET.tostring(root, encoding="unicode").encode("utf-16-le")
    return b"\xff\xfe" + declaration + document


def _xml_transport(xml: bytes) -> tuple[str, int]:
    if xml.startswith(b"\x00\x00\xfe\xff") or xml.startswith(b"\xff\xfe\x00\x00"):
        raise ValueError(_MANAGED_ERROR)
    if xml.startswith(b"\xef\xbb\xbf"):
        return "utf-8", 3
    if xml.startswith(b"\xff\xfe"):
        return "utf-16-le", 2
    if xml.startswith(b"\xfe\xff"):
        return "utf-16-be", 2

    probe = xml[:128]
    while probe.startswith((b" \x00", b"\t\x00", b"\r\x00", b"\n\x00")):
        probe = probe[2:]
    if probe.startswith(b"<\x00"):
        return "utf-16-le", 0

    probe = xml[:128]
    while probe.startswith((b"\x00 ", b"\x00\t", b"\x00\r", b"\x00\n")):
        probe = probe[2:]
    if probe.startswith(b"\x00<"):
        return "utf-16-be", 0

    if xml[:128].lstrip(b" \t\r\n").startswith(b"<"):
        return "utf-8", 0
    raise ValueError(_MANAGED_ERROR)


def _decode_xml(xml: bytes) -> str:
    if (
        not isinstance(xml, bytes)
        or not xml
        or len(xml) > _MAX_TASK_XML_BYTES
    ):
        raise ValueError(_MANAGED_ERROR)
    encoding, offset = _xml_transport(xml)
    return xml[offset:].decode(encoding, errors="strict")


def _local(tag: str) -> str:
    if not isinstance(tag, str) or not tag.startswith(_TAG):
        raise ValueError(_MANAGED_ERROR)
    return tag[len(_TAG):]


def _children(parent: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in parent if _local(child.tag) == name]


def _child_names(parent: ET.Element) -> list[str]:
    return [_local(child.tag) for child in parent]


def _only(parent: ET.Element, name: str) -> ET.Element:
    values = _children(parent, name)
    if len(values) != 1:
        raise ValueError(_MANAGED_ERROR)
    return values[0]


def _exact_children(parent: ET.Element, names: Sequence[str]) -> None:
    if _child_names(parent) != list(names):
        raise ValueError(_MANAGED_ERROR)


def _required_optional_children(
    parent: ET.Element,
    required: Sequence[str],
    optional: Sequence[str],
) -> None:
    names = _child_names(parent)
    allowed = tuple(required) + tuple(optional)
    if (
        any(name not in allowed for name in names)
        or any(names.count(name) != 1 for name in required)
        or any(names.count(name) > 1 for name in optional)
    ):
        raise ValueError(_MANAGED_ERROR)


def _text(parent: ET.Element, name: str) -> str:
    value = _only(parent, name).text
    if value is None:
        raise ValueError(_MANAGED_ERROR)
    return value


class _TaskXmlMismatch(ValueError):
    def __init__(self, category: str) -> None:
        super().__init__(_MANAGED_ERROR)
        self.category = category


def _validate_task_xml(xml: bytes, expected: WindowsSchedule) -> str:
    try:
        source = _decode_xml(xml)
        lowered = source.lower()
        after_declaration = source.lstrip()
        if after_declaration.startswith("<?xml "):
            declaration_end = after_declaration.find("?>")
            if declaration_end < 0:
                raise ValueError(_MANAGED_ERROR)
            after_declaration = after_declaration[declaration_end + 2:]
        if (
            "<!doctype" in lowered
            or "<!entity" in lowered
            or "<![" in source
            or "<!--" in source
            or "<?" in after_declaration
            or _NUMERIC_ENTITY.search(source)
        ):
            raise ValueError(_MANAGED_ERROR)
        parser = ET.XMLParser(
            target=ET.TreeBuilder(insert_comments=True, insert_pis=True)
        )
        root = ET.fromstring(source, parser=parser)
        if root.tag != _TAG + "Task" or root.attrib != {"version": "1.4"}:
            raise ValueError(_MANAGED_ERROR)
        for element in root.iter():
            _local(element.tag)
        _valid_task_name(expected.task_name, expected.sid)
        _exact_children(
            root,
            [
                "RegistrationInfo",
                "Principals",
                "Triggers",
                "Settings",
                "Actions",
            ],
        )

        registration = _only(root, "RegistrationInfo")
        _required_optional_children(
            registration,
            ["Source", "URI", "Description"],
            ["Date", "Author"],
        )
        registration_names = _child_names(registration)
        metadata_end = 0
        for metadata_name in ("Date", "Author"):
            if (
                metadata_end < len(registration_names)
                and registration_names[metadata_end] == metadata_name
            ):
                metadata_end += 1
        if registration_names[metadata_end:] not in (
            ["Source", "URI", "Description"],
            ["Source", "Description", "URI"],
        ):
            raise ValueError(_MANAGED_ERROR)
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
            principal,
            ["UserId", "LogonType"],
            ["RunLevel"],
        )
        if _child_names(principal) not in (
            ["UserId", "LogonType"],
            ["UserId", "LogonType", "RunLevel"],
        ):
            raise ValueError(_MANAGED_ERROR)

        triggers = _only(root, "Triggers")
        _exact_children(triggers, ["CalendarTrigger"])
        trigger = _only(triggers, "CalendarTrigger")
        _required_optional_children(
            trigger,
            ["StartBoundary", "ScheduleByDay"],
            ["Enabled"],
        )
        if _child_names(trigger) not in (
            ["StartBoundary", "ScheduleByDay"],
            ["StartBoundary", "ScheduleByDay", "Enabled"],
        ):
            raise ValueError(_MANAGED_ERROR)
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
            (
                "ExecutionTimeLimit",
                "PT4H",
                "settings.execution-time-limit",
            ),
            (
                "UseUnifiedSchedulingEngine",
                "true",
                "settings.unified-engine",
            ),
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
        _required_optional_children(
            settings,
            [key for key, _value, _category in required_settings],
            [key for key, _value, _category in default_settings]
            + ["IdleSettings"],
        )
        if _child_names(settings) not in (
            [
                "MultipleInstancesPolicy",
                "Enabled",
                "StartWhenAvailable",
                "DisallowStartIfOnBatteries",
                "StopIfGoingOnBatteries",
                "RunOnlyIfNetworkAvailable",
                "WakeToRun",
                "ExecutionTimeLimit",
                "UseUnifiedSchedulingEngine",
            ],
            [
                "DisallowStartIfOnBatteries",
                "StopIfGoingOnBatteries",
                "ExecutionTimeLimit",
                "MultipleInstancesPolicy",
                "StartWhenAvailable",
                "IdleSettings",
                "UseUnifiedSchedulingEngine",
            ],
        ):
            raise ValueError(_MANAGED_ERROR)
        idle = None
        if _children(settings, "IdleSettings"):
            idle = _only(settings, "IdleSettings")
            _exact_children(idle, ["StopOnIdleEnd", "RestartOnIdle"])

        actions = _only(root, "Actions")
        _exact_children(actions, ["Exec"])
        action = _only(actions, "Exec")
        _exact_children(action, ["Command", "Arguments"])

        containers = {
            root,
            registration,
            principals,
            principal,
            triggers,
            trigger,
            _only(trigger, "ScheduleByDay"),
            settings,
            actions,
            action,
        }
        if idle is not None:
            containers.add(idle)
        for element in root.iter():
            if element in containers:
                if element.text is not None and element.text.strip():
                    raise ValueError(_MANAGED_ERROR)
            elif list(element):
                raise ValueError(_MANAGED_ERROR)
            if element.tail is not None and element.tail.strip():
                raise ValueError(_MANAGED_ERROR)
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
        parent: ET.Element,
        name: str,
        default: str,
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
        lambda: text_or_default(
            principal,
            "RunLevel",
            "LeastPrivilege",
        )
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
        lambda: _text(trigger, "StartBoundary")
        == f"2000-01-01T{time}:00",
    )
    check(
        "trigger.daily-interval",
        lambda: _text(
            _only(trigger, "ScheduleByDay"),
            "DaysInterval",
        )
        == "1",
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
            lambda key=key, value=value: text_or_default(
                settings,
                key,
                value,
            )
            == value,
        )
    if idle is not None:
        check(
            "settings.idle",
            lambda: _text(idle, "StopOnIdleEnd") == "true"
            and _text(idle, "RestartOnIdle") == "false",
        )

    check(
        "action.binding",
        lambda: actions.attrib["Context"] == _PRINCIPAL_ID,
    )
    check(
        "action.command",
        lambda: _text(action, "Command")
        == _canonical_memberkit_executable(expected.executable)
        and not _is_shell(_text(action, "Command")),
    )

    def argv_matches() -> bool:
        arguments = _text(action, "Arguments")
        expected_arguments = ["scheduled-run"]
        return (
            decode_arguments(arguments) == expected_arguments
            and encode_arguments(expected_arguments) == arguments
        )

    check("action.argv", argv_matches)
    return time


def parse_task_xml(xml: bytes, expected: WindowsSchedule) -> str:
    """Validate a managed definition without disclosing observed values."""
    try:
        return _validate_task_xml(xml, expected)
    except _TaskXmlMismatch:
        raise RuntimeError(_MANAGED_ERROR) from None


def task_xml_mismatch_categories(
    xml: bytes,
    expected: WindowsSchedule,
) -> tuple[str, ...]:
    """Return one fixed mismatch category and never observed task values."""
    try:
        _validate_task_xml(xml, expected)
    except _TaskXmlMismatch as mismatch:
        return (mismatch.category,)
    return ()
