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
            if command[:3] == ["systemctl", "--user", "is-enabled"]
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
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "teammem-daily.timer"],
    ]
    assert all(
        kwargs == {"check": True, "capture_output": True, "text": True}
        for _, kwargs in runner.calls
    )
    assert sorted(item.name for item in systemd_dir.iterdir()) == [
        "teammem-daily.service",
        "teammem-daily.timer",
    ]

    enabled = (
        "systemctl",
        "--user",
        "is-enabled",
        "teammem-daily.timer",
    )
    status_runner = RecordingRunner({enabled: 0})
    status = schedule_status(
        platform="linux", systemd_dir=systemd_dir, runner=status_runner
    )
    assert status.installed is True
    assert status.time == "18:20"
    assert status.backend == "systemd"
    assert status.path == timer_path
    assert status_runner.commands == [list(enabled)]


def test_systemd_remove_is_idempotent_and_uses_explicit_user_commands(tmp_path):
    systemd_dir = tmp_path / "systemd" / "user"
    cfg = _cfg(tmp_path)
    install_schedule(
        cfg,
        platform="linux",
        systemd_dir=systemd_dir,
        executable="/opt/pipx/bin/teammem",
        runner=RecordingRunner(),
    )
    timer_path = systemd_dir / "teammem-daily.timer"
    service_path = systemd_dir / "teammem-daily.service"

    def inspect_order(command):
        if "disable" in command:
            assert timer_path.exists() and service_path.exists()
        if "daemon-reload" in command:
            assert not timer_path.exists() and not service_path.exists()

    enabled = (
        "systemctl",
        "--user",
        "is-enabled",
        "teammem-daily.timer",
    )
    runner = RecordingRunner({enabled: 0}, before=inspect_order)
    assert remove_schedule(
        platform="linux", systemd_dir=systemd_dir, runner=runner
    ) is True
    call_count = len(runner.calls)
    assert remove_schedule(
        platform="linux", systemd_dir=systemd_dir, runner=runner
    ) is False
    assert len(runner.calls) == call_count
    assert runner.commands == [
        list(enabled),
        [
            "systemctl",
            "--user",
            "disable",
            "--now",
            "teammem-daily.timer",
        ],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_systemd_remove_failure_preserves_definitions(tmp_path):
    systemd_dir = tmp_path / "systemd" / "user"
    install_schedule(
        _cfg(tmp_path),
        platform="linux",
        systemd_dir=systemd_dir,
        executable="/opt/pipx/bin/teammem",
        runner=RecordingRunner(),
    )
    disable = (
        "systemctl",
        "--user",
        "disable",
        "--now",
        "teammem-daily.timer",
    )
    enabled = (
        "systemctl",
        "--user",
        "is-enabled",
        "teammem-daily.timer",
    )
    runner = RecordingRunner({enabled: 0, disable: 1})

    with pytest.raises(subprocess.CalledProcessError):
        remove_schedule(
            platform="linux", systemd_dir=systemd_dir, runner=runner
        )

    assert (systemd_dir / "teammem-daily.timer").exists()
    assert (systemd_dir / "teammem-daily.service").exists()
    assert runner.commands == [list(enabled), list(disable)]


@pytest.mark.parametrize("failure_stage", ["write", "reload", "enable"])
def test_systemd_install_failure_restores_pair_and_enabled_state(
    tmp_path, monkeypatch, failure_stage
):
    systemd_dir, service_path, timer_path = _old_systemd_pair(tmp_path)
    enabled = tuple(_systemctl("is-enabled", "teammem-daily.timer"))
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
        install_schedule(
            _cfg(tmp_path),
            platform="linux",
            systemd_dir=systemd_dir,
            executable="/opt/pipx/bin/teammem",
            runner=runner,
        )

    assert service_path.read_bytes() == b"old service"
    assert timer_path.read_bytes() == b"old timer"
    expected = [list(enabled)]
    if failure_stage != "write":
        expected.append(list(reload_command))
    if failure_stage == "enable":
        expected.append(list(enable))
    expected.extend([list(reload_command), list(enable)])
    assert runner.commands == expected


def test_systemd_failed_first_install_removes_definitions_and_disables_timer(
    tmp_path,
):
    systemd_dir = tmp_path / "systemd" / "user"
    enable = (
        "systemctl",
        "--user",
        "enable",
        "--now",
        "teammem-daily.timer",
    )
    runner = RecordingRunner({enable: 9})

    with pytest.raises(
        RuntimeError,
        match="^systemd schedule installation failed; previous state restored$",
    ):
        install_schedule(
            _cfg(tmp_path),
            platform="linux",
            systemd_dir=systemd_dir,
            executable="/opt/pipx/bin/teammem",
            runner=runner,
        )

    assert not (systemd_dir / "teammem-daily.service").exists()
    assert not (systemd_dir / "teammem-daily.timer").exists()
    assert runner.commands == [
        ["systemctl", "--user", "daemon-reload"],
        list(enable),
        ["systemctl", "--user", "daemon-reload"],
        [
            "systemctl",
            "--user",
            "disable",
            "--now",
            "teammem-daily.timer",
        ],
    ]


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
    expected = []
    if existing == "timer":
        expected.append(
            [
                "systemctl",
                "--user",
                "is-enabled",
                "teammem-daily.timer",
            ]
        )
    expected.append(["systemctl", "--user", "daemon-reload"])
    assert runner.commands == expected


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
    ("state", "expected_time", "expected_commands"),
    [
        (
            "disabled",
            "18:20",
            [_systemctl("is-enabled", "teammem-daily.timer")],
        ),
        ("service-only", None, []),
        ("timer-only", "18:20", []),
        ("malformed", "18:20", []),
    ],
)
def test_systemd_status_requires_complete_valid_enabled_definition(
    tmp_path, state, expected_time, expected_commands
):
    systemd_dir = tmp_path / "systemd" / "user"
    install_schedule(
        _cfg(tmp_path),
        platform="linux",
        systemd_dir=systemd_dir,
        executable="/opt/pipx/bin/teammem",
        runner=RecordingRunner(),
    )
    if state == "service-only":
        (systemd_dir / "teammem-daily.timer").unlink()
    elif state == "timer-only":
        (systemd_dir / "teammem-daily.service").unlink()
    elif state == "malformed":
        (systemd_dir / "teammem-daily.service").write_text("malformed")
    runner = RecordingRunner()

    status = schedule_status(
        platform="linux", systemd_dir=systemd_dir, runner=runner
    )

    assert status.installed is False
    assert status.time == expected_time
    assert runner.commands == expected_commands


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

    install_schedule(
        _cfg(tmp_path),
        platform="linux",
        systemd_dir=systemd_dir,
        executable="/opt/pipx/bin/teammem",
        runner=RecordingRunner(),
    )

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
