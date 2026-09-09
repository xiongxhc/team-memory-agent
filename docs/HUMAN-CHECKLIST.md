# Human checklist — meeting storage and rendering

Everything in the meeting plan that an agent must not do. Each step is a command
a person runs on the deployment host, with the check that proves it worked.

Placeholders are written `<LIKE_THIS>`. Substitute real values at the moment you
run the command; do not commit them to this repository.

---

## 1. Set the meeting environment in the private overlay

The hub reads its private environment from `~/.config/teammem/hub.env` (override
with `teammem --env-file <PATH>`). That file lives in the private overlay, never
in this repository.

Meetings are all-or-nothing. The meeting commands accept the six directories
only as a complete set, so configuring one requires configuring every one plus
the web base. A hub that processes no meetings sets none of them and behaves
exactly as it always did — the daily pipeline never fails on a meeting setting.

Add to the overlay environment file:

```sh
TEAMMEM_MEETING_INBOX=/<ABSOLUTE_PATH>/meeting-inbox
TEAMMEM_MEETING_ARCHIVE=/<ABSOLUTE_PATH>/meeting-archive
TEAMMEM_MEETING_QUARANTINE=/<ABSOLUTE_PATH>/meeting-quarantine
TEAMMEM_MEETING_AUDIO_STORE=/<ABSOLUTE_PATH>/meeting-audio
TEAMMEM_MEETING_RECEIPTS=/<ABSOLUTE_PATH>/meeting-receipts
TEAMMEM_MEETING_ALERTS=/<ABSOLUTE_PATH>/meeting-alerts
TEAMMEM_VAULT_WEB_BASE=https://<FORGE_HOST>/<GROUP>/<VAULT_REPO>
TEAMMEM_VAULT_REMOTE=origin
TEAMMEM_VAULT_REF=refs/heads/<BRANCH>
```

### Required shape

Each of the six paths must be **absolute**, must contain no `..` segment, and
must be **neither equal to nor nested inside** any other meeting path or
`TEAMMEM_VAULT`. Nesting is refused because the inbox is walked recursively: an
archive inside the inbox is re-read on the next scan, and a receipt or a bundle
inside the vault is published.

`TEAMMEM_VAULT_WEB_BASE` must be the **bare repository root**:

- `https://` — no other scheme, `http://` is refused
- a host, and no credentials (no `user@host`)
- no whitespace, no query string, no fragment
- no `..` or `.` path segment
- no `-` path segment. `-` opens every forge's view namespace (`/-/blob/…`,
  `/-/tree/…`), so a base carrying one is already a page, and appending to it
  yields a link that resolves to nothing.

Good: `https://<FORGE_HOST>/<GROUP>/<VAULT_REPO>`
Bad: `https://<FORGE_HOST>/<GROUP>/<VAULT_REPO>/-/tree/main`

`TEAMMEM_VAULT_REF` must be a full ref such as `refs/heads/main`, not a bare
branch name. `TEAMMEM_VAULT_REMOTE` must be a plain git remote name.

### This value is validated, never defaulted

There is no default and no fallback for `TEAMMEM_VAULT_WEB_BASE`. If it is
missing, publication fails and no receipt is written. It is never quietly
replaced by a local filesystem path, so a receipt can never carry one. Set the
real value here, or meetings do not publish.

The environment file itself must be mode **0600** exactly, or every command
refuses to start:

```sh
chmod 600 ~/.config/teammem/hub.env
```

### Verify

`stats` does not validate meeting settings — only the meeting commands do.
Validation is shape-only and touches no filesystem, so this runs before the
directories exist:

```sh
teammem expire-meeting-audio --dry-run
```

A misconfiguration exits **2** and names the offending variable on stderr, for
example:

```
error: TEAMMEM_MEETING_ARCHIVE is required once any meeting setting is set
error: TEAMMEM_VAULT_WEB_BASE must be a bare repository root: no query, no fragment, no view path
```

Once it is clean, create the directories and confirm a full dry run:

```sh
mkdir -p /<ABSOLUTE_PATH>/meeting-{inbox,archive,quarantine,audio,receipts,alerts}
chmod 700 /<ABSOLUTE_PATH>/meeting-{inbox,archive,quarantine,audio,receipts,alerts}
teammem process-meetings --dry-run
```

---

## 2. Install the generated meeting schedule units on the deployment host

`teammem schedule install` generates the units and activates them in one step.
It writes the daily job as before, and adds the four meeting units whenever the
meeting settings are configured. Run it as the hub's own user — the units are
per-user, not system-wide.

```sh
teammem schedule install --time <HH:MM>
```

What it generates:

| Backend | Units |
|---|---|
| systemd (Linux) | `teammem-meetings.service`, `teammem-meetings.timer`, `teammem-meeting-retention.service`, `teammem-meeting-retention.timer` |
| launchd (macOS) | `org.teammem.meetings`, `org.teammem.meeting-retention` |

Publication polls every minute, leaving room for processing within the initial
ten-minute URL target; real end-to-end timing still requires acceptance evidence.
Audio expiry currently runs once a day, one hour after the daily run. This does
not satisfy the exact fourteen-day deletion deadline. The retention scheduling
contract is unresolved; do not treat generated definitions as deployment-ready
proof of the deadline.

Files land in `~/.config/systemd/user/` on Linux and `~/Library/LaunchAgents/`
on macOS.

### Verify they are active

```sh
teammem schedule status
```

On systemd, confirm both timers are enabled and scheduled:

```sh
systemctl --user is-enabled teammem-meetings.timer teammem-meeting-retention.timer
systemctl --user is-active  teammem-meetings.timer teammem-meeting-retention.timer
systemctl --user list-timers 'teammem-*'
```

`list-timers` must show a NEXT time for both. If the host has no graphical
session, a user manager stops at logout and the timers stop with it; enable
lingering so it survives:

```sh
loginctl enable-linger <HUB_USER>
```

On launchd:

```sh
launchctl print gui/$(id -u)/org.teammem.meetings
launchctl print gui/$(id -u)/org.teammem.meeting-retention
```

Both must print a job with `state = running` or a scheduled interval. A
`Could not find service` error means the job was not bootstrapped.

### Verify end to end

Drop one real bundle into the inbox and watch a full cycle:

```sh
teammem process-meetings
ls /<ABSOLUTE_PATH>/meeting-receipts
```

A receipt appears only after the remote is proven to hold the commit. Open its
`markdown_url` in a browser and confirm it resolves to the note.

---

## 3. Push and merge this branch — Chris only

Reserved for Chris. No agent pushes this branch, opens the merge request, or
merges it.

The branch is `feat/meeting-storage`. It has never been pushed. When Chris
decides to integrate:

```sh
git -C <REPO> log --oneline origin/main..feat/meeting-storage
git -C <REPO> diff --stat origin/main...feat/meeting-storage
git -C <REPO> push -u origin feat/meeting-storage
```

Then open the merge request and merge through the forge. Merging is a
deliberate decision about whether the meeting feature ships, not a mechanical
step at the end of the plan.
