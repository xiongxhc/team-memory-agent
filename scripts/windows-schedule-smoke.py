"""Exercise one real, disposable Windows Task Scheduler definition in CI.

This is intentionally limited to GitHub's ephemeral Windows runner.  It never
runs the daily workflow or supplies provider credentials.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from teammem.config import Config
from teammem.schedule import install_schedule, remove_schedule, schedule_status
from teammem.schedule_windows import task_name
from teammem.windows_security import current_user_sid


_SAFE_SUFFIX = re.compile(r"[A-Za-z0-9_-]+\Z")
_FIRST_TRIGGER_DELAY = timedelta(minutes=10)
_REPLACEMENT_TRIGGER_DELAY = timedelta(minutes=20)
_ROLLOVER_WAIT_SECONDS = 21 * 60


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument(
        "--suffix",
        default=os.environ.get("GITHUB_RUN_ID", "local"),
        help="unique suffix for isolated CI files, not the managed task name",
    )
    return parser.parse_args()


def _require_ci() -> None:
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
    ):
        raise RuntimeError(
            "Windows scheduler smoke tests run only on GitHub-hosted Actions runners"
        )


def _task_name() -> str:
    return task_name(current_user_sid())


def _delete_task(name: str) -> None:
    subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", name, "/F"],
        capture_output=True,
        check=False,
    )


def _teammem_executable() -> str:
    command = shutil.which("teammem.exe") or shutil.which("teammem")
    if not command:
        raise RuntimeError("installed teammem executable is not on PATH")
    return str(Path(command).resolve())


def _paths(suffix: str) -> tuple[Path, Path]:
    if not _SAFE_SUFFIX.fullmatch(suffix):
        raise ValueError("smoke suffix must contain only letters, digits, _ and -")
    try:
        appdata = Path(os.environ["APPDATA"])
        local_appdata = Path(os.environ["LOCALAPPDATA"])
    except KeyError as failure:
        raise RuntimeError(f"{failure.args[0]} is required on Windows") from None
    return (
        appdata / f"TeamMemory-smoke-{suffix}",
        local_appdata / f"TeamMemory-smoke-{suffix}",
    )


def _future_schedule_times(now: datetime) -> tuple[str, str] | None:
    """Return two safely future local times, or wait for tomorrow's local date."""
    first = now + _FIRST_TRIGGER_DELAY
    replacement = now + _REPLACEMENT_TRIGGER_DELAY
    if first.date() != now.date() or replacement.date() != now.date():
        return None
    return first.strftime("%H:%M"), replacement.strftime("%H:%M")


def _select_future_schedule_times() -> tuple[str, str]:
    """Avoid a catch-up-eligible task while rolling over midnight at most 21 min."""
    deadline = time.monotonic() + _ROLLOVER_WAIT_SECONDS
    while True:
        selected = _future_schedule_times(datetime.now().astimezone())
        if selected is not None:
            return selected
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Windows smoke test could not reach a safe next-day window")
        time.sleep(min(60, remaining))


def run_smoke(suffix: str) -> None:
    env_dir, state_dir = _paths(suffix)
    env_dir.mkdir(parents=True, exist_ok=True)
    env_file = env_dir / "hub.env"
    env_file.write_text("TEAMMEM_SINCE_DAYS=1\n", encoding="utf-8")
    cfg = Config.load(env_file=env_file, require_env_file=True, platform="win32")
    executable = _teammem_executable()
    install_time, replace_time = _select_future_schedule_times()

    install_schedule(
        cfg,
        install_time,
        platform="win32",
        executable=executable,
        windows_state_dir=state_dir,
    )
    installed = schedule_status(
        platform="win32",
        windows_state_dir=state_dir,
        windows_executable=executable,
        windows_env_file=env_file,
    )
    if not installed.installed or installed.time != install_time:
        raise RuntimeError("Windows scheduler did not retain the installed task")

    install_schedule(
        cfg,
        replace_time,
        platform="win32",
        executable=executable,
        windows_state_dir=state_dir,
    )
    replaced = schedule_status(
        platform="win32",
        windows_state_dir=state_dir,
        windows_executable=executable,
        windows_env_file=env_file,
    )
    if not replaced.installed or replaced.time != replace_time:
        raise RuntimeError("Windows scheduler did not retain the replacement task")

    if not remove_schedule(
        platform="win32",
        windows_state_dir=state_dir,
        windows_executable=executable,
        windows_env_file=env_file,
    ):
        raise RuntimeError("Windows scheduler did not remove the smoke task")
    removed = schedule_status(
        platform="win32",
        windows_state_dir=state_dir,
        windows_executable=executable,
        windows_env_file=env_file,
    )
    if removed.installed:
        raise RuntimeError("Windows scheduler retained the removed smoke task")


def main() -> int:
    args = _arguments()
    _require_ci()
    name = _task_name()
    if args.cleanup_only:
        _delete_task(name)
        env_dir, state_dir = _paths(args.suffix)
        shutil.rmtree(env_dir, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)
        return 0
    env_dir, state_dir = _paths(args.suffix)
    try:
        run_smoke(args.suffix)
    finally:
        _delete_task(name)
        shutil.rmtree(env_dir, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
