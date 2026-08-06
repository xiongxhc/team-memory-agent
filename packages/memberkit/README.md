# Team Memory MemberKit

MemberKit is the member-facing client for
[Team Memory Agent](https://github.com/xiongxhc/team-memory-agent). Members install
this package directly; they do not clone the repository or install the hub.
MemberKit is a standalone command-line package, not a skill.

MemberKit prepares reviewable local drafts from configured observations. Scheduled
runs create drafts and reminders only. Nothing is transmitted until the member
explicitly runs `memberkit push`.

The default draft preserves one short frozen-v1 event for every eligible local
observation in timestamp order. MemberKit does not score, consolidate,
semantically deduplicate, or cap the evidence, so a busy day can contain hundreds
of events. TeamMem performs downstream synthesis after import.

## Before installing

MemberKit requires Python 3.11 or newer, `pipx`, Git, and a local
[`claude-mem`](https://github.com/thedotmack/claude-mem) observations database. Ask
the team-memory operator for your roster slug, the inbox Git URL, and push access.

## Install

```bash
pipx install teammem-memberkit
memberkit --help
```

Installing the package does not create a schedule or transmit anything. Configure
it separately:

```bash
memberkit setup
```

Setup asks for the roster slug and inbox URL. On macOS and Windows it then offers
a daily 17:30 reminder in the machine's local timezone. Press Enter to accept,
enter another `HH:MM`, or enter `no` to decline. Scheduling is opt-in:
installation alone never schedules. Private configuration is stored at
`~/.config/teammem/memberkit.env` on macOS and Linux and at
`%APPDATA%\TeamMemory\memberkit.env` on Windows.

For a machine whose host timezone differs from the member, pass an IANA timezone
such as `memberkit setup --timezone Asia/Dubai`. Invalid explicit timezone names
are rejected. This setting controls member-calendar attribution, not the native
scheduler's host-local trigger.

## Review workflow

```bash
memberkit draft --date YYYY-MM-DD
memberkit review --date YYYY-MM-DD
memberkit push --date YYYY-MM-DD
```

The date defaults to today in the configured member timezone. Drafts are stored at
`~/.memberkit/out/bundle-<member>-<YYYY-MM-DD>.json`. Remove a private item from
the JSON `events` list, save valid JSON, and run `memberkit review` again before
pushing. Editing only `journal_md` does not remove an event because that preview is
regenerated from `events` during review and again before push.

The former `--all` spelling remains accepted for compatibility:

```bash
memberkit draft --all --force --date YYYY-MM-DD
```

`--all` produces the same event set as the default; `--force` explicitly permits
replacement of an existing draft. Without `--force`, `memberkit draft` preserves
any existing file, including malformed or partially edited JSON, byte-for-byte.
This remains a bounded v1 projection rather than a raw database export, but title
and narrative summaries may contain sensitive text. Human review and redaction
are mandatory before every push. The `ts` field is normalized from the
observation epoch into the member's local timezone so it matches the bundle
date.

To exclude an unfinished date without transmitting it:

```bash
memberkit dismiss --date YYYY-MM-DD
```

Removed events stay excluded from catch-up drafts. Invalid member-edited drafts are
left untouched, and MemberKit never pushes automatically.

## Project exclusions

Create `MEMBERKIT_WORKDIR/exclude-projects.txt` with one exact project,
trailing-star project prefix, or `project ~ regular-expression` per line. Exact
and prefix projects are case-sensitive. Regex project matching is case-sensitive;
its search against the final frozen-v1 summary is case-insensitive. Preview rules
before relying on an unattended schedule:

```bash
memberkit exclusions list
memberkit exclusions preview --date YYYY-MM-DD
```

MemberKit parses the complete file once per invocation. An unreadable file,
invalid UTF-8, malformed syntax, C0/DEL control character, or invalid regular
expression fails closed before any bundle or review-state write. A scheduled run
with one of these errors sends no success notification.

Rules affect only newly generated or forced drafts; they do not rewrite an
existing draft. `memberkit draft --all` also filters. A direct draft whose events
are all filtered is a valid empty file, while a scheduled all-filtered date creates
no pending draft. `memberkit review` and `memberkit push` do not retroactively
filter a draft that already exists.

Ordinary rule edits require no schedule reinstall. Only released-package
verification may replace a wrapper or reinstall an existing wrapper-based
schedule; that migration is separate from changing local rules.

## Schedule

```bash
memberkit schedule status
memberkit schedule install --time 17:30
memberkit schedule remove
memberkit scheduled-run
```

The same commands automatically dispatch to macOS launchd or Windows Task
Scheduler. Linux automatic installation remains deferred; a Linux scheduler may
invoke the portable `memberkit scheduled-run` command manually.

On Windows, MemberKit stores scheduler state under
`%LOCALAPPDATA%\TeamMemory\MemberKit` and derives one task name from the current
user SID. The least-privilege task runs only while that user is logged in; a
locked session is eligible, but logout prevents a run. `StartWhenAvailable`
catches up a missed trigger when an interactive session is available, `IgnoreNew`
prevents overlap, and the task does not wake a sleeping machine. The `msg.exe`
reminder is best effort. Scheduled diagnostics stay under `MEMBERKIT_WORKDIR` in
`schedule.log` and `schedule.err`, each capped at 1 MiB with one `.1` rollover.

MemberKit refuses a same-name task whose complete managed definition conflicts,
and initial creation refuses a name collision instead of overwriting it, so
MemberKit does not overwrite or delete tasks it does not own. The lifecycle lock
serializes cooperating MemberKit commands. A different Task Scheduler client
running as the same Windows identity can still mutate the name between query,
revalidation, and mutation; Task Scheduler provides no atomic compare-and-swap,
so non-cooperating same-identity concurrency is outside the transaction
guarantee.

A scheduled run is triggered at the configured time in the host scheduler's
local clock. Once running, it checks yesterday and today in the configured member
timezone, even if that differs from the host. Late work remains attributed to its
member-local date, work after member-local midnight belongs to the new day, and
unfinished dates remain in later reminders. `MEMBERKIT_TIMEZONE` does not
dynamically change the host trigger. Scheduling uses the same evidence-first
projection as direct drafting and never replaces an existing draft; explicitly
regenerate with `memberkit draft --force` after review if later observations need
to be included. Scheduled runs never approve, commit, push, or transmit anything.

## Upgrade and uninstall

```bash
pipx upgrade teammem-memberkit
memberkit schedule remove
pipx uninstall teammem-memberkit
```

Remove the schedule before uninstalling. Package removal does not delete
member-owned configuration, drafts, state, or the local inbox clone.

For configuration options, non-interactive setup, file locations, timing examples,
and troubleshooting, see the
[complete MemberKit guide](https://github.com/xiongxhc/team-memory-agent/blob/main/docs/member-guide.md).

Licensed under Apache-2.0.
