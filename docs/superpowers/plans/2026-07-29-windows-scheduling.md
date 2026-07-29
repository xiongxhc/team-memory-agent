# Windows Operator Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe current-user Windows Task Scheduler support while preserving
the existing macOS launchd and Linux systemd schedule lifecycle.

**Architecture:** Keep `teammem.schedule` as the portable API and explicit
dispatcher, move the current POSIX implementation into `schedule_unix`, and add
a Windows backend that generates and validates Task Scheduler XML registered via
`schtasks.exe`. Close the known launchd and systemd defects before the platform
split so their regression evidence remains isolated.

**Tech Stack:** Python 3.11+, `plistlib`, systemd user units, Task Scheduler XML,
`schtasks.exe`, `ctypes`, `msvcrt`, `xml.etree.ElementTree`, pytest, GitHub Actions

## Global Constraints

- `teammem run-daily` remains a one-shot command.
- Package installation and ordinary package commands never create a schedule.
- Windows uses generated Task Scheduler XML registered by `schtasks.exe`.
- The Windows task uses the current user, `InteractiveToken`,
  `LeastPrivilege`, and no stored password.
- The default time is 18:20 in the host's local timezone.
- Windows uses `StartWhenAvailable=true` and
  `MultipleInstancesPolicy=IgnoreNew`.
- The action directly runs the absolute `teammem.exe` with
  `--env-file <absolute-path> run-daily`; it never uses PowerShell or `cmd.exe`.
- Scheduler definitions never contain provider, Git, or Windows credentials.
- Windows execution is logged-in-only; a locked screen is supported, logout is
  not.
- Existing launchd/systemd paths, manager commands, missed-run semantics, logs,
  CLI output, and public Python interfaces remain compatible.
- `ScheduleStatus(installed, time, backend, path)`, `install_schedule`,
  `schedule_status`, and `remove_schedule` remain public.
- Unknown platforms are rejected explicitly and never fall through to Linux.
- No production scheduler is touched by unit tests.
- Do not push.

---

### Task 1: Close the Known Unix Definition Defects

**Files:**
- Modify: `teammem/schedule.py:283-284`
- Modify: `teammem/schedule.py:389-442`
- Modify: `teammem/schedule.py:475-483`
- Modify: `tests/test_schedule.py`

**Interfaces:**
- Preserves: `_systemd_exec(arguments: Sequence[str]) -> str`
- Preserves: `_parse_systemd_exec(command: str | None) -> list[str]`
- Preserves: `_parse_launchd(definition: bytes | None) -> tuple[bool, str | None]`

- [ ] **Step 1: Add the launchd oversized-integer regression test**

Add a canonical-looking XML plist whose `Hour` integer is
`18446744073709551616`. Assert status returns not installed and never queries
launchd:

```python
def test_launchd_status_rejects_unserializable_integer_without_manager_query(
    tmp_path,
):
    agents_dir, path = _installed_launchd_definition(tmp_path)
    xml = path.read_text().replace(
        "<integer>18</integer>",
        "<integer>18446744073709551616</integer>",
        1,
    )
    path.write_text(xml)
    runner = RecordingRunner()

    status = schedule_status(
        platform="darwin", agents_dir=agents_dir, runner=runner
    )

    assert status.installed is False
    assert status.time is None
    assert runner.calls == []
```

- [ ] **Step 2: Run the launchd regression and confirm RED**

Run:

```bash
.venv/bin/pytest -q tests/test_schedule.py \
  -k unserializable_integer
```

Expected: FAIL with uncaught `OverflowError`.

- [ ] **Step 3: Catch canonical serialization failures**

Extend `_parse_launchd`'s validation boundary to catch `OverflowError` together
with its existing invalid-definition exceptions. Do not broaden it to
`Exception`.

- [ ] **Step 4: Add the complete systemd argument-codec matrix**

Parameterize generated and parsed arguments:

