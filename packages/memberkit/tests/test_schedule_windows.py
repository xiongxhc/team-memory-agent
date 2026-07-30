import hashlib

import pytest

import memberkit.schedule_windows as windows
from memberkit.schedule_windows import (
    decode_arguments,
    encode_arguments,
    task_name,
)


SID = "S-1-5-21-111-222-333-1001"


def test_task_name_is_sid_specific_without_exposing_the_sid():
    expected = (
        "\\TeamMem-MemberKit-Daily-"
        + hashlib.sha256(SID.encode("utf-8")).hexdigest()[:12]
    )

    assert task_name(SID) == expected
    assert SID not in task_name(SID)
    assert task_name("S-1-5-21-111-222-333-1002") != expected


@pytest.mark.parametrize(
    ("arguments", "command_line"),
    [
        ([], ""),
        ([""], '""'),
        (["scheduled-run"], "scheduled-run"),
        (["two words"], '"two words"'),
        (['say"hello'], '"say\\"hello"'),
        (["C:\\path with space\\"], '"C:\\path with space\\\\"'),
        (["مرحبا", "笔记"], "مرحبا 笔记"),
        (
            ["", "two words", 'say"hello', "tail\\"],
            '"" "two words" "say\\"hello" tail\\',
        ),
    ],
)
def test_argument_codec_uses_canonical_windows_c_runtime_forms(
    arguments,
    command_line,
):
    assert encode_arguments(arguments) == command_line
    assert decode_arguments(command_line) == arguments
    assert encode_arguments(decode_arguments(command_line)) == command_line


@pytest.mark.parametrize(
    "arguments",
    [
        ["nul\0byte"],
        ["line\nbreak"],
        ["carriage\rreturn"],
        ["tab\tseparator"],
        ["unit\x1fseparator"],
    ],
)
def test_argument_encoder_rejects_control_characters(arguments):
    with pytest.raises(ValueError, match="unsafe Windows argument"):
        encode_arguments(arguments)


@pytest.mark.parametrize(
    "command_line",
    [
        "nul\0byte",
        "line\nbreak",
        "carriage\rreturn",
        "tab\tseparator",
        "unit\x1fseparator",
    ],
)
def test_argument_decoder_rejects_control_characters(command_line):
    with pytest.raises(ValueError, match="unsafe Windows argument"):
        decode_arguments(command_line)


def _schedule(**changes):
    values = {
        "sid": SID,
        "task_name": task_name(SID),
        "time": "17:30",
        "executable": r"C:\Program Files\TeamMem\memberkit.exe",
    }
    values.update(changes)
    return windows.WindowsSchedule(**values)


def _xml_text(schedule=None):
    schedule = schedule or _schedule()
    return windows.build_task_xml(schedule)[2:].decode("utf-16-le")


def _xml_bytes(text, encoding="utf-16-le", bom=b"\xff\xfe"):
    return bom + text.encode(encoding)


def test_xml_is_deterministic_complete_and_contains_only_the_reminder_action():
    schedule = _schedule()

    xml = windows.build_task_xml(schedule)
    decoded = xml[2:].decode("utf-16-le")

    assert xml.startswith(b"\xff\xfe")
    assert decoded.startswith('<?xml version="1.0" encoding="UTF-16"?>\r\n')
    for literal in (
        "<Source>TeamMem-MemberKit</Source>",
        "<Description>TeamMem MemberKit daily draft reminder</Description>",
        f"<URI>{schedule.task_name}</URI>",
        f"<UserId>{SID}</UserId>",
        "<LogonType>InteractiveToken</LogonType>",
        "<RunLevel>LeastPrivilege</RunLevel>",
        "<StartBoundary>2000-01-01T17:30:00</StartBoundary>",
        "<DaysInterval>1</DaysInterval>",
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        "<StartWhenAvailable>true</StartWhenAvailable>",
        "<Enabled>true</Enabled>",
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
        "<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>",
        "<WakeToRun>false</WakeToRun>",
        "<UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>",
        "<ExecutionTimeLimit>PT4H</ExecutionTimeLimit>",
        r"<Command>C:\Program Files\TeamMem\memberkit.exe</Command>",
        "<Arguments>scheduled-run</Arguments>",
    ):
        assert literal in decoded
    assert decoded.count('<Principal id="Author">') == 1
    assert decoded.count("<CalendarTrigger>") == 1
    assert decoded.count("<Exec>") == 1
    for forbidden in (
        "<WorkingDirectory>",
        "--env-file",
        "memberkit.env",
        "config.toml",
        "secret-token",
    ):
        assert forbidden not in decoded
    assert windows.parse_task_xml(xml, schedule) == "17:30"
    assert windows.build_task_xml(schedule) == xml


