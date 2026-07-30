import hashlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

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
    "executable",
    [
        r"C:\TeamMem\other.exe",
        r"C:\Windows\System32\cmd.exe.",
        "C:\\Windows\\System32\\cmd.exe ",
        r"C:\Windows\System32\powershell.exe.",
        "C:\\Program Files\\PowerShell\\7\\pwsh.exe ",
        r"C:\TeamMem\MEMBERKIT.EXE",
        r"C:\TeamMem\memberkit.exe.",
        "C:\\TeamMem\\memberkit.exe ",
        r"C:\TeamMem.\memberkit.exe",
        "C:\\TeamMem \\memberkit.exe",
        r"C:\TeamMem\.\memberkit.exe",
        r"C:\TeamMem\\memberkit.exe",
        "C:/TeamMem/memberkit.exe",
        "C:\\TeamMem\\memberkit.exe\n",
    ],
)
def test_xml_builder_rejects_noncanonical_memberkit_executable(executable):
    with pytest.raises(ValueError, match="canonical memberkit.exe"):
        windows.build_task_xml(_schedule(executable=executable))


def test_xml_parser_rejects_matching_non_memberkit_executable():
    expected = _schedule(executable=r"C:\TeamMem\other.exe")
    text = _xml_text().replace(
        r"C:\Program Files\TeamMem\memberkit.exe",
        expected.executable,
        1,
    )

    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(_xml_bytes(text), expected)


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


@pytest.mark.parametrize(
    "addition",
    [
        "<!-- lexical tampering -->",
        "<?memberkit lexical-tampering?>",
    ],
)
def test_xml_rejects_comments_and_processing_instructions(addition):
    schedule = _schedule()
    text = _xml_text(schedule)
    mutations = [
        text.replace(
            "</RegistrationInfo>",
            "</RegistrationInfo>" + addition,
            1,
        ),
        text.replace("\r\n<Task", "\r\n" + addition + "<Task", 1),
        text + addition,
    ]

    for mutation in mutations:
        with pytest.raises(RuntimeError, match="not managed"):
            windows.parse_task_xml(_xml_bytes(mutation), schedule)


@pytest.mark.parametrize(
    ("original", "tampered"),
    [
        ("<RegistrationInfo>", "<RegistrationInfo>lexical-tampering"),
        ("<Settings>", "<Settings>lexical-tampering"),
        ("</Source><URI>", "</Source>lexical-tampering<URI>"),
    ],
)
def test_xml_rejects_non_whitespace_container_text_and_tails(
    original,
    tampered,
):
    schedule = _schedule()
    text = _xml_text(schedule).replace(original, tampered, 1)

    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(_xml_bytes(text), schedule)


def _swap_xml_blocks(text, first_open, first_close, second_open, second_close):
    first_start = text.index(first_open)
    first_end = text.index(first_close, first_start) + len(first_close)
    second_start = text.index(second_open, first_end)
    second_end = text.index(second_close, second_start) + len(second_close)
    return (
        text[:first_start]
        + text[second_start:second_end]
        + text[first_end:second_start]
        + text[first_start:first_end]
        + text[second_end:]
    )


@pytest.mark.parametrize(
    ("first_open", "first_close", "second_open", "second_close"),
    [
        (
            "<RegistrationInfo>",
            "</RegistrationInfo>",
            "<Principals>",
            "</Principals>",
        ),
        ("<Source>", "</Source>", "<URI>", "</URI>"),
        ("<UserId>", "</UserId>", "<LogonType>", "</LogonType>"),
        (
            "<StartBoundary>",
            "</StartBoundary>",
            "<ScheduleByDay>",
            "</ScheduleByDay>",
        ),
        (
            "<MultipleInstancesPolicy>",
            "</MultipleInstancesPolicy>",
            "<Enabled>",
            "</Enabled>",
        ),
        ("<Command>", "</Command>", "<Arguments>", "</Arguments>"),
    ],
)
def test_xml_rejects_unproven_child_order(
    first_open,
    first_close,
    second_open,
    second_close,
):
    schedule = _schedule()
    text = _swap_xml_blocks(
        _xml_text(schedule),
        first_open,
        first_close,
        second_open,
        second_close,
    )

    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(_xml_bytes(text), schedule)


@pytest.mark.parametrize(
    "tampered",
    [
        "<Source>TeamMem&#45;MemberKit</Source>",
        "<Source><![CDATA[TeamMem-MemberKit]]></Source>",
    ],
)
def test_xml_rejects_noncanonical_lexical_spelling_of_managed_text(tampered):
    schedule = _schedule()
    text = _xml_text(schedule).replace(
        "<Source>TeamMem-MemberKit</Source>",
        tampered,
        1,
    )

    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(_xml_bytes(text), schedule)


def test_xml_rejects_apos_named_entity_alias_in_executable_text():
    schedule = _schedule(
        executable=r"C:\Chris's Tools\memberkit.exe",
    )
    text = _xml_text(schedule)
    assert "Chris's Tools" in text
    assert windows.parse_task_xml(_xml_bytes(text), schedule) == "17:30"

    tampered = text.replace("Chris's Tools", "Chris&apos;s Tools", 1)
    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(_xml_bytes(tampered), schedule)


