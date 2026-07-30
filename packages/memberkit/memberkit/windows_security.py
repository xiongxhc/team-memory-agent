"""Windows identity and private-file helpers used by MemberKit.

Native libraries are loaded only when a real Windows operation is requested.
Every public operation also accepts an injected API for platform-independent
tests.
"""

from __future__ import annotations

import ntpath
import os
import uuid
from pathlib import Path
from typing import Any, Iterable


RESTRICTED_READ_SIDS = {
    "S-1-1-0",
    "S-1-5-11",
    "S-1-5-32-545",
}
PRIVATE_DACL_SIDS = (
    "S-1-5-18",
    "S-1-5-32-544",
)

_READ_CAPABLE_MASK = (
    0x00000001  # FILE_READ_DATA
    | 0x00000008  # FILE_READ_EA
    | 0x00000080  # FILE_READ_ATTRIBUTES
    | 0x00020000  # READ_CONTROL
    | 0x80000000  # GENERIC_READ
    | 0x10000000  # GENERIC_ALL
)


def _native_api() -> "NativeWindowsApi":
    return NativeWindowsApi()


def current_user_sid(api: Any = None) -> str:
    """Return the current process token's user SID."""
    return (api or _native_api()).current_process_sid()


def current_username(api: Any = None) -> str:
    """Return the current Windows username without invoking a shell."""
    return (api or _native_api()).current_username()


def current_session_id(api: Any = None) -> int:
    """Return the current process's validated non-reserved Windows session ID."""
    value = (api or _native_api()).current_session_id()
    if type(value) is not int:
        raise ValueError("invalid Windows session ID")
    if not 0 <= value < 0xFFFFFFFF:
        raise ValueError("invalid Windows session ID")
    return value


def _is_absolute_windows_filesystem_path(path: Path) -> bool:
    value = str(path)
    if not value or "\0" in value:
        return False
    normalized = value.replace("/", "\\")
    if normalized.startswith(("\\\\.\\", "\\\\?\\")):
        return False

    drive, tail = ntpath.splitdrive(normalized)
    if len(drive) == 2 and drive[0].isalpha() and drive[1] == ":":
        return tail.startswith("\\")
    if not normalized.startswith("\\\\"):
        return False

    components = normalized[2:].split("\\")
    return len(components) >= 2 and bool(components[0] and components[1])


def _path_error(path: Path, rule: str) -> ValueError:
    return ValueError(f"Windows private path {rule}: {path}")


def _validate_path_syntax(path: Path) -> None:
    if not _is_absolute_windows_filesystem_path(path):
        raise _path_error(path, "must be an absolute Windows filesystem path")


def _unapproved_read_grant(
    aces: Iterable[tuple[str, int]] | None,
    sid: str,
) -> bool:
    if aces is None:
        return True
    allowed_sids = {sid, *PRIVATE_DACL_SIDS}
    return any(
        ace_sid not in allowed_sids and bool(mask & _READ_CAPABLE_MASK)
        for ace_sid, mask in aces
    )


def _validate_handle(
    handle: Any,
    path: Path,
    sid: str,
    api: Any,
    *,
    directory: bool,
) -> None:
    record = api.describe_handle(handle)
    if record.get("file_type") not in {"disk", 1}:
        raise _path_error(path, "must be backed by a disk handle")
    if record.get("owner_sid") != sid:
        raise _path_error(path, "must be owned by the current user")
    if record.get("reparse_point"):
        raise _path_error(path, "must not be a reparse point")
    if record.get("dacl_protected") is not True:
        raise _path_error(path, "must have a protected DACL")
    if directory:
        if record.get("directory") is not True:
            raise _path_error(path, "must be a directory")
    elif record.get("regular") is not True:
        raise _path_error(path, "must be a regular file")
    if _unapproved_read_grant(record.get("allow_aces"), sid):
        raise _path_error(
            path,
            "grants read access to an unapproved principal (shared principal)",
        )


