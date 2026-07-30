import ctypes
import ntpath
from pathlib import Path

import pytest

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
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
SECRET_TEXT = "MEMBERKIT_INBOX_URL=https://token@example.invalid/inbox\n"


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
        self.dacl_calls = []
        self.destination = None
        self.parent = None
        self._cleanup_recorded = False

    def _phase(self, phase):
        self.phases.append(phase)
        if self.fail_phase == phase:
            raise OSError(f"{phase} failed while processing {SECRET_TEXT}")

    def current_process_sid(self):
        return SID

    def current_username(self):
        return USERNAME

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
        self.closed.append(handle)

    def create_directory(self, path):
        path = Path(path)
        self._phase("provision private parent")
        if path in self.records:
            raise FileExistsError(path)
        record = _directory(dacl_protected=False)
        record["path"] = path
        self.records[path] = record
        return record

    def create_empty_file(self, path):
        self._phase("create empty candidate")
        path = Path(path)
        if path in self.records:
            raise FileExistsError(path)
        record = _file(data=b"", dacl_protected=False)
        record["path"] = path
        self.records[path] = record
        return record

    def apply_protected_dacl(self, handle, sid, principals):
        phase = (
            "apply protected DACL"
            if not handle["directory"]
            else "protect private parent"
        )
        self._phase(phase)
        principals = tuple(principals)
        self.dacl_calls.append((handle["path"], sid, principals))
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
    assert kernel32.CloseHandle.argtypes == [ctypes.c_void_p]
    assert kernel32.FlushFileBuffers.argtypes == [ctypes.c_void_p]
    assert advapi32.OpenProcessToken.argtypes[0] is ctypes.c_void_p
    assert advapi32.GetSecurityInfo.argtypes[0] is ctypes.c_void_p
    assert advapi32.SetSecurityInfo.argtypes[0] is ctypes.c_void_p


def test_native_created_handles_request_write_dac_before_protection(
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
    for _path, access, _share, _creation, flags in calls:
        assert access & 0x00040000  # WRITE_DAC
        assert flags & 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT


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
            (SYSTEM_SID, 0x10000000),
            (ADMINISTRATORS_SID, 0x10000000),
        ]
    )

    assert validate_windows_private_file(path, SID, FakeWindowsApi({path: record})) == path


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
    assert api.dacl_calls == [
        (path, SID, (SID, SYSTEM_SID, ADMINISTRATORS_SID))
    ]
    assert validate_windows_private_dir(path, SID, api) == path


def test_provision_windows_private_directory_rejects_existing_unsafe_directory():
    path = Path(r"C:\Users\Alex\AppData\Roaming\TeamMemory")
    unsafe = _directory(dacl_protected=False)
    api = FakeWindowsApi({path: unsafe})

    with pytest.raises(ValueError):
        provision_windows_private_dir(path, SID, api)

    assert api.dacl_calls == []


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
        "create empty candidate",
        "apply protected DACL",
        "validate candidate handle",
        "write UTF-8 through candidate handle",
        "flush candidate handle",
        "close candidate handle",
        "atomically replace destination",
        "open and validate destination",
        "remove backup and candidate",
    ]
    assert api.dacl_calls[-1][2] == (SID, SYSTEM_SID, ADMINISTRATORS_SID)
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


@pytest.mark.parametrize(
    "phase",
    [
        "provision private parent",
        "create empty candidate",
        "apply protected DACL",
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

    def fail_destination_validation(candidate, *, directory=False):
        if Path(candidate) == path and not directory:
            raise OSError(f"validation failed: {SECRET_TEXT}")
        return original_open(candidate, directory=directory)

    api.open_file = fail_destination_validation

    with pytest.raises(RuntimeError, match="rollback failed") as error:
        atomic_write_windows_private_text(path, SECRET_TEXT, SID, api)

    assert SECRET_TEXT.strip() not in str(error.value)
