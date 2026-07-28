# Operator Scheduling and Installation Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator explicitly schedule `teammem run-daily` on an always-on Mac mini, Linux server, or VPS and make the complete installation lifecycle unmistakable in public documentation.

**Architecture:** `run-daily` remains a one-shot command. A separate schedule module writes and manages a macOS LaunchAgent or Linux systemd user service/timer that invokes the installed executable with the user-only hub environment file; package installation never creates a background job.

**Tech Stack:** Python 3.11+, plistlib, systemd user units, subprocess, pytest

## Global Constraints

- This plan depends on `teammem run-daily` from the provider-neutral connectors plan.
- `pipx install teammem` never installs or enables a schedule.
- Schedule installation requires the explicit `teammem schedule install` command.
- Default hub time is 18:20 in the host's local timezone.
- macOS uses launchd; Linux uses a persistent systemd user timer.
- Scheduler definitions never contain credentials.
- `run-daily` never installs, updates, or removes its own schedule.
- Documentation must not describe a command before its implementation exists.

---

### Task 1: Cross-Platform Schedule Definitions

**Files:**
- Create: `teammem/schedule.py`
- Test: `tests/test_schedule.py`

**Interfaces:**
- Produces: `ScheduleStatus(installed: bool, time: str | None, backend: str, path: Path)`
- Produces: `install_schedule(cfg, time="18:20", platform=None, executable=None, agents_dir=None, systemd_dir=None, runner=None) -> Path`
- Produces: `schedule_status(platform=None, agents_dir=None, systemd_dir=None, runner=None) -> ScheduleStatus`
- Produces: `remove_schedule(platform=None, agents_dir=None, systemd_dir=None, runner=None) -> bool`

- [ ] **Step 1: Write failing time-validation and macOS plist tests**

```python
def test_launchd_schedule_runs_only_run_daily_with_env_file(tmp_path):
    path = install_schedule(
        cfg, agents_dir=tmp_path, platform="darwin",
        executable="/opt/pipx/bin/teammem", runner=fake_runner,
    )
    data = plistlib.loads(path.read_bytes())
    assert data["StartCalendarInterval"] == {"Hour": 18, "Minute": 20}
    assert data["ProgramArguments"] == [
        "/opt/pipx/bin/teammem", "--env-file", str(cfg.env_file), "run-daily",
    ]
    assert "TOKEN" not in path.read_text()
```

Assert invalid values such as `25:00`, `18:99`, and `6:20` raise
`ValueError("schedule time must be HH:MM")`.

- [ ] **Step 2: Write failing Linux unit/timer tests**

```python
def test_systemd_timer_is_persistent_and_uses_local_1820(tmp_path):
    install_schedule(
        cfg, systemd_dir=tmp_path, platform="linux",
        executable="/opt/pipx/bin/teammem", runner=fake_runner,
    )
    timer = (tmp_path / "teammem-daily.timer").read_text()
    service = (tmp_path / "teammem-daily.service").read_text()
    assert "OnCalendar=*-*-* 18:20:00" in timer
    assert "Persistent=true" in timer
    assert "teammem --env-file" in service and "run-daily" in service
    assert "TOKEN" not in timer + service
```

- [ ] **Step 3: Run tests and confirm missing schedule module**

Run: `pytest -q tests/test_schedule.py`

Expected: import fails.

- [ ] **Step 4: Implement launchd management**

Write `~/Library/LaunchAgents/org.teammem.hub-daily.plist` atomically with
`RunAtLoad=false`, calendar time, stdout/stderr under `~/.local/state/teammem/`,
and exact executable/env-file arguments. Invoke `launchctl bootout` before
replacement when already loaded, then `launchctl bootstrap` for the current GUI
user. Inject the command runner in tests.

- [ ] **Step 5: Implement systemd user management**

Write `~/.config/systemd/user/teammem-daily.service` and `.timer` atomically. Run:

```text
systemctl --user daemon-reload
systemctl --user enable --now teammem-daily.timer
```

Removal disables the timer, deletes both files, and reloads the user daemon.
Status reads files and may query `systemctl --user is-enabled`; repeated removal
returns `False`.

- [ ] **Step 6: Run schedule tests**

Run: `pytest -q tests/test_schedule.py`

Expected: all pass without touching the real LaunchAgents or systemd directories.

- [ ] **Step 7: Commit schedule definitions**

```bash
git add teammem/schedule.py tests/test_schedule.py
git commit -m "feat: add explicit operator scheduling"
```