def test_xml_rejects_quot_named_entity_alias_in_scheduler_metadata():
    schedule = _schedule()
    text = _xml_text(schedule).replace(
        "<RegistrationInfo>",
        '<RegistrationInfo><Author>CI"runner</Author>',
        1,
    )
    assert windows.parse_task_xml(_xml_bytes(text), schedule) == "17:30"

    tampered = text.replace('CI"runner', "CI&quot;runner", 1)
    with pytest.raises(RuntimeError, match="not managed"):
        windows.parse_task_xml(_xml_bytes(tampered), schedule)


def test_xml_accepts_required_amp_named_entity_escape():
    schedule = _schedule(executable=r"C:\R&D\memberkit.exe")
    xml = windows.build_task_xml(schedule)

    assert "R&amp;D" in xml[2:].decode("utf-16-le")
    assert windows.parse_task_xml(xml, schedule) == "17:30"


def _scheduler_normalized_xml(schedule):
    text = _xml_text(schedule)
    text = text.replace(
        (
            "<Source>TeamMem-MemberKit</Source>"
            f"<URI>{schedule.task_name}</URI>"
            "<Description>TeamMem MemberKit daily draft reminder</Description>"
        ),
        (
            "<Source>TeamMem-MemberKit</Source>"
            "<Description>TeamMem MemberKit daily draft reminder</Description>"
            f"<URI>{schedule.task_name}</URI>"
        ),
        1,
    )
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


class FakeTaskRunner:
    """Byte-only model of the schtasks.exe surface MemberKit owns."""

    def __init__(self, tasks=None):
        self.tasks = dict(tasks or {})
        self.calls = []
        self.failures = {}
        self.hook = None
        self.list_bytes = None

    def __call__(self, command, **kwargs):
        assert kwargs == {"capture_output": True, "text": False}
        command = list(command)
        self.calls.append(command)
        operation = command[1]
        if operation in self.failures:
            return subprocess.CompletedProcess(
                command,
                self.failures[operation],
                b"secret stdout",
                b"secret stderr",
            )
        if operation == "/Query" and "/XML" in command:
            name = command[command.index("/TN") + 1]
            xml = self.tasks.get(name)
            result = subprocess.CompletedProcess(
                command,
                0 if xml is not None else 1,
                xml or b"",
                b"" if xml is not None else "不存在".encode(),
            )
        elif operation == "/Query":
            output = self.list_bytes
            if output is None:
                output = b"".join(
                    f'"{name}","Ready"\r\n'.encode() for name in self.tasks
                )
            result = subprocess.CompletedProcess(command, 0, output, b"")
        elif operation == "/Create":
            name = command[command.index("/TN") + 1]
            path = Path(command[command.index("/XML") + 1])
            if name in self.tasks and "/F" not in command:
                result = subprocess.CompletedProcess(
                    command,
                    1,
                    b"",
                    b"collision",
                )
            else:
                self.tasks[name] = path.read_bytes()
                result = subprocess.CompletedProcess(command, 0, b"", b"")
        elif operation == "/Delete":
            name = command[command.index("/TN") + 1]
            self.tasks.pop(name, None)
            result = subprocess.CompletedProcess(command, 0, b"", b"")
        else:
            raise AssertionError(command)
        return self.hook(command, result) if self.hook else result


class FakeIdentityApi:
    def current_process_sid(self):
        return SID


class RecordingLock:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        self.events.append("lock-enter")

    def __exit__(self, *_args):
        self.events.append("lock-exit")


def _lifecycle_args(tmp_path, monkeypatch, runner=None):
    events = []
    runner = runner or FakeTaskRunner()
    monkeypatch.setattr(
        windows,
        "provision_windows_private_dir",
        lambda path, sid, api: events.append(("state", Path(path))) or tmp_path,
    )
    return runner, events, FakeIdentityApi()


def test_status_absent_is_read_only_and_uses_exact_byte_queries(tmp_path):
    runner = FakeTaskRunner()
    state = tmp_path / "missing-state"

    status = windows.schedule_status(
        api=FakeIdentityApi(),
        runner=runner,
        state_dir=state,
        executable=r"C:\Program Files\TeamMem\memberkit.exe",
    )

    assert status == windows.ScheduleStatus(False, Path(task_name(SID)), None)
    assert not state.exists()
    assert runner.calls == [
        ["schtasks.exe", "/Query", "/TN", task_name(SID), "/XML"],
        ["schtasks.exe", "/Query", "/FO", "CSV", "/NH"],
    ]


def test_status_accepts_only_the_exact_managed_definition():
    schedule = _schedule()
    runner = FakeTaskRunner({schedule.task_name: windows.build_task_xml(schedule)})

    status = windows.schedule_status(
        api=FakeIdentityApi(),
        runner=runner,
        executable=schedule.executable,
    )

    assert status == windows.ScheduleStatus(
        True,
        Path(schedule.task_name),
        "17:30",
    )