def _open_and_validate(
    path: Path,
    sid: str,
    api: Any,
    *,
    directory: bool,
) -> Path:
    _validate_path_syntax(path)
    try:
        handle = api.open_file(path, directory=directory)
    except FileNotFoundError:
        raise
    except OSError as failure:
        raise _path_error(path, "cannot be opened securely") from failure
    try:
        _validate_handle(handle, path, sid, api, directory=directory)
        return path
    finally:
        api.close_handle(handle)


def validate_windows_private_file(
    path: Path,
    sid: str,
    api: Any = None,
) -> Path:
    """Validate an existing private regular file through its opened handle."""
    return _open_and_validate(Path(path), sid, api or _native_api(), directory=False)


def read_windows_private_text(
    path: Path,
    sid: str,
    api: Any = None,
) -> str:
    """Validate and read UTF-8 text through one opened handle."""
    path = Path(path)
    _validate_path_syntax(path)
    native = api or _native_api()
    try:
        handle = native.open_file(path, directory=False)
    except FileNotFoundError:
        raise
    except OSError as failure:
        raise _path_error(path, "cannot be opened securely") from failure
    try:
        _validate_handle(handle, path, sid, native, directory=False)
        try:
            return native.read_utf8(handle)
        except UnicodeError:
            raise _path_error(path, "must contain UTF-8 text") from None
    finally:
        native.close_handle(handle)


def validate_windows_private_dir(
    path: Path,
    sid: str,
    api: Any = None,
) -> Path:
    """Validate an existing private directory through its opened handle."""
    return _open_and_validate(Path(path), sid, api or _native_api(), directory=True)


def provision_windows_private_dir(
    path: Path,
    sid: str,
    api: Any = None,
) -> Path:
    """Create and protect a missing directory, or validate an existing one."""
    path = Path(path)
    _validate_path_syntax(path)
    native = api or _native_api()
    try:
        return _open_and_validate(path, sid, native, directory=True)
    except FileNotFoundError:
        pass

    handle = None
    try:
        try:
            handle = native.create_directory(path)
        except FileExistsError:
            return _open_and_validate(path, sid, native, directory=True)
        native.apply_protected_dacl(
            handle,
            sid,
            (sid, *PRIVATE_DACL_SIDS),
        )
        _validate_handle(handle, path, sid, native, directory=True)
        return path
    except OSError as failure:
        raise _path_error(path, "cannot be provisioned securely") from failure
    finally:
        if handle is not None:
            native.close_handle(handle)


def _private_sibling(path: Path, token: str, suffix: str) -> Path:
    parent = ntpath.dirname(str(path))
    name = ntpath.basename(str(path))
    return Path(ntpath.join(parent, f".{name}.{token}.{suffix}"))


def _best_effort_cleanup(
    native: Any,
    *paths: Path,
    stop_on_failure: bool = False,
) -> Exception | None:
    first_failure = None
    for path in paths:
        try:
            native.delete_file(path)
        except Exception as failure:
            if first_failure is None:
                first_failure = failure
            if stop_on_failure:
                break
    return first_failure