```python
@pytest.mark.parametrize(
    "value",
    [
        "/opt/Team Mem/teammem",
        "/opt/team'mem/teammem",
        r"C:\tools\teammem",
        "/opt/$USER/teammem",
        "/opt/100%/teammem",
        "/opt/团队/teammem",
    ],
)
def test_systemd_exec_round_trips_generator_values(value):
    command = schedule_module._systemd_exec(
        [value, "--env-file", "/tmp/hub.env", "run-daily"]
    )
    assert schedule_module._parse_systemd_exec(command) == [
        value, "--env-file", "/tmp/hub.env", "run-daily"
    ]


@pytest.mark.parametrize("value", ["line\nbreak", "nul\0byte", "tab\tvalue"])
def test_systemd_exec_rejects_control_characters(value):
    with pytest.raises(ValueError, match="unsafe systemd argument"):
        schedule_module._systemd_exec([value])
```

Also assert generated literal dollar is `$$`, literal percent is `%%`, raw
systemd specifiers such as `%h` remain invalid, and noncanonical hand-written
quoting is rejected.

- [ ] **Step 5: Run the codec tests and confirm RED**

Run:

```bash
.venv/bin/pytest -q tests/test_schedule.py -k 'systemd_exec'
```

Expected: failures for apostrophe, backslash, dollar, and control-character
cases under the POSIX `shlex` codec.

- [ ] **Step 6: Implement the narrow systemd codec**

Replace `shlex.join` with helpers that:

```python
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
```

Implement the inverse only for this canonical grammar. `_parse_systemd_exec`
must decode, regenerate with `_systemd_exec`, and require byte-for-byte equality.
Remove the unused `shlex` import.

- [ ] **Step 7: Run focused and full schedule tests**

Run:

```bash
.venv/bin/pytest -q tests/test_schedule.py
```

Expected: all schedule tests pass and no real manager commands run.

- [ ] **Step 8: Commit the Unix defect closure**

```bash
git add teammem/schedule.py tests/test_schedule.py
git commit -m "fix: close scheduler definition edge cases"
```

---

### Task 2: Split the Portable Facade from the Unix Backend

**Files:**
- Rewrite: `teammem/schedule.py`
- Create: `teammem/schedule_unix.py`
- Modify: `tests/test_schedule.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: portable `DEFAULT_TIME`, `ScheduleStatus`, `_parse_time`,
  `install_schedule`, `schedule_status`, `remove_schedule`
- Produces: Unix backend functions accepting the existing injected paths and
  runner
- Preserves all existing public signatures and re-exports the existing
  `LABEL`, `SYSTEMD_SERVICE`, and `SYSTEMD_TIMER` constants for compatibility

- [ ] **Step 1: Add import-isolation and explicit-dispatch tests**

```python
def test_facade_selects_windows_without_importing_unix(monkeypatch):
    imported = []
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        imported.append(name)
        if name in {"fcntl", "teammem.schedule_unix"}:
            raise AssertionError("Unix backend imported")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    assert schedule_module._backend("win32") == "windows"
    assert "fcntl" not in imported


def test_unknown_platform_does_not_fall_through_to_systemd():
    with pytest.raises(
        RuntimeError, match="unsupported scheduling platform: freebsd"
    ):
        schedule_module._backend("freebsd")
```

Keep the existing subprocess-level Windows CLI import test and change its
expected result from unsupported to a lazily loaded Windows backend seam. It
must import the portable facade successfully while rejecting only `fcntl` and
`teammem.schedule_unix`; inject a fake `teammem.schedule_windows` module so no
real scheduler command runs.

- [ ] **Step 2: Run the dispatch tests and confirm RED**

Run:

```bash
.venv/bin/pytest -q tests/test_schedule.py tests/test_cli.py \
  -k 'facade or unknown_platform or windows_cli_imports'
```

Expected: Windows is rejected and importing `teammem.schedule` imports `fcntl`.

- [ ] **Step 3: Move Unix code without behavioral edits**

Move launchd/systemd constants and functions to `schedule_unix.py`. Keep Unix
function arguments explicit:

```python
def install_schedule(
    cfg, hour, minute, executable, *,
    backend, agents_dir=None, systemd_dir=None, runner=None,
) -> Path: ...

def schedule_status(
    *, backend, agents_dir=None, systemd_dir=None, runner=None
) -> ScheduleStatus: ...