@pytest.mark.parametrize(
    "xml",
    [
        b"malformed secret",
        _xml_bytes(_xml_text().replace("TeamMem-MemberKit", "Foreign", 1)),
        _xml_bytes(
            _xml_text().replace(
                "<Arguments>scheduled-run</Arguments>",
                "<Arguments>other-task</Arguments>",
                1,
            )
        ),
    ],
)
def test_status_reports_foreign_malformed_or_unexpected_action_as_conflict(xml):
    runner = FakeTaskRunner({task_name(SID): xml})

    with pytest.raises(RuntimeError, match="conflicts") as error:
        windows.schedule_status(
            api=FakeIdentityApi(),
            runner=runner,
            executable=r"C:\Program Files\TeamMem\memberkit.exe",
        )

    assert "secret" not in str(error.value)


def test_install_never_overwrites_a_conflicting_task(tmp_path, monkeypatch):
    expected = _schedule()
    foreign = _xml_bytes(
        _xml_text().replace(
            "<Arguments>scheduled-run</Arguments>",
            "<Arguments>foreign-action</Arguments>",
            1,
        )
    )
    runner, events, api = _lifecycle_args(
        tmp_path,
        monkeypatch,
        FakeTaskRunner({expected.task_name: foreign}),
    )

    with pytest.raises(RuntimeError, match="conflicts"):
        windows.install_schedule(
            17,
            30,
            expected.executable,
            api=api,
            runner=runner,
            state_dir=tmp_path,
            lock_factory=lambda _path: RecordingLock(events),
        )

    assert not any(call[1] in {"/Create", "/Delete"} for call in runner.calls)


def test_status_localized_query_failure_for_present_task_is_unavailable():
    schedule = _schedule()
    runner = FakeTaskRunner({schedule.task_name: windows.build_task_xml(schedule)})

    def localized_exact_query_failure(command, result):
        if command[1] == "/Query" and "/XML" in command:
            return subprocess.CompletedProcess(
                command,
                1,
                b"",
                "访问被拒绝".encode(),
            )
        return result

    runner.hook = localized_exact_query_failure

    with pytest.raises(RuntimeError, match="status is unavailable") as error:
        windows.schedule_status(
            api=FakeIdentityApi(),
            runner=runner,
            executable=schedule.executable,
        )

    assert "secret" not in str(error.value)


def test_status_treats_malformed_csv_fallback_as_unavailable():
    runner = FakeTaskRunner()
    runner.list_bytes = b'"unterminated secret'

    with pytest.raises(RuntimeError, match="status is unavailable") as error:
        windows.schedule_status(
            api=FakeIdentityApi(),
            runner=runner,
            executable=r"C:\Program Files\TeamMem\memberkit.exe",
        )

    assert "secret" not in str(error.value)


def test_status_exception_chain_never_retains_runner_details():
    def failing_runner(_command, **_kwargs):
        raise OSError("secret scheduler failure")

    with pytest.raises(RuntimeError, match="status is unavailable") as error:
        windows.schedule_status(
            api=FakeIdentityApi(),
            runner=failing_runner,
            executable=r"C:\Program Files\TeamMem\memberkit.exe",
        )

    seen = set()
    pending = [error.value]
    messages = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        messages.append(str(current))
        pending.extend(
            related
            for related in (current.__cause__, current.__context__)
            if related is not None
        )
    assert "secret" not in " ".join(messages)


def test_default_runner_preserves_byte_output_without_text_decoding():
    result = windows._run(
        None,
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'\\xff\\xfeok')",
        ],
    )

    assert result.returncode == 0
    assert result.stdout == b"\xff\xfeok"
    assert result.stderr == b""