@pytest.mark.parametrize(
    ("encoding", "bom"),
    [
        ("utf-8", b""),
        ("utf-8", b"\xef\xbb\xbf"),
        ("utf-16-le", b""),
        ("utf-16-le", b"\xff\xfe"),
        ("utf-16-be", b""),
        ("utf-16-be", b"\xfe\xff"),
    ],
)
def test_xml_accepts_only_signature_recognized_utf_transports(encoding, bom):
    schedule = _schedule()
    text = _xml_text(schedule)

    assert windows.parse_task_xml(
        _xml_bytes(text, encoding=encoding, bom=bom),
        schedule,
    ) == "17:30"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not xml",
        b"\xff\xfe\x00",
        b"\xff\xfe\x00\x00" + b"<\x00\x00\x00",
        b"\x00\x00\xfe\xff" + b"\x00\x00\x00<",
    ],
)
def test_xml_rejects_empty_malformed_or_unrecognized_transport(payload):
    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(payload, _schedule())


def test_xml_rejects_oversized_input_before_parsing():
    payload = b"<" + b"x" * (1024 * 1024)

    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(payload, _schedule())


@pytest.mark.parametrize(
    "executable",
    [
        "memberkit.exe",
        r"C:memberkit.exe",
        r"\relative\memberkit.exe",
        r"\\.\pipe\memberkit",
        r"\\?\C:\Program Files\TeamMem\memberkit.exe",
        r"\\server",
        r"1:\TeamMem\memberkit.exe",
        "C:\\TeamMem\\memberkit.exe\0suffix",
    ],
)
def test_xml_rejects_non_absolute_or_device_executable_paths(executable):
    with pytest.raises(ValueError, match="absolute Windows"):
        windows.build_task_xml(_schedule(executable=executable))


def test_xml_accepts_complete_unc_executable_path():
    schedule = _schedule(executable=r"\\server\share\TeamMem\memberkit.exe")

    assert windows.parse_task_xml(
        windows.build_task_xml(schedule),
        schedule,
    ) == "17:30"


@pytest.mark.parametrize(
    "executable",
    [
        r"C:\Windows\System32\cmd.exe",
        r"C:\Windows\System32\powershell.exe",
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        r"C:\WINDOWS\SYSTEM32\CMD.EXE",
    ],
)
def test_xml_rejects_shell_executables(executable):
    with pytest.raises(ValueError, match="shell"):
        windows.build_task_xml(_schedule(executable=executable))


@pytest.mark.parametrize(
    "time",
    [
        "7:30",
        "17:3",
        "24:00",
        "17:60",
        " 17:30",
        "17:30 ",
        "１７:３０",
    ],
)
def test_xml_rejects_noncanonical_or_out_of_range_times(time):
    with pytest.raises(ValueError, match="HH:MM"):
        windows.build_task_xml(_schedule(time=time))


