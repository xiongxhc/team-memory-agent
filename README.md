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

The hub operator manages the central collectors, roster, inbox importer, ledger,
and rendered views. Install the published command in an isolated environment:

```bash
pipx install teammem
mkdir -p ~/.config/teammem
curl -fsSL \
  https://raw.githubusercontent.com/xiongxhc/team-memory-agent/master/config/roster.example.yaml \
  -o ~/.config/teammem/roster.yaml
curl -fsSL \
  https://raw.githubusercontent.com/xiongxhc/team-memory-agent/master/config/projects.example.yaml \
  -o ~/.config/teammem/projects.yaml
```

Edit both configuration files before collecting or importing data. Operators who
are developing adapters or running from a pinned checkout can instead use:

```bash
git clone https://github.com/xiongxhc/team-memory-agent.git
cd team-memory-agent
python3 -m venv .venv
.venv/bin/pip install -e .
cp config/roster.example.yaml config/roster.yaml
cp config/projects.example.yaml config/projects.yaml
```

Import reviewed bundles into a local ledger:

```bash
TEAMMEM_DB=ledger.db TEAMMEM_CONFIG_DIR=~/.config/teammem \
  teammem import-bundles \
    --inbox inbox \
    --archive archive \
    --quarantine quarantine
```

Render Markdown views:

```bash
TEAMMEM_DB=ledger.db TEAMMEM_CONFIG_DIR=~/.config/teammem TEAMMEM_VAULT=vault \
  teammem render
```

GitLab and Feishu collectors are optional. LLM-backed synthesis is optional; the
ledger, bundle importer, queries, and deterministic renderer work without it.

The operator should create a private inbox Git repository, grant each member push
access, and give each member an inbox URL and roster slug. Import from a disposable
export of the inbox, not directly from its Git working tree, because accepted
files are consumed from the import directory. See
[deployment](https://github.com/xiongxhc/team-memory-agent/blob/master/docs/deployment.md)
for the complete operator and MemberKit lifecycle.

Upgrade or uninstall the packaged command with:

```bash
pipx upgrade teammem
pipx uninstall teammem
```

Uninstalling the command does not remove operator-owned configuration, ledgers,
archives, quarantine records, or rendered views.

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