def test_default_runner_uses_two_closed_temporary_file_spools(monkeypatch):
    spools = []
    real_factory = tempfile.TemporaryFile

    def recording_factory(*args, **kwargs):
        spool = real_factory(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(windows, "_SPOOL_FACTORY", recording_factory)

    result = windows._run(
        None,
        [sys.executable, "-c", "print('bounded')"],
    )

    assert result.stdout == b"bounded\n"
    assert result.stderr == b""
    assert len(spools) == 2
    assert all(spool.closed for spool in spools)


def test_default_runner_sanitizes_temporary_spool_cleanup_failure(monkeypatch):
    wrappers = []
    real_factory = tempfile.TemporaryFile

    class CloseFailure:
        def __init__(self, spool):
            self.spool = spool

        def __getattr__(self, name):
            return getattr(self.spool, name)

        def close(self):
            self.spool.close()
            raise OSError("secret cleanup detail")

        @property
        def closed(self):
            return self.spool.closed

    def failing_factory(*args, **kwargs):
        wrapper = CloseFailure(real_factory(*args, **kwargs))
        wrappers.append(wrapper)
        return wrapper

    monkeypatch.setattr(windows, "_SPOOL_FACTORY", failing_factory)

    with pytest.raises(RuntimeError, match="status is unavailable") as error:
        windows._run(None, [sys.executable, "-c", "print('bounded')"])

    assert len(wrappers) == 2
    assert all(wrapper.closed for wrapper in wrappers)
    messages = []
    current = error.value
    while current is not None:
        messages.append(str(current))
        current = current.__cause__ or current.__context__
    assert "secret" not in " ".join(messages)


def test_reaper_attempts_bounded_terminate_wait_kill_wait_after_native_errors():
    events = []

    class PersistentWaitFailure:
        def poll(self):
            events.append("poll")
            raise OSError("secret poll failure")

        def terminate(self):
            events.append("terminate")

        def wait(self, *, timeout):
            events.append(("wait", timeout))
            raise OSError("secret wait failure")

        def kill(self):
            events.append("kill")

    assert windows._stop_and_reap(PersistentWaitFailure()) is False
    assert events == [
        "poll",
        "terminate",
        ("wait", windows._PROCESS_STOP_GRACE_SECONDS),
        "kill",
        ("wait", windows._PROCESS_STOP_GRACE_SECONDS),
    ]


def test_reaper_failure_still_attempts_every_spool_close(monkeypatch):
    process_events = []
    spools = []

    class PersistentWaitFailure:
        returncode = None

        def poll(self):
            process_events.append("poll")
            raise OSError("secret poll failure")

        def terminate(self):
            process_events.append("terminate")

        def wait(self, *, timeout):
            process_events.append(("wait", timeout))
            raise OSError("secret wait failure")

        def kill(self):
            process_events.append("kill")

    class RecordingSpool:
        def __init__(self, fail_close):
            self.fail_close = fail_close
            self.close_attempts = 0

        def close(self):
            self.close_attempts += 1
            if self.fail_close:
                raise OSError("secret close failure")

        def fileno(self):
            return 1

    def spool_factory(*_args, **_kwargs):
        spool = RecordingSpool(fail_close=not spools)
        spools.append(spool)
        return spool

    monkeypatch.setattr(windows, "_SPOOL_FACTORY", spool_factory)
    monkeypatch.setattr(
        windows.subprocess,
        "Popen",
        lambda *_args, **_kwargs: PersistentWaitFailure(),
    )

    with pytest.raises(RuntimeError, match="status is unavailable") as error:
        windows._run(None, ["schtasks.exe", "/Query"])

    assert [spool.close_attempts for spool in spools] == [1, 1]
    assert process_events == [
        "poll",
        "poll",
        "terminate",
        ("wait", windows._PROCESS_STOP_GRACE_SECONDS),
        "kill",
        ("wait", windows._PROCESS_STOP_GRACE_SECONDS),
    ]
    messages = []
    current = error.value
    while current is not None:
        messages.append(str(current))
        current = current.__cause__ or current.__context__
    assert "secret" not in " ".join(messages)


def test_default_runner_does_not_wait_for_inherited_output_descendant():
    grandchild = (
        "import sys,time;"
        "time.sleep(3);"
        "sys.stdout.write('late');"
        "sys.stdout.flush()"
    )
    direct = (
        "import subprocess,sys;"
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}],"
        "stdout=sys.stdout,stderr=sys.stderr,close_fds=False)"
    )
    started = time.monotonic()

    result = windows._run(None, [sys.executable, "-c", direct])

    assert time.monotonic() - started < 1.5
    assert result.returncode == 0


@pytest.mark.parametrize("stream", ("stdout", "stderr"))
def test_default_runner_terminates_before_excess_output_can_continue(
    tmp_path,
    stream,
):
    marker = tmp_path / "continued"
    source = (
        "import pathlib,sys,time;"
        f"sys.{stream}.buffer.write(b'secret'+b'x'*(1024*1024));"
        f"sys.{stream}.flush();"
        "time.sleep(2);"
        f"pathlib.Path({str(marker)!r}).write_text('unsafe')"
    )
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="status is unavailable") as error:
        windows._run(None, [sys.executable, "-c", source])

    assert time.monotonic() - started < 1.5
    assert not marker.exists()
    messages = []
    current = error.value
    while current is not None:
        messages.append(str(current))
        current = current.__cause__ or current.__context__
    assert "secret" not in " ".join(messages)


