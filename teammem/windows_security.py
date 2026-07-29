"""Native Windows identity and file-security helpers.

The public helpers take an injectable API so their security contract can be
tested on every platform.  The ctypes implementation is loaded only when a
real Windows operation needs it.
"""

from __future__ import annotations

import os
import ntpath
from pathlib import Path
from typing import Any, Iterable


_RESTRICTED_READ_SIDS = {
    "S-1-1-0",       # Everyone
    "S-1-5-11",      # Authenticated Users
    "S-1-5-32-545",  # BUILTIN\\Users
}
_RESTRICTED_READ_NAMES = {"Everyone", "Authenticated Users", "Users"}
_FILE_TYPE_DISK = 1


def _windows_filesystem_path(path: Path) -> bool:
    """Accept only drive-absolute paths or complete UNC share paths."""
    value = str(path)
    if value.startswith(("\\\\.\\", "\\\\?\\")):
        return False
    drive, tail = ntpath.splitdrive(value)
    if (
        len(drive) == 2
        and drive[0].isalpha()
        and drive[1] == ":"
        and tail.startswith("\\")
    ):
        return True
    if not value.startswith("\\\\"):
        return False
    components = value[2:].split("\\")
    return len(components) >= 2 and bool(components[0] and components[1])


def _native_api() -> "NativeWindowsApi":
    return NativeWindowsApi()


def current_user_sid(api: Any = None) -> str:
    """Return the SID for the current process token without spawning a shell."""
    return (api or _native_api()).current_process_sid()


def _safe_path(path: Path, rule: str) -> ValueError:
    return ValueError(f"Windows {rule}: {path}")


def _has_restricted_read(aces: Iterable[tuple[str, str | int]]) -> bool:
    for sid, rights in aces:
        if (sid in _RESTRICTED_READ_SIDS or sid in _RESTRICTED_READ_NAMES) and (
            rights == "read" or (isinstance(rights, int) and rights & 0x80000001)
        ):
            return True
    return False


def _validate_handle(handle: Any, path: Path, sid: str, api: Any, *, directory: bool) -> None:
    info = api.file_info(handle)
    if info.get("file_type") not in {"disk", _FILE_TYPE_DISK}:
        raise _safe_path(path, "environment path must be backed by a disk file")
    expected = bool(info.get("directory")) if directory else bool(info.get("regular"))
    if not expected or info.get("reparse_point"):
        kind = "directory" if directory else "regular non-reparse-point file"
        raise _safe_path(path, f"environment path must be a {kind}")
    if api.owner_sid(handle) != sid:
        raise _safe_path(path, "environment path must be owned by the current user")
    aces = api.allow_aces(handle)
    if aces is None or _has_restricted_read(aces):
        raise _safe_path(path, "environment path grants read access to a shared principal")


def _validate_path(path: Path, sid: str, api: Any, *, directory: bool) -> Path:
    if not _windows_filesystem_path(path):
        raise _safe_path(path, "environment path must be an absolute Windows filesystem path")
    try:
        handle = api.open_file(path, directory=directory)
    except FileNotFoundError:
        raise
    except OSError as failure:
        raise _safe_path(path, "environment path cannot be opened securely") from failure
    try:
        _validate_handle(handle, path, sid, api, directory=directory)
        return path
    finally:
        api.close_handle(handle)


def validate_windows_env_file(path: Path, sid: str, api: Any = None) -> Path:
    """Validate the file through an opened Windows handle, never path metadata."""
    return _validate_path(Path(path), sid, api or _native_api(), directory=False)


def read_windows_env_file(path: Path, sid: str, api: Any = None) -> list[str]:
    """Validate and read one opened file handle, closing it on every outcome."""
    path = Path(path)
    if not _windows_filesystem_path(path):
        raise _safe_path(path, "environment path must be an absolute Windows filesystem path")
    native = api or _native_api()
    try:
        handle = native.open_file(path, directory=False)
    except FileNotFoundError:
        raise
    except OSError as failure:
        raise _safe_path(path, "environment path cannot be opened securely") from failure
    transferred = False
    try:
        _validate_handle(handle, path, sid, native, directory=False)
        if hasattr(native, "transfer_for_read"):
            descriptor = native.transfer_for_read(handle)
            transferred = True
            lines = native.read_lines_from_descriptor(descriptor)
        else:
            lines = native.read_lines(handle)
        return lines
    except UnicodeError:
        raise _safe_path(path, "environment file must contain UTF-8 text") from None
    finally:
        if not transferred:
            native.close_handle(handle)


