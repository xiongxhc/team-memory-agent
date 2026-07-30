import ctypes
import ntpath
import traceback
from pathlib import Path

import pytest

from memberkit import windows_security
from memberkit.windows_security import (
    NativeWindowsApi,
    atomic_write_windows_private_text,
    current_user_sid,
    current_username,
    provision_windows_private_dir,
    read_windows_private_text,
    validate_windows_private_dir,
    validate_windows_private_file,
)


SID = "S-1-5-21-111-222-333-1001"
USERNAME = "Alex"
SESSION_ID = 42
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
FOREIGN_SID = "S-1-5-21-999-888-777-1001"
SECRET_TEXT = (
    "MEMBERKIT_INBOX_"
    "URL=https://token@example.invalid/inbox\n"
)


def _file(
    *,
    data=b"member=alex\n",
    owner_sid=SID,
    file_type="disk",
    reparse_point=False,
    dacl_protected=True,
    allow_aces=(),
):
    return {
        "data": data,
        "file_type": file_type,
        "owner_sid": owner_sid,
        "reparse_point": reparse_point,
        "dacl_protected": dacl_protected,
        "regular": True,
        "directory": False,
        "allow_aces": list(allow_aces),
        "path": None,
    }


def _directory(
    *,
    owner_sid=SID,
    file_type="disk",
    reparse_point=False,
    dacl_protected=True,
    allow_aces=(),
):
    record = _file(
        data=b"",
        owner_sid=owner_sid,
        file_type=file_type,
        reparse_point=reparse_point,
        dacl_protected=dacl_protected,
        allow_aces=allow_aces,
    )
    record["regular"] = False
    record["directory"] = True
    return record


class FakeWindowsApi:
    def __init__(self, records=None, *, fail_phase=None):
        self.records = {}
        for path, record in (records or {}).items():
            record["path"] = Path(path)
            self.records[Path(path)] = record
        self.fail_phase = fail_phase
        self.phases = []
        self.opened = []
        self.closed = []
        self.security_calls = []
        self.destination = None
        self.parent = None
        self._cleanup_recorded = False
        self._failed_phases = set()
        self.open_handles = set()
        self.created_owner_sids = []

    def _phase(self, phase):
        self.phases.append(phase)
        if self.fail_phase == phase and phase not in self._failed_phases:
            self._failed_phases.add(phase)
            raise OSError(f"{phase} failed while processing {SECRET_TEXT}")

    def current_process_sid(self):
        return SID

    def current_username(self):
        return USERNAME

    def current_session_id(self):
        return SESSION_ID

    def open_file(self, path, *, directory=False):
        path = Path(path)
        if path == self.parent and directory:
            self._phase("provision private parent")
        elif path == self.destination and not directory:
            self._phase("open and validate destination")
        try:
            handle = self.records[path]
        except KeyError:
            raise FileNotFoundError(path) from None
        self.opened.append((path, directory, handle))
        self.open_handles.add(id(handle))
        return handle

    def describe_handle(self, handle):
        if (
            handle["path"] != self.destination
            and not handle["directory"]
            and handle["path"] is not None
        ):
            self._phase("validate candidate handle")
        return {
            key: handle[key]
            for key in (
                "file_type",
                "owner_sid",
                "reparse_point",
                "dacl_protected",
                "regular",
                "directory",
                "allow_aces",
            )
        }

    def read_utf8(self, handle):
        return handle["data"].decode("utf-8")

    def close_handle(self, handle):
        if (
            handle["path"] != self.destination
            and not handle["directory"]
            and handle["path"] is not None
            and handle not in self.closed
        ):
            self._phase("close candidate handle")
        self.open_handles.discard(id(handle))
        self.closed.append(handle)

    def create_directory(self, path):
        path = Path(path)
        self._phase("provision private parent")
        if path in self.records:
            raise FileExistsError(path)
        record = _directory(
            owner_sid=FOREIGN_SID,
            dacl_protected=False,
        )
        record["path"] = path
        self.created_owner_sids.append(record["owner_sid"])
        self.records[path] = record
        self.open_handles.add(id(record))
        return record

    def create_empty_file(self, path):
        self._phase("create empty candidate")
        path = Path(path)
        if path in self.records:
            raise FileExistsError(path)
        record = _file(
            data=b"",
            owner_sid=FOREIGN_SID,
            dacl_protected=False,
        )
        record["path"] = path
        self.created_owner_sids.append(record["owner_sid"])
        self.records[path] = record
        self.open_handles.add(id(record))
        return record

    def apply_private_security(self, handle, sid, principals):
        phase = (
            "apply private security"
            if not handle["directory"]
            else "protect private parent"
        )
        self._phase(phase)
        principals = tuple(principals)
        self.security_calls.append((handle["path"], sid, principals))
        handle["owner_sid"] = sid
        handle["dacl_protected"] = True
        handle["allow_aces"] = [(principal, 0x10000000) for principal in principals]

    def write_utf8(self, handle, data):
        self._phase("write UTF-8 through candidate handle")
        handle["data"] = bytes(data)

    def flush_handle(self, handle):
        self._phase("flush candidate handle")

    def path_exists(self, path):
        return Path(path) in self.records

    def replace_file(self, destination, candidate, backup):
        self._phase("atomically replace destination")
        destination = Path(destination)
        candidate = Path(candidate)
        backup = Path(backup)
        self.records[backup] = self.records.pop(destination)
        self.records[backup]["path"] = backup
        self.records[destination] = self.records.pop(candidate)
        self.records[destination]["path"] = destination

    def move_file(self, candidate, destination):
        self._phase("atomically replace destination")
        candidate = Path(candidate)
        destination = Path(destination)
        if destination in self.records:
            raise FileExistsError(destination)
        self.records[destination] = self.records.pop(candidate)
        self.records[destination]["path"] = destination

    def restore_backup(self, destination, backup):
        destination = Path(destination)
        backup = Path(backup)
        if self.fail_phase == "rollback":
            raise OSError(f"rollback failed while processing {SECRET_TEXT}")
        self.records.pop(destination, None)
        self.records[destination] = self.records.pop(backup)
        self.records[destination]["path"] = destination

    def delete_file(self, path):
        path = Path(path)
        if not self._cleanup_recorded:
            self._cleanup_recorded = True
            self._phase("remove backup and candidate")
        record = self.records.get(path)
        if record is not None and id(record) in self.open_handles:
            raise PermissionError(
                f"cannot delete open candidate while processing {SECRET_TEXT}"
            )
        self.records.pop(path, None)


