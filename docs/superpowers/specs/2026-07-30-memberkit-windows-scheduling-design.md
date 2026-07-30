# MemberKit Windows Scheduling Design

**Date:** 2026-07-30

**Status:** Approved in conversation

## Problem

MemberKit exposes one schedule workflow, but its implementation is currently
macOS-only. On Windows, accepting the schedule during `memberkit setup` writes a
LaunchAgent plist and then fails at macOS-specific APIs. A manually configured
Windows task can prepare drafts and then fail when MemberKit tries to run
`osascript`.

Windows members need the same public commands as macOS members. MemberKit must
select the native scheduler internally, remain independently installable from
the TeamMem hub, preserve its review-before-push boundary, and avoid leaving
partial scheduler state.

## User-Facing Contract

The commands remain platform-neutral:

```text
memberkit setup
memberkit schedule install --time 17:30
memberkit schedule status
memberkit schedule remove
memberkit scheduled-run
```

MemberKit dispatches from the detected runtime platform:

- `darwin` uses the existing launchd lifecycle.
- `win32` uses Windows Task Scheduler.
- Linux and unknown platforms do not install a schedule in this change. They
  fail before creating platform-specific scheduling artifacts and direct the
  member to `--no-schedule` or a manually configured `memberkit scheduled-run`.

Package installation never creates a schedule. `memberkit setup` continues to
offer the default `17:30` host-local time, accepts another strict `HH:MM` value,
or allows the member to decline scheduling. Scheduling never pushes, commits,
or transmits data.

## Package Boundary

`teammem-memberkit` remains independently installable:

```text
pipx install teammem-memberkit
```

It does not import or depend on the hub's `teammem` package. The MemberKit
Windows backend follows the proven hub scheduler's security and lifecycle
patterns but owns its own task definition and tests.

A third shared scheduler distribution is deferred. It would add publication,
compatibility, and release coordination for only two consumers. Importing the
hub scheduler is rejected because it would require every member to install the
operator package.

## Architecture

`memberkit.schedule` becomes the portable facade. It owns:

- `DEFAULT_TIME`, `ScheduleStatus`, and strict time parsing;
- explicit platform dispatch with lazy backend imports;
- the platform-neutral `install_schedule`, `schedule_status`, and
  `remove_schedule` functions;
- scheduled draft preparation;
- best-effort platform notification dispatch.

The existing launchd lifecycle moves without behavior changes to
`memberkit.schedule_macos`. It remains isolated from Windows imports.

`memberkit.schedule_windows` owns Task Scheduler XML generation, strict managed
definition validation, status, replacement, rollback, and removal. Windows-only
APIs and modules are imported lazily so MemberKit continues to import and draft
on other platforms.

`memberkit.windows_security` owns current-user SID resolution and Windows
filesystem security checks needed by configuration and scheduler state. Its
native implementation is loaded only on Windows and exposes injectable seams for
platform-independent unit tests.

The MemberKit distribution declares `tzdata` only on Windows. Python's
`zoneinfo` uses the operating system IANA database when available, but a clean
Windows installation has no well-known IANA database. The first-party `tzdata`
fallback preserves `MEMBERKIT_TIMEZONE` behavior, including daylight-saving
transitions, without changing macOS dependencies.

## Windows Configuration

New Windows setup uses:

```text
%APPDATA%\TeamMemory\memberkit.env
```

The existing `MEMBERKIT_*` process environment variables continue to override
file values. macOS and other platforms retain:

```text
~/.config/teammem/memberkit.env
```

Windows setup first provisions and handle-validates `%APPDATA%\TeamMemory` as a
private, current-user-owned, non-reparse-point directory. It creates an empty
candidate file in that directory, applies a protected DACL for the current user,
Administrators, and SYSTEM before writing configuration content, validates the
opened handle, writes and flushes UTF-8 content through that handle, atomically
replaces the destination, and revalidates the destination through a new handle.
The existing configuration remains byte-for-byte intact if any directory,
DACL, write, flush, replace, or validation step fails.

Every Windows configuration read, including `memberkit scheduled-run`, opens the
file without following a reparse point, validates that same handle, and reads
through it. Validation requires a regular disk file owned by the current user
with no read grant to Everyone, Authenticated Users, or the built-in Users
group. This avoids a validate-then-replace race. POSIX `chmod(0600)` remains the
macOS behavior and is not treated as the Windows security contract.

