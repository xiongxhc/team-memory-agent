import fcntl
import os
import plistlib
import queue
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from teammem.config import Config
from teammem.schedule import (
    LABEL,
    install_schedule,
    remove_schedule,
    schedule_status,
)


class RecordingRunner:
    def __init__(self, returncodes=None, before=None, handler=None):
        self.returncodes = {
            command: list(value) if isinstance(value, list) else value
            for command, value in (returncodes or {}).items()
        }
        self.before = before
        self.handler = handler
        self.calls = []

    @property
    def commands(self):
        return [call[0] for call in self.calls]

    def __call__(self, command, **kwargs):
        command = list(command)
        self.calls.append((command, kwargs))
        if self.before is not None:
            self.before(command)
        default = (
            1
            if command[:3]
            in (
                ["systemctl", "--user", "is-enabled"],
                ["systemctl", "--user", "is-active"],
            )
            else 0
        )
        configured = (
            self.handler(command)
            if self.handler is not None
            else self.returncodes.get(tuple(command), default)
        )
        returncode = configured.pop(0) if isinstance(configured, list) else configured
        result = subprocess.CompletedProcess(command, returncode, "", "")
        if kwargs.get("check") and returncode:
            raise subprocess.CalledProcessError(returncode, command)
        return result


def _cfg(tmp_path):
    return Config(
        env_file=tmp_path / "hub.env",
        gitlab_token="TOKEN",
        slack_bot_token="TOKEN",
    )


def _launchctl(*arguments):
    return ["launchctl", *arguments]


def _systemctl(*arguments):
    return ["systemctl", "--user", *arguments]


def _launchctl_print():
    return _launchctl("print", f"gui/{os.getuid()}/{LABEL}")


def _install_launchd(tmp_path, directory, runner, *, time="18:20", cfg=None):
    return install_schedule(
        cfg or _cfg(tmp_path),
        time=time,
        platform="darwin",
        agents_dir=directory,
        executable="/opt/pipx/bin/teammem",
        runner=runner,
    )


def _install_systemd(tmp_path, directory, runner):
    return install_schedule(
        _cfg(tmp_path),
        platform="linux",
        systemd_dir=directory,
        executable="/opt/pipx/bin/teammem",
        runner=runner,
    )


def _installed_systemd(tmp_path):
    directory = tmp_path / "systemd" / "user"
    _install_systemd(tmp_path, directory, RecordingRunner())
    return (
        directory,
        directory / "teammem-daily.service",
        directory / "teammem-daily.timer",
    )


def _manager_returncodes(enabled=False, active=False):
    return {
        tuple(_systemctl("is-enabled", "teammem-daily.timer")): 0 if enabled else 1,
        tuple(_systemctl("is-active", "teammem-daily.timer")): 0 if active else 3,
    }


def _manager_queries():
    return [
        _systemctl("is-enabled", "teammem-daily.timer"),
        _systemctl("is-active", "teammem-daily.timer"),
    ]


def _move_directive(text, prefix, section):
    lines = text.splitlines()
    line = next(line for line in lines if line.startswith(prefix))
    lines.remove(line)
    lines.insert(lines.index(f"[{section}]") + 1, line)
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("value", ["25:00", "18:99", "6:20", "18:2", "18:20:00"])
def test_schedule_time_requires_strict_24_hour_hhmm(tmp_path, value):
    runner = RecordingRunner()

    with pytest.raises(ValueError, match="^schedule time must be HH:MM$"):
        _install_launchd(
            tmp_path, tmp_path / "LaunchAgents", runner, time=value
        )

    assert runner.calls == []
    assert not (tmp_path / "LaunchAgents").exists()