def _windows_parent(path):
    return Path(ntpath.dirname(str(path)))


def _private_records(path, *, existing=True):
    parent = _windows_parent(path)
    records = {parent: _directory()}
    if existing:
        records[path] = _file(data=b"old private bytes\n")
    return parent, records


def test_current_windows_identity_uses_injected_native_api():
    api = FakeWindowsApi()

    assert current_user_sid(api) == SID
    assert current_username(api) == USERNAME
    assert windows_security.current_session_id(api) == SESSION_ID


@pytest.mark.parametrize(
    "value",
    [-1, 2**32 - 1, 2**32, True, "42", "@sessions", None],
)
def test_current_windows_session_rejects_unsafe_or_reserved_values(value):
    class InvalidSessionApi:
        @staticmethod
        def current_session_id():
            return value

    with pytest.raises(ValueError, match="Windows session ID"):
        windows_security.current_session_id(InvalidSessionApi())


def test_native_bindings_use_pointer_width_handles():
    class Procedure:
        pass

    class Library:
        def __getattr__(self, name):
            procedure = Procedure()
            setattr(self, name, procedure)
            return procedure

    kernel32 = Library()
    advapi32 = Library()
    NativeWindowsApi._configure_api(ctypes, kernel32, advapi32)

    assert kernel32.CreateFileW.restype is ctypes.c_void_p
    assert kernel32.GetCurrentProcessId.restype is ctypes.c_ulong
    assert kernel32.ProcessIdToSessionId.argtypes == [
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    assert kernel32.CloseHandle.argtypes == [ctypes.c_void_p]
    assert kernel32.FlushFileBuffers.argtypes == [ctypes.c_void_p]
    assert advapi32.OpenProcessToken.argtypes[0] is ctypes.c_void_p
    assert advapi32.GetSecurityInfo.argtypes[0] is ctypes.c_void_p
    assert advapi32.GetSecurityDescriptorOwner.argtypes == [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int),
    ]
    assert advapi32.SetSecurityInfo.argtypes[0] is ctypes.c_void_p


