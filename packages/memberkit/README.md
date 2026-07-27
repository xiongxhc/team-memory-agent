# Team Memory MemberKit

MemberKit is the member-facing client for
[Team Memory Agent](https://github.com/xiongxhc/team-memory-agent). Members install
this package directly; they do not clone the repository or install the hub.
MemberKit is a standalone command-line package, not a skill.

MemberKit prepares reviewable local drafts from configured observations. Scheduled
runs create drafts and reminders only. Nothing is transmitted until the member
explicitly runs `memberkit push`.

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
local reminder. Press Enter to accept, enter another `HH:MM`, or enter `no` to
decline. It stores private configuration at
`~/.config/teammem/memberkit.env`.

## Review workflow

```bash
memberkit review --date YYYY-MM-DD
memberkit push --date YYYY-MM-DD
```

The date defaults to today. Drafts are stored at
`~/.memberkit/out/bundle-<member>-<YYYY-MM-DD>.json`. Remove a private item from
the JSON `events` list, save valid JSON, and run `memberkit review` again before
pushing. Editing only `journal_md` does not remove an event because that preview is
regenerated from `events` at push time.

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
run checks yesterday and today. Late work remains attributed to its original local
date, work after midnight belongs to the new day, and unfinished dates remain in
later reminders.

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
