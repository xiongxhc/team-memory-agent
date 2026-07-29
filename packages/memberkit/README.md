# Team Memory MemberKit

MemberKit is the member-facing client for
[Team Memory Agent](https://github.com/xiongxhc/team-memory-agent). Members install
this package directly; they do not clone the repository or install the hub.
MemberKit is a standalone command-line package, not a skill.

MemberKit prepares reviewable local drafts from configured observations. Scheduled
runs create drafts and reminders only. Nothing is transmitted until the member
explicitly runs `memberkit push`.

The default draft is a deterministic local curation of normally three to seven
highlights per project (fewer for a sparse day), with at most one best outcome per
work session and a hard cap of seven. It makes no LLM or network call.

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

Setup asks for the roster slug and inbox URL. On macOS it then offers a daily 17:30
reminder in the Mac's local timezone. Press Enter to accept, enter another
`HH:MM`, or enter `no` to decline. It stores private configuration at
`~/.config/teammem/memberkit.env`.

For a machine whose host timezone differs from the member, pass an IANA timezone
such as `memberkit setup --timezone Asia/Dubai`. Invalid explicit timezone names
are rejected. This setting controls member-calendar attribution, not the
host-local launchd trigger.

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
regenerated from `events` at push time.

If the curated draft may have omitted something important, use the raw
one-row-per-observation compatibility mode:

```bash
memberkit draft --all --force --date YYYY-MM-DD
```

`--all` changes selection; `--force` explicitly permits replacement of an
existing draft. Without `--force`, `memberkit draft` preserves any existing file,
including malformed or partially edited JSON, byte-for-byte. Raw mode deliberately
preserves legacy, unfiltered title/narrative summaries for local inspection and
may reveal sensitive observation text. Review and redact every raw event before
push; curated path filtering does not apply to `--all`. The `ts` field is
normalized from the observation epoch into the member's local timezone so it
matches the bundle date.

To exclude an unfinished date without transmitting it:

```bash
memberkit dismiss --date YYYY-MM-DD
```

Removed events stay excluded from catch-up drafts. Invalid member-edited drafts are
left untouched, and MemberKit never pushes automatically.

## Schedule

```bash
memberkit schedule status
memberkit schedule install --time 17:30
memberkit schedule remove
memberkit scheduled-run
```

MemberKit supports macOS launchd for automatic schedule installation. Other
platforms can schedule the portable `memberkit scheduled-run` command. A scheduled
run is triggered at the configured time in the host scheduler's local clock. Once
running, it checks yesterday and today in the configured member timezone, even if
that differs from the host. Late work remains attributed to its member-local date,
work after member-local midnight belongs to the new day, and unfinished dates
remain in later reminders. `MEMBERKIT_TIMEZONE` does not dynamically change the
launchd trigger. Scheduling uses curated mode and never replaces an existing
draft; explicitly regenerate with `memberkit draft --force` after review if later
observations need to be included.

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
[complete MemberKit guide](https://github.com/xiongxhc/team-memory-agent/blob/master/docs/member-guide.md).

Licensed under Apache-2.0.
