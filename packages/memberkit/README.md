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
launchd trigger. Scheduling uses the same evidence-first projection as direct
drafting and never replaces an existing draft; explicitly regenerate with
`memberkit draft --force` after review if later observations need to be included.

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