def test_default_runner_times_out_and_reaps_the_child(
    tmp_path,
    monkeypatch,
):
    marker = tmp_path / "continued"
    source = (
        "import pathlib,time;"
        "time.sleep(2);"
        f"pathlib.Path({str(marker)!r}).write_text('unsafe')"
    )
    real_popen = subprocess.Popen
    processes = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(windows.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(windows, "_SCHEDULER_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(windows, "_PROCESS_STOP_GRACE_SECONDS", 0.1)
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="status is unavailable"):
        windows._run(None, [sys.executable, "-c", source])

    assert time.monotonic() - started < 1
    assert len(processes) == 1
    assert processes[0].returncode is not None
    assert not marker.exists()


@pytest.mark.parametrize("stream", ("xml", "csv", "stderr"))
def test_status_rejects_oversized_command_output_before_parsing(stream):
    schedule = _schedule()
    runner = FakeTaskRunner()
    oversized = b"secret-" + b"x" * (1024 * 1024)
    if stream == "xml":
        runner.tasks[schedule.task_name] = oversized
    elif stream == "csv":
        runner.list_bytes = oversized
    else:
        def oversized_stderr(command, result):
            return subprocess.CompletedProcess(
                command,
                result.returncode,
                result.stdout,
                oversized,
            )

        runner.hook = oversized_stderr

    with pytest.raises(RuntimeError, match="unavailable|conflicts") as error:
        windows.schedule_status(
            api=FakeIdentityApi(),
            runner=runner,
            executable=schedule.executable,
        )

    assert "secret" not in str(error.value)


def test_install_replaces_under_lock_and_removes_private_temp(
    tmp_path,
    monkeypatch,
):
    previous = _schedule()
    replacement = _schedule(time="07:05")
    runner, events, api = _lifecycle_args(
        tmp_path,
        monkeypatch,
        FakeTaskRunner({previous.task_name: windows.build_task_xml(previous)}),
    )

    path = windows.install_schedule(
        7,
        5,
        replacement.executable,
        api=api,
        runner=runner,
        state_dir=tmp_path,
        lock_factory=lambda _path: RecordingLock(events),
    )

    assert path == Path(replacement.task_name)
    assert events[0][0] == "state"
    assert events[1:] == ["lock-enter", "lock-exit"]
    assert windows.parse_task_xml(
        runner.tasks[replacement.task_name],
        replacement,
    ) == "07:05"
    create = next(call for call in runner.calls if call[1] == "/Create")
    assert create[:5] == [
        "schtasks.exe", "/Create", "/TN", replacement.task_name, "/XML",
    ]
    assert create[-1] == "/F"
    assert Path(create[5]).parent == tmp_path
    assert list(tmp_path.glob("*.xml")) == []


def test_first_install_success_uses_create_only_without_force(
    tmp_path,
    monkeypatch,
):
    schedule = _schedule()
    runner, events, api = _lifecycle_args(tmp_path, monkeypatch)

    path = windows.install_schedule(
        17,
        30,
        schedule.executable,
        api=api,
        runner=runner,
        state_dir=tmp_path,
        lock_factory=lambda _path: RecordingLock(events),
    )

    assert path == Path(schedule.task_name)
    assert windows.parse_task_xml(
        runner.tasks[schedule.task_name],
        schedule,
    ) == "17:30"
    create = next(call for call in runner.calls if call[1] == "/Create")
    assert create[:5] == [
        "schtasks.exe",
        "/Create",
        "/TN",
        schedule.task_name,
        "/XML",
    ]
    assert len(create) == 6
    assert Path(create[5]).parent == tmp_path


def test_first_install_uses_create_only_and_preserves_collision_winner(
    tmp_path,
    monkeypatch,
):
    schedule = _schedule()
    foreign = _xml_bytes(
        _xml_text().replace(
            "<Arguments>scheduled-run</Arguments>",
            "<Arguments>foreign-action</Arguments>",
            1,
        )
    )
    runner, events, api = _lifecycle_args(tmp_path, monkeypatch)
    absent_queries = 0

    def collide_after_absence(command, result):
        nonlocal absent_queries
        if command[1] == "/Query" and "/XML" not in command:
            absent_queries += 1
            if absent_queries == 1:
                runner.tasks[schedule.task_name] = foreign
        return result

    runner.hook = collide_after_absence
    with pytest.raises(RuntimeError, match="conflicting state preserved"):
        windows.install_schedule(
            17,
            30,
            schedule.executable,
            api=api,
            runner=runner,
            state_dir=tmp_path,
            lock_factory=lambda _path: RecordingLock(events),
        )

    assert runner.tasks[schedule.task_name] == foreign
    create = next(call for call in runner.calls if call[1] == "/Create")
    assert create[:5] == [
        "schtasks.exe",
        "/Create",
        "/TN",
        schedule.task_name,
        "/XML",
    ]
    assert len(create) == 6


def test_first_install_collision_preserves_matching_managed_winner(
    tmp_path,
    monkeypatch,
):
    schedule = _schedule()
    managed = windows.build_task_xml(schedule)
    runner, events, api = _lifecycle_args(tmp_path, monkeypatch)
    absent_queries = 0

    def collide_after_absence(command, result):
        nonlocal absent_queries
        if command[1] == "/Query" and "/XML" not in command:
            absent_queries += 1
            if absent_queries == 1:
                runner.tasks[schedule.task_name] = managed
        return result

    runner.hook = collide_after_absence
    with pytest.raises(RuntimeError, match="conflicting state preserved"):
        windows.install_schedule(
            17,
            30,
            schedule.executable,
            api=api,
            runner=runner,
            state_dir=tmp_path,
            lock_factory=lambda _path: RecordingLock(events),
        )

    assert runner.tasks[schedule.task_name] == managed
    assert not any(call[1] == "/Delete" for call in runner.calls)


def test_replacement_revalidates_exact_snapshot_before_force_create(
    tmp_path,
    monkeypatch,
):
    previous = _schedule()
    replacement = _schedule(time="07:05")
    prior_xml = windows.build_task_xml(previous)
    foreign = _xml_bytes(
        _xml_text().replace(
            "<Arguments>scheduled-run</Arguments>",
            "<Arguments>foreign-action</Arguments>",
            1,
        )
    )
    runner, events, api = _lifecycle_args(
        tmp_path,
        monkeypatch,
        FakeTaskRunner({previous.task_name: prior_xml}),
    )
    exact_queries = 0

    def replace_after_snapshot(command, result):
        nonlocal exact_queries
        if command[1] == "/Query" and "/XML" in command:
            exact_queries += 1
            if exact_queries == 1:
                runner.tasks[previous.task_name] = foreign
        return result

    runner.hook = replace_after_snapshot
    with pytest.raises(RuntimeError, match="conflicting state preserved"):
        windows.install_schedule(
            7,
            5,
            replacement.executable,
            api=api,
            runner=runner,
            state_dir=tmp_path,
            lock_factory=lambda _path: RecordingLock(events),
        )

    assert exact_queries >= 2
    assert runner.tasks[previous.task_name] == foreign
    assert not any(call[1] == "/Create" for call in runner.calls)


def test_remove_revalidates_exact_snapshot_before_delete(
    tmp_path,
    monkeypatch,
):
    schedule = _schedule()
    prior_xml = windows.build_task_xml(schedule)
    foreign = _xml_bytes(
        _xml_text().replace(
            "<Arguments>scheduled-run</Arguments>",
            "<Arguments>foreign-action</Arguments>",
            1,
        )
    )
    runner, events, api = _lifecycle_args(
        tmp_path,
        monkeypatch,
        FakeTaskRunner({schedule.task_name: prior_xml}),
    )
    exact_queries = 0

    def replace_after_snapshot(command, result):
        nonlocal exact_queries
        if command[1] == "/Query" and "/XML" in command:
            exact_queries += 1
            if exact_queries == 1:
                runner.tasks[schedule.task_name] = foreign
        return result

    runner.hook = replace_after_snapshot
    with pytest.raises(RuntimeError, match="conflicting state preserved"):
        windows.remove_schedule(
            api=api,
            runner=runner,
            state_dir=tmp_path,
            executable=schedule.executable,
            lock_factory=lambda _path: RecordingLock(events),
        )

    assert exact_queries >= 2
    assert runner.tasks[schedule.task_name] == foreign
    assert not any(call[1] == "/Delete" for call in runner.calls)


def test_private_state_validation_precedes_lock_or_temp_writes(
    tmp_path,
    monkeypatch,
):
    calls = []

    def reject_state(path, sid, api):
        calls.append(("state", Path(path)))
        raise ValueError("unsafe state")

    monkeypatch.setattr(windows, "provision_windows_private_dir", reject_state)

    with pytest.raises(ValueError, match="unsafe state"):
        windows.install_schedule(
            17,
            30,
            r"C:\Program Files\TeamMem\memberkit.exe",
            api=FakeIdentityApi(),
            runner=FakeTaskRunner(),
            state_dir=tmp_path,
            lock_factory=lambda _path: pytest.fail("lock must not be created"),
        )

    assert calls == [("state", tmp_path)]
    assert list(tmp_path.iterdir()) == []


def test_default_state_provisions_private_parent_before_memberkit_directory(
    monkeypatch,
):
    calls = []
    local = Path(r"C:\Users\Alex\AppData\Local")
    parent = local / "TeamMemory"
    memberkit = parent / "MemberKit"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(
        windows,
        "provision_windows_private_dir",
        lambda path, sid, api: calls.append(Path(path)) or Path(path),
    )

    with pytest.raises(RuntimeError, match="previous state restored"):
        windows.install_schedule(
            17,
            30,
            r"C:\Program Files\TeamMem\memberkit.exe",
            api=FakeIdentityApi(),
            runner=FakeTaskRunner(),
            lock_factory=lambda _path: RecordingLock([]),
        )

    assert calls == [parent, memberkit]


def test_install_rolls_back_exact_prior_xml_after_verification_failure(
    tmp_path,
    monkeypatch,
):
    previous = _schedule()
    replacement = _schedule(time="07:05")
    prior_xml = windows.build_task_xml(previous)
    runner, events, api = _lifecycle_args(
        tmp_path,
        monkeypatch,
        FakeTaskRunner({previous.task_name: prior_xml}),
    )
    creates = 0
    corrupted = False

    def corrupt_first_verification(command, result):
        nonlocal creates, corrupted
        if command[1] == "/Create":
            creates += 1
        if (
            command[1] == "/Query"
            and "/XML" in command
            and creates == 1
            and not corrupted
        ):
            corrupted = True
            return subprocess.CompletedProcess(command, 0, b"secret bad xml", b"")
        return result

    runner.hook = corrupt_first_verification
    with pytest.raises(RuntimeError, match="previous state restored") as error:
        windows.install_schedule(
            7,
            5,
            replacement.executable,
            api=api,
            runner=runner,
            state_dir=tmp_path,
            lock_factory=lambda _path: RecordingLock(events),
        )

    assert runner.tasks[previous.task_name] == prior_xml
    assert "secret" not in str(error.value)


def test_first_install_rollback_never_deletes_concurrently_appearing_foreign_task(
    tmp_path,
    monkeypatch,
):
    schedule = _schedule()
    foreign = _xml_bytes(
        _xml_text().replace(
            "<Arguments>scheduled-run</Arguments>",
            "<Arguments>foreign-action</Arguments>",
            1,
        )
    )
    runner, events, api = _lifecycle_args(tmp_path, monkeypatch)
    created = False

    def replace_candidate_before_verification(command, result):
        nonlocal created
        if command[1] == "/Create":
            created = True
        if command[1] == "/Query" and "/XML" in command and created:
            runner.tasks[schedule.task_name] = foreign
            return subprocess.CompletedProcess(command, 0, foreign, b"")
        return result

    runner.hook = replace_candidate_before_verification
    with pytest.raises(RuntimeError, match="conflicting state preserved"):
        windows.install_schedule(
            17,
            30,
            schedule.executable,
            api=api,
            runner=runner,
            state_dir=tmp_path,
            lock_factory=lambda _path: RecordingLock(events),
        )

    assert runner.tasks[schedule.task_name] == foreign
    assert not any(call[1] == "/Delete" for call in runner.calls)


def test_prior_snapshot_rollback_never_overwrites_concurrent_foreign_task(
    tmp_path,
    monkeypatch,
):
    previous = _schedule()
    replacement = _schedule(time="07:05")
    foreign = _xml_bytes(
        _xml_text().replace(
            "<Arguments>scheduled-run</Arguments>",
            "<Arguments>foreign-action</Arguments>",
            1,
        )
    )
    runner, events, api = _lifecycle_args(
        tmp_path,
        monkeypatch,
        FakeTaskRunner(
            {previous.task_name: windows.build_task_xml(previous)}
        ),
    )
    creates = 0

    def replace_candidate_before_verification(command, result):
        nonlocal creates
        if command[1] == "/Create":
            creates += 1
        if command[1] == "/Query" and "/XML" in command and creates == 1:
            runner.tasks[replacement.task_name] = foreign
            return subprocess.CompletedProcess(command, 0, foreign, b"")
        return result

    runner.hook = replace_candidate_before_verification
    with pytest.raises(RuntimeError, match="conflicting state preserved"):
        windows.install_schedule(
            7,
            5,
            replacement.executable,
            api=api,
            runner=runner,
            state_dir=tmp_path,
            lock_factory=lambda _path: RecordingLock(events),
        )

    assert runner.tasks[replacement.task_name] == foreign
    assert creates == 1


def test_first_install_failure_rolls_back_to_confirmed_absence(
    tmp_path,
    monkeypatch,
):
    runner, events, api = _lifecycle_args(tmp_path, monkeypatch)
    monkeypatch.setattr(
        windows,
        "_write_xml",
        lambda *_args: (_ for _ in ()).throw(OSError("secret disk failure")),
    )

    with pytest.raises(RuntimeError, match="previous state restored") as error:
        windows.install_schedule(
            17,
            30,
            r"C:\Program Files\TeamMem\memberkit.exe",
            api=api,
            runner=runner,
            state_dir=tmp_path,
            lock_factory=lambda _path: RecordingLock(events),
        )

    assert runner.tasks == {}
    messages = []
    current = error.value
    while current is not None:
        messages.append(str(current))
        current = current.__cause__ or current.__context__
    assert "secret" not in " ".join(messages)


def test_partial_temp_write_failure_removes_candidate_before_reporting(
    tmp_path,
    monkeypatch,
):
    runner, events, api = _lifecycle_args(tmp_path, monkeypatch)
    monkeypatch.setattr(
        windows.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError("secret flush")),
    )

    with pytest.raises(RuntimeError, match="previous state restored") as error:
        windows.install_schedule(
            17,
            30,
            r"C:\Program Files\TeamMem\memberkit.exe",
            api=api,
            runner=runner,
            state_dir=tmp_path,
            lock_factory=lambda _path: RecordingLock(events),
        )

    assert list(tmp_path.glob("*.xml")) == []
    assert "secret" not in str(error.value)