def test_native_current_session_uses_the_current_process_id(monkeypatch):
    calls = []

    class Kernel32:
        @staticmethod
        def GetCurrentProcessId():
            calls.append(("process",))
            return 987

        @staticmethod
        def ProcessIdToSessionId(process_id, session_id):
            calls.append(("session", process_id))
            session_id._obj.value = SESSION_ID
            return 1

    monkeypatch.setattr(
        NativeWindowsApi,
        "_libraries",
        staticmethod(lambda: (ctypes, Kernel32(), object())),
    )

    api = object.__new__(NativeWindowsApi)

    assert api.current_session_id() == SESSION_ID
    assert calls == [("process",), ("session", 987)]


def test_native_created_handles_request_write_dac_and_owner_before_protection(
    monkeypatch,
):
    calls = []

    class Kernel32:
        @staticmethod
        def CreateDirectoryW(path, security):
            return 1

        @staticmethod
        def CreateFileW(path, access, share, security, creation, flags, template):
            calls.append((path, access, share, creation, flags))
            return 42

        @staticmethod
        def RemoveDirectoryW(path):
            return 1

    monkeypatch.setattr(
        NativeWindowsApi,
        "_libraries",
        staticmethod(lambda: (ctypes, Kernel32(), object())),
    )
    api = object.__new__(NativeWindowsApi)

    api.create_directory(Path(r"C:\TeamMemory"))
    api.create_empty_file(Path(r"C:\TeamMemory\.memberkit.env.tmp"))

    assert len(calls) == 2
    assert calls[0][1] == 0x000E0080
    assert calls[1][1] == 0xC00E0000
    for _path, access, _share, _creation, flags in calls:
        assert access & 0x00040000  # WRITE_DAC
        assert access & 0x00080000  # WRITE_OWNER
        assert flags & 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT


def test_native_open_file_requests_write_owner_independently(monkeypatch):
    calls = []

    class Kernel32:
        @staticmethod
        def CreateFileW(path, access, share, security, creation, flags, template):
            calls.append(access)
            return 42

    monkeypatch.setattr(
        NativeWindowsApi,
        "_libraries",
        staticmethod(lambda: (ctypes, Kernel32(), object())),
    )
    api = object.__new__(NativeWindowsApi)

    api.open_file(Path(r"C:\TeamMemory"), directory=True, write_owner=True)

    assert calls[0] & 0x00080000  # WRITE_OWNER
    assert not calls[0] & 0x00040000  # WRITE_DAC


def test_native_private_security_sets_owner_and_dacl_together(monkeypatch):
    calls = []

    class Kernel32:
        @staticmethod
        def LocalFree(descriptor):
            calls.append(("free", descriptor.value))

    class Advapi32:
        @staticmethod
        def ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, revision, descriptor, size
        ):
            calls.append(("convert", sddl, revision))
            descriptor._obj.value = 0x1000
            return 1

        @staticmethod
        def GetSecurityDescriptorOwner(descriptor, owner, defaulted):
            calls.append(("owner", descriptor.value))
            owner._obj.value = 0x2000
            return 1

        @staticmethod
        def GetSecurityDescriptorDacl(descriptor, present, dacl, defaulted):
            calls.append(("dacl", descriptor.value))
            present._obj.value = 1
            dacl._obj.value = 0x3000
            return 1

        @staticmethod
        def SetSecurityInfo(handle, kind, flags, owner, group, dacl, sacl):
            calls.append(
                (
                    "set",
                    handle,
                    kind,
                    flags,
                    owner.value,
                    group,
                    dacl.value,
                    sacl,
                )
            )
            return 0

    monkeypatch.setattr(
        NativeWindowsApi,
        "_libraries",
        staticmethod(lambda: (ctypes, Kernel32(), Advapi32())),
    )
    api = object.__new__(NativeWindowsApi)

    api.apply_private_security(
        42,
        SID,
        (SID, SYSTEM_SID, ADMINISTRATORS_SID),
    )

    assert calls == [
        (
            "convert",
            (
                f"O:{SID}D:P"
                f"(A;;FA;;;{SID})"
                f"(A;;FA;;;{SYSTEM_SID})"
                f"(A;;FA;;;{ADMINISTRATORS_SID})"
            ),
            1,
        ),
        ("owner", 0x1000),
        ("dacl", 0x1000),
        ("set", 42, 1, 0x80000005, 0x2000, None, 0x3000, None),
        ("free", 0x1000),
    ]


