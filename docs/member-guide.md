# MemberKit guide

MemberKit is the member-facing command-line package for Team Memory Agent. It is
not a skill, does not require the hub package, and does not require a repository
checkout. It reads configured local observations, prepares a JSON draft, and waits
for the member to decide what to share.

## What you need

- macOS or Windows for automatic schedule installation; Linux can run the
  manual scheduled command from an operator-configured scheduler;
- Python 3.11 or newer;
- [`pipx`](https://pipx.pypa.io/) to keep the command in an isolated environment;
- Git, with your name and email configured;
- a local
  [`claude-mem`](https://github.com/thedotmack/claude-mem) observations database;
- your roster slug and an inbox Git URL from the team-memory operator;
- permission to push to that inbox.

Check the local prerequisites:

```bash
python3 --version
pipx --version
git --version
git config user.name
git config user.email
test -f ~/.claude-mem/claude-mem.db && echo "claude-mem database found"
```

## Install

Install the published package:

```bash
pipx install teammem-memberkit
memberkit --help
```

Only contributors installing from a source checkout should use:

```bash
python3 -m venv .venv
.venv/bin/pip install -e packages/memberkit
```

## Configure once

Interactive setup asks for the values supplied by the operator:

```bash
memberkit setup
```

Example prompts:

```text
Member slug: alex
Inbox Git URL: git@forge.example:team/team-memory-inbox.git
Daily reminder time [17:30], or 'no' to decline:
```

Press Enter at the final prompt to accept 17:30 in the machine's local timezone,
enter another `HH:MM`, or enter `no`. This is an explicit opt-in choice: package
installation alone never creates a schedule.

The equivalent non-interactive setup is:

```bash
memberkit setup \
  --member alex \
  --inbox-url git@forge.example:team/team-memory-inbox.git \
  --timezone Asia/Dubai \
  --time 17:30
```

To configure without a schedule:

```bash
memberkit setup \
  --member alex \
  --inbox-url git@forge.example:team/team-memory-inbox.git \
  --no-schedule
```

Advanced overrides are available through setup options or `MEMBERKIT_*`
environment variables:

| Setting | Default | Purpose |
|---|---|---|
| `MEMBERKIT_DB` | `~/.claude-mem/claude-mem.db` | Read-only local observations database |
| `MEMBERKIT_WORKDIR` | `~/.memberkit` | Drafts, state, inbox clone, and schedule logs |
| `MEMBERKIT_TIMEZONE` | detected local timezone | Observation dates, scheduled day selection, bounds, and event timestamps |

Setup stores the required values in `~/.config/teammem/memberkit.env` with mode
`0600` on macOS and Linux. Windows uses the current user's protected
`%APPDATA%\TeamMemory\memberkit.env`; MemberKit validates its owner and ACL on
every read. Use an IANA name such as `Asia/Dubai` or
`America/Los_Angeles`. An invalid explicit name is rejected. A process
`MEMBERKIT_TIMEZONE` overrides the private file for that invocation.

## Daily workflow

The installed macOS or Windows schedule runs `memberkit scheduled-run`. It
creates local drafts for yesterday and today and shows a reminder for every
unfinished date. It never approves a draft, invokes Git, pushes, or transmits
anything. New drafts contain a
short frozen-v1 event for every eligible observation in timestamp order.
MemberKit does not score, consolidate, semantically deduplicate, or cap this
evidence. A busy day can therefore contain hundreds of events. Drafting makes no
LLM or network call; TeamMem performs downstream synthesis after import.

Review a date:

```bash
memberkit review --date YYYY-MM-DD
```

The date defaults to today in the configured member timezone:

```bash
memberkit review
```

The JSON file is:

```text
~/.memberkit/out/bundle-<member>-<YYYY-MM-DD>.json
```

The former `--all` spelling remains available as a compatibility alias:

```bash
memberkit draft --all --force --date YYYY-MM-DD
```

`--all` produces the same event set as the default. `--force` is a separate,
explicit overwrite choice. Without `--force`, `memberkit draft` does not replace
any existing file, including valid, member-edited, malformed, or partially
written JSON. The v1 projection omits raw rows, facts, sessions, files, source
metadata, and complete narratives, but its title or bounded narrative summaries
may still contain sensitive text. Review and redact every event before pushing.
`ts` is normalized from each observation epoch into the member's local timezone
to satisfy the bundle-date contract.

To redact an item:

1. Open that file in a text editor.
2. Delete the complete item from its `events` list.
3. Keep the file as valid JSON.
4. Run `memberkit review --date YYYY-MM-DD` again.

The `events` list is authoritative. MemberKit regenerates `journal_md` from that
list during review and again before push, so editing only `journal_md` does not
redact an event.

Push only after review:

```bash
memberkit push --date YYYY-MM-DD
```

The first push clones the configured inbox under `~/.memberkit/inbox`. MemberKit
writes only the reviewed bundle, creates a Git commit, and pushes it. If Git asks
for credentials or reports a permission error, confirm your inbox access with the
operator. TeamMem imports the remaining events as evidence and performs
deduplication and concise synthesis for human-facing reports afterward.

Dismiss a date without sharing it:

```bash
memberkit dismiss --date YYYY-MM-DD
```

Removed and dismissed events remain excluded from later catch-up drafts.

## Timing and catch-up

The default launchd or Task Scheduler trigger is 17:30 in the host's local
timezone. It is static: `MEMBERKIT_TIMEZONE` does not dynamically move the
trigger. Once the command runs, direct and scheduled drafts use the configured
member timezone—or the detected local timezone when none is configured—for
scheduled yesterday/today selection, observation bounds, and event timestamps.

When the host and member zones are the same, the familiar catch-up rule applies:
events after the 17:30 run stay attributable to their original member date when
that draft is explicitly regenerated, while unfinished dates remain in later
host-local reminders. When the zones differ, 17:30 host-local may be another hour
for the member, but calendar attribution still follows the member-local timestamp:

- events seen before the daily run appear in that day's draft;
- events created after the daily run but before midnight remain attributable to
  the earlier date when the member explicitly regenerates that existing draft;
- events created at or after midnight belong to the new calendar day;
- unfinished older dates remain in later reminders until pushed or dismissed.

The schedule never overwrites an existing draft, whether valid, manually edited,
malformed, or partially written. After reviewing an existing draft, use explicit
`memberkit draft --force --date YYYY-MM-DD` if you want to regenerate it with
later observations. A malformed draft remains pending so the member can repair
it. To discard one, delete that local draft file first, then run:

```bash
memberkit dismiss --date YYYY-MM-DD
```

## Schedule management

The same commands select launchd on macOS and Task Scheduler on Windows:

```bash
memberkit schedule status
memberkit schedule install --time 17:30
memberkit schedule remove
```

Changing the time replaces the existing managed MemberKit schedule. The value is
interpreted in the host's local timezone. On macOS, MemberKit installs one
LaunchAgent:

```text
~/Library/LaunchAgents/org.teammem.memberkit-daily.plist
```

On Windows, MemberKit keeps private scheduler lifecycle state at:

```text
%LOCALAPPDATA%\TeamMemory\MemberKit
```

It derives the Task Scheduler name
`\TeamMem-MemberKit-Daily-<sid-hash>` from the current user's SID without placing
the SID in the name. The task uses `InteractiveToken` and least privilege: it
runs only while that user is logged in. Locking the screen is fine; logging out
prevents execution. `StartWhenAvailable` requests a catch-up run after a missed
trigger once the interactive token is available. `IgnoreNew` prevents overlapping
runs, and `WakeToRun=false` means MemberKit does not wake a sleeping computer.

Windows reminders use `msg.exe` for the current process's Windows session ID
with a 60-second expiry. Delivery is best effort: an unavailable or denied
reminder does not fail draft preparation. On Windows, `schedule.log` and
`schedule.err` under
`MEMBERKIT_WORKDIR` record bounded diagnostics; each is capped at 1 MiB and keeps
one `.1` rollover. On macOS, launchd redirects output to those filenames
directly, without MemberKit's bounded rotation.

MemberKit refuses to replace or delete any same-name task whose complete
definition does not validate as MemberKit-managed. Initial creation also refuses
a name collision instead of overwriting it. The lifecycle lock serializes
cooperating MemberKit commands, but Task Scheduler has no atomic compare-and-swap.
A non-cooperating client running as the same Windows identity can mutate the task
between MemberKit's query, revalidation, and mutation; that same-identity
concurrency is outside the transaction guarantee and requires a separately
privileged service for a stronger boundary.

Linux automatic schedule installation is deferred. A Linux scheduler can invoke:

```bash
memberkit scheduled-run
```

The command is portable, but `memberkit setup --time ...` and
`memberkit schedule install` intentionally reject Linux rather than creating a
partial native schedule. Use `memberkit setup --no-schedule` when configuring
MemberKit on Linux.

## Local files

| Path | Contents |
|---|---|
| `~/.config/teammem/memberkit.env` (macOS/Linux) | Private MemberKit configuration |
| `%APPDATA%\TeamMemory\memberkit.env` (Windows) | Private MemberKit configuration |
| `%LOCALAPPDATA%\TeamMemory\MemberKit` (Windows) | Scheduler lock and transient task-definition state |
| `~/.memberkit/out/` | Local review drafts |
| `~/.memberkit/state.json` | Pending, approved, and excluded fingerprints |
| `~/.memberkit/inbox/` | Local clone used only by explicit push |
| `~/.memberkit/schedule.log` | Scheduled-run output; bounded with one rollover on Windows, direct launchd output on macOS |
| `~/.memberkit/schedule.err` | Scheduled-run errors; bounded with one rollover on Windows, direct launchd output on macOS |

The configured source database is opened read-only. Scheduled runs never push,
commit, or transmit. `~/.memberkit` represents the default
`MEMBERKIT_WORKDIR`; Windows displays the same configured path using Windows
path syntax.

To verify drafting against your configured real database without writing the
normal `~/.memberkit` directory, use a temporary work directory:

```bash
MEMBERKIT_WORKDIR="$(mktemp -d)" memberkit draft --date YYYY-MM-DD
```

PowerShell:

```powershell
$temporaryWorkdir = Join-Path $env:TEMP ("memberkit-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temporaryWorkdir | Out-Null
$env:MEMBERKIT_WORKDIR = $temporaryWorkdir
memberkit draft --date YYYY-MM-DD
Remove-Item Env:MEMBERKIT_WORKDIR
```

This opens the configured observation database read-only and writes the draft and
review-state files only under the printed temporary directory. It is a local
inspection, not a push.

## Upgrade or remove

Upgrade:

```bash
pipx upgrade teammem-memberkit
```

Before uninstalling, remove the schedule while the command still exists:

```bash
memberkit schedule remove
pipx uninstall teammem-memberkit
```

Uninstalling the package does not delete the member-owned configuration, drafts,
state, or inbox clone. Remove those local files separately only after deciding they
are no longer needed.

## Troubleshooting

Check the installation and schedule:

```bash
memberkit --help
memberkit schedule status
```

Run one draft pass in the terminal to see errors directly:

```bash
memberkit scheduled-run
```

Common local causes are a missing or unreadable claude-mem SQLite database,
missing or invalid `MEMBERKIT_*` configuration such as the timezone, or a
`MEMBERKIT_WORKDIR` that cannot create or update draft and state files. Check
`schedule.err` under that work directory; on Windows, remember that `msg.exe`
notification failure is non-fatal and `memberkit schedule status` is
authoritative.