def test_remove_partial_write_with_persistent_cleanup_reports_rollback_failed(
    tmp_path,
    monkeypatch,
):
    schedule = _schedule()
    runner, events, api = _lifecycle_args(
        tmp_path,
        monkeypatch,
        FakeTaskRunner(
            {schedule.task_name: windows.build_task_xml(schedule)}
        ),
    )
    monkeypatch.setattr(
        windows.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError("secret flush")),
    )
    monkeypatch.setattr(
        windows,
        "_remove_temp",
        lambda _path: (_ for _ in ()).throw(OSError("secret cleanup")),
    )

    with pytest.raises(RuntimeError, match="rollback failed") as error:
        windows.remove_schedule(
            api=api,
            runner=runner,
            state_dir=tmp_path,
            executable=schedule.executable,
            lock_factory=lambda _path: RecordingLock(events),
        )

    assert runner.tasks[schedule.task_name] == windows.build_task_xml(schedule)
    assert "secret" not in str(error.value)


def test_remove_is_idempotent_and_restores_after_failed_absence_verification(
    tmp_path,
    monkeypatch,
):
    schedule = _schedule()
    prior_xml = windows.build_task_xml(schedule)
    runner, events, api = _lifecycle_args(
        tmp_path,
        monkeypatch,
        FakeTaskRunner({schedule.task_name: prior_xml}),
    )
    deleted = False

    def fail_absence_verification(command, result):
        nonlocal deleted
        if command[1] == "/Delete":
            deleted = True
        if (
            deleted
            and command[1] == "/Query"
            and "/XML" in command
            and schedule.task_name not in runner.tasks
        ):
            runner.tasks[schedule.task_name] = prior_xml
            return subprocess.CompletedProcess(command, 0, prior_xml, b"")
        return result

    runner.hook = fail_absence_verification
    with pytest.raises(RuntimeError, match="previous state restored"):
        windows.remove_schedule(
            api=api,
            runner=runner,
            state_dir=tmp_path,
            executable=schedule.executable,
            lock_factory=lambda _path: RecordingLock(events),
        )
    assert runner.tasks[schedule.task_name] == prior_xml

    empty = FakeTaskRunner()
    assert windows.remove_schedule(
        api=api,
        runner=empty,
        state_dir=tmp_path,
        executable=schedule.executable,
        lock_factory=lambda _path: RecordingLock([]),
    ) is False