### Task 2: Schedule CLI and Safe Lifecycle

**Files:**
- Modify: `teammem/cli.py`
- Modify: `teammem/config.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_schedule.py`

**Interfaces:**
- Adds: `teammem --env-file PATH`
- Adds: `teammem schedule install --time HH:MM`
- Adds: `teammem schedule status`
- Adds: `teammem schedule remove`

- [ ] **Step 1: Write failing CLI safety tests**

```python
def test_package_commands_do_not_install_schedule(monkeypatch):
    calls = []
    monkeypatch.setattr("teammem.cli.install_schedule", lambda *a, **k: calls.append(1))
    assert main(["connectors", "list"]) == 0
    assert calls == []


def test_schedule_install_defaults_to_1820(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(
        "teammem.cli.install_schedule",
        lambda cfg, time: seen.append(time) or Path("/tmp/job"),
    )
    assert main(["schedule", "install"]) == 0
    assert seen == ["18:20"]
```

- [ ] **Step 2: Run CLI tests and confirm missing commands**

Run: `pytest -q tests/test_cli.py tests/test_schedule.py`

Expected: parser rejects `schedule`.

- [ ] **Step 3: Add global environment-file parsing**

Resolve `--env-file` before `Config.load`. Default to
`~/.config/teammem/hub.env`. Require mode `0600` when the file exists. Keep process
environment precedence.

- [ ] **Step 4: Add schedule subcommands**

Print exact backend, path, and time on install/status. Return 2 with a direct
message on unsupported platforms. Remove is idempotent and prints `not installed`
when absent.

- [ ] **Step 5: Run CLI and schedule tests**

Run: `pytest -q tests/test_cli.py tests/test_schedule.py tests/test_config.py`

Expected: all pass.

- [ ] **Step 6: Commit CLI lifecycle**

```bash
git add teammem/cli.py teammem/config.py tests/test_cli.py tests/test_schedule.py
git commit -m "feat: expose hub schedule lifecycle"
```

### Task 3: README and Deployment Instructions

**Files:**
- Modify: `README.md`
- Modify: `docs/deployment.md`
- Modify: `docs/architecture.md`
- Modify: `docs/privacy.md`
- Modify: `scripts/check-public.sh`

**Interfaces:**
- Documents: install → configure → manual run → explicit schedule install
- Documents: Mac mini launchd and Linux VPS systemd

- [ ] **Step 1: Rewrite the hub quick start around deployment reality**

The README must say that `teammem` runs on an operator-controlled, normally
available Mac mini, Linux server, or VPS. Show:

```bash
pipx install teammem
mkdir -p ~/.config/teammem
chmod 700 ~/.config/teammem
$EDITOR ~/.config/teammem/hub.env
chmod 600 ~/.config/teammem/hub.env
teammem connectors check
teammem run-daily
teammem schedule install --time 18:20
teammem schedule status
```

State beside the commands that installation alone creates no schedule and
`run-daily` runs once.

- [ ] **Step 2: Add complete macOS instructions**

Document the plist path, log paths, status/remove commands, local timezone, what
happens when a Mac was asleep, upgrade order, and removal before uninstall:

```bash
teammem schedule remove
pipx upgrade teammem
teammem run-daily
teammem schedule install --time 18:20
```

- [ ] **Step 3: Add complete Linux VPS instructions**

Document the user unit paths, `systemctl --user` inspection commands, log viewing
with `journalctl --user -u teammem-daily.service`, and the requirement to keep the
user manager alive after logout:

```bash
sudo loginctl enable-linger "$USER"
teammem schedule install --time 18:20
systemctl --user list-timers teammem-daily.timer
```

Explain that polling requires outbound provider/Git access but no inbound public
port.

- [ ] **Step 4: Document missed runs and source-specific expectations**

State that persistent systemd scheduling and collection lookback recover missed
events; idempotency prevents duplicates. Reiterate that the private deployment
continues using Feishu, while public Slack is an optional top-level-message
adapter.

- [ ] **Step 5: Run documentation and package verification**

Run:

```bash
pytest -q
python -m build
./scripts/check-public.sh
```

Expected: all pass, and searches show no outdated claim that operators must create
their own launchd/systemd definition.

- [ ] **Step 6: Commit installation documentation**

```bash
git add README.md docs scripts/check-public.sh
git commit -m "docs: explain operator installation and scheduling"
```