def remove_schedule(
    *, backend, agents_dir=None, systemd_dir=None, runner=None
) -> bool: ...
```

Import `fcntl` only in this file.

- [ ] **Step 4: Implement the portable facade**

The facade explicitly maps `darwin -> launchd`, `linux* -> systemd`, and
`win32 -> windows`, validates time before backend loading, resolves the
executable, and lazily imports only the selected backend.

Keep optional parameters in the public functions. Add Windows-only injection
parameters `windows_api=None`, `windows_runner=None`,
`windows_state_dir=None`, and `windows_task_name=None` at the end with defaults
so existing callers remain valid. The task-name override exists for isolated
tests/CI and must still carry the TeamMem ownership marker and current SID.
`windows_runner` is separate from the existing Unix text runner: it returns
`CompletedProcess[bytes]` and is invoked with `capture_output=True` and
`text=False`.

- [ ] **Step 5: Run the complete existing CLI and scheduler suites**

Run:

```bash
.venv/bin/pytest -q tests/test_schedule.py tests/test_cli.py tests/test_config.py
```

Expected: all prior macOS/Linux assertions pass unchanged; Windows import
isolation passes.

- [ ] **Step 6: Commit the backend split**

```bash
git add teammem/schedule.py teammem/schedule_unix.py \
  tests/test_schedule.py tests/test_cli.py
git commit -m "refactor: isolate platform schedule backends"
```

---

### Task 3: Build the Pure Windows Identity, Argument, ACL, and XML Layer

**Files:**
- Create: `teammem/schedule_windows.py`
- Create: `teammem/windows_security.py`
- Create: `tests/test_schedule_windows.py`
- Modify: `teammem/config.py`
- Modify: `teammem/cli.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: `current_user_sid(api=None) -> str`
- Produces: `task_name(sid: str) -> str`
- Produces: `encode_arguments(arguments: Sequence[str]) -> str`
- Produces: `decode_arguments(command_line: str) -> list[str]`
- Produces: `build_task_xml(...) -> bytes`
- Produces: `parse_task_xml(xml: bytes, expected: WindowsSchedule) -> str`
- Produces:
  `validate_windows_env_file(path: Path, sid: str, api=None) -> Path`
  through an injected native API seam
- Produces:
  `read_windows_env_file(path: Path, sid: str, api=None) -> list[str]`
  that validates and reads through the same native file handle
- Produces:
  `default_env_file(platform: str | None = None, env: Mapping | None = None) -> Path`
- Produces: `Config.load(..., platform=None, windows_api=None)` while keeping
  existing callers valid

- [ ] **Step 1: Write SID and task-name tests**

```python
def test_task_name_is_stable_per_sid_without_exposing_sid():
    sid = "S-1-5-21-111-222-333-1001"
    name = windows.task_name(sid)
    assert re.fullmatch(r"\\\\TeamMem-Daily-[0-9a-f]{12}", name)
    assert sid not in name
    assert name == windows.task_name(sid)
```

Inject a fake Win32 token API and assert the current SID is requested from the
process token without invoking PowerShell, `cmd.exe`, or `whoami`.

- [ ] **Step 2: Write Windows command-line round-trip tests**

Cover spaces, empty strings, quotes, trailing backslashes, Unicode, dollar, and
percent:

```python
@pytest.mark.parametrize(
    "arguments",
    [
        ["--env-file", r"C:\Users\Alex\App Data\hub.env", "run-daily"],
        ["--env-file", 'C:\\path with "quote"\\hub.env', "run-daily"],
        ["--env-file", "C:\\团队\\hub.env", "run-daily"],
    ],
)
def test_windows_arguments_round_trip_canonically(arguments):
    encoded = windows.encode_arguments(arguments)
    assert windows.decode_arguments(encoded) == arguments
    assert windows.encode_arguments(windows.decode_arguments(encoded)) == encoded
```

Reject NUL, newline, carriage return, tab, and other control characters. Use
Windows path semantics independently of the host: accept drive-absolute and
UNC executable/env paths using `ntpath.isabs` or `PureWindowsPath`; reject
drive-relative (`C:teammem.exe`), rootless, and ordinary relative paths.

- [ ] **Step 3: Write canonical XML tests**

Assert deterministic UTF-16LE+BOM output containing exactly:

- current SID, `InteractiveToken`, `LeastPrivilege`;
- one daily trigger at `18:20`, `DaysInterval=1`, enabled;
- `StartWhenAvailable=true`, `IgnoreNew`;
- battery false/false, network false, wake false;
- exact `ExecutionTimeLimit=PT4H`;
- one `Exec` action with absolute executable and encoded arguments;
- exact ownership source/URI/description;
- no token or environment-file content.

