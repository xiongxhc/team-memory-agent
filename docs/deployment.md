# Deployment

Team Memory Agent has two installation locations:

| Location | Package | Responsibility |
|---|---|---|
| Operator-controlled, normally available Mac mini, Linux server, or VPS | `teammem` | Poll enabled central providers, import reviewed bundles, own the ledger, and render shared views |
| Each participating member's workstation | `teammem-memberkit` | Prepare local drafts, support manual highlights, remind, review, and explicitly push |

Start the hub manually. Package installation enables no network connector,
performs no provider request, and creates no schedule. `teammem run-daily` is one
run only on the operator machine.

## Hub package lifecycle

Requires Python 3.11 or newer and `pipx`.

### Install the published package

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

Edit the three YAML files and `hub.env` before enabling collection. Never commit
the environment file.

### Upgrade the published package

```bash
pipx upgrade teammem
teammem connectors check
teammem run-daily
```

Review release notes and keep the existing user-owned configuration and runtime
data in place.

### Uninstall the published package

```bash
pipx uninstall teammem
```

Uninstalling the command does not remove operator-owned configuration, ledgers,
archives, quarantine records, inbox checkouts or exports, snapshots, or rendered
views. Preserve or delete those separately according to the team's retention
policy.

### Source-checkout alternative

Contributors developing adapters or operators pinning a reviewed revision can
install an editable checkout:

```bash
git clone https://github.com/xiongxhc/team-memory-agent.git
cd team-memory-agent
python3 -m venv .venv
.venv/bin/pip install -e .
cp config/roster.example.yaml config/roster.yaml
cp config/projects.example.yaml config/projects.yaml
cp config/connectors.example.yaml config/connectors.yaml
```

Keep secrets, ledgers, inboxes, archives, quarantine records, snapshots, and
rendered views outside the checkout. Invoke the hub as `.venv/bin/teammem`.

Upgrade a source installation only after reviewing the target revision:

```bash
git status --short
git pull --ff-only
.venv/bin/pip install --upgrade -e .
.venv/bin/teammem connectors check
.venv/bin/teammem run-daily
```

Uninstall it with:

```bash
.venv/bin/pip uninstall teammem
```

After preserving any mistakenly colocated runtime data, the operator may delete
the virtual environment and checkout.

## Hub runtime configuration

`~/.config/teammem/hub.env` accepts literal `KEY=VALUE` lines and must remain
user-only (`0600`). It does not perform shell expansion: use absolute paths, not
`~`, `$HOME`, or command substitutions. Process environment values override file
values for one run.

| Variable | Required when | Purpose |
|---|---|---|
| `TEAMMEM_CONFIG_DIR` | Recommended | Directory containing `roster.yaml`, `projects.yaml`, and `connectors.yaml` |
| `TEAMMEM_DB` | Recommended | Local SQLite ledger path |
| `TEAMMEM_VAULT` | Recommended | Regenerated Markdown output directory |
| `TEAMMEM_SINCE_DAYS` | Optional | Connector lookback; default is 7 |
| `TEAMMEM_INBOX`, `TEAMMEM_ARCHIVE`, `TEAMMEM_QUARANTINE` | Optional as one complete set | Import an already-exported MemberKit inbox and retain accepted/rejected files |
| `TEAMMEM_SNAPSHOTS` | Optional | Daily SQLite backup directory; newest 14 are retained |
| `TEAMMEM_OBSIDIAN_PROJECTS` | Optional | Source directory for project-document synchronization |
| `TEAMMEM_PUSH` | Optional | Best-effort Git push of the rendered vault when true |
| `ANTHROPIC_API_KEY` | Optional | Enables journal and weekly-report synthesis |
| `TEAMMEM_LLM_DAILY_MODEL`, `TEAMMEM_LLM_REPORT_MODEL` | Optional | Override synthesis model names |

Without an LLM backend, synthesis stages are skipped and deterministic rendering
still succeeds.

## Provider setup and visibility

All provider enable flags are non-secret and default to `false` in
`connectors.yaml`. Provider credentials belong only in `hub.env` or the process
environment. Identity fields live in `roster.yaml`; project and resource
boundaries live in `projects.yaml`.

The permissions below were checked against current official provider
documentation.

