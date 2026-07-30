# MemberKit Windows Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing MemberKit setup and schedule commands select
launchd on macOS or a safe current-user Task Scheduler definition on Windows,
while keeping MemberKit independently installable and deferring Linux automatic
scheduling.

**Architecture:** Keep `memberkit.schedule` as a lazy platform facade, move the
existing macOS implementation unchanged to `schedule_macos`, and add a
MemberKit-owned Windows backend based on the hub scheduler's proven XML,
validation, and rollback patterns. Add Windows-native private configuration,
best-effort current-user reminders, durable bounded logs, real Windows CI, and
platform-neutral documentation.

**Tech Stack:** Python 3.11+, `plistlib`, `ctypes`, `msvcrt`,
`xml.etree.ElementTree`, Task Scheduler XML, `schtasks.exe`, `msg.exe`, `tzdata`,
pytest, GitHub Actions

## Global Constraints

- The public commands remain `memberkit setup`, `memberkit schedule install`,
  `memberkit schedule status`, `memberkit schedule remove`, and
  `memberkit scheduled-run`.
- `darwin` uses the existing launchd behavior; `win32` uses Task Scheduler.
- Linux automatic installation is explicitly unsupported in this change.
- `teammem-memberkit` remains independently installable and never imports or
  depends on the hub `teammem` package.
- Package installation never creates a schedule.
- The default schedule is `17:30` in the host's local timezone.
- Windows uses `InteractiveToken`, `LeastPrivilege`, `StartWhenAvailable=true`,
  `MultipleInstancesPolicy=IgnoreNew`, `WakeToRun=false`, and
  `ExecutionTimeLimit=PT4H`.
- The Windows action directly runs absolute `memberkit.exe scheduled-run`; it
  never uses PowerShell, `cmd.exe`, a working directory, or configuration
  values.
- Windows Task Scheduler XML and errors never contain inbox URLs, credentials,
  event summaries, journals, or bundle content.
- The lifecycle lock serializes cooperating MemberKit commands. Every observed
  unmanaged or conflicting task is refused and preserved.
- Initial registration after an absent query is create-only and omits `/F`, so
  a same-name collision fails without overwriting.
- Another Task Scheduler client using the same Windows identity can race between
  query, revalidation, and mutation. That non-cooperating concurrency is outside
  the transaction guarantee; no atomic compare-and-swap or malicious same-user
  integrity is claimed. A stronger boundary requires a separately privileged
  principal or service.
- The native runner trusts the resolved system `schtasks.exe`. A malicious
  same-user replacement executable or deliberately noisy descendant is outside
  the threat boundary.
- Windows configuration is private before content is written and is validated
  through the same opened handle on every read.
- Windows reminders target only the verified current-process session ID with
  `/TIME:60`; reminder failure is non-fatal.
- Scheduled runs never import push code, approve, commit, push, or transmit.
- Existing member-edited and malformed draft preservation remains unchanged.
- Unit tests never touch a production scheduler.
- All commits use `Chris Xiong <xionghx713@gmail.com>`.
- Do not push.

---

### Task 1: Windows Security and Transactional Private Files

**Files:**

- Create: `packages/memberkit/memberkit/windows_security.py`
- Create: `packages/memberkit/tests/test_windows_security.py`
- Reference only: `teammem/windows_security.py`
- Reference only: `tests/test_schedule_windows.py`

**Interfaces:**

- Produces:

```python
def current_user_sid(api: Any = None) -> str: ...
def current_username(api: Any = None) -> str: ...
def current_session_id(api: Any = None) -> int: ...
def validate_windows_private_file(
    path: Path, sid: str, api: Any = None
) -> Path: ...
def read_windows_private_text(
    path: Path, sid: str, api: Any = None
) -> str: ...
def validate_windows_private_dir(
    path: Path, sid: str, api: Any = None
) -> Path: ...
def provision_windows_private_dir(
    path: Path, sid: str, api: Any = None
) -> Path: ...
def atomic_write_windows_private_text(
    path: Path, text: str, sid: str, api: Any = None
) -> Path: ...
```

- Produces: lazy `NativeWindowsApi`; importing the module off Windows does not
  initialize native libraries.
- Consumed by: Tasks 2, 5, and 6.

- [ ] **Step 1: Read the test-design rules**

Read the complete `superpowers:test-driven-development/writing-good-tests.md`
reference before editing tests.

- [ ] **Step 2: Write the failing identity, path, and handle-validation tests**

Create fake file/directory records and an injected API. Cover current SID,
username, validated current-process session ID, drive-absolute and complete UNC
paths, and rejection of device paths, drive-relative paths, incomplete UNC
paths, non-disk handles, reparse points, wrong owners, inherited DACLs, and read
ACEs for these principals:

```python
RESTRICTED_READ_SIDS = {
    "S-1-1-0",
    "S-1-5-11",
    "S-1-5-32-545",
}
```

