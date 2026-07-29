import re
from pathlib import Path

import pytest

import teammem.schedule_windows as windows
import teammem.windows_security as windows_security
from teammem.windows_security import (
    NativeWindowsApi,
    read_windows_env_file,
    validate_windows_env_file,
    validate_windows_state_dir,
)


SID = "S-1-5-21-111-222-333-1001"


class FakeWindowsApi:
    def __init__(self, records, process_sid=SID):
        self.records = records
        self.process_sid = process_sid
        self.opened = []
        self.closed = []

    def current_process_sid(self):
        return self.process_sid

    def open_file(self, path, *, directory=False):
        self.opened.append((Path(path), directory))
        return self.records[Path(path)]

    def file_info(self, handle):
        return handle["info"]

    def owner_sid(self, handle):
        return handle["owner"]

    def allow_aces(self, handle):
        return handle["aces"]

    def read_lines(self, handle):
        return handle["lines"]

    def close_handle(self, handle):
        self.closed.append(handle)


def _file(*, owner=SID, aces=(), lines=("TEAMMEM_SINCE_DAYS=9\n",)):
    return {
        "info": {"regular": True, "reparse_point": False, "file_type": "disk"},
        "owner": owner,
        "aces": aces,
        "lines": list(lines),
    }


def _directory(*, owner=SID, aces=()):
    return {
        "info": {"directory": True, "reparse_point": False, "file_type": "disk"},
        "owner": owner,
        "aces": aces,
        "lines": [],
    }


def test_task_name_is_stable_per_sid_without_exposing_sid():
    name = windows.task_name(SID)
    assert re.fullmatch(r"\\TeamMem-Daily-[0-9a-f]{12}", name)
    assert SID not in name
    assert name == windows.task_name(SID)


def test_current_user_sid_uses_injected_process_token_api():
    api = FakeWindowsApi({})
    assert windows.current_user_sid(api) == SID


def test_native_windows_bindings_keep_handle_width_and_use_advapi_for_sid_conversion():
    import ctypes

    class Procedure:
        pass

    class Library:
        def __init__(self):
            self.procedures = {}

        def __getattr__(self, name):
            procedure = Procedure()
            self.procedures[name] = procedure
            setattr(self, name, procedure)
            return procedure

    kernel32 = Library()
    advapi32 = Library()
    NativeWindowsApi._configure_api(ctypes, kernel32, advapi32)

    assert hasattr(advapi32, "ConvertSidToStringSidW")
    assert "ConvertSidToStringSidW" not in kernel32.procedures
    assert kernel32.CreateFileW.restype is ctypes.c_void_p
    assert kernel32.CloseHandle.argtypes == [ctypes.c_void_p]
    assert advapi32.GetSecurityInfo.argtypes[0] is ctypes.c_void_p
    assert advapi32.GetAce.argtypes[0] is ctypes.c_void_p


@pytest.mark.parametrize(
    "arguments",
    [
        ["--env-file", r"C:\Users\Alex\App Data\hub.env", "run-daily"],
        ["--env-file", 'C:\\path with "quote"\\hub.env', "run-daily"],
        ["--env-file", "C:\\团队\\hub.env", "run-daily"],
        ["", r"C:\\trailing\\", "$HOME", "100%"],
    ],
)
def test_windows_arguments_round_trip_canonically(arguments):
    encoded = windows.encode_arguments(arguments)
    assert windows.decode_arguments(encoded) == arguments
    assert windows.encode_arguments(windows.decode_arguments(encoded)) == encoded


@pytest.mark.parametrize("value", ["nul\0", "line\nbreak", "return\r", "tab\t", "bell\x07"])
def test_windows_arguments_reject_control_characters(value):
    with pytest.raises(ValueError, match="unsafe Windows argument"):
        windows.encode_arguments([value])


