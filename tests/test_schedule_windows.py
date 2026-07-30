import re
import subprocess
from pathlib import Path

import pytest

import teammem.schedule_windows as windows
import teammem.windows_security as windows_security
from teammem.windows_security import (
    NativeWindowsApi,
    provision_windows_state_dir,
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
        try:
            return self.records[Path(path)]
        except KeyError:
            raise FileNotFoundError(path) from None

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

    def create_directory(self, path):
        self.records[Path(path)] = _directory()


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
        "executable": r"C:\Program Files\TeamMem\teammem.exe",
        "env_file": r"C:\Users\Alex\App Data\hub.env",
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
        '<Principal id="Author">',
        '<Actions Context="Author">',
        windows.OWNERSHIP_SOURCE,
        schedule.task_name,
    ):
        assert value in decoded
    assert decoded.count("<CalendarTrigger>") == 1
    assert decoded.count('<Principal id="Author">') == 1
    assert decoded.count("<Exec>") == 1
    assert "private-token" not in decoded
    assert windows.parse_task_xml(xml, schedule) == "18:20"
    assert windows.build_task_xml(schedule) == xml


@pytest.mark.parametrize(
    "encoding,bom",
    [
        ("utf-8", b""),
        ("utf-8", b"\xef\xbb\xbf"),
        ("utf-16-le", b""),
        ("utf-16-be", b""),
    ],
)
def test_task_xml_accepts_realistic_query_byte_encodings(encoding, bom):
    schedule = _schedule()
    text = windows.build_task_xml(schedule)[2:].decode("utf-16-le")

    assert windows.parse_task_xml(bom + text.encode(encoding), schedule) == "18:20"


def test_task_xml_rejects_oversized_input_before_parsing():
    payload = b"<" + b"x" * windows._MAX_TASK_XML_BYTES

    with pytest.raises(RuntimeError, match="^Windows schedule definition is not managed by TeamMem$"):
        windows.parse_task_xml(payload, _schedule())


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


def _task_xml_with_standard_principal_action_binding(schedule):
    text = windows.build_task_xml(schedule)[2:].decode("utf-16-le")
    text = text.replace("<Principal>", '<Principal id="Author">', 1)
    text = text.replace("<Actions>", '<Actions Context="Author">', 1)
    return text


def test_task_xml_accepts_standard_principal_action_binding():
    schedule = _schedule()
    text = _task_xml_with_standard_principal_action_binding(schedule)

    assert windows.parse_task_xml(
        b"\xff\xfe" + text.encode("utf-16-le"), schedule
    ) == "18:20"


@pytest.mark.parametrize(
    "original,tampered",
    [
        ('<Principal id="Author">', "<Principal>"),
        ('<Actions Context="Author">', "<Actions>"),
        ('<Principal id="Author">', '<Principal id="Other">'),
        ('<Actions Context="Author">', '<Actions Context="Other">'),
        ('<Principal id="Author">', '<Principal id="Author" extra="value">'),
        ('<Actions Context="Author">', '<Actions Context="Author" extra="value">'),
    ],
)
def test_task_xml_rejects_missing_mismatched_or_extra_binding_attributes(
    original, tampered
):
    schedule = _schedule()
    text = _task_xml_with_standard_principal_action_binding(schedule)
    text = text.replace(original, tampered, 1)

    with pytest.raises(RuntimeError, match="^Windows schedule definition is not managed by TeamMem$"):
        windows.parse_task_xml(b"\xff\xfe" + text.encode("utf-16-le"), schedule)


def test_task_xml_accepts_scheduler_added_registration_date_and_author():
    schedule = _schedule()
    text = windows.build_task_xml(schedule)[2:].decode("utf-16-le")
    text = text.replace(
        "<RegistrationInfo>",
        (
            "<RegistrationInfo>"
            "<Date>2026-07-30T08:13:39.1234567</Date>"
            "<Author>CI\\runneradmin</Author>"
        ),
        1,
    )
    xml = b"\xff\xfe" + text.encode("utf-16-le")

    assert windows.parse_task_xml(xml, schedule) == "18:20"


