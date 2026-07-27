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

Members do not clone this repository and do not install the hub.

## Member setup

After packages are published:

```bash
pipx install teammem-memberkit
memberkit setup
```

Setup proposes a daily 17:30 reminder in the member's local timezone. A scheduled
run checks yesterday and today, preserving each event's original local calendar
date. Late work from 17:30–23:59 is offered as a catch-up for that same day; work at
or after midnight belongs to the next day.

The daily review remains:

```bash
memberkit review --date YYYY-MM-DD
memberkit push --date YYYY-MM-DD
```

Removed events remain excluded from future catch-up drafts. MemberKit never
auto-pushes.

## Hub quick start

For a source checkout:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp config/roster.example.yaml config/roster.yaml
cp config/projects.example.yaml config/projects.yaml
```

Import reviewed bundles into a local ledger:

```bash
TEAMMEM_DB=ledger.db TEAMMEM_CONFIG_DIR=config \
  teammem import-bundles \
    --inbox inbox \
    --archive archive \
    --quarantine quarantine
```

Render Markdown views:

```bash
TEAMMEM_DB=ledger.db TEAMMEM_CONFIG_DIR=config TEAMMEM_VAULT=vault \
  teammem render
```

GitLab and Feishu collectors are optional. LLM-backed synthesis is optional; the
ledger, bundle importer, queries, and deterministic renderer work without it.

## Principles

- The SQLite ledger is truth; Markdown output is disposable and regenerated.
- Import is idempotent per attributed event.
- Unknown central identities are surfaced; unknown MemberKit identities are
  quarantined.
- Shared-channel collection requires a visibly present integration.
- Raw local databases, direct messages, and arbitrary files never enter a bundle.
- This project provides work visibility, not employee scoring or performance
  evaluation.

See [architecture](docs/architecture.md), [privacy](docs/privacy.md),
[deployment](docs/deployment.md), and the
[`teammem-bundle/v1` contract](schemas/teammem-bundle-v1.md).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pip install -e packages/memberkit
.venv/bin/pytest -q tests packages/memberkit/tests
./scripts/check-public.sh
```

Apache-2.0 is the proposed license. Publication additionally requires confirmation
that the code owner authorizes that license.