@pytest.mark.parametrize(
    ("original", "tampered"),
    [
        ("TeamMem-MemberKit", "Foreign"),
        (
            "TeamMem MemberKit daily draft reminder",
            "TeamMem MemberKit other action",
        ),
        ("<URI>\\TeamMem-MemberKit-Daily-", "<URI>\\Foreign-"),
        (f"<UserId>{SID}</UserId>", "<UserId>S-1-5-21-foreign</UserId>"),
        ("InteractiveToken", "Password"),
        ("LeastPrivilege", "HighestAvailable"),
        ("2000-01-01T17:30:00", "2000-01-01T19:45:00"),
        ("<DaysInterval>1</DaysInterval>", "<DaysInterval>2</DaysInterval>"),
        (
            "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
            "<MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>",
        ),
        (
            "<StartWhenAvailable>true</StartWhenAvailable>",
            "<StartWhenAvailable>false</StartWhenAvailable>",
        ),
        (
            "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
            "<DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>",
        ),
        (
            "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>",
            "<StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>",
        ),
        (
            "<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>",
            "<RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>",
        ),
        ("<WakeToRun>false</WakeToRun>", "<WakeToRun>true</WakeToRun>"),
        (
            "<UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>",
            "<UseUnifiedSchedulingEngine>false</UseUnifiedSchedulingEngine>",
        ),
        ("<ExecutionTimeLimit>PT4H</ExecutionTimeLimit>", "<ExecutionTimeLimit>PT8H</ExecutionTimeLimit>"),
        ("memberkit.exe", "other.exe"),
        ("<Arguments>scheduled-run</Arguments>", "<Arguments>other</Arguments>"),
        (
            "<Arguments>scheduled-run</Arguments>",
            '<Arguments>"scheduled-run"</Arguments>',
        ),
    ],
)
def test_xml_rejects_semantically_changed_or_noncanonical_definition(
    original,
    tampered,
):
    schedule = _schedule()
    text = _xml_text(schedule).replace(original, tampered, 1)

    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(_xml_bytes(text), schedule)


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        (
            "</Principals>",
            (
                "<Principal><UserId>S-1-5-21-foreign</UserId>"
                "<LogonType>InteractiveToken</LogonType></Principal>"
                "</Principals>"
            ),
        ),
        (
            "</Triggers>",
            (
                "<CalendarTrigger><StartBoundary>2000-01-01T17:30:00"
                "</StartBoundary><ScheduleByDay><DaysInterval>1</DaysInterval>"
                "</ScheduleByDay></CalendarTrigger></Triggers>"
            ),
        ),
        (
            "</Actions>",
            (
                "<Exec><Command>C:\\other.exe</Command>"
                "<Arguments>scheduled-run</Arguments></Exec></Actions>"
            ),
        ),
        (
            "</Exec>",
            "<WorkingDirectory>C:\\Users\\Alex</WorkingDirectory></Exec>",
        ),
        (
            "</Actions>",
            (
                "<ComHandler><ClassId>{00000000-0000-0000-0000-000000000000}"
                "</ClassId></ComHandler></Actions>"
            ),
        ),
        ("</Settings>", "<Priority>7</Priority></Settings>"),
    ],
)
def test_xml_rejects_extra_principal_trigger_action_or_field(
    needle,
    replacement,
):
    schedule = _schedule()
    text = _xml_text(schedule).replace(needle, replacement, 1)

    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(_xml_bytes(text), schedule)


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        (
            "</RegistrationInfo>",
            (
                "</RegistrationInfo><RegistrationInfo>"
                "<Source>TeamMem-MemberKit</Source>"
                f"<URI>{task_name(SID)}</URI>"
                "<Description>TeamMem MemberKit daily draft reminder</Description>"
                "</RegistrationInfo>"
            ),
        ),
        (
            "</Principals>",
            (
                '</Principals><Principals><Principal id="Author">'
                f"<UserId>{SID}</UserId><LogonType>InteractiveToken</LogonType>"
                "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>"
            ),
        ),
        (
            "</Triggers>",
            (
                "</Triggers><Triggers><CalendarTrigger>"
                "<StartBoundary>2000-01-01T17:30:00</StartBoundary>"
                "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
                "<Enabled>true</Enabled></CalendarTrigger></Triggers>"
            ),
        ),
        (
            "</Settings>",
            (
                "</Settings><Settings>"
                "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
                "<StartWhenAvailable>true</StartWhenAvailable>"
                "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
                "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
                "<ExecutionTimeLimit>PT4H</ExecutionTimeLimit>"
                "<UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>"
                "</Settings>"
            ),
        ),
        (
            "</Actions>",
            (
                '</Actions><Actions Context="Author"><Exec>'
                r"<Command>C:\Program Files\TeamMem\memberkit.exe</Command>"
                "<Arguments>scheduled-run</Arguments></Exec></Actions>"
            ),
        ),
        ("<Source>TeamMem-MemberKit</Source>", ""),
        ("<Principal id=\"Author\">", ""),
        ("<CalendarTrigger>", ""),
        ("<Exec>", ""),
    ],
)
def test_xml_rejects_duplicate_or_missing_managed_structure(needle, replacement):
    schedule = _schedule()
    text = _xml_text(schedule).replace(needle, replacement, 1)

    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(_xml_bytes(text), schedule)


@pytest.mark.parametrize(
    ("original", "tampered"),
    [
        (
            ' xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"',
            "",
        ),
        ("version=\"1.4\"", 'version="1.4" extra="value"'),
        ('<Principal id="Author">', '<Principal id="Other">'),
        ('<Actions Context="Author">', '<Actions Context="Other">'),
        ("<Exec>", '<Exec extra="value">'),
    ],
)
def test_xml_requires_exact_namespace_version_and_binding_attributes(
    original,
    tampered,
):
    schedule = _schedule()
    text = _xml_text(schedule).replace(original, tampered, 1)

    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(_xml_bytes(text), schedule)


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE x [<!ENTITY x "y">]><Task>&x;</Task>',
        b'<!ENTITY x "y"><Task>&x;</Task>',
    ],
)
def test_xml_rejects_entity_bearing_documents(payload):
    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(payload, _schedule())


