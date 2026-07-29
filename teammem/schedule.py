"""Explicit user-level scheduling for the one-shot daily hub command."""

import fcntl
import os
import plistlib
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .config import Config


LABEL = "org.teammem.hub-daily"
DEFAULT_TIME = "18:20"
SYSTEMD_SERVICE = "teammem-daily.service"
SYSTEMD_TIMER = "teammem-daily.timer"
_TIME = re.compile(r"([0-9]{2}):([0-9]{2})")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class ScheduleStatus:
    installed: bool
    time: str | None
    backend: str
    path: Path


def _backend(platform: str | None) -> str:
    current = sys.platform if platform is None else platform
    if current == "darwin":
        return "launchd"
    if current.startswith("linux"):
        return "systemd"
    raise RuntimeError(f"unsupported scheduling platform: {current}")


def _parse_time(value: str) -> tuple[int, int]:
    match = _TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("schedule time must be HH:MM")
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        raise ValueError("schedule time must be HH:MM")
    return hour, minute


def _agents_dir(path: Path | None) -> Path:
    return Path.home() / "Library" / "LaunchAgents" if path is None else Path(path)


def _systemd_dir(path: Path | None) -> Path:
    default = Path.home() / ".config" / "systemd" / "user"
    return default if path is None else Path(path)


def _launchd_path(agents_dir: Path | None) -> Path:
    return _agents_dir(agents_dir) / f"{LABEL}.plist"


def _systemd_paths(systemd_dir: Path | None) -> tuple[Path, Path]:
    directory = _systemd_dir(systemd_dir)
    return directory / SYSTEMD_SERVICE, directory / SYSTEMD_TIMER