def _scheduler_normalized_task_xml(schedule):
    text = windows.build_task_xml(schedule)[2:].decode("utf-16-le")
    registration = (
        "<RegistrationInfo>"
        f"<Source>{windows.OWNERSHIP_SOURCE}</Source>"
        f"<URI>{schedule.task_name}</URI>"
        f"<Description>{windows.OWNERSHIP_DESCRIPTION}</Description>"
        "</RegistrationInfo>"
    )
    normalized_registration = (
        "<RegistrationInfo>"
        f"<Source>{windows.OWNERSHIP_SOURCE}</Source>"
        f"<Description>{windows.OWNERSHIP_DESCRIPTION}</Description>"
        f"<URI>{schedule.task_name}</URI>"
        "</RegistrationInfo>"
    )
    generated_settings = (
        "<Settings>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<Enabled>true</Enabled>"
        "<StartWhenAvailable>true</StartWhenAvailable>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>"
        "<WakeToRun>false</WakeToRun>"
        "<ExecutionTimeLimit>PT4H</ExecutionTimeLimit>"
        "</Settings>"
    )
    normalized_settings = (
        "<Settings>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "<ExecutionTimeLimit>PT4H</ExecutionTimeLimit>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<StartWhenAvailable>true</StartWhenAvailable>"
        "<IdleSettings>"
        "<StopOnIdleEnd>true</StopOnIdleEnd>"
        "<RestartOnIdle>false</RestartOnIdle>"
        "</IdleSettings>"
        "<UseUnifiedSchedulingEngine>false</UseUnifiedSchedulingEngine>"
        "</Settings>"
    )
    assert registration in text
    assert generated_settings in text
    text = text.replace(registration, normalized_registration, 1)
    text = text.replace("<RunLevel>LeastPrivilege</RunLevel>", "", 1)
    text = text.replace("<Enabled>true</Enabled></CalendarTrigger>", "</CalendarTrigger>", 1)
    text = text.replace(generated_settings, normalized_settings, 1)
    return b"\xef\xbb\xbf" + text.encode("utf-8")


def test_task_xml_accepts_exact_scheduler_default_normalization():
    schedule = _schedule()
    xml = _scheduler_normalized_task_xml(schedule)

    assert windows.parse_task_xml(xml, schedule) == "18:20"
    assert windows.task_xml_mismatch_categories(xml, schedule) == ()


def test_task_xml_mismatch_categories_are_exact_and_value_free():
    schedule = _schedule()
    text = windows.build_task_xml(schedule)[2:].decode("utf-16-le")
    replacements = (
        (windows.OWNERSHIP_SOURCE, "secret-source"),
        (f"<UserId>{SID}</UserId>", "<UserId>S-1-5-21-secret</UserId>"),
        ("2000-01-01T18:20:00", "2000-01-01T19:45:00"),
        (
            "<StartWhenAvailable>true</StartWhenAvailable>",
            "<StartWhenAvailable>false</StartWhenAvailable>",
        ),
        (
            r"<Command>C:\Program Files\TeamMem\teammem.exe</Command>",
            r"<Command>C:\secret\other.exe</Command>",
        ),
        (
            r'--env-file "C:\Users\Alex\App Data\hub.env" run-daily',
            r"--env-file C:\secret\hub.env run-daily",
        ),
    )
    for original, tampered in replacements:
        text = text.replace(original, tampered, 1)
    xml = b"\xef\xbb\xbf" + text.encode("utf-8")

    categories = windows.task_xml_mismatch_categories(xml, schedule)

    assert categories == ("registration.source",)
    diagnostic = ",".join(categories)
    for secret in ("secret-source", "S-1-5-21-secret", "19:45", r"C:\secret"):
        assert secret not in diagnostic