Assert `read_windows_private_text()` validates and reads the same opened handle
even when the path's fake record is replaced during the read.

- [ ] **Step 3: Run RED for missing security implementation**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_windows_security.py
```

Expected: collection fails because `memberkit.windows_security` does not exist.

- [ ] **Step 4: Implement lazy identity and same-handle validation**

Use `ntpath` for Windows path syntax. Open with
`FILE_FLAG_OPEN_REPARSE_POINT`; configure every `ctypes` handle as
`ctypes.c_void_p`. Validation requires:

```python
{
    "file_type": "disk",
    "owner_sid": current_sid,
    "reparse_point": False,
    "dacl_protected": True,
}
```

The record must additionally be regular for files or directory for directories.
Allow ACEs for restricted principals must reject every read-capable mask,
including `FILE_READ_DATA`, `FILE_READ_EA`, `FILE_READ_ATTRIBUTES`,
`READ_CONTROL`, `GENERIC_READ`, and `GENERIC_ALL`, whether granted separately or
combined with other rights.

- [ ] **Step 5: Write failing transactional-write tests**

Test this exact order:

```text
provision private parent
create empty candidate
apply protected DACL
validate candidate handle
write UTF-8 through candidate handle
flush candidate handle
close candidate handle
atomically replace destination
open and validate destination
remove backup and candidate
```

Inject a failure at each phase. Existing destination bytes must survive; a first
write must leave no destination; candidate and backup must be proven absent
after success or successful rollback. Failure strings must not contain the
candidate configuration text.

- [ ] **Step 6: Run transactional tests and confirm RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_windows_security.py -k atomic
```

Expected: failures because atomic private writing is missing.

- [ ] **Step 7: Implement private directory and atomic file operations**

The protected DACL grants full control only to:

```text
current user SID
S-1-5-18
S-1-5-32-544
```

Create the empty candidate before applying the DACL, but write no content until
the candidate's opened handle validates. Use `ReplaceFileW` for replacement and
`MoveFileExW` for first installation. If post-replacement validation fails,
restore the backup; report separately when rollback fails. Never include file
content in an error.

- [ ] **Step 8: Run Task 1 GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_windows_security.py
```

Expected: all Task 1 tests pass.

- [ ] **Step 9: Commit Task 1**

```bash
git add packages/memberkit/memberkit/windows_security.py \
  packages/memberkit/tests/test_windows_security.py