Configuration content, the inbox URL, and any credentials never appear in Task
Scheduler XML or scheduler command lines.

## Windows Task Identity

The backend resolves the current process user's SID through native Windows APIs.
It derives the task name from the first 12 hexadecimal characters of
`SHA-256(SID)`:

```text
\TeamMem-MemberKit-Daily-<sid-hash>
```

The SID-specific name avoids cross-user ownership conflicts on a shared machine.
The task lives at the Task Scheduler root and has exact MemberKit ownership
markers in `RegistrationInfo/Source`, `RegistrationInfo/Description`, and
`RegistrationInfo/URI`.

Status, replacement, and removal require the exact task name, current SID,
ownership markers, trigger, settings, executable, and argument vector. An
unmanaged or conflicting task is reported and never overwritten or deleted.

## Canonical Windows Task

MemberKit generates deterministic UTF-16LE Task Scheduler XML with a BOM and
matching XML encoding declaration. It registers the definition through the
exact direct command:

```text
schtasks.exe /Create /TN <task-name> /XML <private-xml-path> /F
```

It does not use PowerShell or `cmd.exe`. Query output is bounded to 1 MiB and
decoded with strict BOM or UTF-16 signature detection rather than the active
console code page.

The definition contains exactly one principal, one daily calendar trigger, and
one executable action.

### Principal

- `UserId`: exact current-user SID.
- `LogonType`: `InteractiveToken`.
- `RunLevel`: `LeastPrivilege`.

The task is logged-in-only. A locked session remains eligible; a logged-out user
does not provide an interactive token.

### Trigger and Settings

- Daily host-local trigger at the configured `HH:MM`.
- `DaysInterval`: `1`.
- `StartWhenAvailable`: `true`.
- `MultipleInstancesPolicy`: `IgnoreNew`.
- `Enabled`: `true`.
- `DisallowStartIfOnBatteries`: `false`.
- `StopIfGoingOnBatteries`: `false`.
- `RunOnlyIfNetworkAvailable`: `false`.
- `WakeToRun`: `false`.
- `UseUnifiedSchedulingEngine`: `true`.
- `ExecutionTimeLimit`: `PT4H`.

`StartWhenAvailable` provides catch-up after a missed trigger while the
current-user interactive-token requirement can be satisfied. The scheduler does
not wake a sleeping machine.

### Action

The action directly executes:

```text
<absolute-memberkit.exe> scheduled-run
```

The executable must be absolute and must not be `cmd.exe`, `powershell.exe`, or
`pwsh.exe`. Arguments use canonical Windows C-runtime quoting and reject control
characters. The task has no working-directory dependency and contains no
configuration content or path.

## Lifecycle and Rollback

Windows scheduler state uses:

```text
%LOCALAPPDATA%\TeamMemory\MemberKit
```

The directory is created and validated as current-user-owned private state.
Install, replace, and remove are serialized with one per-user lifecycle lock.

Installation:

1. Validate the requested time, absolute executable, SID, and private state.
2. Query and validate any existing managed task.
3. Preserve the exact existing XML as a rollback snapshot.
4. Write the candidate XML to a private temporary file.
5. Register it through `schtasks.exe`.
6. Query and validate the registered definition.
7. Delete the temporary file.

Any failure restores the previous definition or confirms that a newly created
task is absent. Cleanup failure is reported. A failed install never silently
claims success.

Removal deletes only a validated managed MemberKit task. It is idempotent when
no task exists and restores the prior definition if deletion or verification
fails.

Status is read-only. It creates no directories, locks, or temporary files.

## Desktop Reminder

After draft preparation, notification dispatch is platform-specific:

- macOS keeps the existing `osascript` notification listing pending dates.
- Windows invokes `msg.exe` directly, never through a shell, targeting only the
  current username and never `*`. The message contains only ISO pending dates
  and expires after 60 seconds.
- Other platforms skip desktop notification without failing draft preparation.

Windows `msg.exe` delivery is best-effort because it can be unavailable or
denied by session permissions. Missing executables, permission errors, timeouts,
and non-zero results never change a successful draft run into a failure. Local
drafts and `memberkit schedule status` remain authoritative.