def test_remove_deletes_exact_managed_task_and_cleans_snapshot(
    tmp_path,
    monkeypatch,
):
    schedule = _schedule()
    runner, events, api = _lifecycle_args(
        tmp_path,
        monkeypatch,
        FakeTaskRunner(
            {schedule.task_name: windows.build_task_xml(schedule)}
        ),
    )

    removed = windows.remove_schedule(
        api=api,
        runner=runner,
        state_dir=tmp_path,
        executable=schedule.executable,
        lock_factory=lambda _path: RecordingLock(events),
    )

    assert removed is True
    assert runner.tasks == {}
    assert [
        "schtasks.exe",
        "/Delete",
        "/TN",
        schedule.task_name,
        "/F",
    ] in runner.calls
    assert list(tmp_path.glob("*.xml")) == []


def test_cleanup_retry_keeps_successful_install(
    tmp_path,
    monkeypatch,
):
    previous = _schedule()
    replacement = _schedule(time="07:05")
    prior_xml = windows.build_task_xml(previous)
    runner, events, api = _lifecycle_args(
        tmp_path,
        monkeypatch,
        FakeTaskRunner({previous.task_name: prior_xml}),
    )
    calls = 0

    def fail_once(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("secret cleanup failure")
        path.unlink()

    monkeypatch.setattr(windows, "_remove_temp", fail_once, raising=False)
    path = windows.install_schedule(
        7,
        5,
        replacement.executable,
        api=api,
        runner=runner,
        state_dir=tmp_path,
        lock_factory=lambda _path: RecordingLock(events),
    )

    assert path == Path(replacement.task_name)
    assert calls == 2
    assert windows.parse_task_xml(
        runner.tasks[replacement.task_name],
        replacement,
    ) == "07:05"
    assert runner.tasks[previous.task_name] != prior_xml


@pytest.mark.parametrize("operation", ("install", "remove"))
def test_persistent_cleanup_failure_reports_rollback_failed(
    tmp_path,
    monkeypatch,
    operation,
):
    schedule = _schedule()
    prior_xml = windows.build_task_xml(schedule)
    runner, events, api = _lifecycle_args(
        tmp_path,
        monkeypatch,
        FakeTaskRunner({schedule.task_name: prior_xml}),
    )
    monkeypatch.setattr(
        windows,
        "_remove_temp",
        lambda _path: (_ for _ in ()).throw(OSError("secret cleanup")),
        raising=False,
    )

    if operation == "install":
        call = lambda: windows.install_schedule(
            17,
            30,
            schedule.executable,
            api=api,
            runner=runner,
            state_dir=tmp_path,
            lock_factory=lambda _path: RecordingLock(events),
        )
    else:
        call = lambda: windows.remove_schedule(
            api=api,
            runner=runner,
            state_dir=tmp_path,
            executable=schedule.executable,
            lock_factory=lambda _path: RecordingLock(events),
        )

    with pytest.raises(RuntimeError, match="rollback failed") as error:
        call()
    assert "secret" not in str(error.value)


def test_one_byte_lock_retries_only_lock_contention():
    calls = []
    sleeps = []

    def locking(descriptor, mode, length):
        calls.append((descriptor, mode, length))
        if len(calls) < 3:
            raise OSError(13, "contended")

    windows._lock_byte(locking, 42, sleeps.append)

    assert calls == [(42, 2, 1), (42, 2, 1), (42, 2, 1)]
    assert sleeps == [0.05, 0.05]