def _schedule(**changes):
    values = {
        "sid": SID,
        "task_name": windows.task_name(SID),
        "time": "18:20",
        "executable": r"C:\\Program Files\\TeamMem\\teammem.exe",
        "env_file": r"C:\\Users\\Alex\\App Data\\hub.env",
    }
    values.update(changes)
    return windows.WindowsSchedule(**values)


def test_task_xml_is_deterministic_complete_and_secret_free():
    schedule = _schedule()
    xml = windows.build_task_xml(schedule)
    assert xml.startswith(b"\xff\xfe")
    decoded = xml[2:].decode("utf-16-le")
    assert 'encoding="UTF-16"' in decoded
    for value in (
        SID,
        "InteractiveToken",
        "LeastPrivilege",
        "18:20:00",
        "<DaysInterval>1</DaysInterval>",
        "<StartWhenAvailable>true</StartWhenAvailable>",
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
        "<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>",
        "<WakeToRun>false</WakeToRun>",
        "<ExecutionTimeLimit>PT4H</ExecutionTimeLimit>",
        windows.OWNERSHIP_SOURCE,
        schedule.task_name,
    ):
        assert value in decoded
    assert decoded.count("<CalendarTrigger>") == 1
    assert decoded.count("<Principal>") == 1
    assert decoded.count("<Exec>") == 1
    assert "private-token" not in decoded
    assert windows.parse_task_xml(xml, schedule) == "18:20"
    assert windows.build_task_xml(schedule) == xml


@pytest.mark.parametrize(
    "field,value",
    [
        ("executable", r"C:teammem.exe"),
        ("executable", "teammem.exe"),
        ("executable", r"\relative\\teammem.exe"),
        ("env_file", r"C:hub.env"),
        ("env_file", "hub.env"),
    ],
)
def test_task_xml_rejects_non_absolute_windows_paths(field, value):
    with pytest.raises(ValueError, match="absolute Windows path"):
        windows.build_task_xml(_schedule(**{field: value}))


def test_task_xml_accepts_unc_windows_paths_on_non_windows_host():
    schedule = _schedule(
        executable=r"\\server\share\bin\teammem.exe",
        env_file=r"\\server\share\config\hub.env",
    )
    assert windows.parse_task_xml(windows.build_task_xml(schedule), schedule) == "18:20"


@pytest.mark.parametrize(
    "path",
    [r"\\.\pipe\teammem", r"\\?\C:\\TeamMem\\hub.env", r"\\server", r"1:\\TeamMem\\hub.env"],
)
def test_windows_paths_reject_device_and_invalid_unc_namespaces_before_open(path):
    api = FakeWindowsApi({})
    with pytest.raises(ValueError, match="Windows filesystem path"):
        validate_windows_env_file(Path(path), SID, api)
    with pytest.raises(ValueError, match="Windows filesystem path"):
        read_windows_env_file(Path(path), SID, api)
    assert api.opened == []


def test_windows_environment_file_rejects_non_disk_handle():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\hub.env")
    record = _file()
    record["info"]["file_type"] = "pipe"
    with pytest.raises(ValueError, match="disk file"):
        validate_windows_env_file(path, SID, FakeWindowsApi({path: record}))


def test_task_xml_requires_scheduler_namespace_and_exact_attributes():
    schedule = _schedule()
    text = windows.build_task_xml(schedule)[2:].decode("utf-16-le")
    mutations = [
        text.replace(' xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"', "", 1),
        text.replace('version="1.4"', 'version="1.4" extra="value"', 1),
        text.replace("<Exec>", '<Exec extra="value">', 1),
    ]
    for mutation in mutations:
        with pytest.raises(RuntimeError, match="^Windows schedule definition is not managed by TeamMem$"):
            windows.parse_task_xml(b"\xff\xfe" + mutation.encode("utf-16-le"), schedule)