def test_task_xml_mismatch_categories_report_unmatched_structure():
    schedule = _schedule()
    text = windows.build_task_xml(schedule)[2:].decode("utf-16-le")
    text = text.replace(windows.OWNERSHIP_SOURCE, "secret-source", 1)
    text = text.replace("</Settings>", "<Priority>7</Priority></Settings>", 1)

    assert windows.task_xml_mismatch_categories(
        b"\xef\xbb\xbf" + text.encode("utf-8"), schedule
    ) == ("xml.structure",)


@pytest.mark.parametrize(
    "original,tampered",
    [
        ("</LogonType>", "</LogonType><RunLevel>HighestAvailable</RunLevel>"),
        ("</ScheduleByDay>", "</ScheduleByDay><Enabled>false</Enabled>"),
        ("</Settings>", "<Enabled>false</Enabled></Settings>"),
        (
            "</Settings>",
            "<RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable></Settings>",
        ),
        ("</Settings>", "<WakeToRun>true</WakeToRun></Settings>"),
        ("<StopOnIdleEnd>true</StopOnIdleEnd>", "<StopOnIdleEnd>false</StopOnIdleEnd>"),
        ("<RestartOnIdle>false</RestartOnIdle>", "<RestartOnIdle>true</RestartOnIdle>"),
        (
            "<UseUnifiedSchedulingEngine>false</UseUnifiedSchedulingEngine>",
            "<UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>",
        ),
        (
            "</Settings>",
            "<UseUnifiedSchedulingEngine>false</UseUnifiedSchedulingEngine></Settings>",
        ),
        ("</Settings>", "<Priority>7</Priority></Settings>"),
    ],
)
def test_task_xml_rejects_nondefault_duplicate_or_unknown_scheduler_normalization(
    original, tampered
):
    schedule = _schedule()
    text = _scheduler_normalized_task_xml(schedule).decode("utf-8-sig")
    text = text.replace(original, tampered, 1)

    with pytest.raises(RuntimeError, match="^Windows schedule definition is not managed by TeamMem$"):
        windows.parse_task_xml(b"\xef\xbb\xbf" + text.encode("utf-8"), schedule)


@pytest.mark.parametrize(
    "addition",
    [
        "<Date>2026-07-30T08:13:39.1234567</Date><Date>2026-07-30T08:13:40</Date>",
        "<Author>CI\\runneradmin</Author><Author>CI\\runneradmin</Author>",
        "<SecurityDescriptor>D:(A;;FA;;;WD)</SecurityDescriptor>",
    ],
)
def test_task_xml_rejects_duplicate_or_unknown_registration_metadata(addition):
    schedule = _schedule()
    text = windows.build_task_xml(schedule)[2:].decode("utf-16-le")
    text = text.replace("<RegistrationInfo>", "<RegistrationInfo>" + addition, 1)

    with pytest.raises(RuntimeError, match="^Windows schedule definition is not managed by TeamMem$"):
        windows.parse_task_xml(b"\xff\xfe" + text.encode("utf-16-le"), schedule)


