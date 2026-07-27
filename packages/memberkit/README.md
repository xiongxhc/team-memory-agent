# Team Memory MemberKit

MemberKit is the member-facing client for
[Team Memory Agent](https://github.com/xiongxhc/team-memory-agent). Members install
this package directly; they do not clone the repository or install the hub.

MemberKit prepares reviewable local drafts from configured observations. Scheduled
runs create drafts and reminders only. Nothing is transmitted until the member
explicitly runs `memberkit push`.

## Install and configure

```bash
pipx install teammem-memberkit
memberkit setup
```

Setup offers a daily 17:30 local reminder. Press Enter to accept, enter another
`HH:MM`, or enter `no` to decline.

## Review workflow

```bash
memberkit review --date YYYY-MM-DD
memberkit push --date YYYY-MM-DD
```

To exclude an unfinished date without transmitting it:

```bash
memberkit dismiss --date YYYY-MM-DD
```

Removed events stay excluded from catch-up drafts. Invalid member-edited drafts are
left untouched, and MemberKit never pushes automatically.

MemberKit currently supports macOS launchd for automatic schedule installation.
Other platforms can schedule the portable `memberkit scheduled-run` command.

Licensed under Apache-2.0.
