import os
import plistlib
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
        self.returncodes = returncodes or {}
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
        returncode = self.returncodes.get(tuple(command), 0)
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


def _launchctl_print():
    return ["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"]


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
    assert schedule_status(
        platform="darwin", agents_dir=agents_dir, runner=runner
    ) == schedule_status(
        platform="darwin", agents_dir=agents_dir, runner=RecordingRunner()
    )
    status = schedule_status(
        platform="darwin", agents_dir=agents_dir, runner=RecordingRunner()
    )
    assert status.installed is True
    assert status.time == "18:20"
    assert status.backend == "launchd"
    assert status.path == path


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
        platform="darwin", agents_dir=agents_dir
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

    status = schedule_status(
        platform="linux", systemd_dir=systemd_dir, runner=RecordingRunner()
    )
    assert status.installed is True
    assert status.time == "18:20"
    assert status.backend == "systemd"
    assert status.path == timer_path


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

    runner = RecordingRunner(before=inspect_order)
    assert remove_schedule(
        platform="linux", systemd_dir=systemd_dir, runner=runner
    ) is True
    call_count = len(runner.calls)
    assert remove_schedule(
        platform="linux", systemd_dir=systemd_dir, runner=runner
    ) is False
    assert len(runner.calls) == call_count
    assert runner.commands == [
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
    runner = RecordingRunner({disable: 1})

    with pytest.raises(subprocess.CalledProcessError):
        remove_schedule(
            platform="linux", systemd_dir=systemd_dir, runner=runner
        )

    assert (systemd_dir / "teammem-daily.timer").exists()
    assert (systemd_dir / "teammem-daily.service").exists()
    assert runner.commands == [list(disable)]


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


def test_status_does_not_report_invalid_definition_time(tmp_path):
    agents_dir = tmp_path / "LaunchAgents"
    path = agents_dir / f"{LABEL}.plist"
    agents_dir.mkdir()
    path.write_bytes(
        plistlib.dumps(
            {"StartCalendarInterval": {"Hour": 99, "Minute": 99}}
        )
    )

    status = schedule_status(platform="darwin", agents_dir=agents_dir)

    assert status.installed is True
    assert status.time is None


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