@pytest.mark.parametrize(
    "original,tampered",
    [
        (windows.OWNERSHIP_SOURCE, " " + windows.OWNERSHIP_SOURCE + " "),
        ("C:\\\\Program Files\\\\TeamMem\\\\teammem.exe", " C:\\\\Program Files\\\\TeamMem\\\\teammem.exe "),
        ("run-daily", "run-daily "),
    ],
)
def test_task_xml_rejects_semantic_text_whitespace_tampering(original, tampered):
    schedule = _schedule()
    text = windows.build_task_xml(schedule)[2:].decode("utf-16-le")
    text = text.replace(original, tampered, 1)
    with pytest.raises(RuntimeError, match="^Windows schedule definition is not managed by TeamMem$"):
        windows.parse_task_xml(b"\xff\xfe" + text.encode("utf-16-le"), schedule)


def test_task_xml_binds_its_task_name_to_its_current_sid():
    mismatched = _schedule(task_name=windows.task_name("S-1-5-21-other"))
    with pytest.raises(ValueError, match="current SID"):
        windows.build_task_xml(mismatched)


def test_object_allow_ace_offsets_and_unhandled_compound_aces_fail_closed():
    assert windows_security.NativeWindowsApi._allow_ace_sid_offset(5, 0, 32) == 12
    assert windows_security.NativeWindowsApi._allow_ace_sid_offset(5, 1, 44) == 28
    assert windows_security.NativeWindowsApi._allow_ace_sid_offset(11, 3, 60) == 44
    assert windows_security.NativeWindowsApi._allow_ace_sid_offset(4, 0, 32) is None


@pytest.mark.parametrize(
    "replacement",
    [
        ("<UserId>" + SID + "</UserId>", "<UserId>S-1-5-21-foreign</UserId>"),
        (windows.OWNERSHIP_SOURCE, "Foreign scheduler"),
        ("InteractiveToken", "Password"),
        ("LeastPrivilege", "HighestAvailable"),
        ("<Enabled>true</Enabled>", "<Enabled>false</Enabled>"),
        ("<StartWhenAvailable>true</StartWhenAvailable>", "<StartWhenAvailable>false</StartWhenAvailable>"),
        ("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", "<MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>"),
        ("<ExecutionTimeLimit>PT4H</ExecutionTimeLimit>", "<ExecutionTimeLimit>PT8H</ExecutionTimeLimit>"),
        ("teammem.exe", "cmd.exe"),
        ("18:20:00", "19:20:00"),
        ("run-daily", "other-command"),
        ("InteractiveToken", "S4U"),
    ],
)
def test_task_xml_rejects_tampered_managed_definition(replacement):
    schedule = _schedule()
    xml = windows.build_task_xml(schedule)
    text = xml[2:].decode("utf-16-le").replace(*replacement, 1)
    with pytest.raises(RuntimeError, match="^Windows schedule definition is not managed by TeamMem$"):
        windows.parse_task_xml(b"\xff\xfe" + text.encode("utf-16-le"), schedule)


@pytest.mark.parametrize(
    "addition",
    [
        "<CalendarTrigger><StartBoundary>2000-01-01T18:20:00</StartBoundary><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay><Enabled>true</Enabled></CalendarTrigger>",
        "<Principal><UserId>" + SID + "</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>",
        "<Exec><Command>C:\\other.exe</Command><Arguments>run-daily</Arguments></Exec>",
    ],
)
def test_task_xml_rejects_extra_trigger_principal_or_action(addition):
    schedule = _schedule()
    text = windows.build_task_xml(schedule)[2:].decode("utf-16-le")
    if addition.startswith("<CalendarTrigger"):
        text = text.replace("</Triggers>", addition + "</Triggers>")
    elif addition.startswith("<Principal"):
        text = text.replace("</Principals>", addition + "</Principals>")
    else:
        text = text.replace("</Actions>", addition + "</Actions>")
    with pytest.raises(RuntimeError, match="^Windows schedule definition is not managed by TeamMem$"):
        windows.parse_task_xml(b"\xff\xfe" + text.encode("utf-16-le"), schedule)