Round-trip the generated XML through `parse_task_xml`.

- [ ] **Step 4: Write XML rejection tests**

Parameterize mutations for wrong SID, foreign marker, password/S4U logon, highest
privilege, extra trigger/action/principal, disabled trigger, wrong time,
`StartWhenAvailable=false`, non-`IgnoreNew`, shell executable, relative paths,
altered `ExecutionTimeLimit`, altered arguments, malformed XML, and entity
declarations. Each must raise a
sanitized `RuntimeError("Windows schedule definition is not managed by TeamMem")`.

- [ ] **Step 5: Write Windows environment-file security tests**

Through a fake native security API, assert acceptance only for a regular
non-reparse-point file owned by the current SID with no read allow ACE for
Everyone, Authenticated Users, or built-in Users. Assert Administrators and
SYSTEM ACEs are allowed and errors never include file contents. Add a race test
that swaps the path after opening and proves validation and reading remain tied
to the original handle.

Apply the same ownership/DACL/reparse rules to
`%LOCALAPPDATA%\TeamMemory` before creating `schedule.lock` or temporary XML.
Reject an unsafe existing directory without creating any artifact.

- [ ] **Step 6: Run the new tests and confirm RED**

Run:

```bash
.venv/bin/pytest -q tests/test_schedule_windows.py tests/test_config.py
```

Expected: import fails because `schedule_windows` and Windows config validation
do not exist.

- [ ] **Step 7: Implement pure Windows helpers**

Put SID and Windows file-security helpers in `windows_security.py` so
`config.py` never imports the scheduler backend. Use lazy `ctypes` imports
behind native helper functions so unit tests run on macOS/Linux. Use
`xml.etree.ElementTree` for generation/parsing, reject `DOCTYPE`/`ENTITY` before
parsing, normalize namespace tags, and validate semantic structure rather than
whitespace or namespace-prefix spelling.

Generate XML with an explicit encoding declaration and UTF-16LE BOM. Parse
queried XML as bytes so its BOM controls decoding.

Open the Windows environment file with `CreateFileW` using
`FILE_FLAG_OPEN_REPARSE_POINT`, validate type/reparse attributes, owner, and
DACL from that handle, then convert the same handle with
`msvcrt.open_osfhandle` for UTF-8 reading. Never validate one path object and
reopen it through ordinary `open()`.

Factor the current `KEY=VALUE` parser into `_parse_env_lines(lines, path)`.
`read_env_file` dispatches at call time: Windows obtains lines from
`read_windows_env_file`; Unix retains the current descriptor/owner/`0600`
path. Both feed the same parser and return the same `dict[str, str]`.

- [ ] **Step 8: Add the Windows environment-file default**

Resolve the default at call time:

```python
def default_env_file(
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    current = sys.platform if platform is None else platform
    if current == "win32":
        values = os.environ if env is None else env
        root = values.get("APPDATA")
        if not root:
            raise RuntimeError("APPDATA is required on Windows")
        return Path(root) / "TeamMemory" / "hub.env"
    return Path("~/.config/teammem/hub.env").expanduser()
```

Preserve explicit `--env-file`. Do not evaluate `%APPDATA%` at module import.
`Config.load(env_file=None)` calls `default_env_file()` and passes its
platform/API seam into `read_env_file`. `_parser()` also calls
`default_env_file()` when it is built, so Windows never starts with the POSIX
default. Tests inject `platform="win32"` and an environment mapping.

- [ ] **Step 9: Run pure Windows/config tests**

Run:

```bash
.venv/bin/pytest -q tests/test_schedule_windows.py tests/test_config.py
```

Expected: all pass on macOS/Linux through injected APIs.

- [ ] **Step 10: Commit the pure Windows layer**

```bash
git add teammem/schedule_windows.py teammem/windows_security.py \
  teammem/config.py teammem/cli.py tests/test_schedule_windows.py \
  tests/test_config.py tests/test_cli.py
git commit -m "feat: define Windows schedule security contract"
```

---

### Task 4: Implement Transactional `schtasks.exe` Lifecycle and CLI

