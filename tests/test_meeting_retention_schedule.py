"""Worker lifecycle through the real generator and stateful fake Unix managers."""

import copy
import plistlib
import subprocess

import pytest

from teammem import schedule_unix as unix
from teammem.schedule import install_schedule, remove_schedule
from tests.test_schedule import _cfg, _meeting_cfg


class Manager:
    def __init__(self, backend, directory):
        self.backend, self.directory = backend, directory
        self.states, self.commands = {}, []
        self.fail = None
        self.inspect = lambda command: None

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        self.inspect(command)
        action = command[2] if self.backend == "linux" else command[1]
        name = command[-1]
        result = 0
        if self.fail and self.fail(command):
            self.fail = None
            result = 9
        elif self.backend == "linux":
            state = self.states.setdefault(name, [False, False])
            if action in ("is-enabled", "is-active"):
                result = 0 if state[action == "is-active"] else 1
            elif action in ("enable", "disable"):
                state[0] = action == "enable"
                if "--now" in command:
                    state[1] = action == "enable"
            elif action in ("start", "restart", "stop"):
                state[1] = action != "stop"
        elif action == "bootstrap":
            label = plistlib.loads(self.directory.joinpath(name.split("/")[-1]).read_bytes())["Label"]
            if self.states.get(label, False):
                result = 9
            else:
                self.states[label] = True
        else:
            label = name.split("/")[-1]
            if action == "print":
                result = 0 if self.states.get(label, False) else 1
            elif action == "bootout":
                self.states[label] = False
        if result and kwargs.get("check"):
            raise subprocess.CalledProcessError(result, command)
        return subprocess.CompletedProcess(command, result, "", "")


def install(root, manager, *, enabled=True):
    return install_schedule(_meeting_cfg(root) if enabled else _cfg(root), platform=manager.backend,
                            systemd_dir=manager.directory, agents_dir=manager.directory,
                            executable="/opt/pipx/bin/teammem", runner=manager)


