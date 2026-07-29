# Windows Operator Scheduling Design

**Status:** Approved architecture, implementation pending

**Date:** 2026-07-29

## Problem

`teammem run-daily` is portable, but the schedule lifecycle currently supports
only macOS launchd and Linux systemd user timers. Windows operators need the same
explicit install, status, replace, and remove workflow without storing a Windows
password, embedding credentials in a task definition, or routing execution
through PowerShell or `cmd.exe`.

The existing Unix implementation also has two known correctness gaps that must be
closed before adding another backend:

- launchd status can raise `OverflowError` while canonicalizing a syntactically
  valid but non-generator plist integer;
- systemd `ExecStart` generation uses POSIX shell quoting instead of systemd's
  own argument escaping rules.

## Goals

- Preserve `teammem run-daily` as a one-shot command.
- Preserve explicit scheduling: installation and ordinary package commands never
  create a schedule.
- Support Windows Task Scheduler through generated XML registered by
  `schtasks.exe`.
- Run as the current user with `InteractiveToken`, least privilege, and no
  stored password.
- Run daily at 18:20 local time by default.
- Start a missed run when the user is next available and prevent overlapping
  runs.
- Keep credentials in the user-owned environment file, never in scheduler XML.
- Make install and removal transactional and make status validate the complete
  managed definition.
- Keep current macOS and Linux behavior and public Python interfaces compatible.

## Non-goals

- Running after the user logs out.
- Password, service-account, S4U, SYSTEM, or highest-privilege tasks.
- Waking a sleeping or powered-off computer.
- Installing a machine-wide task.
- Adding a long-running Windows service.
- Adding a PowerShell or `cmd.exe` action wrapper.
- Changing provider collection, bundle import, synthesis, or rendering behavior.

The logged-out case is intentionally excluded. S4U avoids storing a password but
cannot provide the normal network and encrypted-file access required by provider
and Git operations. A password or service-account mode would change the product's
trust model and requires a separate design.

## Chosen Approach

Generate a complete Task Scheduler XML document and register it with:

```text
schtasks.exe /Create /TN <task-name> /XML <temporary-xml> /F
```

This is preferred over direct `/Create` flags because XML represents the
principal, missed-run, battery, concurrency, and action settings exactly. It is
preferred over PowerShell ScheduledTasks cmdlets because it avoids an additional
command, quoting, and version-specific encoding layer.

The task action directly invokes:

```text
<absolute-teammem-executable> --env-file <absolute-env-file> run-daily
```

Task Scheduler receives the executable separately from the encoded argument
string. No command shell participates.

## Module Architecture

The existing public module remains the portable facade:

```text
teammem/schedule.py
```

It owns:

- `DEFAULT_TIME`;
- `ScheduleStatus`;
- time and platform validation;
- explicit backend dispatch;
- the public `install_schedule`, `schedule_status`, and `remove_schedule`
  functions.

Unix-specific implementation moves to:

```text
teammem/schedule_unix.py
```

It owns launchd, systemd, POSIX descriptor operations, `fcntl`, and Unix
permission handling.

Windows-specific implementation lives in:

```text
teammem/schedule_windows.py
```

It owns Task Scheduler XML, `schtasks.exe` invocation, Windows command-line
argument encoding, current-user identity, Windows locking, and Windows ACL
validation. Importing `teammem.schedule` on Windows must not import `fcntl` or
other Unix-only modules.

Dispatch is explicit for `darwin`, `linux`, and `win32`. An unknown platform is
unsupported; it must never fall through to the Linux backend.

## Public Interface Compatibility

The following interfaces remain:

```python
ScheduleStatus(
    installed: bool,
    time: str | None,
    backend: str,
    path: Path,
)

install_schedule(...)
schedule_status(...)
remove_schedule(...)
```

On Windows, `ScheduleStatus.path` is the logical Task Scheduler location rather
than a filesystem path. It is represented as a `Path` for compatibility and
printed as the exact task name.

Existing optional test-injection parameters remain compatible. Windows-specific
injection points may be added without changing ordinary callers.

## Windows Identity and Task Name

The backend resolves the current user's SID through native Windows APIs. It
derives a stable, non-secret suffix from the first 12 hexadecimal characters of
`SHA-256(SID)`.

The managed task is created in the Task Scheduler root:

```text
\TeamMem-Daily-<sid-hash>
```

Using the root avoids depending on separate Task Scheduler folder creation.
Including the SID-derived suffix prevents two users on the same computer from
claiming the same task name.

The XML contains a TeamMem ownership marker in `RegistrationInfo/Source`,
`RegistrationInfo/URI`, and the description. Status, replacement, and removal
require the exact task name, marker, and current SID.