git commit -m "feat: secure MemberKit files on Windows"
```

---

### Task 2: Runtime Configuration and Windows Timezone Packaging

**Files:**

- Modify: `packages/memberkit/memberkit/config.py`
- Modify: `packages/memberkit/tests/test_config.py`
- Modify: `packages/memberkit/pyproject.toml`
- Create: `packages/memberkit/tests/test_package_metadata.py`

**Interfaces:**

- Consumes: all Task 1 private-file interfaces.
- Produces:

```python
def default_config_file(
    platform: str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path: ...

def write_config(
    values: Mapping[str, str],
    *,
    config_file: Path | None = None,
    platform: str | None = None,
    windows_api: Any = None,
) -> Path: ...

def load(
    env: dict[str, str] | None = None,
    *,
    config_file: Path | None = None,
    platform: str | None = None,
    windows_api: Any = None,
) -> Config: ...
```

- Consumed by: Task 6 CLI setup and the Windows smoke in Task 7.

- [ ] **Step 1: Write failing dynamic-path and protected-read tests**

Assert:

```python
default_config_file(
    platform="win32",
    env={"APPDATA": r"C:\Users\Alex\AppData\Roaming"},
) == Path(r"C:\Users\Alex\AppData\Roaming") / "TeamMemory" / "memberkit.env"
```

Missing `APPDATA` raises `RuntimeError("APPDATA is required on Windows")`.
Other platforms retain `Path.home() / ".config" / "teammem" /
"memberkit.env"`. Windows `load()` resolves the SID and reads only through
`read_windows_private_text()`. Process environment values override file values.

- [ ] **Step 2: Run configuration RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_config.py \
  -k "windows or default_config or write_config"
```

Expected: failures because the dynamic Windows interfaces do not exist.

- [ ] **Step 3: Implement dynamic config loading and writing**

Resolve defaults at call time. Windows lazily imports Task 1; other platforms
must not import native APIs. Render only these keys in stable order:

```python
(
    "MEMBERKIT_MEMBER",
    "MEMBERKIT_INBOX_URL",
    "MEMBERKIT_DB",
    "MEMBERKIT_WORKDIR",
    "MEMBERKIT_TIMEZONE",
)
```

Reject CR, LF, and NUL in values. Windows writes through
`atomic_write_windows_private_text`; macOS/other POSIX retains UTF-8 write plus
`chmod(0o600)`. Required-key and timezone errors may name keys and the selected
path, never values.

- [ ] **Step 4: Write failing package-metadata and timezone tests**

Parse `packages/memberkit/pyproject.toml` with `tomllib`. Assert it contains:

```toml
"Operating System :: Microsoft :: Windows"
dependencies = ["tzdata; sys_platform == 'win32'"]
```

Assert no dependency name normalizes to `teammem`. Add a Windows-only
subprocess test that clears the normal `zoneinfo` search path, constructs
`ZoneInfo("America/Los_Angeles")`, and verifies different UTC offsets on
`2026-03-01` and `2026-03-15`.

- [ ] **Step 5: Run metadata RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_package_metadata.py \
  packages/memberkit/tests/test_config.py -k "timezone or tzdata or metadata"
```

Expected: metadata assertions fail because Windows support is not declared.

- [ ] **Step 6: Add Windows-only timezone dependency and classifier**

Add only:

```toml
"Operating System :: Microsoft :: Windows",
dependencies = ["tzdata; sys_platform == 'win32'"]
```

Do not add a hub dependency and do not bump or publish a version.

- [ ] **Step 7: Run Task 2 GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_windows_security.py \
  packages/memberkit/tests/test_config.py \
  packages/memberkit/tests/test_package_metadata.py
```

Expected: all Task 1–2 tests pass.

- [ ] **Step 8: Commit Task 2**

```bash
git add packages/memberkit/memberkit/config.py \
  packages/memberkit/tests/test_config.py \
  packages/memberkit/pyproject.toml \
  packages/memberkit/tests/test_package_metadata.py
git commit -m "feat: configure MemberKit safely on Windows"
```

---

### Task 3: Portable Schedule Facade and Unchanged macOS Backend

**Files:**

- Modify: `packages/memberkit/memberkit/schedule.py`
- Create: `packages/memberkit/memberkit/schedule_macos.py`
- Modify: `packages/memberkit/tests/test_schedule.py`

**Interfaces:**

- Produces:

```python
@dataclass(frozen=True)
class ScheduleStatus:
    installed: bool
    path: Path
    time: str | None = None

def _backend(platform: str | None) -> Literal["macos", "windows"]: ...
def _parse_time(value: str) -> tuple[int, int]: ...
def _executable(value: str | None) -> str: ...

def install_schedule(
    config: Config,
    time: str = DEFAULT_TIME,
    agents_dir: Path | None = None,
    executable: str | None = None,
    platform: str | None = None,
    runner: Callable[..., Any] | None = None,
    windows_api: Any = None,
    windows_runner: Callable[..., Any] | None = None,
    windows_state_dir: Path | None = None,
    windows_task_name: str | None = None,
) -> Path: ...

def schedule_status(
    agents_dir: Path | None = None,
    platform: str | None = None,
    runner: Callable[..., Any] | None = None,
    windows_api: Any = None,
    windows_runner: Callable[..., Any] | None = None,
    windows_state_dir: Path | None = None,
    windows_task_name: str | None = None,
    windows_executable: str | None = None,
) -> ScheduleStatus: ...

def remove_schedule(
    agents_dir: Path | None = None,
    platform: str | None = None,
    runner: Callable[..., Any] | None = None,
    windows_api: Any = None,
    windows_runner: Callable[..., Any] | None = None,
    windows_state_dir: Path | None = None,
    windows_task_name: str | None = None,
    windows_executable: str | None = None,
) -> bool: ...
```

`schedule_status()` and `remove_schedule()` keep `agents_dir` as their first
optional positional parameter. All new platform/backend injection seams follow
the existing positional parameters so existing Python callers remain
compatible.

- Produces: `schedule_macos.install_schedule`, `schedule_macos.schedule_status`,
  `schedule_macos.remove_schedule`, and `schedule_macos.notify_pending`.
- Consumed by: Tasks 4–7.

- [ ] **Step 1: Write failing strict parsing and lazy-dispatch tests**

Cover:

```python
valid = ("00:00", "17:30", "23:59")
invalid = ("7:30", "07:3", "24:00", "12:60", " 07:30", "07:30 ")
```

Guard imports so Windows selection cannot import `memberkit.schedule_macos` and
macOS selection cannot import `memberkit.schedule_windows`. Linux and an unknown
platform must raise `unsupported scheduling platform: <platform>` before
creating directories or resolving a backend.

- [ ] **Step 2: Run facade RED**

Run:

```bash
.venv/bin/python -m pytest -q packages/memberkit/tests/test_schedule.py \
  -k "strict or facade or platform"
```

Expected: shortened time values pass and no platform facade exists.

- [ ] **Step 3: Move macOS code without behavioral edits**

Move the current label, LaunchAgents path, plist payload, `launchctl`
bootout/bootstrap, status parsing, removal, and `osascript` reminder into
`schedule_macos.py`. Preserve:

```python
LABEL = "org.teammem.memberkit-daily"
ProgramArguments = [absolute_memberkit, "scheduled-run"]
StartCalendarInterval = {"Hour": hour, "Minute": minute}
```

Backend functions receive already validated hour/minute/executable and optional
injected paths/runner.

- [ ] **Step 4: Implement the lazy portable facade**

Map only:

```python
{"darwin": "macos", "win32": "windows"}
```

Reject everything else explicitly. Validate strict `HH:MM` before backend
loading and resolve the executable before mutation. Use `import_module()` only
for the chosen backend.

- [ ] **Step 5: Run macOS compatibility and facade GREEN**

Run:

```bash
.venv/bin/python -m pytest -q packages/memberkit/tests/test_schedule.py
```

Expected: existing macOS tests and new dispatch tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add packages/memberkit/memberkit/schedule.py \
  packages/memberkit/memberkit/schedule_macos.py \
  packages/memberkit/tests/test_schedule.py
git commit -m "feat: select MemberKit schedules by platform"
```

---

### Task 4: Canonical MemberKit Task Scheduler Definition

**Files:**

- Create: `packages/memberkit/memberkit/schedule_windows.py`
- Create: `packages/memberkit/tests/test_schedule_windows.py`
- Reference only: `teammem/schedule_windows.py`
- Reference only: `tests/test_schedule_windows.py`

**Interfaces:**

- Consumes: `ScheduleStatus` from Task 3 and `current_user_sid` from Task 1.
- Produces:

```python
@dataclass(frozen=True)
class WindowsSchedule:
    sid: str
    task_name: str
    time: str
    executable: str

def task_name(sid: str) -> str: ...
def encode_arguments(arguments: Sequence[str]) -> str: ...
def decode_arguments(command_line: str) -> list[str]: ...
def build_task_xml(schedule: WindowsSchedule) -> bytes: ...
def parse_task_xml(xml: bytes, expected: WindowsSchedule) -> str: ...
def task_xml_mismatch_categories(
    xml: bytes, expected: WindowsSchedule
) -> tuple[str, ...]: ...
```

- Consumed by: Tasks 5 and 7.

- [ ] **Step 1: Write failing task identity and argument-codec tests**

Require:

```python
task_name(sid) == (
    "\\TeamMem-MemberKit-Daily-"
    + hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]
)
```

Round-trip empty strings, spaces, quotes, trailing backslashes, and Unicode.
Reject NUL/control characters and the shell executables `cmd.exe`,
`powershell.exe`, and `pwsh.exe`.

- [ ] **Step 2: Run identity/codec RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_schedule_windows.py \
  -k "task_name or argument"
```

