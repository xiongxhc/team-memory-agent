"""Unix deadline delivery for the existing meeting retention service.

Known deadlines determine waits. A bounded recovery check covers missed commit
wakes and wall-clock changes; it is not permission to extend a stored expiry.
"""

import errno
import logging
import os
import select
import signal
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


log = logging.getLogger(__name__)


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino


def _directory(db_path: Path, *, create: bool) -> int:
    path = Path(str(db_path.resolve()) + ".retention")
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        metadata = os.fstat(fd)
        if (metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700
                or _identity(metadata) != _identity(path.lstat())):
            raise RuntimeError("retention channel directory is not private and owned")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _fifo(directory_fd: int, flags: int) -> int:
    # Validate before opening: even O_NONBLOCK on an arbitrary device is not a
    # safe notification operation. Recheck the held inode against the name.
    expected = os.stat("wake", dir_fd=directory_fd, follow_symlinks=False)
    if (not stat.S_ISFIFO(expected.st_mode) or expected.st_uid != os.getuid()
            or stat.S_IMODE(expected.st_mode) != 0o600):
        raise RuntimeError("retention wake channel is not a private owned FIFO")
    fd = os.open("wake", flags | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=directory_fd)
    try:
        actual = os.fstat(fd)
        if (not stat.S_ISFIFO(actual.st_mode) or _identity(actual) != _identity(expected)
                or _identity(actual) != _identity(os.stat("wake", dir_fd=directory_fd, follow_symlinks=False))):
            raise RuntimeError("retention wake channel changed")
        return fd
    except BaseException:
        os.close(fd)
        raise


def notify_committed_import(conn) -> None:
    """Best effort only: never create state, block, or expose meeting data."""
    if os.name != "posix":
        return
    directory_fd = fifo_fd = None
    try:
        database = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
        if not database:
            return
        directory_fd = _directory(Path(database), create=False)
        fifo_fd = _fifo(directory_fd, os.O_WRONLY)
        os.write(fifo_fd, b"w")
    except (FileNotFoundError, BlockingIOError):
        pass  # absent worker or full coalesced channel
    except OSError as error:
        if error.errno != errno.ENXIO:  # worker absent, no reader
            log.warning("retention notification unavailable; recovery required")
    except Exception:
        log.warning("retention notification unavailable; recovery required")
    finally:
        if fifo_fd is not None:
            os.close(fifo_fd)
        if directory_fd is not None:
            os.close(directory_fd)


@dataclass(frozen=True)
class WakeChannel:
    read_fd: int
    write_fd: int

    def drain(self) -> None:
        # One bounded batch prevents a continuous importer from starving expiry.
        try:
            os.read(self.read_fd, 65536)
        except BlockingIOError:
            pass

    def wake(self) -> None:
        try:
            os.write(self.write_fd, b"w")
        except BlockingIOError:
            pass


@contextmanager
def retention_channel(db_path: Path):
    """A distinct singleton, with descriptor-owned wake and cleanup identity.

    Retain the private directory after exit: its inode is the lock identity.
    Never delete/recreate a lock directory while another worker could acquire it.
    """
    import fcntl

    directory_fd = _directory(db_path, create=True)
    read_fd = write_fd = owned = None
    try:
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another retention worker is active") from error
        try:
            os.mkfifo("wake", 0o600, dir_fd=directory_fd)
        except FileExistsError:
            pass
        read_fd = _fifo(directory_fd, os.O_RDONLY)
        owned = _identity(os.fstat(read_fd))
        write_fd = _fifo(directory_fd, os.O_WRONLY)
        yield WakeChannel(read_fd, write_fd)
    finally:
        for fd in (write_fd, read_fd):
            if fd is not None:
                os.close(fd)
        try:
            if owned is not None and _identity(os.stat("wake", dir_fd=directory_fd, follow_symlinks=False)) == owned:
                os.unlink("wake", dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory_fd)


def _wait(fd: int, timeout: float) -> None:
    select.select([fd], [], [], timeout)


def watch_meeting_audio(
    cfg, *, clock=lambda: datetime.now(timezone.utc), wait=_wait,
    recovery_seconds: float = 60.0, retry_seconds: float = 5.0,
) -> int:
    """Run immediately, on committed imports, and at exact stored boundaries.

    Close all database connections before waiting. Positive retry/recovery waits
    do not postpone an earlier healthy deadline. Suspension, blocked filesystems
    and the commit-to-notify crash gap can cause late recovery, which is reported.
    """
    from . import meeting_store
    from .services import _run_expire_meeting_audio, redact_secrets
    from .store import open_db

    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        print("error: --watch requires Linux or macOS", file=sys.stderr)
        return 2
    if recovery_seconds <= 0 or retry_seconds <= 0:
        raise ValueError("retention recovery and retry intervals must be positive")
    stopping = False

    try:
        with retention_channel(cfg.db_path) as channel:
            previous = {}

            def shutdown(signum, frame):
                nonlocal stopping
                stopping = True
                channel.wake()

            try:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    previous[sig] = signal.signal(sig, shutdown)
                while not stopping:
                    channel.drain()  # before queries, never clear a newer commit
                    timeout = recovery_seconds
                    upcoming = None
                    try:
                        conn = open_db(cfg.db_path)
                        try:
                            started = clock()
                            upcoming, overdue = meeting_store.retention_deadlines(
                                conn, after=started, warn_days=cfg.meeting_warn_days)
                            if overdue is not None and overdue < started:
                                print(f"retention late/catch-up: oldest obligation overdue by "
                                      f"{(started - overdue).total_seconds():.6f}s; expiry unchanged", file=sys.stderr)
                            started = clock()
                            result, _ = _run_expire_meeting_audio(
                                cfg, now=started.isoformat(), conn=conn)
                            upcoming, overdue = meeting_store.retention_deadlines(
                                conn, after=started, warn_days=cfg.meeting_warn_days)
                        finally:
                            conn.close()
                        if result is None or result.failed or overdue is not None:
                            timeout = min(timeout, retry_seconds)
                    except Exception as error:
                        print(f"retention worker failed; retry required: {redact_secrets(error, cfg)}", file=sys.stderr)
                        timeout = min(timeout, retry_seconds)
                    if upcoming is not None:
                        timeout = min(timeout, max(0.0, (upcoming - clock()).total_seconds()))
                    if not stopping:
                        wait(channel.read_fd, timeout)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    except (OSError, RuntimeError) as error:
        print(f"retention worker unavailable: {redact_secrets(error, cfg)}", file=sys.stderr)
        return 2
    return 0