Because a direct Task Scheduler action has no shell redirection, Windows
`memberkit scheduled-run` writes bounded internal diagnostics to:

```text
<MEMBERKIT_WORKDIR>\schedule.log
<MEMBERKIT_WORKDIR>\schedule.err
```

Each file is capped at 1 MiB with one `.1` rollover. Success records only the
invocation time and pending ISO dates. Error records contain the phase, exception
type, and one bounded single-line diagnostic. Logs never contain configuration
values, inbox URLs, tokens, event summaries, journals, or bundle content.
Uncaught draft failures and `msg.exe` delivery failures are recorded before the
command returns non-zero or, for notification-only failure, returns success.

A guaranteed Windows app notification is deferred. It requires Windows App SDK
or unpackaged-application identity and Start-menu shortcut plumbing, which is a
separate packaging feature rather than a scheduler patch.

## Failure Behavior

- Unsupported automatic scheduling fails before creating a LaunchAgent, Windows
  task, or scheduler-state directory.
- Windows never imports the macOS backend or calls `launchctl` or `osascript`.
- macOS never imports the Windows backend.
- Invalid time values fail before backend mutation.
- A missing `memberkit` executable fails before scheduler mutation.
- Notification failure never loses, overwrites, approves, or pushes a draft.
- Existing malformed or member-edited drafts retain their current preservation
  behavior.
- Setup may leave a successfully written private configuration when scheduler
  installation fails; the error explicitly identifies scheduling as the failed
  stage so the member can retry `memberkit schedule install`.

## Testing

Hermetic tests cover:

- explicit platform dispatch and lazy import isolation;
- unchanged launchd plist generation, status, replacement, and removal;
- Linux and unknown-platform rejection without macOS artifacts;
- Windows SID-specific task naming and canonical argument quoting;
- exact XML ownership, principal, trigger, settings, action, and default
  normalization;
- localized Task Scheduler query output decoding;
- unmanaged-task conflicts;
- transactional install replacement, rollback, cleanup, and idempotent removal;
- Windows configuration path selection, atomic creation, DACL application, and
  handle-based validation on both setup and every read;
- Windows `tzdata` installation and named IANA-zone behavior across one
  daylight-saving transition;
- notification success and every non-fatal failure path;
- current-user targeting, 60-second expiry, ISO-date-only content, and no
  wildcard target;
- CLI setup/install/status/remove selecting launchd on macOS and Task Scheduler
  on Windows;
- scheduled draft generation remaining local and never importing push code.

The GitHub Actions Windows job installs `teammem-memberkit`, runs its Windows
tests, and creates, queries, replaces, and removes one uniquely identified
disposable MemberKit task. It chooses install and replacement times safely in
the future on the same local date so `StartWhenAvailable` cannot execute
`scheduled-run`, asserts that the task action was never run, and waits across
midnight only within the same bounded rollover window used by the proven hub
smoke test. If a safe window cannot be reached, it fails without registering a
task. Cleanup runs unconditionally. Unit tests never touch a production
scheduler.

The full MemberKit, hub, frozen-bundle integration, build, and public-content
checks run before completion.

## Documentation

The root README, MemberKit README, and member guide explain:

- the same commands dispatch to launchd or Task Scheduler;
- Windows configuration and scheduler-state locations;
- logged-in-only and locked-session behavior;
- missed-run and overlap behavior;
- best-effort Windows reminder delivery;
- schedule status and removal before uninstall;
- Linux automatic installation remains deferred;
- package installation never schedules and scheduled runs never push.

## Platform References

- [Microsoft `schtasks /create`](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create)
- [Microsoft Task Scheduler schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema)
- [Microsoft `StartWhenAvailable`](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-startwhenavailable-settingstype-element)
- [Microsoft `msg.exe`](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/msg)
- [Microsoft Windows app notification guidance](https://learn.microsoft.com/en-us/windows/apps/develop/notifications/)
- [Python `zoneinfo` data-source guidance](https://docs.python.org/3/library/zoneinfo.html#data-sources)

## Non-Goals

- Linux systemd schedule installation.
- A guaranteed Windows app toast or MSIX/Start-menu application registration.
- Running while the Windows user is logged out.
- Waking sleeping machines.
- Installing the TeamMem hub for members.
- Automatically pushing or approving MemberKit drafts.
- Refactoring the hub scheduler in the same change.