Expected: collection fails because `schedule_windows` does not exist.

- [ ] **Step 3: Implement identity and canonical Windows C-runtime arguments**

Keep the task name SID-specific without exposing the SID. Encode only canonical
argument forms; decode and re-encode to prove canonical equivalence.

- [ ] **Step 4: Write failing canonical XML and tamper tests**

The expected definition contains:

```text
Source=TeamMem-MemberKit
Description=TeamMem MemberKit daily draft reminder
URI=<exact task name>
UserId=<exact SID>
LogonType=InteractiveToken
RunLevel=LeastPrivilege
DaysInterval=1
MultipleInstancesPolicy=IgnoreNew
StartWhenAvailable=true
Enabled=true
DisallowStartIfOnBatteries=false
StopIfGoingOnBatteries=false
RunOnlyIfNetworkAvailable=false
WakeToRun=false
UseUnifiedSchedulingEngine=true
ExecutionTimeLimit=PT4H
Command=<absolute memberkit.exe>
Arguments=scheduled-run
```

Assert deterministic UTF-16LE+BOM transport, one principal, trigger, and action,
no working directory, no config path, and no secrets. Reject duplicate, extra,
missing, namespace-tampered, entity-bearing, oversized, noncanonical, or
semantically changed definitions. Accept only the exact scheduler-added defaults
already proven by the hub tests.

- [ ] **Step 5: Run XML RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_schedule_windows.py -k xml
```

Expected: XML generator/parser tests fail.

- [ ] **Step 6: Implement bounded XML generation and validation**

Use the Task Scheduler namespace:

```text
http://schemas.microsoft.com/windows/2004/02/mit/task
```

Limit queried XML to 1 MiB. Decode only strict BOM/signature-recognized UTF-16 or
UTF-8. Compare the exact current SID, task name, markers, local start time,
settings, executable, and decoded argument vector. Mismatch categories contain
fixed labels only, never values.

- [ ] **Step 7: Run Task 4 GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_schedule_windows.py \
  -k "task_name or argument or xml"
```

Expected: all definition tests pass.

- [ ] **Step 8: Commit Task 4**