def _run(
    runner: Runner | None,
    command: Sequence[str],
    *,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    execute = subprocess.run if runner is None else runner
    return execute(list(command), check=check, capture_output=True, text=True)


def _launchctl(
    runner: Runner | None,
    *arguments: str,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    return _run(runner, ["launchctl", *arguments], check=check)


def _systemctl(
    runner: Runner | None,
    *arguments: str,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    return _run(runner, ["systemctl", "--user", *arguments], check=check)


def _open_directory(path: Path, *, create: bool, private: bool = False) -> int | None:
    original = path.expanduser()
    if ".." in original.parts:
        raise ValueError(f"unsafe schedule directory: {path}")
    absolute = original if original.is_absolute() else Path.cwd() / original
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    os.close(descriptor)
                    return None
                try:
                    os.mkdir(component, mode=0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except OSError as failure:
                raise ValueError(f"unsafe schedule directory: {path}") from failure
            os.close(descriptor)
            descriptor = child
        if os.fstat(descriptor).st_uid != os.getuid():
            raise PermissionError(f"schedule directory is not user-owned: {path}")
        if private:
            os.fchmod(descriptor, 0o700)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


@contextmanager
def _locked_directory(path: Path, write: bool):
    directory_fd = _open_directory(path, create=write)
    if directory_fd is None:
        yield None
        return
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        yield directory_fd
    finally:
        os.close(directory_fd)


def _read_definition(directory_fd: int, name: str) -> bytes | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as failure:
        message = f"unsafe or symlinked schedule definition: {name}"
        raise ValueError(message) from failure
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"unsafe or symlinked schedule definition: {name}")
    except Exception:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read()


def _write_atomic(directory_fd: int, name: str, data: bytes) -> None:
    temporary = f".{name}.{secrets.token_hex(8)}"
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
            0o600, dir_fd=directory_fd
        )
        try:
            os.fchmod(descriptor, 0o600)
        except Exception:
            os.close(descriptor)
            raise
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
        )
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _unlink_durable(directory_fd: int, name: str) -> bool:
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return False
    os.fsync(directory_fd)
    return True


def _executable(value: str | None) -> str:
    command = value or shutil.which("teammem")
    if not command:
        raise RuntimeError("teammem executable is not on PATH")
    return command


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchd_target() -> str:
    return f"{_launchd_domain()}/{LABEL}"


def _launchd_loaded(runner: Runner | None) -> bool:
    result = _launchctl(runner, "print", _launchd_target(), check=False)
    return result.returncode == 0


def _install_launchd(
    cfg: Config,
    hour: int,
    minute: int,
    executable: str,
    agents_dir: Path | None,
    runner: Runner | None,
) -> Path:
    path = _launchd_path(agents_dir)
    state_dir = Path.home() / ".local" / "state" / "teammem"
    if agents_dir is None:
        state_fd = _open_directory(state_dir, create=True, private=True)
        assert state_fd is not None
        os.close(state_fd)
    with _locked_directory(path.parent, True) as directory_fd:
        assert directory_fd is not None
        previous = _read_definition(directory_fd, path.name)
        payload = {
            "Label": LABEL,
            "ProgramArguments": [
                executable, "--env-file", str(cfg.env_file), "run-daily"
            ],
            "RunAtLoad": False,
            "StandardErrorPath": str(state_dir / "schedule.err"),
            "StandardOutPath": str(state_dir / "schedule.log"),
            "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        }
        definition = plistlib.dumps(payload, sort_keys=True)
        was_loaded = _launchd_loaded(runner)
        if was_loaded and previous is None:
            raise RuntimeError(
                "cannot replace loaded launchd schedule without its definition"
            )
        if was_loaded:
            _launchctl(runner, "bootout", _launchd_target(), check=True)
        try:
            _write_atomic(directory_fd, path.name, definition)
            _launchctl(runner, "bootstrap", _launchd_domain(), str(path), check=True)
        except Exception as failure:
            try:
                if previous is None:
                    _unlink_durable(directory_fd, path.name)
                else:
                    _write_atomic(directory_fd, path.name, previous)
                if was_loaded:
                    _launchctl(
                        runner, "bootstrap", _launchd_domain(), str(path),
                        check=True
                    )
            except Exception as rollback_failure:
                raise RuntimeError(
                    "launchd schedule installation failed and rollback failed"
                ) from rollback_failure
            restored = "previous schedule" if previous is not None else "previous state"
            raise RuntimeError(
                f"launchd schedule installation failed; {restored} restored"
            ) from failure
    return path


def _systemd_exec(arguments: Sequence[str]) -> str:
    return shlex.join([argument.replace("%", "%%") for argument in arguments])


def _systemd_state(runner: Runner | None) -> tuple[bool, bool]:
    return (
        _systemctl(runner, "is-enabled", SYSTEMD_TIMER, check=False).returncode == 0,
        _systemctl(runner, "is-active", SYSTEMD_TIMER, check=False).returncode == 0,
    )


def _restore_systemd_state(runner: Runner | None, state: tuple[bool, bool]) -> None:
    actions = (("enable", "disable"), ("start", "stop"))
    for current, (yes, no) in zip(state, actions):
        _systemctl(runner, yes if current else no, SYSTEMD_TIMER, check=True)
    if _systemd_state(runner) != state:
        raise RuntimeError("restored systemd manager state could not be verified")


def _reload_systemd(runner: Runner | None) -> None:
    _systemctl(runner, "daemon-reload", check=True)


def _snapshot_files(directory_fd: int, names: Sequence[str]) -> dict[str, bytes | None]:
    return {name: _read_definition(directory_fd, name) for name in names}


def _restore_files(directory_fd: int, snapshot: dict[str, bytes | None]) -> None:
    for name, previous in snapshot.items():
        if previous is None:
            _unlink_durable(directory_fd, name)
        else:
            _write_atomic(directory_fd, name, previous)


def _install_systemd(
    cfg: Config,
    hour: int,
    minute: int,
    executable: str,
    systemd_dir: Path | None,
    runner: Runner | None,
) -> Path:
    service_path, timer_path = _systemd_paths(systemd_dir)
    command = _systemd_exec(
        [executable, "--env-file", str(cfg.env_file), "run-daily"]
    )
    service = f"""[Unit]
Description=Run Team Memory Agent daily workflow

[Service]
Type=oneshot
ExecStart={command}
"""
    timer = f"""[Unit]
Description=Run Team Memory Agent daily

[Timer]
OnCalendar=*-*-* {hour:02d}:{minute:02d}:00
Persistent=true
Unit={SYSTEMD_SERVICE}

[Install]
WantedBy=timers.target
"""
    with _locked_directory(service_path.parent, True) as directory_fd:
        assert directory_fd is not None
        previous = _snapshot_files(directory_fd, [SYSTEMD_SERVICE, SYSTEMD_TIMER])
        previous_state = _systemd_state(runner)
        try:
            _write_atomic(directory_fd, SYSTEMD_SERVICE, service.encode())
            _write_atomic(directory_fd, SYSTEMD_TIMER, timer.encode())
            _reload_systemd(runner)
            _systemctl(runner, "enable", "--now", SYSTEMD_TIMER, check=True)
        except Exception as failure:
            try:
                _restore_files(directory_fd, previous)
                _reload_systemd(runner)
                _restore_systemd_state(runner, previous_state)
            except Exception as rollback_failure:
                raise RuntimeError(
                    "systemd schedule installation failed and rollback failed"
                ) from rollback_failure
            message = "systemd schedule installation failed; previous state restored"
            raise RuntimeError(message) from failure
    return timer_path


def install_schedule(
    cfg: Config,
    time: str = DEFAULT_TIME,
    platform: str | None = None,
    executable: str | None = None,
    agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    runner: Runner | None = None,
) -> Path:
    """Install or replace the explicit user schedule for ``run-daily``."""
    hour, minute = _parse_time(time)
    backend = _backend(platform)
    command = _executable(executable)
    if backend == "launchd":
        return _install_launchd(cfg, hour, minute, command, agents_dir, runner)
    return _install_systemd(cfg, hour, minute, command, systemd_dir, runner)


def _parse_launchd(definition: bytes | None) -> tuple[bool, str | None]:
    if definition is None:
        return False, None
    try:
        data = plistlib.loads(definition)
        if definition != plistlib.dumps(data, sort_keys=True):
            return False, None
        if not isinstance(data, dict):
            return False, None
        expected_keys = {
            "Label",
            "ProgramArguments",
            "RunAtLoad",
            "StandardErrorPath",
            "StandardOutPath",
            "StartCalendarInterval",
        }
        if set(data) != expected_keys:
            return False, None
        interval = data["StartCalendarInterval"]
        if not isinstance(interval, dict) or set(interval) != {"Hour", "Minute"}:
            return False, None
        hour = interval["Hour"]
        minute = interval["Minute"]
        if (
            type(hour) is not int
            or type(minute) is not int
            or not 0 <= hour <= 23
            or not 0 <= minute <= 59
        ):
            return False, None
        time = f"{hour:02d}:{minute:02d}"
        arguments = data["ProgramArguments"]
        valid = (
            data["Label"] == LABEL
            and data["RunAtLoad"] is False
            and isinstance(arguments, list)
            and len(arguments) == 4
            and isinstance(arguments[0], str)
            and bool(arguments[0])
            and Path(arguments[0]).is_absolute()
            and arguments[1] == "--env-file"
            and isinstance(arguments[2], str)
            and bool(arguments[2])
            and Path(arguments[2]).is_absolute()
            and arguments[3] == "run-daily"
            and data["StandardOutPath"]
            == str(Path.home() / ".local" / "state" / "teammem" / "schedule.log")
            and data["StandardErrorPath"]
            == str(Path.home() / ".local" / "state" / "teammem" / "schedule.err")
        )
        return valid, time
    except (KeyError, TypeError, ValueError):
        return False, None


def _parse_unit(
    definition: bytes | None,
) -> dict[str, dict[str, str]] | None:
    if definition is None:
        return None
    try:
        text = definition.decode()
    except UnicodeDecodeError:
        return None
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            if not current or current in sections:
                return None
            sections[current] = {}
            continue
        if current is None or "=" not in line:
            return None
        key, value = line.split("=", 1)
        if not key or key in sections[current]:
            return None
        sections[current][key] = value
    return sections


def _parse_systemd_exec(command: str | None) -> list[str]:
    try:
        raw_arguments = shlex.split(command) if command is not None else []
    except ValueError:
        return []
    if any("%" in argument.replace("%%", "") for argument in raw_arguments):
        return []
    arguments = [argument.replace("%%", "%") for argument in raw_arguments]
    return arguments if command == _systemd_exec(arguments) else []


def _parse_systemd(
    service_definition: bytes | None, timer_definition: bytes | None
) -> tuple[bool, str | None]:
    service = _parse_unit(service_definition)
    timer = _parse_unit(timer_definition)
    calendar = timer.get("Timer", {}).get("OnCalendar") if timer else None
    match = re.fullmatch(
        r"\*-\*-\* ([0-9]{2}):([0-9]{2}):00", calendar or ""
    )
    time = None
    if match is not None:
        hour, minute = (int(part) for part in match.groups())
        if hour <= 23 and minute <= 59:
            time = f"{hour:02d}:{minute:02d}"
    command = service.get("Service", {}).get("ExecStart") if service else None
    arguments = _parse_systemd_exec(command)
    expected_service = {
        "Unit": {"Description": "Run Team Memory Agent daily workflow"},
        "Service": {"Type": "oneshot", "ExecStart": command},
    }
    expected_timer = {
        "Unit": {"Description": "Run Team Memory Agent daily"},
        "Timer": {
            "OnCalendar": calendar,
            "Persistent": "true",
            "Unit": SYSTEMD_SERVICE,
        },
        "Install": {"WantedBy": "timers.target"},
    }
    valid = (
        service == expected_service
        and timer == expected_timer
        and len(arguments) == 4
        and bool(arguments[0])
        and Path(arguments[0]).is_absolute()
        and arguments[1] == "--env-file"
        and bool(arguments[2])
        and Path(arguments[2]).is_absolute()
        and arguments[3] == "run-daily"
        and time is not None
    )
    return valid, time


def schedule_status(
    platform: str | None = None,
    agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    runner: Runner | None = None,
) -> ScheduleStatus:
    """Read status without creating artifacts or changing scheduler state."""
    backend = _backend(platform)
    if backend == "launchd":
        path = _launchd_path(agents_dir)
        with _locked_directory(path.parent, False) as directory_fd:
            if directory_fd is None:
                return ScheduleStatus(False, None, backend, path)
            valid, time = _parse_launchd(_read_definition(directory_fd, path.name))
            loaded = _launchd_loaded(runner) if valid else False
            return ScheduleStatus(valid and loaded, time, backend, path)

    service_path, timer_path = _systemd_paths(systemd_dir)
    with _locked_directory(service_path.parent, False) as directory_fd:
        if directory_fd is None:
            return ScheduleStatus(False, None, backend, timer_path)
        snapshot = _snapshot_files(directory_fd, [SYSTEMD_SERVICE, SYSTEMD_TIMER])
        valid, time = _parse_systemd(
            snapshot[SYSTEMD_SERVICE], snapshot[SYSTEMD_TIMER]
        )
        state = _systemd_state(runner) if valid else (False, False)
        return ScheduleStatus(valid and all(state), time, backend, timer_path)


def remove_schedule(
    platform: str | None = None,
    agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    runner: Runner | None = None,
) -> bool:
    """Remove an installed schedule, returning false when none exists."""
    backend = _backend(platform)
    if backend == "launchd":
        path = _launchd_path(agents_dir)
        with _locked_directory(path.parent, True) as directory_fd:
            assert directory_fd is not None
            previous = _read_definition(directory_fd, path.name)
            was_loaded = _launchd_loaded(runner)
            if previous is None:
                if not was_loaded:
                    return False
                _launchctl(runner, "bootout", _launchd_target(), check=True)
                return True
            try:
                if was_loaded:
                    _launchctl(runner, "bootout", _launchd_target(), check=True)
                _unlink_durable(directory_fd, path.name)
            except Exception as failure:
                try:
                    _write_atomic(directory_fd, path.name, previous)
                    if was_loaded:
                        _launchctl(
                            runner,
                            "bootstrap",
                            _launchd_domain(),
                            str(path),
                            check=True,
                        )
                except Exception as rollback_failure:
                    rollback_failure.__context__ = failure
                    raise RuntimeError(
                        "launchd schedule removal failed and rollback failed"
                    ) from rollback_failure
                raise RuntimeError(
                    "launchd schedule removal failed; previous state restored"
                ) from failure
            return True

    service_path, timer_path = _systemd_paths(systemd_dir)
    with _locked_directory(service_path.parent, True) as directory_fd:
        assert directory_fd is not None
        previous = _snapshot_files(directory_fd, [SYSTEMD_SERVICE, SYSTEMD_TIMER])
        previous_state = _systemd_state(runner)
        if all(value is None for value in previous.values()) and not any(previous_state):
            return False
        try:
            enabled, active = previous_state
            if enabled:
                _systemctl(runner, "disable", SYSTEMD_TIMER, check=True)
            if active:
                _systemctl(runner, "stop", SYSTEMD_TIMER, check=True)
            _unlink_durable(directory_fd, SYSTEMD_TIMER)
            _unlink_durable(directory_fd, SYSTEMD_SERVICE)
            _reload_systemd(runner)
        except Exception as failure:
            try:
                _restore_files(directory_fd, previous)
                _reload_systemd(runner)
                _restore_systemd_state(runner, previous_state)
            except Exception as rollback_failure:
                raise RuntimeError(
                    "systemd schedule removal failed and rollback failed"
                ) from rollback_failure
            message = "systemd schedule removal failed; previous state restored"
            raise RuntimeError(message) from failure
        return True