@pytest.mark.parametrize("failure_step", ["owner", "dacl", "set"])
def test_native_private_security_frees_descriptor_on_failure(
    monkeypatch,
    failure_step,
):
    calls = []

    class Kernel32:
        @staticmethod
        def LocalFree(descriptor):
            calls.append(("free", descriptor.value))

    class Advapi32:
        @staticmethod
        def ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, revision, descriptor, size
        ):
            descriptor._obj.value = 0x1000
            return 1

        @staticmethod
        def GetSecurityDescriptorOwner(descriptor, owner, defaulted):
            if failure_step == "owner":
                return 0
            owner._obj.value = 0x2000
            return 1

        @staticmethod
        def GetSecurityDescriptorDacl(descriptor, present, dacl, defaulted):
            if failure_step == "dacl":
                return 0
            present._obj.value = 1
            dacl._obj.value = 0x3000
            return 1

        @staticmethod
        def SetSecurityInfo(handle, kind, flags, owner, group, dacl, sacl):
            return 5 if failure_step == "set" else 0

    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(
        NativeWindowsApi,
        "_libraries",
        staticmethod(lambda: (ctypes, Kernel32(), Advapi32())),
    )
    api = object.__new__(NativeWindowsApi)

    with pytest.raises(OSError) as error:
        api.apply_private_security(
            42,
            SID,
            (SID, SYSTEM_SID, ADMINISTRATORS_SID),
        )

    assert calls == [("free", 0x1000)]
    assert SID not in str(error.value)


@pytest.mark.parametrize(
    "value",
    [
        r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env",
        r"Z:\memberkit.env",
        r"\\server\share\TeamMemory\memberkit.env",
        r"\\server\share",
    ],
)
def test_windows_private_files_accept_drive_absolute_and_complete_unc_paths(value):
    path = Path(value)
    api = FakeWindowsApi({path: _file()})

    assert validate_windows_private_file(path, SID, api) == path


@pytest.mark.parametrize(
    "value",
    [
        r"memberkit.env",
        r"C:memberkit.env",
        r"\TeamMemory\memberkit.env",
        r"\\server",
        "\\\\server\\",
        r"\\.\pipe\memberkit",
        r"\\?\C:\TeamMemory\memberkit.env",
        "//?/C:/TeamMemory/memberkit.env",
        "C:\\TeamMemory\\bad\0name",
    ],
)
def test_windows_private_paths_reject_relative_incomplete_unc_and_device_names(value):
    path = Path(value)
    api = FakeWindowsApi()

    with pytest.raises(ValueError, match="absolute Windows filesystem path"):
        validate_windows_private_file(path, SID, api)

    assert api.opened == []


@pytest.mark.parametrize(
    "changes",
    [
        {"file_type": "pipe"},
        {"reparse_point": True},
        {"owner_sid": "S-1-5-21-foreign"},
        {"dacl_protected": False},
        {"regular": False, "directory": True},
    ],
)
def test_windows_private_file_rejects_unsafe_handle_records(changes):
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    record = _file()
    record.update(changes)

    with pytest.raises(ValueError):
        validate_windows_private_file(path, SID, FakeWindowsApi({path: record}))


@pytest.mark.parametrize(
    "restricted_sid",
    ["S-1-1-0", "S-1-5-11", "S-1-5-32-545"],
)
@pytest.mark.parametrize(
    "read_mask",
    [
        0x00000001,  # FILE_READ_DATA
        0x00000008,  # FILE_READ_EA
        0x00000080,  # FILE_READ_ATTRIBUTES
        0x00020000,  # READ_CONTROL
        0x80000000,  # GENERIC_READ
        0x10000000,  # GENERIC_ALL
        0x80000002,  # GENERIC_READ combined with FILE_WRITE_DATA
    ],
)
def test_windows_private_file_rejects_every_restricted_read_grant(
    restricted_sid, read_mask,
):
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    record = _file(allow_aces=[(restricted_sid, read_mask)])

    with pytest.raises(ValueError, match="shared principal"):
        validate_windows_private_file(path, SID, FakeWindowsApi({path: record}))


def test_windows_private_file_allows_non_read_grants_and_privileged_principals():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    record = _file(
        allow_aces=[
            ("S-1-1-0", 0x00000002),
            (SID, 0x80000000),
            (SYSTEM_SID, 0x10000000),
            (ADMINISTRATORS_SID, 0x10000000),
        ]
    )

    assert validate_windows_private_file(path, SID, FakeWindowsApi({path: record})) == path