```bash
git add packages/memberkit/memberkit/schedule_windows.py \
  packages/memberkit/tests/test_schedule_windows.py
git commit -m "feat: define MemberKit Windows tasks"
```

---

### Task 5: Windows Scheduler Lifecycle and Concurrency Boundary

**Files:**

- Modify: `packages/memberkit/memberkit/schedule_windows.py`
- Modify: `packages/memberkit/tests/test_schedule_windows.py`
- Modify: `packages/memberkit/tests/test_schedule.py`

**Interfaces:**

- Consumes: Task 1 private-directory helpers, Task 3 facade, and Task 4 XML.
- Produces:

```python
def schedule_status(
    *,
    api: Any = None,
    runner: WindowsRunner | None = None,
    state_dir: Path | None = None,
    task_name_override: str | None = None,
    executable: str,
) -> ScheduleStatus: ...

def install_schedule(
    hour: int,
    minute: int,
    executable: str,
    *,
    api: Any = None,
    runner: WindowsRunner | None = None,
    state_dir: Path | None = None,
    task_name_override: str | None = None,
    lock_factory: Callable[[Path], ContextManager[Any]] | None = None,
) -> Path: ...

def remove_schedule(
    *,
    api: Any = None,
    runner: WindowsRunner | None = None,
    state_dir: Path | None = None,
    task_name_override: str | None = None,
    executable: str,
    lock_factory: Callable[[Path], ContextManager[Any]] | None = None,
) -> bool: ...
```

- Consumed by: Tasks 6 and 7.
- `WindowsRunner` captures stdout and stderr into separate bounded
  temporary-file spools rather than anonymous pipes. One finite deadline covers
  direct-process execution and retained-output collection. It polls both spool
  sizes while the direct process runs, retains/parses no more than 1 MiB from
  each stream, and fails safely on timeout or overflow. It never waits for pipe
  EOF or requires process-tree termination, so inherited stdout/stderr handles
  cannot cause an indefinite wait. It closes and cleans all transient scheduler
  output spools and leaves mutation outcome determination to a fresh query.

- [ ] **Step 1: Write the fake byte-only `schtasks.exe` runner**

Model only:

```text
/Query /TN <name> /XML
/Query /FO CSV /NH
/Create /TN <name> /XML <path>
/Create /TN <name> /XML <path> /F
/Delete /TN <name> /F
```

The fake runner returns byte stdout/stderr and stores task XML bytes while
supporting deterministic injected failures and race hooks. Separately test that
the native runner redirects both streams to bounded temporary-file spools, not
anonymous pipes; uses a finite deadline for direct-process execution plus output
collection; polls both spool sizes while the direct process runs; caps retained
and parsed stdout and stderr at 1 MiB each; and closes and cleans its transient
spools. Create without `/F` must fail on a name collision and preserve the
stored definition.

- [ ] **Step 2: Write failing status/conflict tests**

Cover absent status, exact managed status, localized query failure, malformed
managed task, unexpected action, foreign ownership, and the read-only rule:
status creates no persistent MemberKit state directory, lifecycle lock, or
private task-XML artifact. Its transient native-runner output spools are allowed
only for the query and must be closed and cleaned. Also cover successful XML
output, CSV fallback output, and stdout or stderr that exceeds 1 MiB while the
direct process is still running; size polling must detect overflow before
unbounded collection or parsing.

Exercise the native runner with controlled children. Its finite deadline covers
the direct process and collection from both spools. A descendant that inherits
stdout or stderr cannot keep the operation waiting indefinitely after the direct
process exits. The test must not require process-tree termination. Timeout,
overflow, collection, and cleanup failures remain sanitized and do not assume
whether a mutation succeeded.

- [ ] **Step 3: Run lifecycle RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_schedule_windows.py \
  -k "status or conflict"
```

Expected: lifecycle functions are missing.

- [ ] **Step 4: Implement state path, query, and status**

Resolve:

```text
%LOCALAPPDATA%\TeamMemory\MemberKit
```

at operation time. Status does not provision it. Query exact XML first and use a
bounded CSV list only to distinguish an absent task from localized query errors.
Parse only the retained bounded output and never return raw command output.

- [ ] **Step 5: Write failing install/remove/rollback tests**

Cover one-byte `msvcrt` lock contention between cooperating MemberKit commands,
private state validation before lock or temp writes, create-only initial
registration without `/F`, exact managed replacement with `/F`, query and
revalidation immediately before replacement/removal, post-create verification,
first-install rollback to absence only when the observed definition is still the
candidate, prior-XML restoration, cleanup retry, persistent cleanup failure,
idempotent removal, and removal restoration.

Use deterministic race hooks for at least these cases:

- a foreign task appears after the absent query and before initial create:
  create fails on collision and the foreign bytes remain unchanged;
- a validated managed task becomes foreign before replacement or removal
  revalidation: MemberKit refuses mutation and preserves the foreign bytes;
- a task becomes foreign before post-mutation verification or recovery:
  MemberKit reports conflict/uncertain recovery and preserves the observed
  foreign bytes.

Do not write tests that assume atomic compare-and-swap across the final
revalidation-to-mutation window.

- [ ] **Step 6: Run mutation RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_schedule_windows.py \
  -k "install or remove or rollback or cleanup or lock"
```

