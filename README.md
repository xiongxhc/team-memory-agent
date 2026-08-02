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
| `teammem-memberkit` | Individual member | Draft, review, and push local evidence |
| `teammem-bundle/v1` | Both packages | Frozen JSON protocol |

MemberKit is a standalone command-line package, not a skill. Members install only
`teammem-memberkit`; they do not clone this repository or install the hub.

## Member quick start

### 1. Get the two team-specific values

Ask the hub operator for:

- your roster slug, such as `alex`;
- the Git URL of the team-memory inbox, plus permission to push to it.

MemberKit v0.3 reads local observations from
[`claude-mem`](https://github.com/thedotmack/claude-mem). Its default database is
`~/.claude-mem/claude-mem.db`. You also need Python 3.11 or newer, `pipx`, and Git.

### 2. Install and configure

Installing the package does not create a schedule or transmit anything.

```bash
pipx install teammem-memberkit
memberkit setup
```

Setup asks for the roster slug and inbox Git URL. On macOS and Windows it then
proposes a daily 17:30 reminder in the machine's local timezone: press Enter to
accept, enter another `HH:MM`, or enter `no` to decline. This is explicit opt-in;
installing the package never creates a schedule. Private configuration is stored
at `~/.config/teammem/memberkit.env` on macOS and Linux and at
`%APPDATA%\TeamMemory\memberkit.env` on Windows.

For unattended setup:

```bash
memberkit setup \
  --member alex \
  --inbox-url git@forge.example:team/team-memory-inbox.git \
  --timezone Asia/Dubai \
  --time 17:30
```

Use `--no-schedule` instead of `--time` to configure MemberKit without installing
a schedule. `--timezone` is optional; without it, MemberKit detects the machine's
local timezone. It controls observation calendar attribution, not the native
scheduler's host-local trigger clock.

### 3. Review before sharing

The schedule prepares a local draft and shows a reminder. It never pushes. Review
today's draft:

```bash
memberkit review
```

By default, a draft contains one short v1 event for every eligible local
observation, in timestamp order. A busy day can contain hundreds of events.
MemberKit does not score, consolidate, semantically deduplicate, or cap them;
TeamMem performs downstream synthesis after import. This is still a bounded v1
projection, not a raw database export: internal facts, sessions, source metadata,
files, and complete observation payloads remain local.

```bash
memberkit draft --all --force --date YYYY-MM-DD
```

`--all` is a compatibility alias and produces the same events as the default.
`--force` is separately required because MemberKit never overwrites an existing
or partially edited draft by default. Eligible title or bounded narrative
summaries may still contain sensitive text, so human review and redaction are
mandatory before every push. The `ts` field is normalized from the observation
epoch into the member's local timezone so every event remains valid for the
bundle date.

To remove private items, edit the `events` list in
`~/.memberkit/out/bundle-<member>-<YYYY-MM-DD>.json`, save valid JSON, and review
again. `journal_md` is only a preview and is regenerated from the reviewed event
list when reviewing and again before pushing. Only the remaining `events` are
accepted as evidence; TeamMem later deduplicates and summarizes them for shared
human-facing reports.

Share the reviewed date, or dismiss it without sharing:

```bash
memberkit push --date YYYY-MM-DD
memberkit dismiss --date YYYY-MM-DD
```

The date defaults to today in the configured member timezone for `draft`, `review`,
`push`, and `dismiss`. Removed or dismissed events remain excluded from later
catch-up drafts.

For work from WhatsApp, Telegram, LINE, email, meetings, or another source that
the hub does not support, MemberKit remains the reviewed manual fallback. Add a
valid `journal-highlight` entry to an existing local draft's `events` list, run
`memberkit review`, and push only if the draft is correct. The entry stays local
until `memberkit push`; MemberKit does not log in to, scrape, or automatically
read those applications. See the
[`teammem-bundle/v1` contract](https://github.com/xiongxhc/team-memory-agent/blob/master/schemas/teammem-bundle-v1.md)
for the five required event fields.

### Schedule behavior

On macOS, launchd triggers at 17:30 in the Mac's local timezone; on Windows, Task
Scheduler uses the same host-local time. When the command runs,
`MEMBERKIT_TIMEZONE` determines member-local yesterday and today, observation
bounds, and emitted timestamps; it does not move the native trigger. In the
normal same-zone case, work from 17:30–23:59 remains attributable to the earlier
member date when that draft is explicitly regenerated. If the zones differ, the
trigger may occur at another member-local hour, but the same member-calendar
attribution applies. The schedule never changes an existing draft; finish
reviewing it, then use an explicit `memberkit draft --force` if you want to
include later observations from the same date. Older unfinished dates remain in
later host-local reminders.

```bash
memberkit schedule status
memberkit schedule install --time 17:30
memberkit schedule remove
memberkit scheduled-run
```

These same commands automatically use launchd on macOS and Task Scheduler on
Windows. The Windows task name is SID-specific, runs only for the current
logged-in user at least privilege, and remains eligible while the session is
locked. It does not run after logout or wake a sleeping machine. A missed trigger
runs when the interactive session is available; `IgnoreNew` prevents overlap.
Windows reminder delivery through `msg.exe` is best effort, while bounded
`schedule.log` and `schedule.err` files remain available under the configured
work directory; each is capped at 1 MiB with one `.1` rollover. Windows
scheduler lock and temporary-definition state is kept under
`%LOCALAPPDATA%\TeamMemory\MemberKit`.

MemberKit refuses to replace or remove any task whose complete managed definition
does not match, and initial creation refuses a same-name collision instead of
overwriting it. Its lifecycle lock coordinates MemberKit commands, but another
Task Scheduler client running as the same Windows identity can still race
between query, revalidation, and mutation because Task Scheduler offers no
atomic compare-and-swap. Linux automatic schedule installation remains deferred;
on Linux, configure a scheduler manually to invoke `memberkit scheduled-run`.

Scheduled runs create or preserve local drafts and reminders only: they never
approve, commit, push, or transmit anything. If an edited draft is invalid JSON,
a scheduled run leaves it untouched and keeps reminding the member to repair it.
To discard a malformed draft, delete that local draft file before running
`memberkit dismiss --date YYYY-MM-DD`. Run `memberkit schedule remove` before
upgrading or uninstalling the executable used by the schedule.

See the complete
[MemberKit guide](https://github.com/xiongxhc/team-memory-agent/blob/master/docs/member-guide.md)
for upgrades, troubleshooting, files created locally, and safe removal.

## Hub quick start

The hub runs on an always-on, operator-controlled Mac mini, Linux server, VPS, or
Windows machine that remains powered and logged in.
It collects central sources, imports reviewed MemberKit bundles, owns the SQLite
ledger, and renders the shared Markdown views. Members do not install it.

The public quick start uses GitHub and Slack. GitLab, Feishu, and Discord are
equally supported built-in options. Every network connector is disabled by
default; package installation makes no provider request and creates no schedule.

### 1. Install the connector-capable hub

The five-connector hub is version 0.3.0. The following source-checkout path keeps
the example configuration files beside the installation:

```bash
git clone https://github.com/xiongxhc/team-memory-agent.git
cd team-memory-agent
python3 -m venv .venv
.venv/bin/pip install -e .
source .venv/bin/activate
mkdir -p ~/.config/teammem
chmod 700 ~/.config/teammem
cp config/roster.example.yaml ~/.config/teammem/roster.yaml
cp config/projects.example.yaml ~/.config/teammem/projects.yaml
cp config/connectors.example.yaml ~/.config/teammem/connectors.yaml
touch ~/.config/teammem/hub.env
chmod 600 ~/.config/teammem/hub.env
$EDITOR ~/.config/teammem/hub.env
```

Requires Python 3.11 or newer and Git. Keep the virtual environment activated
for the commands below, including schedule installation.

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

In `projects.yaml`, add only the repositories and public or private project
channels containing the app that the hub should collect:

```yaml
projects:
  project-alpha:
    github_repos: [team/project-alpha]
    slack_channels: [C0123]
```

Add each member's GitHub login and Slack user ID to `roster.yaml`. Create a
fine-grained GitHub token limited to those repositories with **Contents: read**
and **Pull requests: read**. Create a Slack app with a bot token, grant
`channels:read` and `channels:history` for public channels, or `groups:read` and
`groups:history` for private channels, and visibly add the app to every
configured channel.

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
change, or remove a schedule. Package installation alone never creates a
background job. Connector, import, synthesis, documentation-sync, and push
failures remain visible as failed steps but are warning-level for the aggregate
exit status and retry on the next run. Ledger, identity-reclaim, render, and
snapshot failures return non-zero so the scheduler reports failures that threaten
the ledger or its durable local projection.

Only after the observed run succeeds, explicitly install the daily job. The
default and example below are 18:20 in the operator machine's local timezone:

```bash
teammem schedule install --time 18:20
teammem schedule status
```

The built-in schedule invokes only `teammem run-daily`; it does not pull or
export a private MemberKit inbox. On Windows it is a current-user, logged-in-only
Task Scheduler task: a screen lock is fine, but logout prevents runs. See the
[deployment guide](https://github.com/xiongxhc/team-memory-agent/blob/master/docs/deployment.md)
for macOS/Linux/Windows paths, logs, missed-run behavior, safe inbox staging,
upgrades, and removal.

### What the built-in connectors can see

| Connector | Collection boundary |
|---|---|
| GitHub | Commits and pull requests from explicitly mapped repositories |
| GitLab | Commits, merge requests, issue lifecycle observations, and repository creations in the operator-configured group hierarchy, including subgroups but excluding projects merely shared into it; polling captures one initial-creation fact and one provider-reported closure fact per issue, while repeated reopen/reclose history requires a state-events or webhook source; mapped repositories get project attribution and other in-scope repositories remain visibly unmapped |
| Slack | Human top-level messages in explicitly mapped public or private project channels containing the app; no DMs and no thread replies |
| Feishu | Human messages in explicitly mapped group chats; no direct chats |
| Discord | Human messages in explicitly mapped guild channels; no DMs, bot messages, or webhooks |

Slack polling uses 15 messages per page and globally paces all history requests
at least 60 seconds apart. Slack's tighter limit applies to affected commercially
distributed apps outside Marketplace approval; Slack says internal
customer-built apps are not affected. The hub uses the conservative policy for
portable deployments and honors `Retry-After` when Slack returns it. Discord may
return empty content or history when `READ_MESSAGE_HISTORY` or
`MESSAGE_CONTENT` access is missing.

Feishu remains a first-class official connector. The existing private deployment
continues to use Feishu unchanged. Public Slack is an optional,
top-level-message-only connector, not a migration or replacement.

### Reviewed bundle inbox

The operator creates a private inbox Git repository, grants each member push
access, and provides each member an inbox URL and roster slug. Import from a
disposable `git archive` export, never directly from the transport checkout:
accepted and quarantined files are consumed from the configured import directory.
See the [deployment guide](https://github.com/xiongxhc/team-memory-agent/blob/master/docs/deployment.md)
for the safe export and `run-daily` workflow.

### Published-package installation

Install the connector-capable release from PyPI with:

```bash
pipx install 'teammem>=0.2.0'
```

For a scheduled package installation, remove the schedule before upgrading,
test one manual run, and explicitly reinstall it:

```bash
teammem schedule remove
pipx upgrade teammem
teammem connectors check
teammem run-daily
teammem schedule install --time 18:20
```

Run `teammem schedule remove` before `pipx uninstall teammem`. Uninstalling the
command does not remove operator-owned configuration or runtime data.

To upgrade a source checkout, review the target
revision, first run `teammem schedule remove`, update with
`git pull --ff-only`, reinstall with `.venv/bin/pip install --upgrade -e .`,
reactivate the environment, check connectors, perform one manual `run-daily`,
and explicitly reinstall the schedule. Remove the schedule before
`.venv/bin/pip uninstall teammem`; preserve runtime data outside the checkout
before deleting the virtual environment or checkout.

LLM-backed synthesis is optional. Without a configured backend, journal and
weekly-report synthesis are skipped while the ledger, importer, queries, and
deterministic renderer continue to work.

## Principles

- The SQLite ledger is truth; Markdown output is disposable and regenerated.
- Import is idempotent per attributed event.
- Unknown central identities are surfaced; unknown MemberKit identities are
  quarantined.
- Project-channel collection requires a visibly present integration.
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