def definitions(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def test_systemd_worker_bootstrap_and_active_timer_upgrade(tmp_path):
    directory = tmp_path / "units"
    manager = Manager("linux", directory)
    install(tmp_path, manager)
    worker = unix._parse_unit((directory / unix.SYSTEMD_RETENTION_SERVICE).read_bytes())
    timer = unix._parse_unit((directory / unix.SYSTEMD_RETENTION_TIMER).read_bytes())
    assert worker["Service"]["Type"] == "simple"
    assert worker["Service"]["Restart"] == "always"
    assert float(worker["Service"]["RestartSec"]) > 0
    assert unix._parse_systemd_exec(worker["Service"]["ExecStart"])[-2:] == ["expire-meeting-audio", "--watch"]
    assert timer["Timer"]["OnActiveSec"] == "1s"
    assert timer["Timer"]["AccuracySec"] == "1us"
    assert timer["Timer"]["OnCalendar"] == "*-*-* 19:20:00"
    manager.commands.clear()
    install(tmp_path, manager)
    assert manager.states[unix.SYSTEMD_RETENTION_SERVICE][1]
    actions = [(command[2], command[-1]) for command in manager.commands]
    assert actions.index(("stop", unix.SYSTEMD_RETENTION_TIMER)) < actions.index(("stop", unix.SYSTEMD_RETENTION_SERVICE))
    assert actions.index(("daemon-reload", "daemon-reload")) < actions.index(("restart", unix.SYSTEMD_RETENTION_SERVICE))


@pytest.mark.parametrize("operation", ["remove", "disable"])
def test_worker_stopped_before_definition_unlink(tmp_path, operation):
    directory = tmp_path / "units"
    manager = Manager("linux", directory)
    install(tmp_path, manager)
    manager.states[unix.SYSTEMD_RETENTION_SERVICE] = [False, True]

    def check(command):
        if command[2] == "stop" and command[-1] == unix.SYSTEMD_RETENTION_SERVICE:
            assert (directory / unix.SYSTEMD_RETENTION_SERVICE).exists()
            assert not manager.states[unix.SYSTEMD_RETENTION_TIMER][1]

    manager.inspect = check
    if operation == "remove":
        remove_schedule(platform="linux", systemd_dir=directory, runner=manager)
    else:
        install(tmp_path, manager, enabled=False)
    assert not manager.states[unix.SYSTEMD_RETENTION_SERVICE][1]
    assert not (directory / unix.SYSTEMD_RETENTION_SERVICE).exists()


@pytest.mark.parametrize("action,unit", [("stop", unix.SYSTEMD_RETENTION_SERVICE),
                                       ("restart", unix.SYSTEMD_RETENTION_SERVICE),
                                       ("enable", unix.SYSTEMD_RETENTION_TIMER)])
def test_systemd_failed_upgrade_restores_definitions_and_worker_states(tmp_path, action, unit):
    directory = tmp_path / "units"
    manager = Manager("linux", directory)
    install(tmp_path, manager)
    manager.states[unix.SYSTEMD_RETENTION_SERVICE] = [False, True]
    before_files = definitions(directory)
    before_states = copy.deepcopy(manager.states)
    manager.fail = lambda command: command[2] == action and command[-1] == unit
    with pytest.raises(RuntimeError, match="previous state restored"):
        install(tmp_path, manager)
    assert definitions(directory) == before_files
    for name in (unix.SYSTEMD_TIMER, *unix.SYSTEMD_MEETING_TIMERS, unix.SYSTEMD_RETENTION_SERVICE):
        assert manager.states[name] == before_states[name]


def test_systemd_first_install_failure_stops_new_worker_before_rollback(tmp_path):
    directory = tmp_path / "units"
    manager = Manager("linux", directory)
    reached = []
    def check(command):
        if command[2] == "enable" and command[-1] == unix.SYSTEMD_RETENTION_TIMER:
            reached.append(True)
            assert manager.states.get(unix.SYSTEMD_RETENTION_SERVICE, [False, False])[1]
    manager.inspect = check
    manager.fail = lambda command: command[2] == "enable" and command[-1] == unix.SYSTEMD_RETENTION_TIMER
    with pytest.raises(RuntimeError):
        install(tmp_path, manager)
    assert reached
    assert not manager.states.get(unix.SYSTEMD_RETENTION_SERVICE, [False, False])[1]
    assert definitions(directory) == {}


def test_launchd_worker_keepalive_and_rollback_unloads_new_jobs(tmp_path):
    directory = tmp_path / "agents"
    manager = Manager("darwin", directory)
    install(tmp_path, manager)
    worker = plistlib.loads((directory / f"{unix.RETENTION_LABEL}.plist").read_bytes())
    assert worker["ProgramArguments"][-2:] == ["expire-meeting-audio", "--watch"]
    assert worker["RunAtLoad"] is True and worker["KeepAlive"] is True
    assert worker["ThrottleInterval"] > 0
    before = definitions(directory)
    states = copy.deepcopy(manager.states)
    manager.fail = lambda command: command[1] == "bootstrap" and command[-1].endswith(f"{unix.RETENTION_LABEL}.plist")
    with pytest.raises(RuntimeError, match="previous schedule restored"):
        install(tmp_path, manager)
    assert definitions(directory) == before
    assert manager.states == states


def test_launchd_remove_failure_restores_only_jobs_actually_stopped(tmp_path):
    directory = tmp_path / "agents"
    manager = Manager("darwin", directory)
    install(tmp_path, manager)
    before, states = definitions(directory), copy.deepcopy(manager.states)
    manager.fail = lambda command: command[1] == "bootout" and command[-1].endswith(unix.RETENTION_LABEL)
    with pytest.raises(RuntimeError, match="previous state restored"):
        remove_schedule(platform="darwin", agents_dir=directory, runner=manager)
    assert definitions(directory) == before
    assert manager.states == states


def test_first_install_rollback_does_not_disable_units_after_unlink(tmp_path):
    directory = tmp_path / "units"
    manager = Manager("linux", directory)
    manager.fail = lambda command: command[2] == "enable" and command[-1] == unix.SYSTEMD_RETENTION_TIMER
    def check(command):
        if command[2] in ("disable", "start", "stop") and command[-1] in unix.SYSTEMD_MEETING_UNITS:
            if not (directory / command[-1]).exists():
                raise subprocess.CalledProcessError(5, command)
    manager.inspect = check
    with pytest.raises(RuntimeError, match="previous state restored"):
        install(tmp_path, manager)
    assert definitions(directory) == {}


def test_launchd_partial_bootout_failure_restores_previously_stopped_jobs(tmp_path):
    directory = tmp_path / "agents"
    manager = Manager("darwin", directory)
    install(tmp_path, manager)
    states = copy.deepcopy(manager.states)
    before = definitions(directory)
    manager.fail = lambda command: command[1] == "bootout" and command[-1].endswith(unix.RETENTION_LABEL)
    with pytest.raises(RuntimeError, match="previous schedule restored"):
        install(tmp_path, manager)
    assert manager.states == states
    assert definitions(directory) == before