Expected: mutation tests fail.

- [ ] **Step 7: Implement the cooperating-command lifecycle**

After an absent query, register only through the create-only command:

```python
[
    "schtasks.exe", "/Create", "/TN", name,
    "/XML", str(private_xml_path),
]
```

If this command reports a collision, query the name and preserve the observed
definition. For replacement, snapshot an exact managed prior definition under
the lifecycle lock, query and revalidate it immediately before using the same
`/Create` command with `/F`. Removal likewise queries and revalidates the
managed snapshot immediately before `/Delete /F`.

Verify every successful create/delete with a fresh query. On failure, use fresh
queries for best-effort recovery: restore the prior definition only when the
observed state has not become foreign, and remove only a definition that still
validates as the newly created candidate. Preserve every unmanaged or
conflicting definition observed. Prove temporary XML removal. Errors distinguish
"previous state restored", "conflicting state preserved", and "rollback failed"
without including XML or subprocess output.

The implementation must document that the lock covers cooperating MemberKit
commands only. It must not represent query/revalidate/mutate as atomic
compare-and-swap or claim integrity against another same-identity scheduler
client.

- [ ] **Step 8: Connect facade injection seams**

Task 3's facade forwards `windows_api`, `windows_runner`,
`windows_state_dir`, `windows_task_name`, and executable to the Windows backend.
Add a subprocess import test proving neither facade nor Windows backend imports
`teammem` or macOS-only modules.

- [ ] **Step 9: Run Task 5 GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_schedule_windows.py \
  packages/memberkit/tests/test_schedule.py
```

Expected: all schedule tests pass without a real scheduler call.

Task 5 is accepted only when the tests prove collision-safe initial create,
cooperating-command lock serialization, managed query/revalidation before
replacement and removal, best-effort rollback, bounded runner completion through
the direct process and temporary-spool output collection, 1 MiB retained/parsed
caps on each stream with size polling during execution, transient spool cleanup,
no indefinite wait on inherited output handles, and preservation of every
foreign definition observed by the deterministic race hooks. The accepted
guarantee excludes unobserved mutation by a non-cooperating same-identity Task
Scheduler client between the final revalidation and mutation, and excludes a
malicious same-user replacement executable or deliberately noisy descendant.

- [ ] **Step 10: Commit Task 5**

```bash
git add packages/memberkit/memberkit/schedule_windows.py \
  packages/memberkit/tests/test_schedule_windows.py \
  packages/memberkit/tests/test_schedule.py
git commit -m "feat: manage MemberKit Windows schedules"
```

---

### Task 6: Windows Reminder, Bounded Logs, and CLI Integration

**Files:**

- Modify: `packages/memberkit/memberkit/schedule.py`
- Modify: `packages/memberkit/memberkit/schedule_windows.py`
- Modify: `packages/memberkit/memberkit/cli.py`
- Modify: `packages/memberkit/tests/test_schedule.py`
- Create: `packages/memberkit/tests/test_schedule_runtime.py`
- Modify: `packages/memberkit/tests/test_cli.py`

**Interfaces:**

- Consumes: Task 1 current-process session ID, Task 2 config, and Tasks 3–5
  scheduling.
- Produces:

```python
class UnsupportedSchedulingPlatformError(RuntimeError): ...

def notify_pending(
    dates: Sequence[str],
    *,
    api: Any = None,
    runner: Callable[..., Any] | None = None,
) -> str | None: ...

def _notify_pending(
    dates: list[str],
    *,
    platform: str | None = None,
    macos_runner: Callable[..., Any] | None = None,
    windows_api: Any = None,
    windows_runner: Callable[..., Any] | None = None,
) -> str | None: ...

def _append_bounded_log(
    path: Path,
    line: str,
    *,
    max_bytes: int = 1024 * 1024,
) -> None: ...
```

- Extends `scheduled_run()` with platform/reminder injection while preserving
  its returned date list and the current four positional arguments exactly:

```python
def scheduled_run(
    config: Config,
    now: datetime | None = None,
    notify: bool = True,
    timezone=None,
    *,
    platform: str | None = None,
    macos_runner: Callable[..., Any] | None = None,
    windows_api: Any = None,
    windows_runner: Callable[..., Any] | None = None,
) -> list[str]: ...
```

- [ ] **Step 1: Write failing Windows reminder tests**

The exact argv is:

```python
[
    "msg.exe",
    str(current_session_id(api)),
    "/TIME:60",
    "MemberKit drafts ready for review: 2026-07-27, 2026-07-28",
]
```

Validate all dates as ISO `YYYY-MM-DD`. Assert there is no shell and never a
`*`, username, or `@list` target. The target is the validated decimal session
ID for the current process. Missing executable, permission error, timeout,
native/API lookup failure, and nonzero exit return fixed safe categories and do
not raise.

- [ ] **Step 2: Run reminder RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_schedule_runtime.py -k reminder
```