| Provider | Environment variables | Non-secret YAML | Minimum provider setup | What collection can see |
|---|---|---|---|---|
| GitHub | `TEAMMEM_GITHUB_TOKEN` | `github` member IDs; `github_repos`; `enabled: true` | Fine-grained token restricted to the selected repositories, with **Contents: read** for [list commits](https://docs.github.com/en/rest/commits/commits) and **Pull requests: read** for [list pull requests](https://docs.github.com/en/rest/pulls/pulls) | Default-branch commits and pull requests updated in the lookback, only for explicitly mapped repositories |
| GitLab | `TEAMMEM_GITLAB_URL`, `TEAMMEM_GITLAB_TOKEN`, `TEAMMEM_GITLAB_GROUP` | `gitlab` member IDs and emails; `gitlab_repos`; `enabled: true` | Token that can see the configured group with [`read_api`](https://docs.gitlab.com/security/tokens/access_token_scopes/). The adapter uses the official [group projects](https://docs.gitlab.com/api/groups/), [commits](https://docs.gitlab.com/api/commits/), and [merge requests](https://docs.gitlab.com/api/merge_requests/) APIs | Projects returned under the configured group, including subgroups. Known `gitlab_repos` receive project attribution; other in-scope projects remain visibly unmapped |
| Slack | `TEAMMEM_SLACK_BOT_TOKEN` | `slack` member IDs; `slack_channels`; `enabled: true` | Bot token only. For public channels grant `channels:read` and `channels:history`; for private project channels also grant `groups:read` and `groups:history`. Add the app visibly to every allowlisted channel. See [`conversations.info`](https://docs.slack.dev/reference/methods/conversations.info/), [`conversations.history`](https://docs.slack.dev/reference/methods/conversations.history/), and the deliberately unused [`conversations.replies`](https://docs.slack.dev/reference/methods/conversations.replies/) | Human top-level messages in allowlisted shared project channels containing the app; no DMs, multi-person DMs, unlisted channels, bot messages, or thread replies |
| Feishu | `TEAMMEM_FEISHU_APP_ID`, `TEAMMEM_FEISHU_APP_SECRET` | `feishu` member IDs; `feishu_channels`; `enabled: true` | Custom app with bot capability, installed in the tenant and visibly added to each allowlisted group. Use app-identity group-read permission (`im:chat:readonly`), message read (`im:message:readonly`), and group-message history (`im:message.group_msg`). See official [tenant token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal), [group information](https://open.feishu.cn/document/server-docs/group/chat/get-2), and [conversation history](https://open.feishu.cn/document/server-docs/im-v1/message/list) documentation | Human messages only in allowlisted group chat IDs; no direct chats or unlisted groups |
| Discord | `TEAMMEM_DISCORD_BOT_TOKEN` | `discord` member IDs; `discord_channels`; `enabled: true` | Bot installed in the guild with `VIEW_CHANNEL` and `READ_MESSAGE_HISTORY` for each allowlisted channel, plus the `MESSAGE_CONTENT` privileged intent in the Developer Portal. See [Get Channel Messages](https://docs.discord.com/developers/resources/message#get-channel-messages), [permissions](https://docs.discord.com/developers/topics/permissions), and [Message Content Intent](https://docs.discord.com/developers/events/gateway#message-content-intent) | Human content messages in allowlisted guild channels; no DM/group-DM channels, unlisted guild channels, bots, or webhooks |

For Slack, the adapter requests 15 messages per history page and waits 60 seconds
between pages. This matches the one-request-per-minute, 15-object limit applicable
to commercially distributed non-Marketplace apps described in Slack's
[official rate-limit notice](https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/).
It never uses a user token and never requests thread replies.

Discord's messages endpoint returns no history without
`READ_MESSAGE_HISTORY`, while missing `MESSAGE_CONTENT` can empty content fields.
An empty channel result therefore produces a diagnostic warning to verify both.

Feishu is a first-class official provider. The private deployment remains
Feishu-based and unchanged. The public GitHub + Slack quick start neither
reconfigures nor replaces it.

### Example GitHub + Slack mapping

`connectors.yaml`:

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

Relevant portions of `projects.yaml` and `roster.yaml`:

```yaml
projects:
  project-alpha:
    github_repos: [team/project-alpha]
    slack_channels: [C0123]

members:
  alex:
    name: Alex Rivera
    emails: [alex@example.com]
    github: [alex-gh]
    slack: [U0123]
```

Use example IDs only in public files. Put actual IDs in the operator-owned
configuration.

## Validate, then run once

The following commands load local configuration and credentials but do not call a
provider:

```bash
teammem connectors list
teammem connectors check
```

`connectors list` shows all five built-ins as `disabled`, `enabled/ok`, or
`enabled/missing ...`. `connectors check` exits 2 when an enabled provider is
missing required values and never prints secrets.

After checks pass, perform one observed run:

```bash
teammem run-daily
```

Each enabled connector runs independently. A provider failure is visible and
makes the aggregate result non-zero without discarding another provider's events.
Required ledger, reclaim, and render failures skip dependent work. LLM failures
remain visible but may still permit deterministic rendering from ledger evidence
and cached summaries.

`run-daily` does not stay resident and does not install, change, or remove a
schedule. The current package has no hub schedule-install command. Built-in
launchd/systemd management belongs to the later operator-scheduling task; until
then, run it manually or invoke this one-shot command from an independently
managed trusted scheduler. Polling needs outbound HTTPS (and Git access when the
operator performs inbox/vault transport), not an inbound public port.

## Safe MemberKit inbox import

The operator must:

1. Create a private Git inbox repository.
2. Grant each participating member permission to push.
3. Add each member's canonical slug to `roster.yaml`.
4. Give each member their slug and the inbox Git URL.
5. Maintain a clean transport checkout that is never passed to the importer.

Accepted and quarantined files are consumed from the import directory. Export the
checkout to a disposable directory so the transport checkout stays clean and can
pull later revisions of the same member and date:

```bash
git -C /path/to/inbox-checkout pull --ff-only
STAGING_INBOX="$(mktemp -d)"
git -C /path/to/inbox-checkout archive HEAD | tar -x -C "$STAGING_INBOX"
```

Inspect the export with a dry run:

```bash
teammem import-bundles \
  --inbox "$STAGING_INBOX" \
  --archive /absolute/path/to/archive \
  --quarantine /absolute/path/to/quarantine \
  --dry-run
```

For the daily workflow, process environment values can point that one run at the
fresh export:

```bash
TEAMMEM_INBOX="$STAGING_INBOX" \
TEAMMEM_ARCHIVE=/absolute/path/to/archive \
TEAMMEM_QUARANTINE=/absolute/path/to/quarantine \
  teammem run-daily
```

Remove the disposable export after the run. Never delete imported files from the
Git checkout. A later export can contain previously accepted bundles; event and
archive idempotency inserts no duplicate events.

`run-daily` does not pull the inbox checkout or create this export. Inbox
transport remains an explicit operator-owned step.

## MemberKit lifecycle

Members install only the independently distributed client on their own
workstations:

```bash
pipx install teammem-memberkit
memberkit setup
```

They need Python 3.11 or newer, `pipx`, Git, a local `claude-mem` observations
database, their roster slug, and push access to the inbox.

`memberkit setup` writes `~/.config/teammem/memberkit.env` with mode `0600`. On
macOS it offers to install a LaunchAgent at 17:30 local time. Press Enter to
accept, enter another `HH:MM`, or enter `no` to decline. Package installation
alone never installs the schedule.

For managed onboarding:

```bash
memberkit setup \
  --member alex \
  --inbox-url git@forge.example:team/team-memory-inbox.git \
  --time 17:30
```

The member reviews generated JSON under `~/.memberkit/out/`, removes private
events, and explicitly chooses whether to share:

```bash
memberkit review --date YYYY-MM-DD
memberkit push --date YYYY-MM-DD
memberkit dismiss --date YYYY-MM-DD
```

For WhatsApp, Telegram, LINE, email, meetings, or any unsupported source, the
member can add a concise fallback to an existing local draft's authoritative
`events` list:

```json
{
  "ts": "2026-07-28T15:00:00+04:00",
  "kind": "journal-highlight",
  "summary": "Meeting: agreed the rollout owner and date",
  "project": "project-alpha",
  "refs": null
}
```

The timestamp's local calendar date must match the draft date. Keep the JSON
valid, run `memberkit review --date YYYY-MM-DD`, and use the separate
`memberkit push` only after review. The manual highlight remains local and
editable until that push. MemberKit never scrapes or authenticates to those
sources. See the [`teammem-bundle/v1` contract](../schemas/teammem-bundle-v1.md)
for the exact shape.

A malformed or partially edited draft is never overwritten by the schedule. It
remains pending so the member can repair it. To discard a malformed draft, delete
that local draft file first, then run:

```bash
memberkit dismiss --date YYYY-MM-DD
```

Upgrade or uninstall the member package with:

```bash
pipx upgrade teammem-memberkit
pipx uninstall teammem-memberkit
```

Uninstalling it does not remove member drafts, review state, the inbox clone, or
private configuration. See the [MemberKit guide](member-guide.md) for schedule
management, a source-checkout development installation, local file inventory,
and troubleshooting.