def validate_windows_state_dir(path: Path, sid: str, api: Any = None) -> Path:
    """Validate an existing per-user scheduler state directory before writes."""
    return _validate_path(Path(path), sid, api or _native_api(), directory=True)


class NativeWindowsApi:
    """Small lazy ctypes wrapper used only by Windows operators."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows native APIs are unavailable on this platform")

    @staticmethod
    def _libraries():
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL(
            "advapi32", use_last_error=True
        )
        NativeWindowsApi._configure_api(ctypes, kernel32, advapi32)
        return ctypes, kernel32, advapi32

    @staticmethod
    def _configure_api(ctypes, kernel32, advapi32) -> None:
        """Declare pointer-width-safe ctypes signatures for every native call."""
        handle = ctypes.c_void_p
        dword = ctypes.c_ulong
        boolean = ctypes.c_int
        pointer = ctypes.POINTER

        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = handle
        kernel32.CloseHandle.argtypes = [handle]
        kernel32.CloseHandle.restype = boolean
        kernel32.LocalFree.argtypes = [handle]
        kernel32.LocalFree.restype = handle
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, dword, dword, handle, dword, dword, handle,
        ]
        kernel32.CreateFileW.restype = handle
        kernel32.GetFileType.argtypes = [handle]
        kernel32.GetFileType.restype = dword

        advapi32.OpenProcessToken.argtypes = [handle, dword, pointer(handle)]
        advapi32.OpenProcessToken.restype = boolean
        advapi32.GetTokenInformation.argtypes = [
            handle, dword, handle, dword, pointer(dword),
        ]
        advapi32.GetTokenInformation.restype = boolean
        advapi32.ConvertSidToStringSidW.argtypes = [
            handle, pointer(ctypes.c_wchar_p),
        ]
        advapi32.ConvertSidToStringSidW.restype = boolean
        advapi32.GetSecurityInfo.argtypes = [
            handle, dword, dword, pointer(handle), pointer(handle),
            pointer(handle), pointer(handle), pointer(handle),
        ]
        advapi32.GetSecurityInfo.restype = dword
        advapi32.GetAclInformation.argtypes = [handle, handle, dword, dword]
        advapi32.GetAclInformation.restype = boolean
        advapi32.GetAce.argtypes = [handle, dword, pointer(handle)]
        advapi32.GetAce.restype = boolean

    @staticmethod
    def _sid_text(pointer) -> str:
        ctypes, kernel32, advapi32 = NativeWindowsApi._libraries()
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
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
        try:
            needed = ctypes.c_ulong()
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(
                token, 1, ctypes.cast(buffer, ctypes.c_void_p), needed,
                ctypes.byref(needed),
            ):
                raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
            return self._sid_text(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])
        finally:
            kernel32.CloseHandle(token)

    def open_file(self, path: Path, *, directory: bool = False):
        ctypes, kernel32, _ = self._libraries()
        flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
        handle = kernel32.CreateFileW(
            str(path), 0x80000000, 0x00000001 | 0x00000002 | 0x00000004,
            None, 3, flags, None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            error = ctypes.get_last_error()
            if error in {2, 3}:
                raise FileNotFoundError(error, "Windows path does not exist", str(path))
            raise OSError(error, "CreateFileW failed")
        return handle

    def file_info(self, handle) -> dict[str, bool]:
        ctypes, kernel32, _ = self._libraries()

        class Info(ctypes.Structure):
            _fields_ = [
                ("attributes", ctypes.c_ulong), ("creation_low", ctypes.c_ulong),
                ("creation_high", ctypes.c_ulong), ("access_low", ctypes.c_ulong),
                ("access_high", ctypes.c_ulong), ("write_low", ctypes.c_ulong),
                ("write_high", ctypes.c_ulong), ("volume", ctypes.c_ulong),
                ("size_high", ctypes.c_ulong), ("size_low", ctypes.c_ulong),
                ("links", ctypes.c_ulong), ("index_high", ctypes.c_ulong),
                ("index_low", ctypes.c_ulong),
            ]

        info = Info()
        kernel32.GetFileInformationByHandle.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(Info),
        ]
        kernel32.GetFileInformationByHandle.restype = ctypes.c_int
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
        return {
            "directory": bool(info.attributes & 0x10),
            "regular": not bool(info.attributes & (0x10 | 0x400)),
            "reparse_point": bool(info.attributes & 0x400),
            "file_type": kernel32.GetFileType(handle),
        }

    def _security(self, handle):
        ctypes, _, advapi32 = self._libraries()
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = advapi32.GetSecurityInfo(
            handle, 1, 0x00000001 | 0x00000004, ctypes.byref(owner), None,
            ctypes.byref(dacl), None, ctypes.byref(descriptor),
        )
        if result:
            raise OSError(result, "GetSecurityInfo failed")
        return owner, dacl, descriptor

    def owner_sid(self, handle) -> str:
        ctypes, kernel32, _ = self._libraries()
        owner, _dacl, descriptor = self._security(handle)
        try:
            return self._sid_text(owner)
        finally:
            kernel32.LocalFree(descriptor)

    def allow_aces(self, handle) -> list[tuple[str, int]] | None:
        ctypes, kernel32, advapi32 = self._libraries()
        _owner, dacl, descriptor = self._security(handle)
        try:
            if not dacl:
                return None

            class AclSize(ctypes.Structure):
                _fields_ = [
                    ("ace_count", ctypes.c_ulong),
                    ("acl_bytes_in_use", ctypes.c_ulong),
                    ("acl_bytes_free", ctypes.c_ulong),
                ]

            size = AclSize()
            if not advapi32.GetAclInformation(
                dacl, ctypes.cast(ctypes.byref(size), ctypes.c_void_p),
                ctypes.sizeof(size), 2,
            ):
                raise OSError(ctypes.get_last_error(), "GetAclInformation failed")
            values = []
            for index in range(size.ace_count):
                ace = ctypes.c_void_p()
                if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                    raise OSError(ctypes.get_last_error(), "GetAce failed")
                raw = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte))
                ace_type = raw[0]
                if ace_type == 4:  # ACCESS_ALLOWED_COMPOUND_ACE_TYPE
                    return None
                if ace_type not in {0, 5, 9, 11}:
                    if ace_type not in {1, 2, 3, 6, 7, 8, 10, 12, 13, 14, 15, 16, 17, 18}:
                        return None
                    continue
                address = ctypes.addressof(raw.contents)
                ace_size = raw[2] | (raw[3] << 8)
                if ace_type in {5, 11}:
                    if ace_size < 12:
                        return None
                    flags = ctypes.c_uint32.from_address(address + 8).value
                else:
                    flags = 0
                sid_offset = self._allow_ace_sid_offset(ace_type, flags, ace_size)
                if sid_offset is None:
                    return None
                mask = ctypes.c_uint32.from_address(address + 4).value
                try:
                    sid = self._sid_text(ctypes.c_void_p(address + sid_offset))
                except OSError:
                    return None
                values.append((sid, mask))
            return values
        finally:
            kernel32.LocalFree(descriptor)

    @staticmethod
    def _allow_ace_sid_offset(ace_type: int, flags: int, ace_size: int) -> int | None:
        """Return a safe SidStart offset for documented allow-ACE layouts."""
        if ace_type in {0, 9}:  # ACCESS_ALLOWED_ACE / CALLBACK_ACE
            offset = 8
        elif ace_type in {5, 11}:  # OBJECT / CALLBACK_OBJECT allow ACE
            if flags & ~0x3:
                return None
            offset = 12 + 16 * (
                int(bool(flags & 0x1)) + int(bool(flags & 0x2))
            )
        else:
            return None
        return offset if ace_size >= offset + 8 else None

    def transfer_for_read(self, handle):
        import msvcrt

        return msvcrt.open_osfhandle(handle, os.O_RDONLY)

    @staticmethod
    def read_lines_from_descriptor(descriptor) -> list[str]:
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            return stream.read().splitlines(keepends=True)

    def close_handle(self, handle) -> None:
        ctypes, kernel32, _ = self._libraries()
        if not kernel32.CloseHandle(handle):
            raise OSError(ctypes.get_last_error(), "CloseHandle failed")
