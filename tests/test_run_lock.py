import builtins
import errno
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from teammem.run_lock import RunLockedError, acquire_run_lock


class FakeFcntl:
    LOCK_EX = 1
    LOCK_NB = 2
    LOCK_UN = 4

    def __init__(self, failures):
        self.failures = failures
        self.calls = []

    def flock(self, descriptor, operation):
        self.calls.append((descriptor, operation))
        if operation != self.LOCK_UN and self.failures:
            self.failures -= 1
            raise BlockingIOError(errno.EAGAIN, "locked")


def test_unix_lock_contends_across_processes(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Unix locking behavior")
    ledger = tmp_path / "ledger.db"
    code = """
import json
import sys
from pathlib import Path
from teammem.run_lock import RunLockedError, acquire_run_lock

try:
    with acquire_run_lock(Path(sys.argv[1]), wait_seconds=0):
        result = {"acquired": True}
except RunLockedError as error:
    result = {"acquired": False, "error": str(error)}
print(json.dumps(result, sort_keys=True))
"""

    with acquire_run_lock(ledger, wait_seconds=0):
        child = subprocess.run(
            [sys.executable, "-c", code, str(ledger)],
            check=True,
            capture_output=True,
            text=True,
        )

    assert json.loads(child.stdout) == {
        "acquired": False,
        "error": "another run is active",
    }


def test_canonical_realpath_aliases_share_one_adjacent_lock(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    alias_dir = tmp_path / "alias"
    try:
        alias_dir.symlink_to(real_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    ledger = real_dir / "ledger.db"
    alias = alias_dir / "ledger.db"

    with acquire_run_lock(ledger, wait_seconds=0):
        with pytest.raises(RunLockedError, match="^another run is active$"):
            with acquire_run_lock(alias, wait_seconds=0):
                pass

    canonical_lock = Path(f"{ledger.resolve()}.lock")
    assert canonical_lock.exists()
    assert Path(f"{alias}.lock").resolve() == canonical_lock.resolve()


def test_lock_is_released_when_context_body_raises(tmp_path):
    ledger = tmp_path / "ledger.db"

    with pytest.raises(LookupError, match="body failed"):
        with acquire_run_lock(ledger, wait_seconds=0):
            raise LookupError("body failed")

    with acquire_run_lock(ledger, wait_seconds=0):
        pass


def test_capture_style_contention_fails_immediately(monkeypatch, tmp_path):
    primitive = FakeFcntl(failures=1)
    monkeypatch.setitem(sys.modules, "fcntl", primitive)
    sleeps = []
    waits = []

    with pytest.raises(RunLockedError, match="^another run is active$"):
        with acquire_run_lock(
            tmp_path / "ledger.db",
            wait_seconds=0,
            platform="linux",
            on_wait=waits.append,
            monotonic=lambda: 10.0,
            sleep=sleeps.append,
        ):
            pass

    assert waits == []
    assert sleeps == []


def test_unix_non_contention_lock_error_is_not_misreported(monkeypatch, tmp_path):
    class BrokenFcntl(FakeFcntl):
        def flock(self, descriptor, operation):
            raise PermissionError(errno.EPERM, "operation not permitted")

    monkeypatch.setitem(sys.modules, "fcntl", BrokenFcntl(failures=0))

    with pytest.raises(PermissionError) as failure:
        with acquire_run_lock(
            tmp_path / "ledger.db", wait_seconds=0, platform="linux"
        ):
            pass

    assert failure.value.errno == errno.EPERM


def test_full_style_wait_is_bounded_and_uses_injected_clock(monkeypatch, tmp_path):
    primitive = FakeFcntl(failures=99)
    monkeypatch.setitem(sys.modules, "fcntl", primitive)
    now = [0.0]
    sleeps = []
    waits = []

    def advance(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    with pytest.raises(RunLockedError, match="^another run is active$"):
        with acquire_run_lock(
            tmp_path / "ledger.db",
            wait_seconds=2.5,
            platform="linux",
            on_wait=waits.append,
            monotonic=lambda: now[0],
            sleep=advance,
        ):
            pass

    assert sleeps == [1.0, 1.0, 0.5]
    assert waits == ["waiting for active run"] * 3


def test_waiting_lock_can_be_acquired_before_deadline(monkeypatch, tmp_path):
    primitive = FakeFcntl(failures=2)
    monkeypatch.setitem(sys.modules, "fcntl", primitive)
    now = [0.0]

    def advance(seconds):
        now[0] += seconds

    with acquire_run_lock(
        tmp_path / "ledger.db",
        wait_seconds=5,
        platform="linux",
        monotonic=lambda: now[0],
        sleep=advance,
    ):
        pass

    assert now[0] == 2.0
    assert primitive.calls[-1][1] == primitive.LOCK_UN


def test_windows_uses_one_byte_locking_without_importing_fcntl(
    monkeypatch, tmp_path
):
    calls = []
    fake_msvcrt = SimpleNamespace(LK_NBLCK=10, LK_UNLCK=11)

    def locking(descriptor, mode, byte_count):
        calls.append((descriptor, mode, byte_count))

    fake_msvcrt.locking = locking
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    real_import = builtins.__import__

    def isolated_import(name, *args, **kwargs):
        if name == "fcntl":
            raise AssertionError("Windows lock imported fcntl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", isolated_import)

    with acquire_run_lock(
        tmp_path / "Ledger.DB", wait_seconds=0, platform="win32"
    ):
        pass

    assert [mode for _, mode, _ in calls] == [10, 11]
    assert [byte_count for _, _, byte_count in calls] == [1, 1]


def test_windows_non_contention_lock_error_is_not_misreported(
    monkeypatch, tmp_path
):
    class BrokenMsvcrt:
        LK_NBLCK = 10
        LK_UNLCK = 11

        @staticmethod
        def locking(descriptor, mode, byte_count):
            raise OSError(errno.EBADF, "bad descriptor")

    monkeypatch.setitem(sys.modules, "msvcrt", BrokenMsvcrt)

    with pytest.raises(OSError) as failure:
        with acquire_run_lock(
            tmp_path / "ledger.db", wait_seconds=0, platform="win32"
        ):
            pass

    assert failure.value.errno == errno.EBADF


def test_windows_native_access_denied_overrides_eacces_fallback(
    monkeypatch, tmp_path
):
    access_denied = OSError(errno.EACCES, "access denied")
    access_denied.winerror = 5

    class BrokenMsvcrt:
        LK_NBLCK = 10
        LK_UNLCK = 11

        @staticmethod
        def locking(descriptor, mode, byte_count):
            raise access_denied

    monkeypatch.setitem(sys.modules, "msvcrt", BrokenMsvcrt)

    with pytest.raises(OSError) as failure:
        with acquire_run_lock(
            tmp_path / "ledger.db", wait_seconds=0, platform="win32"
        ):
            pass

    assert failure.value is access_denied


def test_windows_native_lock_violation_is_contention_even_with_other_errno(
    monkeypatch, tmp_path
):
    lock_violation = OSError(errno.EBADF, "lock violation")
    lock_violation.winerror = 33

    class ContendedMsvcrt:
        LK_NBLCK = 10
        LK_UNLCK = 11

        @staticmethod
        def locking(descriptor, mode, byte_count):
            raise lock_violation

    monkeypatch.setitem(sys.modules, "msvcrt", ContendedMsvcrt)

    with pytest.raises(RunLockedError, match="^another run is active$"):
        with acquire_run_lock(
            tmp_path / "ledger.db", wait_seconds=0, platform="win32"
        ):
            pass


def test_windows_case_aliases_contend_on_the_normalized_lock_file(
    monkeypatch, tmp_path
):
    locked_files = set()

    class FakeMsvcrt:
        LK_NBLCK = 10
        LK_UNLCK = 11

        @staticmethod
        def locking(descriptor, mode, byte_count):
            assert byte_count == 1
            metadata = __import__("os").fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino)
            if mode == FakeMsvcrt.LK_UNLCK:
                locked_files.remove(identity)
            elif identity in locked_files:
                raise OSError(errno.EACCES, "locked")
            else:
                locked_files.add(identity)

    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    monkeypatch.setattr(
        "teammem.run_lock.os.path.normcase",
        lambda value: str(Path(value).with_name("normalized.db")),
    )

    with acquire_run_lock(
        tmp_path / "Ledger-Upper.DB", wait_seconds=0, platform="win32"
    ):
        with pytest.raises(RunLockedError, match="^another run is active$"):
            with acquire_run_lock(
                tmp_path / "ledger-lower.db", wait_seconds=0, platform="win32"
            ):
                pass


def test_unix_does_not_import_msvcrt(monkeypatch, tmp_path):
    primitive = FakeFcntl(failures=0)
    monkeypatch.setitem(sys.modules, "fcntl", primitive)
    real_import = builtins.__import__

    def isolated_import(name, *args, **kwargs):
        if name == "msvcrt":
            raise AssertionError("Unix lock imported msvcrt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", isolated_import)

    with acquire_run_lock(
        tmp_path / "ledger.db", wait_seconds=0, platform="linux"
    ):
        pass
