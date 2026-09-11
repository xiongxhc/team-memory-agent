"""macOS launchd and Linux systemd scheduling backend."""

import fcntl
import os
import plistlib
import re
import secrets
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Sequence

from .config import Config
from .schedule import ScheduleStatus


LABEL = "org.teammem.hub-daily"
SYSTEMD_SERVICE = "teammem-daily.service"
SYSTEMD_TIMER = "teammem-daily.timer"

# Meetings are scheduled beside the daily job, never inside it: publication is a
# minutes-scale loop and the daily run is a once-a-day synthesis. Both backends
# generate them, because `schedule_unix` serves macOS and Linux alike and a
# meeting hub on macOS would otherwise be silently unscheduled.
MEETINGS_LABEL = "org.teammem.meetings"
RETENTION_LABEL = "org.teammem.meeting-retention"
SYSTEMD_MEETINGS_SERVICE = "teammem-meetings.service"
SYSTEMD_MEETINGS_TIMER = "teammem-meetings.timer"
SYSTEMD_RETENTION_SERVICE = "teammem-meeting-retention.service"
SYSTEMD_RETENTION_TIMER = "teammem-meeting-retention.timer"
SYSTEMD_MEETING_UNITS = (
    SYSTEMD_MEETINGS_SERVICE,
    SYSTEMD_MEETINGS_TIMER,
    SYSTEMD_RETENTION_SERVICE,
    SYSTEMD_RETENTION_TIMER,
)
SYSTEMD_MEETING_TIMERS = (SYSTEMD_MEETINGS_TIMER, SYSTEMD_RETENTION_TIMER)
MEETING_LABELS = (MEETINGS_LABEL, RETENTION_LABEL)
# Leave processing and publication room within the initial ten-minute target.
# This polling interval alone is not an end-to-end latency guarantee.
MEETING_INTERVAL_MINUTES = 1
# Daily recovery supports the resident worker; stored deadlines drive deletion.
RETENTION_HOUR_OFFSET = 1
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC

Runner = Callable[..., subprocess.CompletedProcess[str]]


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


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchd_target(label: str = LABEL) -> str:
    return f"{_launchd_domain()}/{label}"


def _launchd_loaded(runner: Runner | None, label: str = LABEL) -> bool:
    result = _launchctl(runner, "print", _launchd_target(label), check=False)
    return result.returncode == 0


def _retention_hour(hour: int) -> int:
    return (hour + RETENTION_HOUR_OFFSET) % 24