def atomic_write_windows_private_text(
    path: Path,
    text: str,
    sid: str,
    api: Any = None,
) -> Path:
    """Install private UTF-8 text transactionally without exposing content.

    The candidate stays empty until its DACL and opened handle validate.  Any
    failure after installation restores the previous destination, or removes a
    failed first installation.
    """
    path = Path(path)
    _validate_path_syntax(path)
    native = api or _native_api()
    parent = Path(ntpath.dirname(str(path)))
    token = uuid.uuid4().hex
    candidate = _private_sibling(path, token, "tmp")
    backup = _private_sibling(path, token, "bak")
    handle = None
    had_destination = False
    installed = False
    phase = "provision private parent"
    boundary_error = None

    try:
        encoded = text.encode("utf-8")
        provision_windows_private_dir(parent, sid, native)

        had_destination = native.path_exists(path)
        phase = "create empty candidate"
        handle = native.create_empty_file(candidate)

        phase = "apply protected DACL"
        native.apply_protected_dacl(
            handle,
            sid,
            (sid, *PRIVATE_DACL_SIDS),
        )

        phase = "validate candidate handle"
        _validate_handle(handle, candidate, sid, native, directory=False)

        phase = "write UTF-8 through candidate handle"
        native.write_utf8(handle, encoded)

        phase = "flush candidate handle"
        native.flush_handle(handle)

        phase = "close candidate handle"
        native.close_handle(handle)
        handle = None

        phase = "atomically replace destination"
        if had_destination:
            native.replace_file(path, candidate, backup)
        else:
            native.move_file(candidate, path)
        installed = True

        phase = "open and validate destination"
        validate_windows_private_file(path, sid, native)

        phase = "remove backup and candidate"
        cleanup_failure = _best_effort_cleanup(
            native,
            candidate,
            backup,
            stop_on_failure=True,
        )
        if cleanup_failure is not None:
            raise cleanup_failure
        return path
    except Exception:
        close_failure = None
        if handle is not None:
            try:
                native.close_handle(handle)
            except Exception as candidate_close_failure:
                close_failure = candidate_close_failure

        rollback_failure = None
        if installed:
            try:
                if had_destination:
                    native.restore_backup(path, backup)
                else:
                    native.delete_file(path)
            except Exception as restore_failure:
                rollback_failure = restore_failure

        if rollback_failure is not None:
            _best_effort_cleanup(native, candidate)
            boundary_error = RuntimeError(
                f"Windows private file write failed during {phase}; rollback failed"
            )
        else:
            cleanup_failure = _best_effort_cleanup(native, candidate, backup)
            if cleanup_failure is not None or close_failure is not None:
                boundary_error = RuntimeError(
                    f"Windows private file write failed during {phase}; cleanup failed"
                )
            else:
                boundary_error = RuntimeError(
                    f"Windows private file write failed during {phase}"
                )

    raise boundary_error


