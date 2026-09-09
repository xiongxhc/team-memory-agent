"""Cross-platform locks for ledger runs and shared vault publication."""

import errno
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO


class RunLockedError(RuntimeError):
    """Raised when another process owns the ledger run lock."""


_publication_owners = threading.local()


@contextmanager
def acquire_publication_lock(
    vault_dir: Path,
    *,
    wait_seconds: float = 1800,
    on_wait: Callable[[str], None] | None = None,
    platform: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    """Serialize writers to a canonical vault, from snapshot through receipt.

    The adjacent lock stays outside published content. The parent must be writable;
    create it for first-run vaults. Only this process/thread may reenter a held
    vault lock. Callers needing both locks acquire the ledger run lock first.
    """
    current_platform = sys.platform if platform is None else platform
    lock_path = _lock_path(vault_dir, current_platform)
    key = (os.getpid(), lock_path)
    held = getattr(_publication_owners, "held", None)
    if held is None:
        held = _publication_owners.held = set()
    if key in held:
        yield
        return
    with ExitStack() as stack:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            stack.enter_context(acquire_run_lock(
                vault_dir, wait_seconds=wait_seconds, on_wait=on_wait,
                platform=current_platform, monotonic=monotonic, sleep=sleep))
        except (OSError, RunLockedError) as error:
            # Lock diagnostics must not expose configured filesystem paths.
            raise RunLockedError("publication lock unavailable; retry required") from error
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)


def _lock_path(ledger_path: Path, platform: str) -> Path:
    canonical = os.path.realpath(os.fspath(ledger_path))
    if platform == "win32":
        canonical = os.path.normcase(canonical)
    return Path(f"{canonical}.lock")


def _try_lock(handle: BinaryIO, platform: str) -> bool:
    handle.seek(0)
    try:
        if platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        contention_errnos = {errno.EACCES, errno.EAGAIN}
        windows_contention_codes = {32, 33}
        native_code = getattr(error, "winerror", None)
        if platform == "win32" and native_code is not None:
            if native_code in windows_contention_codes:
                return False
            raise
        if error.errno in contention_errnos:
            return False
        raise
    return True


def _unlock(handle: BinaryIO, platform: str) -> None:
    handle.seek(0)
    if platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def acquire_run_lock(
    ledger_path: Path,
    *,
    wait_seconds: float,
    on_wait: Callable[[str], None] | None = None,
    platform: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]:
    """Hold the lock adjacent to the canonical ledger path for this context."""
    current_platform = sys.platform if platform is None else platform
    deadline = monotonic() + max(0.0, wait_seconds)
    with _lock_path(ledger_path, current_platform).open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()

        acquired = False
        while not acquired:
            acquired = _try_lock(handle, current_platform)
            if acquired:
                break
            now = monotonic()
            if now >= deadline:
                raise RunLockedError("another run is active")
            if on_wait is not None:
                on_wait("waiting for active run")
            sleep(min(1.0, deadline - now))

        try:
            yield
        finally:
            _unlock(handle, current_platform)