def _launchd_jobs(
    cfg: Config, hour: int, minute: int, executable: str, state_dir: Path
) -> list[tuple[str, bytes]]:
    """The launchd definitions this configuration asks for, daily first.

    The daily payload is spelled exactly as it always was; the meeting jobs are
    additions beside it, each with its own log pair so one job's output never
    explains another's.
    """
    def arguments(command: str) -> list[str]:
        return [executable, "--env-file", str(cfg.env_file), command]

    def payload(label: str, command: str, log: str, schedule: dict) -> dict:
        return {
            "Label": label,
            "ProgramArguments": arguments(command),
            "RunAtLoad": False,
            "StandardErrorPath": str(state_dir / f"{log}.err"),
            "StandardOutPath": str(state_dir / f"{log}.log"),
            **schedule,
        }

    jobs = [(LABEL, payload(
        LABEL, "run-daily", "schedule",
        {"StartCalendarInterval": {"Hour": hour, "Minute": minute}}))]
    if cfg.meetings_enabled:
        jobs.append((MEETINGS_LABEL, payload(
            MEETINGS_LABEL, "process-meetings", "schedule-meetings",
            {"StartInterval": MEETING_INTERVAL_MINUTES * 60})))
        jobs.append((RETENTION_LABEL, payload(
            RETENTION_LABEL, "expire-meeting-audio", "schedule-meeting-retention",
            {"ProgramArguments": arguments("expire-meeting-audio") + ["--watch"],
             "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 5})))
    return [(label, plistlib.dumps(data, sort_keys=True)) for label, data in jobs]


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
    jobs = _launchd_jobs(cfg, hour, minute, executable, state_dir)
    wanted = {label for label, _ in jobs}
    with _locked_directory(path.parent, True) as directory_fd:
        assert directory_fd is not None
        names = [f"{label}.plist" for label in (LABEL, *MEETING_LABELS)]
        previous = _snapshot_files(directory_fd, names)
        # A meeting job that this configuration no longer wants but that a
        # previous install left behind is removed here: a timer still firing a
        # command the hub now refuses is worse than no timer.
        stale = [label for label in MEETING_LABELS
                 if label not in wanted and previous[f"{label}.plist"] is not None]
        loaded: dict[str, bool] = {}
        for label in [label for label, _ in jobs] + stale:
            was_loaded = _launchd_loaded(runner, label)
            loaded[label] = was_loaded
            if was_loaded and previous[f"{label}.plist"] is None:
                raise RuntimeError(
                    "cannot replace loaded launchd schedule without its definition"
                )
        stopped, started = [], []
        try:
            for label, was_loaded in loaded.items():
                if was_loaded:
                    _launchctl(runner, "bootout", _launchd_target(label), check=True)
                    stopped.append(label)
            for label, definition in jobs:
                name = f"{label}.plist"
                _write_atomic(directory_fd, name, definition)
                _launchctl(runner, "bootstrap", _launchd_domain(),
                           str(path.parent / name), check=True)
                started.append(label)
            for label in stale:
                _unlink_durable(directory_fd, f"{label}.plist")
        except Exception as failure:
            # Preserve the old first-bootout failure contract: no transition
            # occurred and no definition was touched.
            if not stopped and any(loaded.values()) and not started:
                raise
            try:
                for label in reversed(started):
                    _launchctl(runner, "bootout", _launchd_target(label), check=True)
                _restore_files(directory_fd, previous)
                for label in stopped:
                    _launchctl(
                        runner, "bootstrap", _launchd_domain(),
                        str(path.parent / f"{label}.plist"), check=True
                    )
            except Exception as rollback_failure:
                raise RuntimeError(
                    "launchd schedule installation failed and rollback failed"
                ) from rollback_failure
            restored = ("previous schedule" if previous[path.name] is not None
                        else "previous state")
            raise RuntimeError(
                f"launchd schedule installation failed; {restored} restored"
            ) from failure
    return path


def _systemd_exec(arguments: Sequence[str]) -> str:
    return " ".join(_systemd_quote(argument) for argument in arguments)


def _systemd_quote(argument: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in argument):
        raise ValueError("unsafe systemd argument")
    escaped = (
        argument.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "$$")
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def _timer_state(runner: Runner | None, timer: str) -> tuple[bool, bool]:
    return (
        _systemctl(runner, "is-enabled", timer, check=False).returncode == 0,
        _systemctl(runner, "is-active", timer, check=False).returncode == 0,
    )


def _restore_timer_state(
    runner: Runner | None, timer: str, state: tuple[bool, bool]
) -> None:
    actions = (("enable", "disable"), ("start", "stop"))
    for current, (yes, no) in zip(state, actions):
        _systemctl(runner, yes if current else no, timer, check=True)
    if _timer_state(runner, timer) != state:
        raise RuntimeError("restored systemd manager state could not be verified")


def _systemd_state(runner: Runner | None) -> tuple[bool, bool]:
    return _timer_state(runner, SYSTEMD_TIMER)


def _restore_systemd_state(runner: Runner | None, state: tuple[bool, bool]) -> None:
    _restore_timer_state(runner, SYSTEMD_TIMER, state)


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


def _meeting_unit_text(
    cfg: Config, hour: int, minute: int, executable: str
) -> dict[str, bytes]:
    """The four meeting units, or nothing when the hub processes no meetings."""
    if not cfg.meetings_enabled:
        return {}

    def service(description: str, command: str, *, watch: bool = False) -> bytes:
        settings = "Type=simple\nRestart=always\nRestartSec=5" if watch else "Type=oneshot"
        arguments = [executable, "--env-file", str(cfg.env_file), command]
        if watch:
            arguments.append("--watch")
        return f"""[Unit]
Description={description}

[Service]
{settings}
ExecStart={_systemd_exec(arguments)}
""".encode()

    def timer(description: str, calendar: str, unit: str, *, bootstrap: bool = False) -> bytes:
        wake = "OnActiveSec=1s\nAccuracySec=1us\n" if bootstrap else ""
        return f"""[Unit]
Description={description}

[Timer]
{wake}OnCalendar={calendar}
Persistent=true
Unit={unit}

[Install]
WantedBy=timers.target
""".encode()

    publish = "Publish Team Memory Agent meeting notes"
    expire = "Expire Team Memory Agent meeting audio"
    return {
        SYSTEMD_MEETINGS_SERVICE: service(publish, "process-meetings"),
        SYSTEMD_MEETINGS_TIMER: timer(
            publish, f"*-*-* *:00/{MEETING_INTERVAL_MINUTES:02d}:00",
            SYSTEMD_MEETINGS_SERVICE),
        SYSTEMD_RETENTION_SERVICE: service(expire, "expire-meeting-audio", watch=True),
        SYSTEMD_RETENTION_TIMER: timer(
            expire, f"*-*-* {_retention_hour(hour):02d}:{minute:02d}:00",
            SYSTEMD_RETENTION_SERVICE, bootstrap=True),
    }


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
    meetings = _meeting_unit_text(cfg, hour, minute, executable)
    with _locked_directory(service_path.parent, True) as directory_fd:
        assert directory_fd is not None
        previous = _snapshot_files(
            directory_fd, [SYSTEMD_SERVICE, SYSTEMD_TIMER, *SYSTEMD_MEETING_UNITS])
        previous_state = _systemd_state(runner)
        # Meeting timer state is read only when there is a meeting unit to write
        # or an abandoned one to take away, so a hub without meetings issues
        # exactly the systemctl calls it always did.
        stale = [name for name in SYSTEMD_MEETING_UNITS
                 if not meetings and previous[name] is not None]
        meeting_state = (
            {name: _timer_state(runner, name)
             for name in (*SYSTEMD_MEETING_TIMERS, SYSTEMD_RETENTION_SERVICE)}
            if meetings or stale else {}
        )
        try:
            # Stop the trigger first, so it cannot revive the old worker while
            # definitions are being replaced or the feature is disabled.
            for name in (SYSTEMD_RETENTION_TIMER, SYSTEMD_RETENTION_SERVICE):
                if name in meeting_state and (previous[name] is not None or any(meeting_state[name])):
                    _systemctl(runner, "stop", name, check=True)
            _write_atomic(directory_fd, SYSTEMD_SERVICE, service.encode())
            _write_atomic(directory_fd, SYSTEMD_TIMER, timer.encode())
            for name, definition in meetings.items():
                _write_atomic(directory_fd, name, definition)
            for name in SYSTEMD_MEETING_TIMERS:
                if name in stale:
                    _systemctl(runner, "disable", "--now", name, check=True)
            if SYSTEMD_RETENTION_SERVICE in stale and meeting_state[SYSTEMD_RETENTION_SERVICE][0]:
                _systemctl(runner, "disable", SYSTEMD_RETENTION_SERVICE, check=True)
            for name in stale:
                _unlink_durable(directory_fd, name)
            _reload_systemd(runner)
            _systemctl(runner, "enable", "--now", SYSTEMD_TIMER, check=True)
            if meetings:
                _systemctl(runner, "restart", SYSTEMD_RETENTION_SERVICE, check=True)
            for name in SYSTEMD_MEETING_TIMERS:
                if name in meetings:
                    _systemctl(runner, "enable", "--now", name, check=True)
        except Exception as failure:
            try:
                for name in (SYSTEMD_RETENTION_TIMER, SYSTEMD_RETENTION_SERVICE):
                    if name in meeting_state and _read_definition(directory_fd, name) is not None:
                        _systemctl(runner, "stop", name, check=True)
                # Disable newly introduced units while their definitions still
                # exist; systemctl can refuse a disable after daemon-reload has
                # forgotten an unlinked unit.
                for name, state in meeting_state.items():
                    if previous[name] is None and not state[0] and _read_definition(directory_fd, name) is not None:
                        _systemctl(runner, "disable", "--now", name, check=True)
                _restore_files(directory_fd, previous)
                _reload_systemd(runner)
                _restore_systemd_state(runner, previous_state)
                # Restore the service before rearming its timer.
                for name, state in reversed(tuple(meeting_state.items())):
                    if previous[name] is None and not any(state):
                        if _timer_state(runner, name) != state:
                            raise RuntimeError("removed systemd worker state could not be verified")
                    else:
                        _restore_timer_state(runner, name, state)
            except Exception as rollback_failure:
                raise RuntimeError(
                    "systemd schedule installation failed and rollback failed"
                ) from rollback_failure
            message = "systemd schedule installation failed; previous state restored"
            raise RuntimeError(message) from failure
    return timer_path


def install_schedule(
    cfg: Config,
    hour: int,
    minute: int,
    executable: str,
    *,
    backend: str,
    agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    runner: Runner | None = None,
) -> Path:
    if backend == "launchd":
        return _install_launchd(cfg, hour, minute, executable, agents_dir, runner)
    if backend == "systemd":
        return _install_systemd(cfg, hour, minute, executable, systemd_dir, runner)
    raise ValueError(f"unsupported Unix scheduling backend: {backend}")


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
    except (KeyError, OverflowError, TypeError, ValueError):
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
    if command is None:
        return []
    arguments: list[str] = []
    position = 0
    while position < len(command):
        if command[position] != '"':
            return []
        position += 1
        argument: list[str] = []
        while position < len(command) and command[position] != '"':
            character = command[position]
            if character == "\\":
                position += 1
                if position == len(command) or command[position] not in '\\"':
                    return []
                argument.append(command[position])
            elif character in "$%":
                if position + 1 == len(command) or command[position + 1] != character:
                    return []
                argument.append(character)
                position += 1
            else:
                argument.append(character)
            position += 1
        if position == len(command):
            return []
        arguments.append("".join(argument))
        position += 1
        if position < len(command):
            if command[position] != " ":
                return []
            position += 1
    try:
        canonical = _systemd_exec(arguments)
    except ValueError:
        return []
    return arguments if command == canonical else []


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
    *,
    backend: str,
    agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    runner: Runner | None = None,
) -> ScheduleStatus:
    if backend == "launchd":
        path = _launchd_path(agents_dir)
        with _locked_directory(path.parent, False) as directory_fd:
            if directory_fd is None:
                return ScheduleStatus(False, None, backend, path)
            valid, time = _parse_launchd(_read_definition(directory_fd, path.name))
            loaded = _launchd_loaded(runner) if valid else False
            return ScheduleStatus(valid and loaded, time, backend, path)

    if backend != "systemd":
        raise ValueError(f"unsupported Unix scheduling backend: {backend}")
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
    *,
    backend: str,
    agents_dir: Path | None = None,
    systemd_dir: Path | None = None,
    runner: Runner | None = None,
) -> bool:
    if backend == "launchd":
        path = _launchd_path(agents_dir)
        with _locked_directory(path.parent, True) as directory_fd:
            assert directory_fd is not None
            names = [f"{label}.plist" for label in (LABEL, *MEETING_LABELS)]
            snapshot = _snapshot_files(directory_fd, names)
            previous = snapshot[path.name]
            was_loaded = _launchd_loaded(runner)
            # Removing the schedule removes the meeting jobs with it; leaving a
            # meeting timer behind would keep firing after the operator was told
            # the schedule was gone.
            extras = [label for label in MEETING_LABELS
                      if snapshot[f"{label}.plist"] is not None]
            if previous is None and not extras:
                if not was_loaded:
                    return False
                _launchctl(runner, "bootout", _launchd_target(), check=True)
                return True
            loaded = {LABEL: was_loaded}
            stopped = []
            try:
                if was_loaded:
                    _launchctl(runner, "bootout", _launchd_target(), check=True)
                    stopped.append(LABEL)
                _unlink_durable(directory_fd, path.name)
                for label in extras:
                    loaded[label] = _launchd_loaded(runner, label)
                    if loaded[label]:
                        _launchctl(runner, "bootout", _launchd_target(label),
                                   check=True)
                        stopped.append(label)
                    _unlink_durable(directory_fd, f"{label}.plist")
            except Exception as failure:
                try:
                    _restore_files(directory_fd, snapshot)
                    for label, active in loaded.items():
                        if active and (not extras or label in stopped):
                            _launchctl(
                                runner,
                                "bootstrap",
                                _launchd_domain(),
                                str(path.parent / f"{label}.plist"),
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

    if backend != "systemd":
        raise ValueError(f"unsupported Unix scheduling backend: {backend}")
    service_path, timer_path = _systemd_paths(systemd_dir)
    with _locked_directory(service_path.parent, True) as directory_fd:
        assert directory_fd is not None
        previous = _snapshot_files(
            directory_fd, [SYSTEMD_SERVICE, SYSTEMD_TIMER, *SYSTEMD_MEETING_UNITS])
        previous_state = _systemd_state(runner)
        # Removing the schedule removes the meeting units with it; a meeting
        # timer left enabled would keep firing after the operator was told the
        # schedule was gone.
        extras = [name for name in SYSTEMD_MEETING_UNITS if previous[name] is not None]
        meeting_state = (
            {name: _timer_state(runner, name)
             for name in (*SYSTEMD_MEETING_TIMERS, SYSTEMD_RETENTION_SERVICE)}
            if extras else {}
        )
        if all(value is None for value in previous.values()) and not any(previous_state):
            return False
        try:
            for name in (SYSTEMD_RETENTION_TIMER, SYSTEMD_RETENTION_SERVICE):
                if name in meeting_state and (name in extras or any(meeting_state[name])):
                    _systemctl(runner, "stop", name, check=True)
            enabled, active = previous_state
            if enabled:
                _systemctl(runner, "disable", SYSTEMD_TIMER, check=True)
            if active:
                _systemctl(runner, "stop", SYSTEMD_TIMER, check=True)
            for name in SYSTEMD_MEETING_TIMERS:
                if name in extras:
                    _systemctl(runner, "disable", "--now", name, check=True)
            if SYSTEMD_RETENTION_SERVICE in meeting_state and meeting_state[SYSTEMD_RETENTION_SERVICE][0]:
                _systemctl(runner, "disable", SYSTEMD_RETENTION_SERVICE, check=True)
            _unlink_durable(directory_fd, SYSTEMD_TIMER)
            _unlink_durable(directory_fd, SYSTEMD_SERVICE)
            for name in extras:
                _unlink_durable(directory_fd, name)
            _reload_systemd(runner)
        except Exception as failure:
            try:
                _restore_files(directory_fd, previous)
                _reload_systemd(runner)
                _restore_systemd_state(runner, previous_state)
                for name, state in reversed(tuple(meeting_state.items())):
                    _restore_timer_state(runner, name, state)
            except Exception as rollback_failure:
                raise RuntimeError(
                    "systemd schedule removal failed and rollback failed"
                ) from rollback_failure
            message = "systemd schedule removal failed; previous state restored"
            raise RuntimeError(message) from failure
        return True