def test_windows_private_file_rejects_read_grant_to_unapproved_sid():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    record = _file(
        allow_aces=[
            (SID, 0x10000000),
            ("S-1-5-21-999-888-777-1002", 0x00000081),
        ]
    )

    with pytest.raises(ValueError, match="unapproved principal"):
        validate_windows_private_file(path, SID, FakeWindowsApi({path: record}))


def test_windows_private_directory_requires_directory_handle():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory")

    with pytest.raises(ValueError, match="directory"):
        validate_windows_private_dir(path, SID, FakeWindowsApi({path: _file()}))


def test_windows_private_text_validates_and_reads_same_open_handle():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    original = _file(data=b"member=original\n")
    replacement = _file(data=b"member=replacement\n")
    api = FakeWindowsApi({path: original})

    def swapping_read(handle):
        replacement["path"] = path
        api.records[path] = replacement
        return handle["data"].decode("utf-8")

    api.read_utf8 = swapping_read

    assert read_windows_private_text(path, SID, api) == "member=original\n"
    assert api.closed == [original]


def test_windows_private_text_decode_failure_is_sanitized_and_closes_handle():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    record = _file(data=b"\xff")
    api = FakeWindowsApi({path: record})

    with pytest.raises(ValueError, match="UTF-8") as error:
        read_windows_private_text(path, SID, api)

    assert SECRET_TEXT not in str(error.value)
    assert api.closed == [record]


def test_provision_windows_private_directory_creates_protects_and_validates():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory")
    api = FakeWindowsApi()

    assert provision_windows_private_dir(path, SID, api) == path
    assert api.security_calls == [
        (path, SID, (SID, SYSTEM_SID, ADMINISTRATORS_SID))
    ]
    assert api.created_owner_sids == [FOREIGN_SID]
    assert api.records[path]["owner_sid"] == SID
    assert validate_windows_private_dir(path, SID, api) == path


def test_provision_windows_private_directory_rejects_existing_unsafe_directory():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory")
    unsafe = _directory(owner_sid=FOREIGN_SID, dacl_protected=False)
    api = FakeWindowsApi({path: unsafe})

    with pytest.raises(ValueError):
        provision_windows_private_dir(path, SID, api)

    assert api.security_calls == []
    assert unsafe["owner_sid"] == FOREIGN_SID


def test_atomic_private_write_preserves_exact_security_order_and_cleans_artifacts():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    parent, records = _private_records(path)
    api = FakeWindowsApi(records)
    api.parent = parent
    api.destination = path

    assert atomic_write_windows_private_text(path, SECRET_TEXT, SID, api) == path

    assert api.records[path]["data"] == SECRET_TEXT.encode("utf-8")
    assert api.phases == [
        "provision private parent",
        "open and validate destination",
        "create empty candidate",
        "apply private security",
        "validate candidate handle",
        "write UTF-8 through candidate handle",
        "flush candidate handle",
        "close candidate handle",
        "atomically replace destination",
        "open and validate destination",
        "remove backup and candidate",
    ]
    assert api.security_calls[-1][2] == (
        SID,
        SYSTEM_SID,
        ADMINISTRATORS_SID,
    )
    assert api.created_owner_sids == [FOREIGN_SID]
    assert api.records[path]["owner_sid"] == SID
    assert set(api.records) == {parent, path}


def test_atomic_private_first_write_uses_move_and_leaves_no_artifacts():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    parent, records = _private_records(path, existing=False)
    api = FakeWindowsApi(records)
    api.parent = parent
    api.destination = path

    assert atomic_write_windows_private_text(path, SECRET_TEXT, SID, api) == path

    assert api.records[path]["data"] == SECRET_TEXT.encode("utf-8")
    assert set(api.records) == {parent, path}


def test_atomic_private_write_rejects_existing_foreign_owner_without_mutation():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    parent, records = _private_records(path)
    records[path]["owner_sid"] = FOREIGN_SID
    original = records[path]
    api = FakeWindowsApi(records)
    api.parent = parent
    api.destination = path

    with pytest.raises(RuntimeError, match="validate existing destination"):
        atomic_write_windows_private_text(path, SECRET_TEXT, SID, api)

    assert api.records[path] is original
    assert api.records[path]["owner_sid"] == FOREIGN_SID
    assert api.records[path]["data"] == b"old private bytes\n"
    assert api.created_owner_sids == []
    assert api.security_calls == []