@pytest.mark.parametrize(
    "addition",
    [
        "<ComHandler><ClassId>{00000000-0000-0000-0000-000000000000}</ClassId></ComHandler>",
        "<WorkingDirectory>C:\\Users\\Alex</WorkingDirectory>",
    ],
)
def test_task_xml_rejects_extra_non_exec_action_or_working_directory(addition):
    schedule = _schedule()
    text = windows.build_task_xml(schedule)[2:].decode("utf-16-le")
    if addition.startswith("<ComHandler"):
        text = text.replace("</Actions>", addition + "</Actions>")
    else:
        text = text.replace("</Exec>", addition + "</Exec>")
    with pytest.raises(RuntimeError, match="^Windows schedule definition is not managed by TeamMem$"):
        windows.parse_task_xml(b"\xff\xfe" + text.encode("utf-16-le"), schedule)


@pytest.mark.parametrize(
    "payload",
    [
        b"not xml",
        b'<!DOCTYPE x [<!ENTITY x "y">]><Task>&x;</Task>',
    ],
)
def test_task_xml_rejects_malformed_or_entity_declarations(payload):
    with pytest.raises(RuntimeError, match="^Windows schedule definition is not managed by TeamMem$"):
        windows.parse_task_xml(payload, _schedule())


@pytest.mark.parametrize(
    "record, expected",
    [
        (_file(), True),
        (_file(aces=(("Administrators", "read"), ("SYSTEM", "read"))), True),
        (_file(aces=(("Everyone", "read"),)), False),
        (_file(aces=(("S-1-1-0", 0x80000000),)), False),
        (_file(aces=None), False),
        (_file(aces=(("Authenticated Users", "read"),)), False),
        (_file(aces=(("Users", "read"),)), False),
        (_file(owner="S-1-5-21-other"), False),
        ({**_file(), "info": {"regular": False, "reparse_point": True}}, False),
    ],
)
def test_windows_environment_file_requires_safe_owner_type_and_dacl(record, expected):
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\hub.env")
    api = FakeWindowsApi({path: record})
    if expected:
        assert validate_windows_env_file(path, SID, api) == path
    else:
        with pytest.raises(ValueError) as error:
            validate_windows_env_file(path, SID, api)
        assert "TEAMMEM_SINCE_DAYS" not in str(error.value)


def test_windows_environment_read_validates_and_reads_the_same_open_handle():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\hub.env")
    original = _file(lines=("TEAMMEM_SINCE_DAYS=9\n",))
    replacement = _file(lines=("TEAMMEM_SINCE_DAYS=22\n",))
    api = FakeWindowsApi({path: original})

    def swapped_read_lines(handle):
        api.records[path] = replacement
        return handle["lines"]

    api.read_lines = swapped_read_lines
    assert read_windows_env_file(path, SID, api) == ["TEAMMEM_SINCE_DAYS=9\n"]
    assert api.closed == [original]


def test_windows_environment_read_does_not_double_close_after_transfer_decode_error():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\hub.env")
    record = _file()

    class TransferringApi(FakeWindowsApi):
        def transfer_for_read(self, handle):
            return handle

        def read_lines_from_descriptor(self, descriptor):
            raise UnicodeError("invalid UTF-8")

    api = TransferringApi({path: record})
    with pytest.raises(ValueError, match="UTF-8 text"):
        read_windows_env_file(path, SID, api)
    assert api.closed == []


def test_windows_state_directory_is_validated_before_future_lock_or_temp_files():
    state_dir = Path(r"C:\Users\Alex\AppData\Local\TeamMemory")
    unsafe = _directory(aces=(("Everyone", "read"),))
    api = FakeWindowsApi({state_dir: unsafe})
    with pytest.raises(ValueError):
        validate_windows_state_dir(state_dir, SID, api)
    assert not (state_dir / "schedule.lock").exists()