def _scheduler_normalized_xml(schedule):
    text = _xml_text(schedule)
    text = text.replace("<RunLevel>LeastPrivilege</RunLevel>", "", 1)
    text = text.replace(
        "<Enabled>true</Enabled></CalendarTrigger>",
        "</CalendarTrigger>",
        1,
    )
    generated = (
        "<Settings>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<Enabled>true</Enabled>"
        "<StartWhenAvailable>true</StartWhenAvailable>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "<RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>"
        "<WakeToRun>false</WakeToRun>"
        "<ExecutionTimeLimit>PT4H</ExecutionTimeLimit>"
        "<UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>"
        "</Settings>"
    )
    normalized = (
        "<Settings>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "<ExecutionTimeLimit>PT4H</ExecutionTimeLimit>"
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<StartWhenAvailable>true</StartWhenAvailable>"
        "<IdleSettings><StopOnIdleEnd>true</StopOnIdleEnd>"
        "<RestartOnIdle>false</RestartOnIdle></IdleSettings>"
        "<UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>"
        "</Settings>"
    )
    assert generated in text
    return text.replace(generated, normalized, 1)


def test_xml_accepts_only_the_proven_scheduler_added_defaults():
    schedule = _schedule()
    text = _scheduler_normalized_xml(schedule).replace(
        "<RegistrationInfo>",
        (
            "<RegistrationInfo>"
            "<Date>2026-07-30T08:13:39.1234567</Date>"
            "<Author>CI\\runneradmin</Author>"
        ),
        1,
    )
    xml = _xml_bytes(text, encoding="utf-8", bom=b"\xef\xbb\xbf")

    assert windows.parse_task_xml(xml, schedule) == "17:30"
    assert windows.task_xml_mismatch_categories(xml, schedule) == ()


@pytest.mark.parametrize(
    ("original", "tampered"),
    [
        ("</LogonType>", "</LogonType><RunLevel>HighestAvailable</RunLevel>"),
        ("</ScheduleByDay>", "</ScheduleByDay><Enabled>false</Enabled>"),
        ("</Settings>", "<Enabled>false</Enabled></Settings>"),
        (
            "</Settings>",
            "<RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable></Settings>",
        ),
        ("</Settings>", "<WakeToRun>true</WakeToRun></Settings>"),
        (
            "<StopOnIdleEnd>true</StopOnIdleEnd>",
            "<StopOnIdleEnd>false</StopOnIdleEnd>",
        ),
        (
            "<RestartOnIdle>false</RestartOnIdle>",
            "<RestartOnIdle>true</RestartOnIdle>",
        ),
        (
            "<UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>",
            "",
        ),
        ("</Settings>", "<Priority>7</Priority></Settings>"),
    ],
)
def test_xml_rejects_changed_or_extra_scheduler_defaults(original, tampered):
    schedule = _schedule()
    text = _scheduler_normalized_xml(schedule).replace(original, tampered, 1)

    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(
            _xml_bytes(text, encoding="utf-8", bom=b"\xef\xbb\xbf"),
            schedule,
        )


@pytest.mark.parametrize(
    "addition",
    [
        "<Date>2026-07-30T08:13:39</Date><Date>2026-07-30T08:14:00</Date>",
        "<Author>CI\\runneradmin</Author><Author>CI\\runneradmin</Author>",
        "<SecurityDescriptor>D:(A;;FA;;;WD)</SecurityDescriptor>",
    ],
)
def test_xml_rejects_duplicate_or_unknown_registration_metadata(addition):
    schedule = _schedule()
    text = _xml_text(schedule).replace(
        "<RegistrationInfo>",
        "<RegistrationInfo>" + addition,
        1,
    )

    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(_xml_bytes(text), schedule)


def test_xml_mismatch_categories_are_fixed_and_never_include_values():
    schedule = _schedule()
    secret = "secret-source-value"
    text = _xml_text(schedule).replace("TeamMem-MemberKit", secret, 1)
    xml = _xml_bytes(text, encoding="utf-8", bom=b"\xef\xbb\xbf")

    categories = windows.task_xml_mismatch_categories(xml, schedule)

    assert categories == ("registration.source",)
    assert secret not in ",".join(categories)


def test_xml_mismatch_categories_collapse_unmatched_shape_to_structure():
    schedule = _schedule()
    text = _xml_text(schedule).replace(
        "</Settings>",
        "<Priority>secret-value</Priority></Settings>",
        1,
    )

    assert windows.task_xml_mismatch_categories(
        _xml_bytes(text),
        schedule,
    ) == ("xml.structure",)


def test_xml_task_name_must_be_derived_from_the_same_sid():
    schedule = _schedule(task_name=task_name("S-1-5-21-foreign"))

    with pytest.raises(ValueError, match="current SID"):
        windows.build_task_xml(schedule)
