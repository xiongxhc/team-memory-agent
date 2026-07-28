# Team Memory Agent

Team Memory Agent turns scattered team activity into a local, queryable event
ledger and regenerated Markdown reports. It combines central signals such as forge
commits and shared project-channel messages with member-reviewed local highlights.

The privacy boundary is simple: MemberKit prepares a JSON bundle locally, the
member reviews it, and nothing is transmitted until that member explicitly runs
`memberkit push`. Scheduled runs create drafts and reminders only.

## Components

| Component | Installed by | Purpose |
|---|---|---|
| `teammem` | Hub operator | Collect, import, query, and render |
| `teammem-memberkit` | Individual member | Draft, review, and push selected highlights |
| `teammem-bundle/v1` | Both packages | Frozen JSON protocol |

MemberKit is a standalone command-line package, not a skill. Members install only
`teammem-memberkit`; they do not clone this repository or install the hub.

## Member quick start

### 1. Get the two team-specific values

Ask the hub operator for:

- your roster slug, such as `alex`;
- the Git URL of the team-memory inbox, plus permission to push to it.

MemberKit v0.1 reads local observations from
[`claude-mem`](https://github.com/thedotmack/claude-mem). Its default database is
`~/.claude-mem/claude-mem.db`. You also need Python 3.11 or newer, `pipx`, and Git.

### 2. Install and configure

Installing the package does not create a schedule or transmit anything.

```bash
pipx install teammem-memberkit
memberkit setup
```

Setup asks for the roster slug and inbox Git URL. On macOS it then proposes a daily
17:30 reminder in the member's local timezone: press Enter to accept, enter another
`HH:MM`, or enter `no` to decline. The configuration is stored with user-only
permissions at `~/.config/teammem/memberkit.env`.

For unattended setup:

```bash
memberkit setup \
  --member alex \
  --inbox-url git@forge.example:team/team-memory-inbox.git \
  --time 17:30
```

Use `--no-schedule` instead of `--time` to configure MemberKit without installing
the macOS schedule.

### 3. Review before sharing

The schedule prepares a local draft and shows a reminder. It never pushes. Review
today's draft:

```bash
memberkit review
```

To remove private items, edit the `events` list in
`~/.memberkit/out/bundle-<member>-<YYYY-MM-DD>.json`, save valid JSON, and review
again. `journal_md` is only a preview and is regenerated from the reviewed event
list when pushing.

Share the reviewed date, or dismiss it without sharing:

```bash
memberkit push --date YYYY-MM-DD
memberkit dismiss --date YYYY-MM-DD
```

The date defaults to today for `draft`, `review`, `push`, and `dismiss`. Removed or
dismissed events remain excluded from later catch-up drafts.

For work from WhatsApp, Telegram, LINE, email, meetings, or another source that
the hub does not support, MemberKit remains the reviewed manual fallback. Add a
valid `journal-highlight` entry to an existing local draft's `events` list, run
`memberkit review`, and push only if the draft is correct. The entry stays local
until `memberkit push`; MemberKit does not log in to, scrape, or automatically
read those applications. See the
[`teammem-bundle/v1` contract](https://github.com/xiongxhc/team-memory-agent/blob/master/schemas/teammem-bundle-v1.md)
for the five required event fields.

### Schedule behavior

A scheduled run checks yesterday and today, preserving every event's original local
calendar date. With the default 17:30 schedule, work from 17:30–23:59 is discovered
the next day but remains in the earlier day's catch-up draft. Work at or after
midnight belongs to the next calendar day. Older unfinished dates remain in later
reminders.

```bash
memberkit schedule status
memberkit schedule install --time 17:30
memberkit schedule remove
memberkit scheduled-run
```

Automatic schedule installation currently supports macOS launchd. Other platforms
can schedule the portable `memberkit scheduled-run` command. If an edited draft is
invalid JSON, a scheduled run leaves it untouched and keeps reminding the member
to repair it. To discard a malformed draft, delete that local draft file before
running `memberkit dismiss --date YYYY-MM-DD`.

See the complete
[MemberKit guide](https://github.com/xiongxhc/team-memory-agent/blob/master/docs/member-guide.md)
for upgrades, troubleshooting, files created locally, and safe removal.

## Hub quick start

The hub runs on an operator-controlled, normally available machine: an always-on
Mac mini, a Linux server, or a VPS. It collects central sources, imports reviewed
MemberKit bundles, owns the SQLite ledger, and renders the shared Markdown views.
Members do not install it.

The public quick start uses GitHub and Slack. GitLab, Feishu, and Discord are
equally supported built-in options. Every network connector is disabled by
default; package installation makes no provider request and creates no schedule.

### 1. Install the published package

```bash
pipx install teammem
mkdir -p ~/.config/teammem
chmod 700 ~/.config/teammem
curl -fsSL \
  https://raw.githubusercontent.com/xiongxhc/team-memory-agent/master/config/roster.example.yaml \
  -o ~/.config/teammem/roster.yaml
curl -fsSL \
  https://raw.githubusercontent.com/xiongxhc/team-memory-agent/master/config/projects.example.yaml \
  -o ~/.config/teammem/projects.yaml
curl -fsSL \
  https://raw.githubusercontent.com/xiongxhc/team-memory-agent/master/config/connectors.example.yaml \
  -o ~/.config/teammem/connectors.yaml
touch ~/.config/teammem/hub.env
chmod 600 ~/.config/teammem/hub.env
```

Requires Python 3.11 or newer and `pipx`. Upgrade or uninstall the packaged
command with:

```bash
pipx upgrade teammem
pipx uninstall teammem
```

Uninstalling the command does not remove operator-owned configuration, ledgers,
archives, quarantine records, inbox exports, snapshots, or rendered views.

### 2. Configure GitHub and Slack

Edit `connectors.yaml` so only the chosen connectors are enabled:

```yaml
connectors:
  github:
    enabled: true
  gitlab:
    enabled: false
  slack:
    enabled: true
  feishu:
    enabled: false
  discord:
    enabled: false
```

In `projects.yaml`, add only the repositories and shared project channels the hub
should collect:

```yaml
projects:
  project-alpha:
    github_repos: [team/project-alpha]
    slack_channels: [C0123]
```

Add each member's GitHub login and Slack user ID to `roster.yaml`. Create a
fine-grained GitHub token limited to those repositories with **Contents: read**
and **Pull requests: read**. Create a Slack app with a bot token, grant
`channels:read` and `channels:history` (plus `groups:read` and `groups:history`
only for private project channels), and visibly add the app to every configured
channel.

Edit the user-only `hub.env` and set actual values for
`TEAMMEM_GITHUB_TOKEN`, `TEAMMEM_SLACK_BOT_TOKEN`, `TEAMMEM_CONFIG_DIR`,
`TEAMMEM_DB`, and `TEAMMEM_VAULT`. Values are literal, so use absolute paths
rather than `~` or shell variables. Process environment values override this
file.

The complete provider table, current official permission links, and all runtime
paths are in the
[deployment guide](https://github.com/xiongxhc/team-memory-agent/blob/master/docs/deployment.md).

### 3. Check locally, then run once

These two commands inspect local configuration only; they do not authenticate or
make network requests:

```bash
teammem connectors list
teammem connectors check
```

Then perform one operator-observed run:

```bash
teammem run-daily
```

`teammem run-daily` executes one idempotent run on the operator machine and
returns a per-step result. It does not remain resident and does not create,
change, or remove a schedule. Hub schedule installation belongs to a later
operator-scheduling release; package installation alone never schedules this
command.

### What the built-in connectors can see

| Connector | Collection boundary |
|---|---|
| GitHub | Commits and pull requests from explicitly mapped repositories |
| GitLab | Commits and merge requests in the operator-configured group; mapped repositories get project attribution and other in-scope repositories remain visibly unmapped |
| Slack | Human top-level messages in explicitly mapped shared project channels; no DMs and no thread replies |
| Feishu | Human messages in explicitly mapped group chats; no direct chats |
| Discord | Human messages in explicitly mapped guild channels; no DMs, bot messages, or webhooks |

Slack polling uses 15 messages per page and 60-second pacing between history
pages, matching the limits applicable to commercially distributed
non-Marketplace apps. Discord may return empty content or history when
`READ_MESSAGE_HISTORY` or `MESSAGE_CONTENT` access is missing.

Feishu remains a first-class official connector. The existing private deployment
continues to use Feishu unchanged; the GitHub + Slack public path is an additional
configuration, not a migration or replacement.

### Reviewed bundle inbox

The operator creates a private inbox Git repository, grants each member push
access, and provides each member an inbox URL and roster slug. Import from a
disposable `git archive` export, never directly from the transport checkout:
accepted and quarantined files are consumed from the configured import directory.
See the [deployment guide](https://github.com/xiongxhc/team-memory-agent/blob/master/docs/deployment.md)
for the safe export and `run-daily` workflow.

### Source-checkout alternative

Operators developing adapters or running a pinned checkout can install it in a
dedicated virtual environment instead:

```bash
git clone https://github.com/xiongxhc/team-memory-agent.git
cd team-memory-agent
python3 -m venv .venv
.venv/bin/pip install -e .
cp config/roster.example.yaml config/roster.yaml
cp config/projects.example.yaml config/projects.yaml
cp config/connectors.example.yaml config/connectors.yaml
```

Run checkout commands as `.venv/bin/teammem ...`. To upgrade, review the target
revision, update the checkout with `git pull --ff-only`, then run
`.venv/bin/pip install --upgrade -e .`. To uninstall the checkout installation,
run `.venv/bin/pip uninstall teammem`; preserve runtime data outside the checkout
before deleting the virtual environment or checkout.

LLM-backed synthesis is optional. Without a configured backend, journal and
weekly-report synthesis are skipped while the ledger, importer, queries, and
deterministic renderer continue to work.

## Principles

- The SQLite ledger is truth; Markdown output is disposable and regenerated.
- Import is idempotent per attributed event.
- Unknown central identities are surfaced; unknown MemberKit identities are
  quarantined.
- Shared-channel collection requires a visibly present integration.
- Raw local databases, direct messages, and arbitrary files never enter a bundle.
- This project provides work visibility, not employee scoring or performance
  evaluation.

See
[architecture](https://github.com/xiongxhc/team-memory-agent/blob/master/docs/architecture.md),
[privacy](https://github.com/xiongxhc/team-memory-agent/blob/master/docs/privacy.md),
[deployment](https://github.com/xiongxhc/team-memory-agent/blob/master/docs/deployment.md),
the
[MemberKit guide](https://github.com/xiongxhc/team-memory-agent/blob/master/docs/member-guide.md),
and the
[`teammem-bundle/v1` contract](https://github.com/xiongxhc/team-memory-agent/blob/master/schemas/teammem-bundle-v1.md).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pip install -e packages/memberkit
.venv/bin/pytest -q tests packages/memberkit/tests
./scripts/check-public.sh
```

Before publication, the extracting operator should also provide a private regular
expression containing origin-specific organization and member identifiers without
committing it:

```bash
TEAMMEM_PUBLIC_DENY_REGEX='<private-regex>' ./scripts/check-public.sh
```

This project is licensed under Apache-2.0. The code owner authorized publication
under that license on 2026-07-27.