**Files:**
- Modify: `teammem/schedule_windows.py`
- Modify: `teammem/schedule.py`
- Modify: `teammem/cli.py`
- Modify: `tests/test_schedule_windows.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: Windows backend `install_schedule`, `schedule_status`,
  `remove_schedule`
- Consumes: Task 2 facade and Task 3 identity/XML/security helpers
- Preserves: CLI output
  `installed: backend=windows path=<task> time=<HH:MM>`

- [ ] **Step 1: Add a byte-preserving fake `schtasks.exe` runner**

The fake runner must model exact task-name to XML state and support injected
failures for query, create, delete, verification, restore, and cleanup. Record
commands and keyword arguments. Query stdout is bytes, not console-decoded
text. Every invocation asserts `capture_output=True` and `text=False`.

- [ ] **Step 2: Write status tests**

Cover:

```python
def test_windows_status_reports_absent_without_side_effects(...): ...
def test_windows_status_validates_registered_xml(...): ...
def test_windows_status_rejects_foreign_same_name_task(...): ...
def test_windows_status_sanitizes_query_failure(...): ...
```

Assert the exact query command is:

```text
schtasks.exe /Query /TN \TeamMem-Daily-<hash> /XML
```

Do not classify localized stderr text. When the exact XML query fails, run
`schtasks.exe /Query /FO CSV /NH` and parse the first CSV column:

- successful list query with no exact task name means absent;
- successful list query containing the task means the XML query failed;
- failed list query means scheduler status is unavailable.

Absent returns `ScheduleStatus(False, None, "windows", Path(task_name))`.
Foreign or malformed same-name tasks raise the conflict error.

- [ ] **Step 3: Write lifecycle-lock tests**

Use an injected lock seam for hermetic tests and a focused Windows-only test for
the initialized one-byte `%LOCALAPPDATA%\TeamMemory\schedule.lock` file.
Assert the lock encloses snapshot, create/delete, verification, and rollback.
Assert state-directory security validation completes before the lock file is
created.

- [ ] **Step 4: Write first-install and replacement tests**

Assert install:

1. validates identity, executable, env path, and ACL;
2. snapshots absence or valid prior XML;
3. writes a private XML file under the validated per-user state directory;
4. invokes `/Create /TN ... /XML ... /F`;
5. re-queries and validates;
6. removes the temporary XML;
7. refuses to overwrite a foreign task.

- [ ] **Step 5: Write install rollback tests**

Cover create failure, verification failure, restore success, first-install
cleanup, rollback failure, and temporary-file cleanup. A successful rollback
raises:

```text
Windows schedule installation failed; previous state restored
```

A failed rollback raises:

```text
Windows schedule installation failed and rollback failed
```

No error may contain XML, environment values, provider tokens, or subprocess
output.

- [ ] **Step 6: Write remove and rollback tests**

Assert absent removal returns `False`, valid removal snapshots then deletes and
verifies absence, foreign tasks are refused, repeated removal is idempotent, and
verification failure restores the snapshot. Document/test that removal does not
terminate an already-running process.

- [ ] **Step 7: Run lifecycle tests and confirm RED**

Run:

```bash
.venv/bin/pytest -q tests/test_schedule_windows.py tests/test_cli.py \
  -k 'windows or schtasks'
```

Expected: backend lifecycle functions are missing and CLI still rejects win32.

- [ ] **Step 8: Implement the lifecycle**

Use a `WindowsRunner` that captures bytes. Recognize not-found only through the
documented return code plus an injected/classified result; treat all other
failures as sanitized scheduler errors.

Create temporary XML only inside `%LOCALAPPDATA%\TeamMemory`, under the lifecycle
lock. Restore exact prior queried bytes through `/Create /XML /F`. Verify the
restored or absent state before releasing the lock.

- [ ] **Step 9: Enable Windows CLI dispatch**

Change `_schedule_backend()` to return `"windows"` on `sys.platform == "win32"`.
Keep lazy `_schedule_api()` loading. Status/removal continue to ignore a missing
or broken env file; install requires the Windows-secure env file.

- [ ] **Step 10: Run Windows, facade, CLI, and full unit suites**

Run:

```bash
.venv/bin/pytest -q tests/test_schedule_windows.py \
  tests/test_schedule.py tests/test_cli.py tests/test_config.py