@pytest.mark.parametrize(
    "phase",
    [
        "provision private parent",
        "create empty candidate",
        "apply private security",
        "validate candidate handle",
        "write UTF-8 through candidate handle",
        "flush candidate handle",
        "close candidate handle",
        "atomically replace destination",
        "open and validate destination",
        "remove backup and candidate",
    ],
)
@pytest.mark.parametrize("existing", [False, True])
def test_atomic_private_write_rolls_back_every_failed_phase_without_leaking_text(
    phase, existing,
):
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    parent, records = _private_records(path, existing=existing)
    api = FakeWindowsApi(records, fail_phase=phase)
    api.parent = parent
    api.destination = path

    with pytest.raises(RuntimeError) as error:
        atomic_write_windows_private_text(path, SECRET_TEXT, SID, api)

    assert SECRET_TEXT.strip() not in str(error.value)
    if existing:
        assert api.records[path]["data"] == b"old private bytes\n"
    else:
        assert path not in api.records
    assert all(
        not (str(candidate).endswith(".tmp") or str(candidate).endswith(".bak"))
        for candidate in api.records
    )


def test_atomic_private_write_reports_rollback_failure_separately():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    parent, records = _private_records(path)
    api = FakeWindowsApi(records, fail_phase="rollback")
    api.parent = parent
    api.destination = path

    original_open = api.open_file
    destination_opens = 0

    def fail_destination_validation(candidate, *, directory=False):
        nonlocal destination_opens
        if Path(candidate) == path and not directory:
            destination_opens += 1
            if destination_opens == 2:
                raise OSError(f"validation failed: {SECRET_TEXT}")
        return original_open(candidate, directory=directory)

    api.open_file = fail_destination_validation

    with pytest.raises(RuntimeError, match="rollback failed") as error:
        atomic_write_windows_private_text(path, SECRET_TEXT, SID, api)

    backups = [
        record["data"]
        for candidate, record in api.records.items()
        if str(candidate).endswith(".bak")
    ]
    assert backups == [b"old private bytes\n"]
    assert SECRET_TEXT.strip() not in str(error.value)


def test_atomic_private_write_rolls_back_after_second_destination_open_fails():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    parent, records = _private_records(path)
    api = FakeWindowsApi(records)
    api.parent = parent
    api.destination = path
    destination_opens = 0
    restore_calls = []
    original_open = api.open_file
    original_restore = api.restore_backup

    def fail_post_replace_validation(candidate, *, directory=False):
        nonlocal destination_opens
        if Path(candidate) == path and not directory:
            destination_opens += 1
            if destination_opens == 2:
                raise OSError(f"post-replace validation failed: {SECRET_TEXT}")
        return original_open(candidate, directory=directory)

    def record_restore(destination, backup):
        restore_calls.append((Path(destination), Path(backup)))
        return original_restore(destination, backup)

    api.open_file = fail_post_replace_validation
    api.restore_backup = record_restore

    with pytest.raises(RuntimeError, match="open and validate destination"):
        atomic_write_windows_private_text(path, SECRET_TEXT, SID, api)

    assert destination_opens == 2
    assert len(restore_calls) == 1
    assert restore_calls[0][0] == path
    assert api.records[path]["data"] == b"old private bytes\n"
    assert set(api.records) == {parent, path}


def test_atomic_private_write_removes_secret_from_entire_exception_chain():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    parent, records = _private_records(path)
    api = FakeWindowsApi(records, fail_phase="write UTF-8 through candidate handle")
    api.parent = parent
    api.destination = path

    with pytest.raises(RuntimeError) as error:
        atomic_write_windows_private_text(path, SECRET_TEXT, SID, api)

    exception = error.value
    formatted = "".join(
        traceback.format_exception(type(exception), exception, exception.__traceback__)
    )
    assert exception.__cause__ is None
    assert exception.__context__ is None
    assert SECRET_TEXT.strip() not in formatted


def test_atomic_private_write_retries_failed_close_before_candidate_cleanup():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory\memberkit.env")
    parent, records = _private_records(path)
    api = FakeWindowsApi(records, fail_phase="close candidate handle")
    api.parent = parent
    api.destination = path

    with pytest.raises(RuntimeError, match="close candidate handle"):
        atomic_write_windows_private_text(path, SECRET_TEXT, SID, api)

    assert api.phases.count("close candidate handle") == 2
    assert api.open_handles == set()
    assert all(
        not (str(candidate).endswith(".tmp") or str(candidate).endswith(".bak"))
        for candidate in api.records
    )
    assert api.records[path]["data"] == b"old private bytes\n"
