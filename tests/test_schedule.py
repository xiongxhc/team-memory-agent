import os
import plistlib
import stat
import subprocess
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
    def __init__(self, returncodes=None, before=None):
        self.returncodes = {
            command: list(value) if isinstance(value, list) else value
            for command, value in (returncodes or {}).items()
        }
        self.before = before
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
        configured = self.returncodes.get(tuple(command), default)
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


def _old_systemd_pair(tmp_path):
    directory = tmp_path / "systemd" / "user"
    service = directory / "teammem-daily.service"
    timer = directory / "teammem-daily.timer"
    directory.mkdir(parents=True)
    service.write_bytes(b"old service")
    timer.write_bytes(b"old timer")
    return directory, service, timer


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
        install_schedule(
            _cfg(tmp_path),
            time=value,
            platform="darwin",
            agents_dir=tmp_path / "LaunchAgents",
            executable="/opt/pipx/bin/teammem",
            runner=runner,
        )

    assert runner.calls == []
    assert not (tmp_path / "LaunchAgents").exists()


def test_launchd_schedule_runs_only_run_daily_with_env_file(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    runner = RecordingRunner({tuple(_launchctl_print()): 1})
    cfg = _cfg(tmp_path)

    path = install_schedule(
        cfg,
        agents_dir=agents_dir,
        platform="darwin",
        executable="/opt/pipx/bin/teammem",
        runner=runner,
    )

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

    install_schedule(
        _cfg(tmp_path),
        time="07:05",
        platform="darwin",
        agents_dir=agents_dir,
        executable="/opt/pipx/bin/teammem",
        runner=runner,
    )

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
    assert sorted(item.name for item in agents_dir.iterdir()) == [path.name]


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
        install_schedule(
            _cfg(tmp_path),
            platform="darwin",
            agents_dir=agents_dir,
            executable="/opt/pipx/bin/teammem",
            runner=runner,
        )

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

        def fail_once(source, destination):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("simulated write failure")
            real_replace(source, destination)

        monkeypatch.setattr("teammem.schedule.os.replace", fail_once)
        runner = RecordingRunner()
    else:
        runner = RecordingRunner({bootstrap: [9, 0]})

    with pytest.raises(RuntimeError, match="previous schedule restored$"):
        install_schedule(
            _cfg(tmp_path),
            platform="darwin",
            agents_dir=agents_dir,
            executable="/opt/pipx/bin/teammem",
            runner=runner,
        )

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
        install_schedule(
            _cfg(tmp_path),
            platform="darwin",
            agents_dir=agents_dir,
            executable="/opt/pipx/bin/teammem",
            runner=runner,
        )

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
    ]


@pytest.mark.parametrize("failure_stage", ["write", "reload", "enable"])
def test_systemd_install_failure_restores_pair_and_enabled_state(
    tmp_path, monkeypatch, failure_stage
):
    systemd_dir, service_path, timer_path = _old_systemd_pair(tmp_path)
    enabled = tuple(_systemctl("is-enabled", "teammem-daily.timer"))
    active = tuple(_systemctl("is-active", "teammem-daily.timer"))
    reload_command = tuple(_systemctl("daemon-reload"))
    enable = tuple(_systemctl("enable", "--now", "teammem-daily.timer"))
    returncodes = {enabled: 0}
    if failure_stage == "write":
        real_replace = os.replace
        attempts = 0

        def fail_second_replace(source, destination):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise OSError("simulated timer write failure")
            real_replace(source, destination)

        monkeypatch.setattr("teammem.schedule.os.replace", fail_second_replace)
    elif failure_stage == "reload":
        returncodes[reload_command] = [7, 0]
    else:
        returncodes[enable] = [9, 0]
    runner = RecordingRunner(returncodes)

    with pytest.raises(RuntimeError, match="previous state restored$"):
        _install_systemd(tmp_path, systemd_dir, runner)

    assert service_path.read_bytes() == b"old service"
    assert timer_path.read_bytes() == b"old timer"
    expected = [list(enabled), list(active)]
    if failure_stage != "write":
        expected.append(list(reload_command))
    if failure_stage == "enable":
        expected.append(list(enable))
    expected.extend(
        [
            list(reload_command),
            _systemctl("enable", "teammem-daily.timer"),
            _systemctl("stop", "teammem-daily.timer"),
        ]
    )
    assert runner.commands == expected