def test_launchd_schedule_runs_only_run_daily_with_env_file(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    runner = RecordingRunner({tuple(_launchctl_print()): 1})
    cfg = _cfg(tmp_path)

    path = _install_launchd(tmp_path, agents_dir, runner, cfg=cfg)

    data = plistlib.loads(path.read_bytes())
    state_dir = Path.home() / ".local" / "state" / "teammem"
    assert data == {
        "Label": LABEL,
        "ProgramArguments": [
            "/opt/pipx/bin/teammem",
            "--env-file",
            str(cfg.env_file),
            "run-daily",
        ],
        "RunAtLoad": False,
        "StandardErrorPath": str(state_dir / "schedule.err"),
        "StandardOutPath": str(state_dir / "schedule.log"),
        "StartCalendarInterval": {"Hour": 18, "Minute": 20},
    }
    assert "TOKEN" not in path.read_text()
    assert runner.commands == [
        _launchctl_print(),
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
    ]
    assert all(
        kwargs == {"check": check, "capture_output": True, "text": True}
        for (_, kwargs), check in zip(runner.calls, [False, True])
    )
    status_runner = RecordingRunner()
    status = schedule_status(
        platform="darwin", agents_dir=agents_dir, runner=status_runner
    )
    assert status.installed is True
    assert status.time == "18:20"
    assert status.backend == "launchd"
    assert status.path == path
    assert status_runner.commands == [_launchctl_print()]


def test_launchd_loaded_job_is_booted_out_before_atomic_replacement(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    path = agents_dir / f"{LABEL}.plist"
    agents_dir.mkdir()
    path.write_text("old definition")
    seen = []

    def inspect_order(command):
        seen.append((command, path.read_text()))

    runner = RecordingRunner(before=inspect_order)

    _install_launchd(tmp_path, agents_dir, runner, time="07:05")

    service_target = f"gui/{os.getuid()}/{LABEL}"
    assert seen[0] == (_launchctl_print(), "old definition")
    assert seen[1] == (
        ["launchctl", "bootout", service_target],
        "old definition",
    )
    assert seen[2][0] == [
        "launchctl",
        "bootstrap",
        f"gui/{os.getuid()}",
        str(path),
    ]
    assert seen[2][1] != "old definition"
    assert schedule_status(
        platform="darwin",
        agents_dir=agents_dir,
        runner=RecordingRunner(),
    ).time == "07:05"
    assert sorted(item.name for item in agents_dir.iterdir()) == [
        ".teammem-launchd.lock",
        path.name,
    ]


def test_launchd_replacement_failure_preserves_loaded_definition(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    path = agents_dir / f"{LABEL}.plist"
    agents_dir.mkdir()
    original = b"old definition"
    path.write_bytes(original)
    service_target = f"gui/{os.getuid()}/{LABEL}"
    runner = RecordingRunner(
        {("launchctl", "bootout", service_target): 5}
    )

    with pytest.raises(subprocess.CalledProcessError):
        _install_launchd(tmp_path, agents_dir, runner)

    assert path.read_bytes() == original
    assert runner.commands == [
        _launchctl_print(),
        ["launchctl", "bootout", service_target],
    ]


@pytest.mark.parametrize("failure_stage", ["write", "bootstrap"])
def test_launchd_install_failure_restores_prior_definition_and_job(
    tmp_path, monkeypatch, failure_stage
):
    agents_dir = tmp_path / "LaunchAgents"
    path = agents_dir / f"{LABEL}.plist"
    agents_dir.mkdir()
    original = b"old definition"
    path.write_bytes(original)
    bootstrap = tuple(
        _launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    )
    if failure_stage == "write":
        real_replace = os.replace
        attempts = 0

        def fail_once(source, destination, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("simulated write failure")
            real_replace(source, destination, *args, **kwargs)

        monkeypatch.setattr("teammem.schedule.os.replace", fail_once)
        runner = RecordingRunner()
    else:
        runner = RecordingRunner({bootstrap: [9, 0]})

    with pytest.raises(RuntimeError, match="previous schedule restored$"):
        _install_launchd(tmp_path, agents_dir, runner)

    assert path.read_bytes() == original
    expected = [
        _launchctl_print(),
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}"),
    ]
    if failure_stage == "bootstrap":
        expected.append(list(bootstrap))
    expected.append(list(bootstrap))
    assert runner.commands == expected


def test_launchd_failed_first_install_removes_new_definition(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    path = agents_dir / f"{LABEL}.plist"
    bootstrap = (
        "launchctl",
        "bootstrap",
        f"gui/{os.getuid()}",
        str(path),
    )
    runner = RecordingRunner(
        {
            tuple(_launchctl_print()): 1,
            bootstrap: 9,
        }
    )

    with pytest.raises(
        RuntimeError,
        match="^launchd schedule installation failed; previous state restored$",
    ):
        _install_launchd(tmp_path, agents_dir, runner)

    assert not path.exists()
    assert runner.commands == [
        _launchctl_print(),
        list(bootstrap),
    ]


def test_launchd_remove_is_idempotent_and_preserves_file_on_bootout_failure(
    tmp_path,
):
    agents_dir = tmp_path / "LaunchAgents"
    path = agents_dir / f"{LABEL}.plist"
    agents_dir.mkdir()
    path.write_bytes(plistlib.dumps({"StartCalendarInterval": {"Hour": 8, "Minute": 15}}))
    service_target = f"gui/{os.getuid()}/{LABEL}"
    failing = RecordingRunner(
        {("launchctl", "bootout", service_target): 5}
    )

    with pytest.raises(subprocess.CalledProcessError):
        remove_schedule(
            platform="darwin", agents_dir=agents_dir, runner=failing
        )
    assert path.exists()

    runner = RecordingRunner()
    assert remove_schedule(
        platform="darwin", agents_dir=agents_dir, runner=runner
    ) is True
    call_count = len(runner.calls)
    assert remove_schedule(
        platform="darwin", agents_dir=agents_dir, runner=runner
    ) is False
    assert len(runner.calls) == call_count
    assert runner.commands == [
        _launchctl_print(),
        ["launchctl", "bootout", service_target],
    ]


def test_systemd_timer_is_persistent_and_uses_local_1820(tmp_path):
    systemd_dir = tmp_path / "systemd" / "user"
    runner = RecordingRunner()
    cfg = _cfg(tmp_path)

    path = install_schedule(
        cfg,
        systemd_dir=systemd_dir,
        platform="linux",
        executable="/opt/pipx/bin/teammem",
        runner=runner,
    )

    timer_path = systemd_dir / "teammem-daily.timer"
    service_path = systemd_dir / "teammem-daily.service"
    timer = timer_path.read_text()
    service = service_path.read_text()
    assert path == timer_path
    assert "OnCalendar=*-*-* 18:20:00" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer
    assert (
        f"ExecStart=/opt/pipx/bin/teammem --env-file {cfg.env_file} run-daily"
        in service
    )
    assert "Type=oneshot" in service
    assert "TOKEN" not in timer + service
    assert runner.commands == [
        *_manager_queries(),
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "teammem-daily.timer"],
    ]
    assert [kwargs["check"] for _, kwargs in runner.calls] == [
        False,
        False,
        True,
        True,
    ]
    assert sorted(item.name for item in systemd_dir.iterdir()) == [
        ".teammem-systemd.lock",
        "teammem-daily.service",
        "teammem-daily.timer",
    ]

    manager = _manager_returncodes(enabled=True, active=True)
    status_runner = RecordingRunner(manager)
    status = schedule_status(
        platform="linux", systemd_dir=systemd_dir, runner=status_runner
    )
    assert status.installed is True
    assert status.time == "18:20"
    assert status.backend == "systemd"
    assert status.path == timer_path
    assert status_runner.commands == _manager_queries()


def test_systemd_remove_is_idempotent_and_uses_explicit_user_commands(tmp_path):
    systemd_dir, service_path, timer_path = _installed_systemd(tmp_path)

    def inspect_order(command):
        if "disable" in command:
            assert timer_path.exists() and service_path.exists()
        if "daemon-reload" in command:
            assert not timer_path.exists() and not service_path.exists()

    enabled = tuple(_systemctl("is-enabled", "teammem-daily.timer"))
    active = tuple(_systemctl("is-active", "teammem-daily.timer"))
    runner = RecordingRunner(
        {enabled: [0, 1], active: [3, 3]}, before=inspect_order
    )
    assert remove_schedule(
        platform="linux", systemd_dir=systemd_dir, runner=runner
    ) is True
    call_count = len(runner.calls)
    assert remove_schedule(
        platform="linux", systemd_dir=systemd_dir, runner=runner
    ) is False
    assert runner.commands == [
        list(enabled),
        list(active),
        _systemctl("disable", "teammem-daily.timer"),
        ["systemctl", "--user", "daemon-reload"],
        list(enabled),
        list(active),
    ]


def test_systemd_remove_failure_preserves_definitions(tmp_path):
    systemd_dir, service_path, timer_path = _installed_systemd(tmp_path)
    disable = tuple(_systemctl("disable", "teammem-daily.timer"))
    enabled = tuple(_systemctl("is-enabled", "teammem-daily.timer"))
    active = tuple(_systemctl("is-active", "teammem-daily.timer"))
    runner = RecordingRunner({enabled: 0, disable: 1})

    with pytest.raises(RuntimeError, match="previous state restored$"):
        remove_schedule(
            platform="linux", systemd_dir=systemd_dir, runner=runner
        )

    assert timer_path.exists() and service_path.exists()
    assert runner.commands == [
        list(enabled),
        list(active),
        list(disable),
        _systemctl("daemon-reload"),
        _systemctl("enable", "teammem-daily.timer"),
        _systemctl("stop", "teammem-daily.timer"),
        *_manager_queries(),
    ]


@pytest.mark.parametrize(
    ("prior_files", "enabled", "active", "failure_stage"),
    [
        ("pair", True, False, "write"),
        ("pair", True, False, "reload"),
        ("pair", True, False, "enable"),
        ("service", True, False, "enable"),
        ("timer", False, True, "enable"),
        ("none", False, False, "enable"),
    ],
)
def test_systemd_install_failure_restores_files_and_manager_state(
    tmp_path, monkeypatch, prior_files, enabled, active, failure_stage
):
    systemd_dir = tmp_path / "systemd" / "user"
    systemd_dir.mkdir(parents=True)
    paths = {
        "service": systemd_dir / "teammem-daily.service",
        "timer": systemd_dir / "teammem-daily.timer",
    }
    expected_files = (
        {"service", "timer"} if prior_files == "pair" else {prior_files} - {"none"}
    )
    for kind in expected_files:
        paths[kind].write_bytes(f"old {kind}".encode())
    reload_command = tuple(_systemctl("daemon-reload"))
    enable = tuple(_systemctl("enable", "--now", "teammem-daily.timer"))
    returncodes = _manager_returncodes(enabled, active)
    if failure_stage == "write":
        real_replace = os.replace
        attempts = 0

        def fail_second_replace(source, destination, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise OSError("simulated timer write failure")
            real_replace(source, destination, *args, **kwargs)

        monkeypatch.setattr("teammem.schedule.os.replace", fail_second_replace)
    elif failure_stage == "reload":
        returncodes[reload_command] = [7, 0]
    else:
        returncodes[enable] = [9, 0]
    runner = RecordingRunner(returncodes)

    with pytest.raises(RuntimeError, match="previous state restored$"):
        _install_systemd(tmp_path, systemd_dir, runner)

    restored = {
        kind: path.read_bytes() for kind, path in paths.items() if path.exists()
    }
    assert restored == {kind: f"old {kind}".encode() for kind in expected_files}
    expected = _manager_queries()
    if failure_stage != "write":
        expected.append(list(reload_command))
    if failure_stage == "enable":
        expected.append(list(enable))
    expected.extend(
        [
            list(reload_command),
            _systemctl("enable" if enabled else "disable", "teammem-daily.timer"),
            _systemctl("start" if active else "stop", "teammem-daily.timer"),
            *_manager_queries(),
        ]
    )
    assert runner.commands == expected


def test_systemd_stale_manager_rollback_is_verified_without_definitions(tmp_path):
    systemd_dir = tmp_path / "systemd" / "user"
    state = {"enabled": True, "active": True}

    def manager(command):
        action = command[2]
        if action == "is-enabled":
            return 0 if state["enabled"] else 1
        if action == "is-active":
            return 0 if state["active"] else 3
        if action == "enable" and "--now" in command:
            state.update(enabled=False, active=False)
            return 9
        if action in {"enable", "disable"}:
            state["enabled"] = action == "enable"
        if action in {"start", "stop"}:
            state["active"] = action == "start"
        return 0

    runner = RecordingRunner(handler=manager)
    with pytest.raises(RuntimeError, match="previous state restored$"):
        _install_systemd(tmp_path, systemd_dir, runner)

    assert state == {"enabled": True, "active": True}
    assert runner.commands[-2:] == _manager_queries()
    assert not any(
        path.suffix in {".service", ".timer"} for path in systemd_dir.iterdir()
    )


def test_systemd_remove_cleans_stale_active_manager_without_files(tmp_path):
    systemd_dir = tmp_path / "systemd" / "user"
    runner = RecordingRunner(_manager_returncodes(False, True))

    assert remove_schedule(
        platform="linux", systemd_dir=systemd_dir, runner=runner
    ) is True

    assert runner.commands == [
        *_manager_queries(),
        _systemctl("stop", "teammem-daily.timer"),
        _systemctl("daemon-reload"),
    ]


def test_systemd_remove_reload_failure_restores_partial_state_for_retry(tmp_path):
    systemd_dir = tmp_path / "systemd" / "user"
    systemd_dir.mkdir(parents=True)
    service = systemd_dir / "teammem-daily.service"
    service.write_bytes(b"old service")
    reload_command = tuple(_systemctl("daemon-reload"))
    returncodes = _manager_returncodes(False, True)
    returncodes[reload_command] = [8, 0]
    runner = RecordingRunner(returncodes)

    with pytest.raises(
        RuntimeError, match="systemd schedule removal failed; previous state restored"
    ):
        remove_schedule(
            platform="linux", systemd_dir=systemd_dir, runner=runner
        )

    assert service.read_bytes() == b"old service"
    retry = RecordingRunner(_manager_returncodes(False, True))
    assert remove_schedule(
        platform="linux", systemd_dir=systemd_dir, runner=retry
    ) is True
    assert not service.exists()


@pytest.mark.parametrize("existing", ["service", "timer"])
def test_systemd_remove_cleans_partial_definition_without_false_failure(
    tmp_path, existing
):
    systemd_dir = tmp_path / "systemd" / "user"
    systemd_dir.mkdir(parents=True)
    path = systemd_dir / f"teammem-daily.{existing}"
    path.write_text("partial")
    runner = RecordingRunner()

    assert remove_schedule(
        platform="linux", systemd_dir=systemd_dir, runner=runner
    ) is True

    assert not path.exists()
    assert runner.commands == [
        *_manager_queries(),
        _systemctl("daemon-reload"),
    ]


def test_missing_schedule_status_is_backend_specific(tmp_path):
    launchd = schedule_status(
        platform="darwin",
        agents_dir=tmp_path / "LaunchAgents",
        runner=RecordingRunner(),
    )
    systemd = schedule_status(
        platform="linux",
        systemd_dir=tmp_path / "systemd" / "user",
        runner=RecordingRunner(),
    )

    assert launchd.installed is False
    assert launchd.time is None
    assert launchd.backend == "launchd"
    assert launchd.path.name == f"{LABEL}.plist"
    assert systemd.installed is False
    assert systemd.time is None
    assert systemd.backend == "systemd"
    assert systemd.path.name == "teammem-daily.timer"


def test_complete_launchd_definition_reports_time_but_not_installed_when_unloaded(
    tmp_path,
):
    agents_dir = tmp_path / "LaunchAgents"
    _install_launchd(
        tmp_path,
        agents_dir,
        RecordingRunner({tuple(_launchctl_print()): 1}),
    )
    runner = RecordingRunner({tuple(_launchctl_print()): 1})

    status = schedule_status(
        platform="darwin", agents_dir=agents_dir, runner=runner
    )

    assert status.installed is False
    assert status.time == "18:20"
    assert runner.commands == [_launchctl_print()]


@pytest.mark.parametrize(
    ("state", "enabled", "active", "expected_time", "query_manager"),
    [
        ("disabled", False, False, "18:20", True),
        ("enabled-inactive", True, False, "18:20", True),
        ("disabled-active", False, True, "18:20", True),
        ("service-only", False, False, None, False),
        ("timer-only", False, False, "18:20", False),
        ("malformed", False, False, "18:20", False),
    ],
)
def test_systemd_status_requires_complete_valid_enabled_and_active_definition(
    tmp_path, state, enabled, active, expected_time, query_manager
):
    systemd_dir, service, timer = _installed_systemd(tmp_path)
    if state == "service-only":
        timer.unlink()
    elif state == "timer-only":
        service.unlink()
    elif state == "malformed":
        service.write_text("malformed")
    runner = RecordingRunner(_manager_returncodes(enabled, active))

    status = schedule_status(
        platform="linux", systemd_dir=systemd_dir, runner=runner
    )

    assert status.installed is False
    assert status.time == expected_time
    assert runner.commands == (_manager_queries() if query_manager else [])


@pytest.mark.parametrize(
    ("filename", "prefix", "wrong_section"),
    [
        ("teammem-daily.service", "ExecStart=", "Unit"),
        ("teammem-daily.timer", "OnCalendar=", "Unit"),
        ("teammem-daily.timer", "Persistent=", "Install"),
        ("teammem-daily.timer", "Unit=", "Unit"),
        ("teammem-daily.timer", "WantedBy=", "Timer"),
    ],
)
def test_systemd_status_rejects_misplaced_directives(
    tmp_path, filename, prefix, wrong_section
):
    systemd_dir, _, _ = _installed_systemd(tmp_path)
    path = systemd_dir / filename
    path.write_text(_move_directive(path.read_text(), prefix, wrong_section))
    runner = RecordingRunner(_manager_returncodes(True, True))

    status = schedule_status(
        platform="linux", systemd_dir=systemd_dir, runner=runner
    )

    assert status.installed is False
    assert runner.calls == []


def test_status_does_not_report_invalid_definition_time(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    path = agents_dir / f"{LABEL}.plist"
    agents_dir.mkdir()
    path.write_bytes(
        plistlib.dumps(
            {"StartCalendarInterval": {"Hour": 99, "Minute": 99}}
        )
    )

    runner = RecordingRunner()
    status = schedule_status(
        platform="darwin", agents_dir=agents_dir, runner=runner
    )

    assert status.installed is False
    assert status.time is None
    assert runner.calls == []


def test_launchd_install_corrects_private_state_and_definition_modes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    state_dir = tmp_path / ".local" / "state" / "teammem"
    state_dir.mkdir(parents=True, mode=0o755)
    state_dir.chmod(0o755)

    path = _install_launchd(
        tmp_path, None, RecordingRunner({tuple(_launchctl_print()): 1})
    )

    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("unsafe_directory", ["agents", "state"])
def test_launchd_rejects_symlinked_directories_before_bootout(
    tmp_path, monkeypatch, unsafe_directory
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    agents_dir = tmp_path / "Library" / "LaunchAgents"
    state_dir = tmp_path / ".local" / "state" / "teammem"
    if unsafe_directory == "agents":
        agents_dir.parent.mkdir(parents=True)
        agents_dir.symlink_to(target, target_is_directory=True)
    else:
        agents_dir.mkdir(parents=True)
        state_dir.parent.mkdir(parents=True)
        state_dir.symlink_to(target, target_is_directory=True)
    path = agents_dir / f"{LABEL}.plist"
    path.write_bytes(b"old definition")
    runner = RecordingRunner()

    with pytest.raises(ValueError, match="symlink"):
        _install_launchd(tmp_path, None, runner)

    assert runner.calls == []
    assert path.read_bytes() == b"old definition"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_schedule_install_rejects_symlinked_ancestor_components(
    tmp_path, platform
):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    directory = root / "escape" / "schedule"
    runner = RecordingRunner()

    with pytest.raises(ValueError, match="unsafe schedule directory"):
        install_schedule(
            _cfg(tmp_path),
            platform=platform,
            agents_dir=directory,
            systemd_dir=directory,
            executable="/opt/pipx/bin/teammem",
            runner=runner,
        )

    assert list(outside.iterdir()) == []
    assert runner.calls == []


@pytest.mark.parametrize(
    ("platform", "operation"),
    [("linux", "install"), ("linux", "status"), ("linux", "remove"),
     ("darwin", "status")],
)
def test_schedule_operations_reject_symlinked_definitions(
    tmp_path, platform, operation
):
    directory = (
        tmp_path / "systemd" / "user"
        if platform == "linux"
        else tmp_path / "LaunchAgents"
    )
    directory.mkdir(parents=True)
    target = tmp_path / "outside"
    target.write_bytes(b"outside")
    filename = (
        "teammem-daily.service"
        if platform == "linux"
        else f"{LABEL}.plist"
    )
    (directory / filename).symlink_to(target)
    runner = RecordingRunner()

    with pytest.raises(ValueError, match="symlink"):
        if operation == "install":
            _install_systemd(tmp_path, directory, runner)
        elif platform == "linux" and operation == "remove":
            remove_schedule(
                platform=platform, systemd_dir=directory, runner=runner
            )
        elif platform == "linux":
            schedule_status(
                platform=platform, systemd_dir=directory, runner=runner
            )
        else:
            schedule_status(
                platform=platform, agents_dir=directory, runner=runner
            )

    assert target.read_bytes() == b"outside"
    assert runner.calls == []


def test_systemd_status_cannot_substitute_bytes_after_path_validation(
    tmp_path, monkeypatch
):
    systemd_dir, _, timer = _installed_systemd(tmp_path)
    outside = tmp_path / "outside.timer"
    outside.write_text(timer.read_text().replace("18:20", "23:59"))
    original = timer.with_suffix(".original")
    real_lstat = Path.lstat
    real_open = os.open
    swapped = False
    opens = {}

    def swap_after_lstat(path):
        nonlocal swapped
        result = real_lstat(path)
        if path == timer and not swapped:
            swapped = True
            timer.rename(original)
            timer.symlink_to(outside)
        return result

    def count_opens(path, flags, *args, **kwargs):
        if path in {"teammem-daily.service", "teammem-daily.timer"}:
            opens[path] = opens.get(path, 0) + 1
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", swap_after_lstat)
    monkeypatch.setattr("teammem.schedule.os.open", count_opens)
    runner = RecordingRunner(_manager_returncodes(True, True))
    status = schedule_status(
        platform="linux", systemd_dir=systemd_dir, runner=runner
    )

    assert status.time == "18:20"
    assert opens == {"teammem-daily.service": 1, "teammem-daily.timer": 1}
    assert outside.read_text().endswith("WantedBy=timers.target\n")


def test_atomic_write_never_chmods_a_replaced_definition_path(
    tmp_path, monkeypatch
):
    agents_dir = tmp_path / "LaunchAgents"
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    outside.chmod(0o640)
    captured = "captured.plist"
    real_replace = os.replace
    real_fsync = os.fsync
    injected = False
    fsync_kinds = []

    def swap_after_replace(source, destination, *args, **kwargs):
        nonlocal injected
        real_replace(source, destination, *args, **kwargs)
        if str(destination).endswith(f"{LABEL}.plist") and not injected:
            injected = True
            directory_fd = kwargs.get("dst_dir_fd")
            if directory_fd is None:
                path = Path(destination)
                path.rename(path.with_name(captured))
                path.symlink_to(outside)
            else:
                os.rename(
                    destination, captured,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd
                )
                os.symlink(outside, destination, dir_fd=directory_fd)

    def record_fsync(descriptor):
        fsync_kinds.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr("teammem.schedule.os.replace", swap_after_replace)
    monkeypatch.setattr("teammem.schedule.os.fsync", record_fsync)
    runner = RecordingRunner({tuple(_launchctl_print()): 1})
    _install_launchd(tmp_path, agents_dir, runner)

    assert outside.read_bytes() == b"outside"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o640
    assert stat.S_IMODE((agents_dir / captured).stat().st_mode) == 0o600
    assert False in fsync_kinds and True in fsync_kinds


def test_concurrent_systemd_installs_cannot_publish_a_mixed_pair(
    tmp_path, monkeypatch
):
    systemd_dir = tmp_path / "systemd" / "user"
    service_name = "teammem-daily.service"
    a_written = threading.Event()
    release_a = threading.Event()
    boundary = queue.Queue()
    errors = []
    real_replace = os.replace
    real_flock = fcntl.flock

    def controlled_replace(source, destination, *args, **kwargs):
        real_replace(source, destination, *args, **kwargs)
        if Path(destination).name != service_name:
            return
        if threading.current_thread().name == "install-a":
            a_written.set()
            assert release_a.wait(3)
        elif threading.current_thread().name == "install-b":
            boundary.put("replace")

    def observed_flock(descriptor, operation):
        if threading.current_thread().name == "install-b" and operation & fcntl.LOCK_EX:
            boundary.put("lock")
        return real_flock(descriptor, operation)

    monkeypatch.setattr("teammem.schedule.os.replace", controlled_replace)
    monkeypatch.setattr(fcntl, "flock", observed_flock)

    def run(marker, time):
        try:
            cfg = _cfg(tmp_path)
            cfg.env_file = tmp_path / f"{marker}.env"
            install_schedule(
                cfg, time=time, platform="linux", systemd_dir=systemd_dir,
                executable=f"/opt/{marker}/teammem", runner=RecordingRunner()
            )
        except BaseException as failure:
            errors.append(failure)

    first = threading.Thread(target=run, args=("a", "01:01"), name="install-a")
    second = threading.Thread(target=run, args=("b", "02:02"), name="install-b")
    first.start()
    assert a_written.wait(3)
    second.start()
    observed = boundary.get(timeout=3)
    if observed == "replace":
        second.join(3)
        assert not second.is_alive()
    release_a.set()
    first.join(3)
    second.join(3)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    service = (systemd_dir / service_name).read_text()
    timer = (systemd_dir / "teammem-daily.timer").read_text()
    assert ("/opt/a/" in service, "01:01:00" in timer) in {
        (True, True),
        (False, False),
    }


def test_unsupported_platform_has_no_side_effects(tmp_path):
    runner = RecordingRunner()

    with pytest.raises(
        RuntimeError, match="^unsupported scheduling platform: win32$"
    ):
        install_schedule(
            _cfg(tmp_path),
            platform="win32",
            agents_dir=tmp_path / "LaunchAgents",
            systemd_dir=tmp_path / "systemd",
            executable="/opt/pipx/bin/teammem",
            runner=runner,
        )

    assert runner.calls == []
    assert list(tmp_path.iterdir()) == []