.venv/bin/pytest -q
```

Expected: all pass with no real `schtasks`, launchd, or systemd mutations.

- [ ] **Step 11: Commit the Windows lifecycle**

```bash
git add teammem/schedule_windows.py teammem/schedule.py teammem/cli.py \
  tests/test_schedule_windows.py tests/test_cli.py
git commit -m "feat: add Windows Task Scheduler lifecycle"
```

---

### Task 5: Document and Verify Windows Operation

**Files:**
- Modify: `README.md`
- Modify: `docs/deployment.md`
- Modify: `docs/architecture.md`
- Modify: `docs/privacy.md`
- Modify: `scripts/check-public.sh`
- Modify: `.github/workflows/test.yml`
- Modify: `pyproject.toml`
- Create: `scripts/windows-schedule-smoke.py`

**Interfaces:**
- Documents: Windows install/status/remove/upgrade/troubleshooting lifecycle
- Verifies: real ephemeral Task Scheduler create/query/replace/delete in Windows
  CI

- [ ] **Step 1: Add public-documentation contract tests**

Extend `scripts/check-public.sh` to require Windows documentation and reject
claims that Windows runs after logout, stores a password, uses S4U, or invokes a
shell wrapper. Keep all existing macOS/Linux checks.

- [ ] **Step 2: Write Windows operator instructions**

Document:

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\\TeamMemory"
notepad "$env:APPDATA\\TeamMemory\\hub.env"
teammem --env-file "$env:APPDATA\\TeamMemory\\hub.env" run-daily
teammem schedule install --time 18:20
teammem schedule status
teammem schedule remove
```

State clearly:

- installation alone creates no task;
- the task is current-user, least-privilege, and logged-in-only;
- screen lock is fine, logout prevents runs;
- `StartWhenAvailable` catches a missed trigger after the user is available;
- the machine must remain powered;
- Task Scheduler History and Last Run Result provide scheduler evidence;
- manual `run-daily` provides detailed output;
- remove before uninstalling and reinstall after upgrade;
- password/service-account/logged-out operation is unsupported.

- [ ] **Step 3: Update architecture, privacy, and package metadata**

Describe the portable facade and three backends. Document that XML contains only
executable/config paths and the current SID, never secrets. Keep the
`Operating System :: OS Independent` classifier only after all three platform
test contracts pass; otherwise replace it with explicit MacOS, POSIX Linux, and
Microsoft Windows classifiers.

- [ ] **Step 4: Add the Windows CI smoke script**

The script receives a unique suffix, creates an isolated env/state directory,
uses the installed `teammem.exe`, installs a uniquely named test task through an
explicit test-only task-name injection, validates status, replaces its time,
validates again, and removes it.

Wrap the entire body:

```python
try:
    run_smoke()
finally:
    subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
        capture_output=True,
    )
```

It must not run `run-daily` or use provider credentials.

- [ ] **Step 5: Add the `windows-latest` CI job**

Add a separate job that:

```yaml
windows-schedule:
  runs-on: windows-latest
  steps:
    - uses: actions/checkout@v7
    - uses: actions/setup-python@v7
      with:
        python-version: "3.12"
    - run: python -m pip install -e ".[dev]"
    - run: pytest -q tests/test_schedule_windows.py tests/test_cli.py tests/test_config.py
    - run: python scripts/windows-schedule-smoke.py
```

Use an unconditional cleanup step in YAML in addition to the script's `finally`.

- [ ] **Step 6: Run local verification**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q teammem packages/memberkit/memberkit
./scripts/check-public.sh
.venv/bin/python -m build
(cd packages/memberkit && ../../.venv/bin/python -m build)
git diff --check
```

Expected: all local tests, both builds, public scan, compilation, and diff check
pass. Record that no real Windows manager was available locally.

- [ ] **Step 7: Commit documentation and CI**

```bash
git add README.md docs scripts/check-public.sh scripts/windows-schedule-smoke.py \
  .github/workflows/test.yml pyproject.toml
git commit -m "docs: explain Windows operator scheduling"
```

- [ ] **Step 8: Verify CI and branch identity**

After the branch is pushed only with explicit user authorization, require the
`windows-schedule` job to pass and confirm the real create/query/replace/delete
cleanup. Before any push, verify every new commit has:

```text
Chris Xiong <xionghx713@gmail.com>
```

No push is part of this plan.