Expected: runtime test module or notification interface is missing.

- [ ] **Step 3: Implement platform notification dispatch**

macOS calls the moved `osascript` helper. Windows resolves the current process
ID to its Windows session ID, calls `msg.exe` directly with that decimal target,
a short timeout, and discarded localized output. Linux/other platforms skip
desktop notification. Do not interpolate exception messages into returned
categories.

- [ ] **Step 4: Write failing bounded-log and scheduled-run tests**

On Windows:

```text
<config.workdir>\schedule.log
<config.workdir>\schedule.err
```

Each is capped at 1 MiB with one `.1` rollover. Success logs only invocation
time and ISO dates. Draft failures re-raise after logging phase and exception
class; a fake token embedded in `str(exception)` must not appear. Reminder-only
failure logs a fixed category and still returns dates. Assert
`memberkit.push` is never imported.

- [ ] **Step 5: Run logging RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_schedule_runtime.py -k "log or scheduled"
```

Expected: bounded logging behavior is missing.

- [ ] **Step 6: Implement bounded Windows runtime diagnostics**

Rotate `schedule.log` to `schedule.log.1` and `schedule.err` to
`schedule.err.1` before an append would exceed 1 MiB. Retain one backup only.
Both current and backup files remain capped even when a pre-existing current
file is already oversized. Normalize each record to one bounded UTF-8 line.
Never log configuration values, URLs, event summaries, journals, bundle data,
or `str(exception)`.

- [ ] **Step 7: Write failing platform-neutral CLI tests**

Require both help strings to say "host's local timezone". Setup builds the fixed
configuration mapping and calls:

```python
config_path = config.write_config(values)
cfg = config.load(config_file=config_path)
install_schedule(cfg, time=schedule_time)
```

The same CLI path serves macOS and Windows; no `sys.platform` branch belongs in
`cli.py`. `--no-schedule` writes config and skips scheduling. An operational
macOS or Windows schedule failure preserves config and raises a concise message
naming `memberkit schedule install`. A distinct unsupported-platform exception
lets Linux preserve the saved config while directing the member to rerun setup
with `--no-schedule` or configure `memberkit scheduled-run` manually. Linux
creates no scheduler artifacts.

- [ ] **Step 8: Run CLI RED**

Run:

```bash
.venv/bin/python -m pytest -q packages/memberkit/tests/test_cli.py \
  -k "setup or schedule or host_local"
```

Expected: setup still writes the module-level POSIX file and help names Mac.

- [ ] **Step 9: Implement the single CLI setup/schedule path**

Validate timezone before writing. Use Task 2 `write_config()` and reload the
returned path. Keep schedule install/status/remove calls platform-neutral.
Report a saved configuration separately from a failed scheduler stage.

- [ ] **Step 10: Run Task 6 GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_schedule_runtime.py \
  packages/memberkit/tests/test_schedule.py \
  packages/memberkit/tests/test_cli.py \
  packages/memberkit/tests/test_config.py
```

Expected: all runtime and CLI tests pass.

- [ ] **Step 11: Commit Task 6**

```bash
git add packages/memberkit/memberkit/schedule.py \
  packages/memberkit/memberkit/schedule_windows.py \
  packages/memberkit/memberkit/cli.py \
  packages/memberkit/tests/test_schedule.py \
  packages/memberkit/tests/test_schedule_runtime.py \
  packages/memberkit/tests/test_cli.py
git commit -m "feat: add Windows MemberKit reminders"
```

---

### Task 7: Real Windows Smoke, CI, Documentation, and Final Verification

**Files:**

- Create: `scripts/memberkit-windows-schedule-smoke.py`
- Create: `packages/memberkit/tests/test_windows_schedule_smoke.py`
- Modify: `.github/workflows/test.yml`
- Modify: `README.md`
- Modify: `packages/memberkit/README.md`
- Modify: `docs/member-guide.md`

**Interfaces:**

- Consumes: all prior tasks.
- Produces:

```python
def _require_ci() -> None: ...
def _memberkit_executable() -> str: ...
def _future_schedule_times(now: datetime) -> tuple[str, str] | None: ...
def _select_future_schedule_times() -> tuple[str, str]: ...
def _paths(suffix: str) -> tuple[Path, Path]: ...
def _delete_task(name: str) -> None: ...
def run_smoke(suffix: str) -> None: ...
def main() -> int: ...
```