@pytest.mark.parametrize(
    "original,tampered",
    [
        (windows.OWNERSHIP_SOURCE, " " + windows.OWNERSHIP_SOURCE + " "),
        ("C:\\Program Files\\TeamMem\\teammem.exe", " C:\\Program Files\\TeamMem\\teammem.exe "),
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


class FakeTaskRunner:
    """Hermetic byte-only model of the small schtasks surface TeamMem uses."""

    def __init__(self, tasks=None):
        self.tasks = dict(tasks or {})
        self.calls = []
        self.failures = {}
        self.hook = None
        self.list_bytes = None

    def __call__(self, command, **kwargs):
        assert kwargs == {"capture_output": True, "text": False}
        self.calls.append((list(command), kwargs))
        operation = command[1]
        failure = self.failures.get(operation)
        if failure:
            return subprocess.CompletedProcess(command, failure, b"secret stdout", b"secret stderr")
        if operation == "/Query" and "/XML" in command:
            name = command[command.index("/TN") + 1]
            xml = self.tasks.get(name)
            if xml is None:
                result = subprocess.CompletedProcess(command, 1, b"", b"not found")
            else:
                result = subprocess.CompletedProcess(command, 0, xml, b"")
            return self.hook(command, result) if self.hook else result
        if operation == "/Query":
            rows = self.list_bytes if self.list_bytes is not None else b"".join(
                name.encode() + b",Task\r\n" for name in self.tasks
            )
            result = subprocess.CompletedProcess(command, 0, rows, b"")
            return self.hook(command, result) if self.hook else result
        if operation == "/Create":
            name = command[command.index("/TN") + 1]
            xml_path = Path(command[command.index("/XML") + 1])
            self.tasks[name] = xml_path.read_bytes()
            result = subprocess.CompletedProcess(command, 0, b"", b"")
            return self.hook(command, result) if self.hook else result
        if operation == "/Delete":
            name = command[command.index("/TN") + 1]
            self.tasks.pop(name, None)
            result = subprocess.CompletedProcess(command, 0, b"", b"")
            return self.hook(command, result) if self.hook else result
        raise AssertionError(command)


class RecordingLock:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        self.events.append("lock-enter")
        return self

    def __exit__(self, *_args):
        self.events.append("lock-exit")


def _lifecycle_schedule(**changes):
    values = {
        "executable": r"C:\Program Files\TeamMem\teammem.exe",
        "env_file": r"C:\Users\Alex\AppData\Roaming\TeamMemory\hub.env",
    }
    values.update(changes)
    return _schedule(**values)


def _lifecycle_args(tmp_path, monkeypatch, *, runner=None, task=None):
    schedule = task or _lifecycle_schedule()
    runner = runner or FakeTaskRunner()
    events = []
    monkeypatch.setattr(windows, "provision_windows_state_dir", lambda path, sid, api: events.append("state") or tmp_path)
    monkeypatch.setattr(windows, "validate_windows_env_file", lambda path, sid, api: events.append("env") or path)
    api = FakeWindowsApi({})
    cfg = type("Cfg", (), {"env_file": Path(schedule.env_file)})()
    return schedule, runner, events, api, cfg


def test_windows_status_reports_absent_with_exact_byte_query(tmp_path, monkeypatch):
    schedule, runner, _events, api, _cfg = _lifecycle_args(tmp_path, monkeypatch)
    status = windows.schedule_status(
        api=api, runner=runner, executable=schedule.executable,
        env_file=schedule.env_file,
    )
    assert status.installed is False
    assert status.time is None
    assert status.backend == "windows"
    assert status.path == Path(schedule.task_name)
    assert runner.calls == [
        (["schtasks.exe", "/Query", "/TN", schedule.task_name, "/XML"], {"capture_output": True, "text": False}),
        (["schtasks.exe", "/Query", "/FO", "CSV", "/NH"], {"capture_output": True, "text": False}),
    ]


def test_windows_status_validates_registered_xml_and_refuses_foreign_task(tmp_path, monkeypatch):
    schedule, runner, _events, api, _cfg = _lifecycle_args(tmp_path, monkeypatch)
    runner.tasks[schedule.task_name] = windows.build_task_xml(schedule)
    status = windows.schedule_status(
        api=api, runner=runner, executable=schedule.executable,
        env_file=schedule.env_file,
    )
    assert (status.installed, status.time, status.backend, status.path) == (
        True, "18:20", "windows", Path(schedule.task_name)
    )

    text = windows.build_task_xml(schedule)[2:].decode("utf-16-le")
    foreign = b"\xff\xfe" + text.replace("TeamMem", "Other", 1).encode("utf-16-le")
    runner.tasks[schedule.task_name] = foreign
    with pytest.raises(RuntimeError, match="conflicts"):
        windows.schedule_status(
            api=api, runner=runner, executable=schedule.executable,
            env_file=schedule.env_file,
        )


def test_windows_status_sanitizes_query_failure_when_list_contains_task(tmp_path, monkeypatch):
    schedule, runner, _events, api, _cfg = _lifecycle_args(tmp_path, monkeypatch)
    runner.tasks[schedule.task_name] = windows.build_task_xml(schedule)
    runner.failures["/Query"] = 1
    with pytest.raises(RuntimeError, match="Windows scheduler status is unavailable") as error:
        windows.schedule_status(
            api=api, runner=runner, executable=schedule.executable,
            env_file=schedule.env_file,
        )
    assert "secret" not in str(error.value)


def test_windows_install_replaces_under_lock_and_removes_private_temp_file(tmp_path, monkeypatch):
    previous = _lifecycle_schedule()
    replacement = _lifecycle_schedule(time="07:05")
    runner = FakeTaskRunner({previous.task_name: windows.build_task_xml(previous)})
    _schedule_value, runner, events, api, cfg = _lifecycle_args(
        tmp_path, monkeypatch, runner=runner, task=replacement
    )
    path = windows.install_schedule(
        cfg, 7, 5, replacement.executable, api=api, runner=runner,
        state_dir=tmp_path, lock_factory=lambda _path: RecordingLock(events),
    )
    assert path == Path(replacement.task_name)
    assert events[:3] == ["env", "state", "lock-enter"]
    assert events[-1] == "lock-exit"
    assert windows.parse_task_xml(runner.tasks[replacement.task_name], replacement) == "07:05"
    create = next(command for command, _kwargs in runner.calls if command[1] == "/Create")
    assert Path(create[create.index("/XML") + 1]).parent == tmp_path
    assert list(tmp_path.glob("*.xml")) == []


def test_windows_install_rolls_back_exact_prior_xml_and_sanitizes_failure(tmp_path, monkeypatch):
    previous = _lifecycle_schedule()
    replacement = _lifecycle_schedule(time="07:05")
    prior_xml = windows.build_task_xml(previous)
    runner = FakeTaskRunner({previous.task_name: prior_xml})
    _value, runner, events, api, cfg = _lifecycle_args(tmp_path, monkeypatch, runner=runner, task=replacement)
    seen_create = 0

    def fail_verification(command, result):
        nonlocal seen_create
        if command[1] == "/Create":
            seen_create += 1
        if command[1] == "/Query" and "/XML" in command and seen_create == 1:
            return subprocess.CompletedProcess(command, 0, b"bad xml secret-token", b"")
        return result

    runner.hook = fail_verification
    with pytest.raises(RuntimeError, match="previous state restored") as error:
        windows.install_schedule(cfg, 7, 5, replacement.executable, api=api, runner=runner,
                                 state_dir=tmp_path, lock_factory=lambda _path: RecordingLock(events))
    assert "secret-token" not in str(error.value)
    assert runner.tasks[previous.task_name] == prior_xml


def test_windows_remove_is_idempotent_and_restores_after_verification_failure(tmp_path, monkeypatch):
    schedule = _lifecycle_schedule()
    prior_xml = windows.build_task_xml(schedule)
    runner = FakeTaskRunner({schedule.task_name: prior_xml})
    _value, runner, events, api, _cfg = _lifecycle_args(tmp_path, monkeypatch, runner=runner, task=schedule)
    deleted = False

    def fail_absence_verification(command, result):
        nonlocal deleted
        if command[1] == "/Delete":
            deleted = True
        if command[1] == "/Query" and "/XML" in command and deleted and schedule.task_name not in runner.tasks:
            return subprocess.CompletedProcess(command, 0, prior_xml, b"")
        return result

    runner.hook = fail_absence_verification
    with pytest.raises(RuntimeError, match="previous state restored"):
        windows.remove_schedule(api=api, runner=runner, state_dir=tmp_path,
                                executable=schedule.executable, env_file=schedule.env_file,
                                lock_factory=lambda _path: RecordingLock(events))
    assert runner.tasks[schedule.task_name] == prior_xml
    assert windows.remove_schedule(api=api, runner=FakeTaskRunner(), state_dir=tmp_path,
                                   executable=schedule.executable, env_file=schedule.env_file,
                                   lock_factory=lambda _path: RecordingLock([])) is False


def test_windows_status_and_install_reject_managed_task_with_unexpected_action(tmp_path, monkeypatch):
    expected = _lifecycle_schedule()
    attacker = _lifecycle_schedule(
        executable=r"C:\Users\Alex\attacker.exe",
        env_file=r"C:\Users\Alex\evil.env",
    )
    runner = FakeTaskRunner({expected.task_name: windows.build_task_xml(attacker)})
    _value, runner, events, api, cfg = _lifecycle_args(
        tmp_path, monkeypatch, runner=runner, task=expected
    )
    with pytest.raises(RuntimeError, match="conflicts"):
        windows.schedule_status(
            api=api, runner=runner, executable=expected.executable,
            env_file=expected.env_file,
        )
    with pytest.raises(RuntimeError, match="conflicts"):
        windows.install_schedule(
            cfg, 18, 20, expected.executable, api=api, runner=runner,
            state_dir=tmp_path, lock_factory=lambda _path: RecordingLock(events),
        )
    assert not any(command[1] == "/Create" for command, _kwargs in runner.calls)


@pytest.mark.parametrize(
    "payload",
    [
        b"\xef\xbb\xbf\\TeamMem-Daily-abc,Task\r\n",
        b"\xff\xfe" + "\\TeamMem-Daily-abc,Task\r\n".encode("utf-16-le"),
        b"\xfe\xff" + "\\TeamMem-Daily-abc,Task\r\n".encode("utf-16-be"),
    ],
)
def test_windows_csv_fallback_matches_exact_name_with_bom(payload):
    assert windows._csv_contains_task(payload, r"\TeamMem-Daily-abc") is True


def test_windows_state_directory_is_created_then_validated_and_rejects_racing_unsafe_creator():
    path = Path(r"C:\Users\Alex\AppData\Local\TeamMemory")
    api = FakeWindowsApi({})
    assert provision_windows_state_dir(path, SID, api) == path
    assert api.opened == [(path, True), (path, True)]

    class UnsafeCreator(FakeWindowsApi):
        def create_directory(self, created):
            self.records[Path(created)] = _directory(aces=(("Everyone", "read"),))

    with pytest.raises(ValueError, match="shared principal"):
        provision_windows_state_dir(path, SID, UnsafeCreator({}))


def test_windows_first_install_write_failure_with_absence_reports_restored(tmp_path, monkeypatch):
    schedule, runner, events, api, cfg = _lifecycle_args(tmp_path, monkeypatch)
    monkeypatch.setattr(windows, "_write_xml", lambda *_args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(RuntimeError, match="previous state restored"):
        windows.install_schedule(
            cfg, 18, 20, schedule.executable, api=api, runner=runner,
            state_dir=tmp_path, lock_factory=lambda _path: RecordingLock(events),
        )
    assert runner.tasks == {}
    assert not any(command[1] == "/Delete" for command, _kwargs in runner.calls)


def test_windows_cleanup_failure_restores_prior_task_and_surfaces_sanitized_error(tmp_path, monkeypatch):
    previous = _lifecycle_schedule()
    replacement = _lifecycle_schedule(time="07:05")
    prior_xml = windows.build_task_xml(previous)
    runner = FakeTaskRunner({previous.task_name: prior_xml})
    _value, runner, events, api, cfg = _lifecycle_args(
        tmp_path, monkeypatch, runner=runner, task=replacement
    )
    calls = 0

    def fail_once(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("private cleanup detail")
        path.unlink()

    monkeypatch.setattr(windows, "_remove_temp", fail_once)
    with pytest.raises(RuntimeError, match="previous state restored") as error:
        windows.install_schedule(
            cfg, 7, 5, replacement.executable, api=api, runner=runner,
            state_dir=tmp_path, lock_factory=lambda _path: RecordingLock(events),
        )
    assert "private cleanup detail" not in str(error.value)
    assert runner.tasks[previous.task_name] == prior_xml
    assert calls == 2


@pytest.mark.parametrize("operation", ["install", "remove"])
def test_windows_persistent_cleanup_failure_reports_rollback_failed(tmp_path, monkeypatch, operation):
    schedule = _lifecycle_schedule()
    prior_xml = windows.build_task_xml(schedule)
    runner = FakeTaskRunner({schedule.task_name: prior_xml})
    _value, runner, events, api, cfg = _lifecycle_args(
        tmp_path, monkeypatch, runner=runner, task=schedule
    )

    def always_fail(_path):
        raise OSError("private persistent cleanup detail")

    monkeypatch.setattr(windows, "_remove_temp", always_fail)
    if operation == "install":
        call = lambda: windows.install_schedule(
            cfg, 18, 20, schedule.executable, api=api, runner=runner,
            state_dir=tmp_path, lock_factory=lambda _path: RecordingLock(events),
        )
    else:
        call = lambda: windows.remove_schedule(
            api=api, runner=runner, state_dir=tmp_path,
            executable=schedule.executable, env_file=schedule.env_file,
            lock_factory=lambda _path: RecordingLock(events),
        )
    with pytest.raises(RuntimeError, match="rollback failed") as error:
        call()
    assert "private persistent cleanup detail" not in str(error.value)


def test_windows_lock_retries_contention_until_one_byte_lock_is_acquired():
    calls = []
    sleeps = []

    def locking(descriptor, mode, length):
        calls.append((descriptor, mode, length))
        if len(calls) < 3:
            raise OSError(13, "contended")

    windows._lock_byte(locking, 42, sleeps.append)
    assert calls == [(42, 2, 1), (42, 2, 1), (42, 2, 1)]
    assert sleeps == [0.05, 0.05]


@pytest.mark.parametrize("failure", [OSError(5, "access denied"), OSError(9, "bad handle")])
def test_windows_lock_propagates_non_contention_errors_without_retry(failure):
    calls = []
    with pytest.raises(OSError) as error:
        windows._lock_byte(
            lambda *_args: calls.append(1) or (_ for _ in ()).throw(failure),
            42,
            lambda _delay: pytest.fail("non-contention errors must not sleep"),
        )
    assert error.value.errno == failure.errno
    assert calls == [1]


def test_windows_lock_gives_winerror_priority_over_errno_and_retries_lock_violation():
    denied = OSError(13, "access denied")
    denied.winerror = 5
    with pytest.raises(OSError) as error:
        windows._lock_byte(
            lambda *_args: (_ for _ in ()).throw(denied),
            42,
            lambda _delay: pytest.fail("non-contention winerror must not sleep"),
        )
    assert error.value.winerror == 5

    calls = []

    def locking(*_args):
        calls.append(1)
        if len(calls) == 1:
            contended = OSError(5, "lock violation")
            contended.winerror = 33
            raise contended

    windows._lock_byte(locking, 42, lambda _delay: None)
    assert calls == [1, 1]


def test_windows_cleanup_requires_explicit_not_found_proof_after_unlink_error():
    class InaccessibleTemp:
        def unlink(self):
            raise PermissionError(13, "denied")

        def stat(self):
            raise PermissionError(13, "stat denied")

        def exists(self):
            pytest.fail("cleanup must not rely on Path.exists")

    assert windows._cleanup_temp(InaccessibleTemp()) is False
