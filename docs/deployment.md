# Deployment

Start manually with fixture configuration and a temporary ledger. Do not introduce
a scheduler until the manual flow has been reviewed.

## Hub

Install the root package in an isolated environment. Configure paths and optional
collectors with `TEAMMEM_*` environment variables. Runtime databases, inboxes,
archives, quarantine files, generated views, certificates, and secrets must stay
outside source control.

Run the daily hub sequence only after individual commands work:

```text
collect configured central sources
import reviewed bundles
reclaim newly mapped central identities
render local Markdown views
optionally push the view repository
```

The repository does not install an operator schedule automatically. Operators may
call the run-once commands from launchd, systemd timers, cron, or another trusted
scheduler.

## MemberKit

`memberkit setup` writes `~/.config/teammem/memberkit.env` with mode `0600` and,
unless declined, installs a macOS LaunchAgent at 17:30 local time. Manage it with:

```bash
memberkit schedule status
memberkit schedule install --time 17:30
memberkit schedule remove
```

Non-macOS users can schedule `memberkit scheduled-run`. Transmission is always the
separate, interactive `memberkit push --date YYYY-MM-DD` action.