- [ ] **Step 1: Write failing smoke-helper tests**

Require GitHub-hosted Windows Actions. Select install/replacement times exactly
10 and 20 minutes in the future on the same local date. Crossing midnight
returns `None`; selection may wait only within the hub smoke's existing bounded
rollover window. Safe shape/mismatch output contains tags/categories only, never
values.

- [ ] **Step 2: Run smoke-helper RED**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_windows_schedule_smoke.py
```

Expected: smoke module is missing.

- [ ] **Step 3: Implement the disposable MemberKit Windows smoke**

The smoke:

1. Requires a GitHub-hosted Windows runner.
2. Resolves absolute `memberkit.exe`.
3. Creates a valid empty claude-mem SQLite database and disposable workdir.
4. Requires both the default MemberKit config and deterministic SID-derived task
   to be absent, failing without mutation if either exists, then writes a
   run-ID-specific ownership sentinel before creating any smoke artifact and
   writes the private smoke config through `write_config()`.
5. Installs, queries, validates, replaces, queries, removes, and confirms
   absence using safely future times.
6. Asserts the workdir has no schedule logs, state, or drafts, proving the action
   never ran.
7. Removes the exact SID-derived task and smoke config only when the validated
   run-ID-specific ownership sentinel exists and the task revalidates as the
   managed smoke definition, then removes disposable files and the sentinel in
   unconditional cleanup. `--cleanup-only` follows the same ownership and task
   revalidation checks; it preserves every conflicting config or task it
   observes.

- [ ] **Step 4: Add the dedicated Windows CI job**

Add:

```yaml
memberkit-windows-schedule:
  runs-on: windows-latest
  steps:
    - uses: actions/checkout@v7
    - uses: actions/setup-python@v7
      with:
        python-version: "3.12"
    - run: python -m pip install -e "packages/memberkit[dev]"
    - run: pytest -q packages/memberkit/tests -k windows
    - run: python scripts/memberkit-windows-schedule-smoke.py
    - if: always()
      run: python scripts/memberkit-windows-schedule-smoke.py --cleanup-only
```

- [ ] **Step 5: Run smoke-helper GREEN**

Run:

```bash
.venv/bin/python -m pytest -q \
  packages/memberkit/tests/test_windows_schedule_smoke.py
```

Expected: helper tests pass; real `schtasks.exe` remains CI-only.

- [ ] **Step 6: Update all installation and scheduling documentation**

Document the same commands dispatching to launchd/Task Scheduler, explicit
opt-in setup, Windows config/state paths, SID-specific task, logged-in-only and
locked-session behavior, `StartWhenAvailable`, no wake, `IgnoreNew`, best-effort
`msg.exe`, bounded logs, removal before uninstall, Linux automatic scheduling
deferral, the no-auto-push rule, collision refusal, and the documented
same-identity non-cooperating concurrency boundary.

- [ ] **Step 7: Install build tooling and run the complete local verification**

Run:

```bash
UV_CACHE_DIR=/private/tmp/memberkit-windows-uv-cache \
  uv pip install --python .venv/bin/python -e '.[dev]'
UV_CACHE_DIR=/private/tmp/memberkit-windows-uv-cache \
  uv pip install --python .venv/bin/python build
.venv/bin/python -m pytest -q packages/memberkit/tests
.venv/bin/python -m pytest -q tests --ignore=tests/test_memberkit_integration.py
.venv/bin/python -m pytest -q tests/test_memberkit_integration.py
.venv/bin/python -m compileall -q \
  packages/memberkit/memberkit \
  scripts/memberkit-windows-schedule-smoke.py
.venv/bin/python -m build packages/memberkit
./scripts/check-public.sh
git diff --check
```

Expected: all tests and checks pass.

- [ ] **Step 8: Inspect the built wheel**

Run:

```bash
unzip -p packages/memberkit/dist/*.whl \
  'teammem_memberkit-*.dist-info/METADATA'
```

Verify the Windows classifier and conditional `tzdata`; verify there is no hub
dependency and the wheel contains `schedule_macos.py`, `schedule_windows.py`,
and `windows_security.py`.

- [ ] **Step 9: Commit Task 7**

```bash
git add scripts/memberkit-windows-schedule-smoke.py \
  packages/memberkit/tests/test_windows_schedule_smoke.py \
  .github/workflows/test.yml README.md packages/memberkit/README.md \
  docs/member-guide.md
git commit -m "docs: deliver MemberKit Windows scheduling"
```

- [ ] **Step 10: Run final review inputs**

Record:

```bash
git log --oneline master..HEAD
git diff --stat master...HEAD
git status --short --branch
```

Expected: only approved MemberKit Windows scheduling changes, reviewed plan/spec
documents, no generated build artifacts, and no uncommitted source changes.