## Canonical Task Definition

The generated XML uses the Task Scheduler schema and contains exactly one
principal, one daily calendar trigger, and one executable action.

Generated task files use deterministic UTF-16LE with a BOM and an XML encoding
declaration. `schtasks.exe` query output is captured as bytes and passed to the
XML parser so its BOM/declaration controls decoding; scheduler output is not
decoded through the active console code page.

### Principal

- `UserId`: exact current-user SID.
- `LogonType`: `InteractiveToken`.
- `RunLevel`: `LeastPrivilege`.

The task therefore runs while that user is logged in. A locked screen is fine;
logging out ends availability for future runs.

### Trigger

- Local daily start boundary at the configured `HH:MM`.
- `ScheduleByDay/DaysInterval`: `1`.
- `Enabled`: `true`.

The task-level `StartWhenAvailable` setting provides catch-up after a missed
daily trigger, subject to the current-user interactive-token requirement.

### Settings

- `MultipleInstancesPolicy`: `IgnoreNew`.
- `Enabled`: `true`.
- `StartWhenAvailable`: `true`.
- `DisallowStartIfOnBatteries`: `false`.
- `StopIfGoingOnBatteries`: `false`.
- `RunOnlyIfNetworkAvailable`: `false`.
- `WakeToRun`: `false`.
- A finite execution limit large enough for a normal daily collection run.

Network availability is not made a Task Scheduler precondition because local
bundle import and rendering can still provide value when an external provider is
temporarily unavailable.

### Action

- `Command`: absolute installed `teammem.exe`.
- `Arguments`: canonical Windows command-line encoding of:
  `--env-file`, the absolute environment-file path, and `run-daily`.
- No working-directory dependency.
- No extra actions.

The argument encoder follows Windows C runtime quoting: quote only as needed,
escape backslashes preceding quotes or the closing quote, preserve Unicode, and
reject NUL and control characters. The validator decodes only the canonical form
emitted by the generator and compares the exact argument vector.

## Environment File and Credentials

The task XML stores only the path to the environment file. It never stores file
contents, provider tokens, Git credentials, or Windows credentials.

On Windows, the default environment file is:

```text
%APPDATA%\TeamMemory\hub.env
```

An explicit global `--env-file` continues to override the default.

Windows cannot use POSIX mode `0600` as its security contract. A Windows-specific
validator uses native security APIs to require:

- a regular non-reparse-point file;
- ownership by the current user;
- no allow ACE granting read access to Everyone, Authenticated Users, or the
  built-in Users group.

Normal access for the current user, Administrators, and SYSTEM is permitted.
Validation errors identify the path and rule but never include file contents.

## Lifecycle Lock

Install, status transitions, and removal use one per-user lifecycle lock under:

```text
%LOCALAPPDATA%\TeamMemory\schedule.lock
```

The parent directory is user-owned and ACL-validated. The backend locks a fixed
initialized byte with `msvcrt.locking` for the complete
read/modify/query/rollback transaction. Contending operations serialize rather
than observing partial state.

## Status Semantics

Status queries the exact task through:

```text
schtasks.exe /Query /TN <task-name> /XML
```

An explicit not-found result returns `installed=False`. Other command failures
raise a sanitized scheduling error.

Returned XML is parsed with external entities disabled by the standard-library
parser and validated semantically. A managed installed task must have:

- the exact ownership marker, URI, and current SID;
- `InteractiveToken` and `LeastPrivilege`;
- exactly one enabled daily trigger at the configured time;
- `StartWhenAvailable=true`;
- `MultipleInstancesPolicy=IgnoreNew`;
- the exact supported battery, network, wake, and execution settings;
- exactly one direct executable action;
- the exact executable and decoded argument vector;
- no additional triggers, principals, or actions.

An existing same-name task that is foreign, malformed, or tampered is not
reported as a valid installation. The API raises a sanitized conflict error so
the CLI does not misleadingly print `not installed`.

The validator compares meaning rather than insignificant XML whitespace or
namespace-prefix spelling. Generator tests separately pin deterministic output.

## Transactional Installation

Installation holds the lifecycle lock for the entire operation:

1. Validate time, current identity, absolute executable, environment-file path,
   ACLs, and task name.
2. Query and snapshot the exact existing task XML, if present.
3. If a same-name task exists but is not a valid TeamMem task for this SID,
   refuse to overwrite it.
4. Write generated XML to a user-private temporary file.
5. Register it with `schtasks.exe /Create /XML ... /F`.
6. Re-query the registered XML and validate its full semantics.
7. Delete the temporary file in all paths.