class NativeWindowsApi:
    """Small lazy ctypes adapter for Windows filesystem security APIs."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows native APIs are unavailable on this platform")

    @staticmethod
    def _libraries():
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        NativeWindowsApi._configure_api(ctypes, kernel32, advapi32)
        return ctypes, kernel32, advapi32

    @staticmethod
    def _configure_api(ctypes, kernel32, advapi32) -> None:
        handle = ctypes.c_void_p
        dword = ctypes.c_ulong
        boolean = ctypes.c_int
        pointer = ctypes.POINTER

        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = handle
        kernel32.GetCurrentProcessId.argtypes = []
        kernel32.GetCurrentProcessId.restype = dword
        kernel32.ProcessIdToSessionId.argtypes = [dword, pointer(dword)]
        kernel32.ProcessIdToSessionId.restype = boolean
        kernel32.CloseHandle.argtypes = [handle]
        kernel32.CloseHandle.restype = boolean
        kernel32.LocalFree.argtypes = [handle]
        kernel32.LocalFree.restype = handle
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            dword,
            dword,
            handle,
            dword,
            dword,
            handle,
        ]
        kernel32.CreateFileW.restype = handle
        kernel32.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, handle]
        kernel32.CreateDirectoryW.restype = boolean
        kernel32.RemoveDirectoryW.argtypes = [ctypes.c_wchar_p]
        kernel32.RemoveDirectoryW.restype = boolean
        kernel32.GetFileType.argtypes = [handle]
        kernel32.GetFileType.restype = dword
        kernel32.ReadFile.argtypes = [
            handle,
            handle,
            dword,
            pointer(dword),
            handle,
        ]
        kernel32.ReadFile.restype = boolean
        kernel32.WriteFile.argtypes = [
            handle,
            handle,
            dword,
            pointer(dword),
            handle,
        ]
        kernel32.WriteFile.restype = boolean
        kernel32.FlushFileBuffers.argtypes = [handle]
        kernel32.FlushFileBuffers.restype = boolean
        kernel32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetFileAttributesW.restype = dword
        kernel32.ReplaceFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            dword,
            handle,
            handle,
        ]
        kernel32.ReplaceFileW.restype = boolean
        kernel32.MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, dword]
        kernel32.MoveFileExW.restype = boolean
        kernel32.DeleteFileW.argtypes = [ctypes.c_wchar_p]
        kernel32.DeleteFileW.restype = boolean

        advapi32.OpenProcessToken.argtypes = [handle, dword, pointer(handle)]
        advapi32.OpenProcessToken.restype = boolean
        advapi32.GetTokenInformation.argtypes = [
            handle,
            dword,
            handle,
            dword,
            pointer(dword),
        ]
        advapi32.GetTokenInformation.restype = boolean
        advapi32.GetUserNameW.argtypes = [ctypes.c_wchar_p, pointer(dword)]
        advapi32.GetUserNameW.restype = boolean
        advapi32.ConvertSidToStringSidW.argtypes = [
            handle,
            pointer(ctypes.c_wchar_p),
        ]
        advapi32.ConvertSidToStringSidW.restype = boolean
        advapi32.GetSecurityInfo.argtypes = [
            handle,
            dword,
            dword,
            pointer(handle),
            pointer(handle),
            pointer(handle),
            pointer(handle),
            pointer(handle),
        ]
        advapi32.GetSecurityInfo.restype = dword
        advapi32.GetSecurityDescriptorControl.argtypes = [
            handle,
            pointer(ctypes.c_ushort),
            pointer(dword),
        ]
        advapi32.GetSecurityDescriptorControl.restype = boolean
        advapi32.GetAclInformation.argtypes = [handle, handle, dword, dword]
        advapi32.GetAclInformation.restype = boolean
        advapi32.GetAce.argtypes = [handle, dword, pointer(handle)]
        advapi32.GetAce.restype = boolean
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            ctypes.c_wchar_p,
            dword,
            pointer(handle),
            pointer(dword),
        ]
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = boolean
        advapi32.GetSecurityDescriptorDacl.argtypes = [
            handle,
            pointer(boolean),
            pointer(handle),
            pointer(boolean),
        ]
        advapi32.GetSecurityDescriptorDacl.restype = boolean
        advapi32.SetSecurityInfo.argtypes = [
            handle,
            dword,
            dword,
            handle,
            handle,
            handle,
            handle,
        ]
        advapi32.SetSecurityInfo.restype = dword

    @staticmethod
    def _sid_text(pointer) -> str:
        import ctypes

        _ctypes, kernel32, advapi32 = NativeWindowsApi._libraries()
        text = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(pointer, ctypes.byref(text)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            return text.value
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    def current_process_sid(self) -> str:
        ctypes, kernel32, advapi32 = self._libraries()
        token = ctypes.c_void_p()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            0x0008,
            ctypes.byref(token),
        ):
            raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
        try:
            needed = ctypes.c_ulong()
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(
                token,
                1,
                ctypes.cast(buffer, ctypes.c_void_p),
                needed,
                ctypes.byref(needed),
            ):
                raise OSError(
                    ctypes.get_last_error(),
                    "GetTokenInformation failed",
                )
            token_user = ctypes.cast(
                buffer,
                ctypes.POINTER(ctypes.c_void_p),
            )[0]
            return self._sid_text(token_user)
        finally:
            kernel32.CloseHandle(token)

    def current_username(self) -> str:
        ctypes, _kernel32, advapi32 = self._libraries()
        size = ctypes.c_ulong(0)
        advapi32.GetUserNameW(None, ctypes.byref(size))
        buffer = ctypes.create_unicode_buffer(size.value)
        if not advapi32.GetUserNameW(buffer, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "GetUserNameW failed")
        return buffer.value

    def current_session_id(self) -> int:
        ctypes, kernel32, _advapi32 = self._libraries()
        process_id = kernel32.GetCurrentProcessId()
        session_id = ctypes.c_ulong()
        if not kernel32.ProcessIdToSessionId(
            process_id,
            ctypes.byref(session_id),
        ):
            raise OSError(
                ctypes.get_last_error(),
                "ProcessIdToSessionId failed",
            )
        return session_id.value

    def open_file(
        self,
        path: Path,
        *,
        directory: bool = False,
        write_dac: bool = False,
    ):
        ctypes, kernel32, _advapi32 = self._libraries()
        desired_access = 0x00020000 | (0x00000080 if directory else 0x80000000)
        if write_dac:
            desired_access |= 0x00040000
        flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
        handle = kernel32.CreateFileW(
            str(path),
            desired_access,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            flags,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in {2, 3}:
                raise FileNotFoundError(error, "Windows path does not exist", str(path))
            raise OSError(error, "CreateFileW failed")
        return handle

    def create_directory(self, path: Path):
        ctypes, kernel32, _advapi32 = self._libraries()
        if not kernel32.CreateDirectoryW(str(path), None):
            error = ctypes.get_last_error()
            if error == 183:
                raise FileExistsError(error, "Windows directory exists", str(path))
            raise OSError(error, "CreateDirectoryW failed")
        try:
            return self.open_file(path, directory=True, write_dac=True)
        except BaseException:
            kernel32.RemoveDirectoryW(str(path))
            raise

    def create_empty_file(self, path: Path):
        ctypes, kernel32, _advapi32 = self._libraries()
        handle = kernel32.CreateFileW(
            str(path),
            0x80000000 | 0x40000000 | 0x00020000 | 0x00040000,
            0,
            None,
            1,
            0x00000080 | 0x00200000,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in {80, 183}:
                raise FileExistsError(error, "Windows candidate exists", str(path))
            raise OSError(error, "CreateFileW failed")
        return handle

    def _file_info(self, handle) -> dict[str, Any]:
        ctypes, kernel32, _advapi32 = self._libraries()

        class FileInfo(ctypes.Structure):
            _fields_ = [
                ("attributes", ctypes.c_ulong),
                ("creation_low", ctypes.c_ulong),
                ("creation_high", ctypes.c_ulong),
                ("access_low", ctypes.c_ulong),
                ("access_high", ctypes.c_ulong),
                ("write_low", ctypes.c_ulong),
                ("write_high", ctypes.c_ulong),
                ("volume", ctypes.c_ulong),
                ("size_high", ctypes.c_ulong),
                ("size_low", ctypes.c_ulong),
                ("links", ctypes.c_ulong),
                ("index_high", ctypes.c_ulong),
                ("index_low", ctypes.c_ulong),
            ]

        kernel32.GetFileInformationByHandle.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(FileInfo),
        ]
        kernel32.GetFileInformationByHandle.restype = ctypes.c_int
        info = FileInfo()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise OSError(
                ctypes.get_last_error(),
                "GetFileInformationByHandle failed",
            )
        directory = bool(info.attributes & 0x10)
        reparse_point = bool(info.attributes & 0x400)
        return {
            "directory": directory,
            "regular": not directory and not reparse_point,
            "reparse_point": reparse_point,
            "file_type": kernel32.GetFileType(handle),
        }

    def _security_descriptor(self, handle):
        ctypes, _kernel32, advapi32 = self._libraries()
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = advapi32.GetSecurityInfo(
            handle,
            1,
            0x00000001 | 0x00000004,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise OSError(result, "GetSecurityInfo failed")
        return owner, dacl, descriptor

    def describe_handle(self, handle) -> dict[str, Any]:
        ctypes, kernel32, advapi32 = self._libraries()
        owner, dacl, descriptor = self._security_descriptor(handle)
        try:
            control = ctypes.c_ushort()
            revision = ctypes.c_ulong()
            if not advapi32.GetSecurityDescriptorControl(
                descriptor,
                ctypes.byref(control),
                ctypes.byref(revision),
            ):
                raise OSError(
                    ctypes.get_last_error(),
                    "GetSecurityDescriptorControl failed",
                )
            record = self._file_info(handle)
            record.update(
                {
                    "owner_sid": self._sid_text(owner),
                    "dacl_protected": bool(control.value & 0x1000),
                    "allow_aces": self._allow_aces(dacl),
                }
            )
            return record
        finally:
            kernel32.LocalFree(descriptor)

    def _allow_aces(self, dacl) -> list[tuple[str, int]] | None:
        if not dacl:
            return None
        ctypes, _kernel32, advapi32 = self._libraries()

        class AclSize(ctypes.Structure):
            _fields_ = [
                ("ace_count", ctypes.c_ulong),
                ("acl_bytes_in_use", ctypes.c_ulong),
                ("acl_bytes_free", ctypes.c_ulong),
            ]

        size = AclSize()
        if not advapi32.GetAclInformation(
            dacl,
            ctypes.cast(ctypes.byref(size), ctypes.c_void_p),
            ctypes.sizeof(size),
            2,
        ):
            raise OSError(ctypes.get_last_error(), "GetAclInformation failed")

        values = []
        for index in range(size.ace_count):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise OSError(ctypes.get_last_error(), "GetAce failed")
            raw = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte))
            ace_type = raw[0]
            if ace_type == 4:
                return None
            if ace_type not in {0, 5, 9, 11}:
                if ace_type not in {
                    1, 2, 3, 6, 7, 8, 10, 12, 13, 14, 15, 16, 17, 18,
                }:
                    return None
                continue
            address = ctypes.addressof(raw.contents)
            ace_size = raw[2] | (raw[3] << 8)
            flags = (
                ctypes.c_uint32.from_address(address + 8).value
                if ace_type in {5, 11} and ace_size >= 12
                else 0
            )
            offset = self._allow_ace_sid_offset(ace_type, flags, ace_size)
            if offset is None:
                return None
            mask = ctypes.c_uint32.from_address(address + 4).value
            try:
                sid = self._sid_text(ctypes.c_void_p(address + offset))
            except OSError:
                return None
            values.append((sid, mask))
        return values

    @staticmethod
    def _allow_ace_sid_offset(
        ace_type: int,
        flags: int,
        ace_size: int,
    ) -> int | None:
        if ace_type in {0, 9}:
            offset = 8
        elif ace_type in {5, 11}:
            if flags & ~0x3:
                return None
            offset = 12 + 16 * (
                int(bool(flags & 0x1)) + int(bool(flags & 0x2))
            )
        else:
            return None
        return offset if ace_size >= offset + 8 else None

    def apply_protected_dacl(
        self,
        handle,
        sid: str,
        principals: Iterable[str],
    ) -> None:
        ctypes, kernel32, advapi32 = self._libraries()
        principals = tuple(principals)
        expected = (sid, *PRIVATE_DACL_SIDS)
        if principals != expected:
            raise ValueError("private DACL principals do not match the security contract")
        sddl = "D:P" + "".join(f"(A;;FA;;;{principal})" for principal in principals)
        descriptor = ctypes.c_void_p()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            1,
            ctypes.byref(descriptor),
            None,
        ):
            raise OSError(
                ctypes.get_last_error(),
                "ConvertStringSecurityDescriptorToSecurityDescriptorW failed",
            )
        try:
            present = ctypes.c_int()
            defaulted = ctypes.c_int()
            dacl = ctypes.c_void_p()
            if not advapi32.GetSecurityDescriptorDacl(
                descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(defaulted),
            ) or not present.value:
                raise OSError(
                    ctypes.get_last_error(),
                    "GetSecurityDescriptorDacl failed",
                )
            result = advapi32.SetSecurityInfo(
                handle,
                1,
                0x00000004 | 0x80000000,
                None,
                None,
                dacl,
                None,
            )
            if result:
                raise OSError(result, "SetSecurityInfo failed")
        finally:
            kernel32.LocalFree(descriptor)

    def read_utf8(self, handle) -> str:
        ctypes, kernel32, _advapi32 = self._libraries()
        chunks = []
        while True:
            buffer = ctypes.create_string_buffer(65536)
            read = ctypes.c_ulong()
            if not kernel32.ReadFile(
                handle,
                ctypes.cast(buffer, ctypes.c_void_p),
                len(buffer),
                ctypes.byref(read),
                None,
            ):
                raise OSError(ctypes.get_last_error(), "ReadFile failed")
            if not read.value:
                break
            chunks.append(buffer.raw[: read.value])
        return b"".join(chunks).decode("utf-8")

    def write_utf8(self, handle, data: bytes) -> None:
        ctypes, kernel32, _advapi32 = self._libraries()
        offset = 0
        while offset < len(data):
            chunk = data[offset : offset + 65536]
            buffer = ctypes.create_string_buffer(chunk)
            written = ctypes.c_ulong()
            if not kernel32.WriteFile(
                handle,
                ctypes.cast(buffer, ctypes.c_void_p),
                len(chunk),
                ctypes.byref(written),
                None,
            ):
                raise OSError(ctypes.get_last_error(), "WriteFile failed")
            if not written.value:
                raise OSError("WriteFile made no progress")
            offset += written.value

    def flush_handle(self, handle) -> None:
        ctypes, kernel32, _advapi32 = self._libraries()
        if not kernel32.FlushFileBuffers(handle):
            raise OSError(ctypes.get_last_error(), "FlushFileBuffers failed")

    def path_exists(self, path: Path) -> bool:
        ctypes, kernel32, _advapi32 = self._libraries()
        attributes = kernel32.GetFileAttributesW(str(path))
        if attributes != 0xFFFFFFFF:
            return True
        error = ctypes.get_last_error()
        if error in {2, 3}:
            return False
        raise OSError(error, "GetFileAttributesW failed")

    def replace_file(
        self,
        destination: Path,
        candidate: Path,
        backup: Path,
    ) -> None:
        ctypes, kernel32, _advapi32 = self._libraries()
        if not kernel32.ReplaceFileW(
            str(destination),
            str(candidate),
            str(backup),
            0x00000001,
            None,
            None,
        ):
            raise OSError(ctypes.get_last_error(), "ReplaceFileW failed")

    def move_file(self, candidate: Path, destination: Path) -> None:
        ctypes, kernel32, _advapi32 = self._libraries()
        if not kernel32.MoveFileExW(
            str(candidate),
            str(destination),
            0x00000008,
        ):
            raise OSError(ctypes.get_last_error(), "MoveFileExW failed")

    def restore_backup(self, destination: Path, backup: Path) -> None:
        ctypes, kernel32, _advapi32 = self._libraries()
        if not kernel32.ReplaceFileW(
            str(destination),
            str(backup),
            None,
            0x00000001,
            None,
            None,
        ):
            raise OSError(ctypes.get_last_error(), "ReplaceFileW rollback failed")

    def delete_file(self, path: Path) -> None:
        ctypes, kernel32, _advapi32 = self._libraries()
        if kernel32.DeleteFileW(str(path)):
            return
        error = ctypes.get_last_error()
        if error not in {2, 3}:
            raise OSError(error, "DeleteFileW failed")

    def close_handle(self, handle) -> None:
        ctypes, kernel32, _advapi32 = self._libraries()
        if not kernel32.CloseHandle(handle):
            raise OSError(ctypes.get_last_error(), "CloseHandle failed")