@pytest.mark.parametrize(
    ("prior_files", "enabled", "active"),
    [
        ("service", True, False),
        ("timer", False, True),
        ("none", True, True),
        ("none", False, False),
    ],
)
def test_systemd_install_rollback_restores_partial_files_and_manager_state(
    tmp_path, prior_files, enabled, active
):
    systemd_dir = tmp_path / "systemd" / "user"
    systemd_dir.mkdir(parents=True)
    service = systemd_dir / "teammem-daily.service"
    timer = systemd_dir / "teammem-daily.timer"
    if prior_files == "service":
        service.write_bytes(b"old service")
    elif prior_files == "timer":
        timer.write_bytes(b"old timer")
    enable_now = tuple(
        _systemctl("enable", "--now", "teammem-daily.timer")
    )
    returncodes = _manager_returncodes(enabled, active)
    returncodes[enable_now] = 9
    runner = RecordingRunner(returncodes)

    with pytest.raises(RuntimeError, match="previous state restored$"):
        _install_systemd(tmp_path, systemd_dir, runner)

    restored = {
        path.suffix.removeprefix("."): path.read_bytes()
        for path in (service, timer)
        if path.exists()
    }
    assert restored == (
        {prior_files: f"old {prior_files}".encode()}
        if prior_files != "none"
        else {}
    )
    assert runner.commands == [
        *_manager_queries(),
        _systemctl("daemon-reload"),
        list(enable_now),
        _systemctl("daemon-reload"),
        _systemctl(
            "enable" if enabled else "disable", "teammem-daily.timer"
        ),
        _systemctl("start" if active else "stop", "teammem-daily.timer"),
    ]


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
    install_schedule(
        _cfg(tmp_path),
        platform="darwin",
        agents_dir=agents_dir,
        executable="/opt/pipx/bin/teammem",
        runner=RecordingRunner({tuple(_launchctl_print()): 1}),
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


def test_systemd_status_reads_each_definition_once(tmp_path, monkeypatch):
    systemd_dir, service, timer = _installed_systemd(tmp_path)
    reads = {}
    real_read = Path.read_bytes

    def count_reads(path):
        reads[path] = reads.get(path, 0) + 1
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", count_reads)
    schedule_status(
        platform="linux",
        systemd_dir=systemd_dir,
        runner=RecordingRunner(_manager_returncodes(True, True)),
    )

    assert reads == {service: 1, timer: 1}


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

    path = install_schedule(
        _cfg(tmp_path),
        platform="darwin",
        executable="/opt/pipx/bin/teammem",
        runner=RecordingRunner({tuple(_launchctl_print()): 1}),
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
        install_schedule(
            _cfg(tmp_path),
            platform="darwin",
            executable="/opt/pipx/bin/teammem",
            runner=runner,
        )

    assert runner.calls == []
    assert path.read_bytes() == b"old definition"


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


def test_systemd_definitions_are_private_and_parent_replacements_are_fsynced(
    tmp_path, monkeypatch
):
    systemd_dir = tmp_path / "systemd" / "user"
    fsync_kinds = []
    real_fsync = os.fsync

    def record_fsync(descriptor):
        fsync_kinds.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr("teammem.schedule.os.fsync", record_fsync)

    _install_systemd(tmp_path, systemd_dir, RecordingRunner())

    assert stat.S_IMODE(
        (systemd_dir / "teammem-daily.service").stat().st_mode
    ) == 0o600
    assert stat.S_IMODE(
        (systemd_dir / "teammem-daily.timer").stat().st_mode
    ) == 0o600
    assert fsync_kinds.count(False) >= 2
    assert fsync_kinds.count(True) >= 2


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