The temporary file is created inside the ACL-validated per-user state directory
and is never written to a shared temporary directory.

If registration or verification fails:

- restore the prior XML when replacing a valid prior task;
- delete the newly created task when this was the first installation;
- verify the restored/absent state before releasing the lock.

A rollback failure raises a distinct sanitized error chained to the original
failure. It must not claim success or hide the rollback failure.

## Transactional Removal

Removal also holds the lifecycle lock:

1. Query the exact task.
2. Return `False` when it is absent.
3. Refuse to delete a foreign or tampered same-name task.
4. Snapshot the validated XML.
5. Run `schtasks.exe /Delete /TN <task-name> /F`.
6. Verify that the exact task is absent.

If post-delete verification fails, restore the snapshot and verify it before
reporting failure. Repeated removal remains idempotent.

Deleting a Task Scheduler definition prevents future triggers. It does not
promise to terminate an already-running `teammem run-daily` process.

## Unix Defect Closure

Before adding Windows behavior:

### launchd

Treat any load, structural-validation, or canonical-serialization failure,
including `OverflowError`, as an invalid definition. Status must not query
launchd after local definition validation fails.

### systemd

Replace `shlex.join` with a narrow systemd-native codec:

- quote every argument as one systemd item;
- escape backslash and double quote;
- encode literal dollar as `$$`;
- encode literal percent as `%%`;
- reject NUL, newline, and control characters;
- accept only the canonical generator output during validation;
- require an absolute executable before writing.

Tests cover spaces, apostrophes, backslashes, dollar signs, percent signs,
Unicode, and rejected newline/control input.

These fixes must not otherwise change launchd or systemd schedules, paths,
manager commands, missed-run semantics, logs, or CLI output.

## Logging and Troubleshooting

Task Scheduler does not provide direct stdout/stderr file redirection for an
`Exec` action without introducing a wrapper. Version 1 deliberately keeps the
direct action and does not add one.

Windows documentation directs operators to:

- Task Scheduler History;
- Last Run Time and Last Run Result;
- `teammem schedule status`;
- a manual `teammem --env-file <path> run-daily` invocation for detailed output.

Application-level persistent logs can be designed separately if operational use
shows they are needed.

## CLI and Documentation

The existing commands remain:

```text
teammem schedule install --time 18:20
teammem schedule status
teammem schedule remove
```

Documentation must state:

- package installation alone creates no task;
- the schedule is current-user and logged-in-only;
- screen lock does not stop it, logout does;
- missed runs start when the interactive user is next available;
- the computer must remain powered and normally available;
- remove the schedule before uninstalling;
- after upgrade, run a manual daily pass and reinstall the schedule so the task
  records the current executable and configuration paths;
- logged-out/password/service-account operation is not supported.

macOS and Linux instructions remain behaviorally unchanged.

## Testing

### Hermetic tests

Run on every platform without touching the real scheduler:

- explicit facade dispatch and no Unix imports on simulated Windows;
- deterministic XML generation;
- Unicode and Windows argument quoting round trips;
- rejection of extra triggers/actions/principals and altered security settings;
- current-SID and ownership-marker validation;
- not-found, foreign-task, malformed-XML, and command-failure status behavior;
- first install, replacement, verification failure, rollback, rollback failure,
  removal, removal verification, and idempotency;
- lock serialization;
- environment-file ownership/DACL and reparse-point validation through injected
  Windows API seams;
- no credential contents in XML or errors;
- launchd overflow regression;
- complete systemd-native quoting matrix;
- existing launchd/systemd regression suites.

### Windows CI integration

A `windows-latest` job installs the built package and uses a unique test task
name. It performs real create, query, semantic status, replace, and delete
operations through `schtasks.exe`, with cleanup in an unconditional final step.
It does not use production credentials or run the daily provider workflow.

The macOS development host cannot provide live Windows-manager evidence.
Completion therefore distinguishes hermetic local verification from the Windows
CI integration result.

## Acceptance Criteria

- Importing and using non-schedule CLI commands works on Windows.
- Windows install creates the exact current-user interactive task without a
  password, shell wrapper, or credentials in XML.
- Status accepts only the complete managed definition and surfaces conflicts.
- Install and remove recover their prior state on failure.
- Default daily time is 18:20 local, missed runs are enabled, and overlap is
  ignored.
- A locked screen remains supported; logged-out execution is explicitly not
  supported.
- Existing macOS and Linux tests and documented behavior remain unchanged.
- The launchd overflow and systemd quoting regressions are closed.
- Hermetic full-suite, package build, public scan, and Windows CI integration
  pass.
