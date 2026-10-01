# Deployment

Team Memory Agent has two installation locations:

| Location | Package | Responsibility |
|---|---|---|
| Always-on, operator-controlled Mac mini, Linux server, VPS, or logged-in Windows machine | `teammem` | Poll enabled central providers, import reviewed bundles, own the ledger, and render shared views |
| Each participating member's workstation | `teammem-memberkit` | Prepare local drafts, support manual highlights, remind, review, and explicitly push |

Install and configure the hub, validate it, and run it manually before scheduling
it. Package installation enables no network connector, performs no provider
request, and creates no background job. `teammem run-daily` is one run only on
the operator machine. Only `teammem schedule install` creates a schedule.

## Hub installation lifecycle

The connector-capable hub is version 0.4.0 and requires Python 3.11 or newer.

### Source-checkout installation

Use a reviewed source revision when the operator wants the configuration
templates and code in one checkout:

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

Edit the three YAML files and `hub.env` before enabling collection. Never commit
the environment file. Keep secrets, ledgers, inboxes, archives, quarantine
records, snapshots, and rendered views outside the checkout. Keep this virtual
environment activated when installing the schedule so the scheduler records the
correct `teammem` executable.

The shell commands above describe macOS and Linux. On Windows, use a Python
virtual environment or an installed package, and keep the default environment
file at `%APPDATA%\\TeamMemory\\hub.env`. It must be a regular,
non-reparse-point file owned by the current user, with no allow rule granting
read access to Everyone, Authenticated Users, or the built-in Users group. This
Windows owner/DACL contract replaces Unix mode `0600`; do not copy Unix `chmod`
instructions to Windows.

For a scheduled source installation, remove the old schedule before upgrading.
Review the target revision, reinstall, validate, run once manually, and then
explicitly recreate the schedule:

```bash
source .venv/bin/activate
teammem schedule remove
git status --short
git pull --ff-only
.venv/bin/pip install --upgrade -e .
teammem connectors check
teammem run-daily
teammem schedule install --time 18:20
```

Remove the schedule before uninstalling the source installation:

```bash
source .venv/bin/activate
teammem schedule remove
.venv/bin/pip uninstall teammem
```

### Published-package installation

Install the connector-capable release from PyPI with an explicit minimum
version:

```bash
pipx install 'teammem>=0.2.0'
```

For a scheduled 0.2.0-or-newer package installation, use this order:

```bash
teammem schedule remove
pipx upgrade teammem
teammem connectors check
teammem run-daily
teammem schedule install --time 18:20
```

Remove the schedule before uninstalling the packaged command:

```bash
teammem schedule remove
pipx uninstall teammem
```

Uninstalling either command does not remove operator-owned configuration,
ledgers, archives, quarantine records, inbox checkouts or exports, snapshots, or
rendered views. Preserve or delete those separately according to the team's
retention policy.

## Hub runtime configuration

On macOS and Linux, `~/.config/teammem/hub.env` accepts literal `KEY=VALUE`
lines and must remain user-only (`0600`). On Windows, the default is
`%APPDATA%\\TeamMemory\\hub.env` and must satisfy the current-user owner/DACL
and non-reparse-point contract above. Environment files do not perform shell
expansion: use absolute paths, not `~`, `$HOME`, or command substitutions.
Process environment values override file values for one run.

| Variable | Required when | Purpose |
|---|---|---|
| `TEAMMEM_CONFIG_DIR` | Recommended | Directory containing `roster.yaml`, `projects.yaml`, and `connectors.yaml` |
| `TEAMMEM_DB` | Recommended | Local SQLite ledger path |
| `TEAMMEM_VAULT` | Recommended | Regenerated Markdown output directory |
| `TEAMMEM_SINCE_DAYS` | Optional | Connector lookback; default is 7 |
| `TEAMMEM_INBOX`, `TEAMMEM_ARCHIVE`, `TEAMMEM_QUARANTINE` | Optional as one complete set | Import an already-exported MemberKit inbox and retain accepted/rejected files |
| `TEAMMEM_SNAPSHOTS` | Optional | Daily SQLite backup directory; newest 14 are retained |
| `TEAMMEM_OBSIDIAN_PROJECTS` | Optional | Source directory for project-document synchronization |
| `TEAMMEM_PUSH` | Optional, after publication setup below | Best-effort Git push of the rendered vault when true; default off |
| `TEAMMEM_LLM_PROVIDER` | Optional | Synthesis backend: `claude` (default) or `codex` |
| `ANTHROPIC_API_KEY` | Optional | With the Claude provider, use the Anthropic API instead of the Claude CLI fallback |
| `TEAMMEM_LLM_DAILY_MODEL`, `TEAMMEM_LLM_REPORT_MODEL` | Required for the corresponding Claude synthesis workload | Explicit model identifiers supported by your API account or Claude CLI; no defaults; ignored by the Codex provider |
| `TEAMMEM_CODEX_BIN` | Optional | Codex executable; default `codex` |
| `TEAMMEM_LLM_CONCURRENCY` | Optional | Concurrent journal LLM calls; default `2`, valid integers `1..8` |

Without an LLM backend, synthesis stages are skipped and deterministic rendering
still succeeds. For Claude, choose model identifiers supported by your own
Anthropic account or authenticated Claude CLI and set both model keys in the
protected `hub.env`. Daily journals use `TEAMMEM_LLM_DAILY_MODEL`; weekly reports
use `TEAMMEM_LLM_REPORT_MODEL`. A standalone command requires only its workload's
model. TeamMem does not choose a current provider model for you or verify account
entitlement until a real synthesis request.

The Anthropic API key takes precedence over automatic detection of `claude` on
`PATH`. With either backend available, missing, whitespace-only, or the old
`daily-summary-model` / `weekly-summary-model` placeholder values produce an
actionable configuration error before a provider call. This includes hosts
where an installed Claude CLI was discovered automatically. `journal` and
`report` exit `2` for that setup error; a full daily run records failed synthesis
while continuing deterministic stages. Inspect the stage results, not only the
daily exit code. Configuration loading, collection, capture-only, rendering,
and synthesis dry runs do not require models. Claude CLI authentication must be
available to the OS account running the schedule, just like Git authentication.

The Codex provider requires a prior interactive `codex login` for the same OS
account that owns the schedule. It pins `gpt-5.6-sol`, uses medium reasoning for
daily journals and high reasoning for weekly reports, and runs each call
ephemerally with tools disabled, a read-only sandbox, a credential-scrubbed
environment, and structured text output. Begin with
`TEAMMEM_LLM_CONCURRENCY=1`; concurrent processes use isolated temporary output
paths but still share one account's authentication and limits.

## Publish the rendered vault to a private Git remote

This is optional distribution of regenerated Markdown, separate from installing
the public engine. Create a **private** repository on your chosen Git host and
grant the scheduled OS account read/write access to that repository only. Use
the host's repository-creation UI, verify its visibility and readers, and copy
its credential-free SSH clone URL. For a new repository, leave README, license,
and template initialization disabled so the remote starts empty. If it already
has commits, follow the existing-remote path below.

Run these steps as the same OS account and with the same Git executable used by
the hub schedule. Remove/pause the schedule during setup. Keep `TEAMMEM_PUSH=0`
in the protected environment file until the first push is verified. Do not put
the vault inside the public engine checkout, the inbox transport checkout, or
the ledger/backup directory. The renderer owns its managed Markdown paths and
`git add -A` stages the entire vault; keep secrets and raw inputs outside it.

### Authentication and remote selection

Configure a dedicated SSH identity for the service account in its protected
SSH configuration, grant that identity write access on the Git host, and verify
the host key through the host's trusted published fingerprints. Authenticate
once under operator observation. Scheduled runs must not depend on an
interactive password prompt or a terminal's temporary SSH agent. Use the
platform's unattended credential mechanism appropriate to that account; do not
remove credential protection merely to make a test pass.

The following POSIX-shell examples use SSH. Replace the example path, remote and
branch with your choices. `TEAMMEM_VAULT` here is also a shell variable for Git
commands: separately write its exact absolute value into `hub.env`, whose values
are not shell-expanded.

```bash
export TEAMMEM_VAULT=/absolute/path/to/private-rendered-vault
VAULT_REMOTE=git@forge.example:team/team-memory-vault.git
VAULT_BRANCH=main
env -u SSH_AUTH_SOCK GIT_TERMINAL_PROMPT=0 \
  GIT_SSH_COMMAND='ssh -o BatchMode=yes' git ls-remote "$VAULT_REMOTE"
```

A successful empty result means an empty remote; authentication errors are not
an empty repository. If using HTTPS instead, configure a protected credential
helper usable noninteractively by the schedule account and run the same checks
with its credential-free HTTPS URL. Never embed a token in a remote URL, command,
Git config, or checked-in file. A read-only connector API token does not imply
permission to push a Git repository.

### New, empty remote

Start with an absent destination directory; preserve any existing local vault
elsewhere and inspect it before choosing how to migrate its history. Do not
clone over it. After confirming the remote has no refs:

```bash
env -u SSH_AUTH_SOCK GIT_TERMINAL_PROMPT=0 \
  GIT_SSH_COMMAND='ssh -o BatchMode=yes' git clone "$VAULT_REMOTE" "$TEAMMEM_VAULT"
git -C "$TEAMMEM_VAULT" switch --orphan "$VAULT_BRANCH"
```

### Remote with existing commits

Use a fresh, absent local destination, choose an existing remote branch, and
preserve its history. Do not initialize an unrelated local repository or force
push over the remote:

```bash
env -u SSH_AUTH_SOCK GIT_TERMINAL_PROMPT=0 \
  GIT_SSH_COMMAND='ssh -o BatchMode=yes' \
  git clone --branch "$VAULT_BRANCH" --single-branch "$VAULT_REMOTE" "$TEAMMEM_VAULT"
git -C "$TEAMMEM_VAULT" status --short
```

Review the remote's contents before rendering: managed Markdown paths will be
regenerated. Use a dedicated vault repository, not an engine or general document
repository. For an existing local vault with its own commits, inspect its origin,
branch and history first; reconcile divergence explicitly or retain that
checkout and migrate via a fresh clone. These instructions never reset, delete,
merge unrelated histories, or force-push operator data.

### First publication and scheduled write access

For either path, configure the vault-local Git author name and email chosen by
the operator (`git config user.name` and `git config user.email`), since cloning
a repository does not establish commit identity. Configure unattended SSH and
verify the selected origin before creating the first rendered commit:

```bash
git -C "$TEAMMEM_VAULT" config core.sshCommand 'ssh -o BatchMode=yes'
git -C "$TEAMMEM_VAULT" remote -v
TEAMMEM_PUSH=0 teammem run-daily
teammem render --verify
git -C "$TEAMMEM_VAULT" status --short
git -C "$TEAMMEM_VAULT" log -1 --oneline
```

Stop if configuration/stages fail, verification fails, the working tree is
unexpectedly dirty, or the generated tree contains data outside the intended
reader boundary. Review the generated content and commit before publication.
Then verify a noninteractive push and set the upstream used by future plain
`git push` calls:

```bash
env -u SSH_AUTH_SOCK GIT_TERMINAL_PROMPT=0 \
  git -C "$TEAMMEM_VAULT" push --dry-run origin "HEAD:refs/heads/$VAULT_BRANCH"
env -u SSH_AUTH_SOCK GIT_TERMINAL_PROMPT=0 \
  git -C "$TEAMMEM_VAULT" push --set-upstream origin "$VAULT_BRANCH"
git -C "$TEAMMEM_VAULT" rev-parse HEAD
env -u SSH_AUTH_SOCK GIT_TERMINAL_PROMPT=0 \
  git -C "$TEAMMEM_VAULT" ls-remote --exit-code origin "refs/heads/$VAULT_BRANCH"
git -C "$TEAMMEM_VAULT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}'
```

The local commit must equal the remote branch's returned hash and the upstream
must be `origin/<selected-branch>`. The real first push verifies write access;
a dry run alone cannot prove all server hooks will accept publication. Stop on
rejection and inspect permissions or concurrent remote history; never add
`--force`. There is no automatic pull or merge in TeamMem's publication stage.
Keep this a single-writer output repository, or coordinate external edits before
running the hub.

On Linux, repeat the read and dry-run write probes from the user service manager
with the actual absolute vault path, before enabling publication. For example:

```bash
systemd-run --user --wait --pipe --collect \
  /usr/bin/env -u SSH_AUTH_SOCK GIT_TERMINAL_PROMPT=0 \
  git -C "$TEAMMEM_VAULT" ls-remote --exit-code origin "refs/heads/$VAULT_BRANCH"
systemd-run --user --wait --pipe --collect \
  /usr/bin/env -u SSH_AUTH_SOCK GIT_TERMINAL_PROMPT=0 \
  git -C "$TEAMMEM_VAULT" push --dry-run origin "HEAD:refs/heads/$VAULT_BRANCH"
```

Use the equivalent account/session checks for launchd or Task Scheduler; Windows
uses the logged-in account described below, not a separate service account.
Do not assume a successful test in an administrator's shell proves scheduler
access. Only after these checks set `TEAMMEM_PUSH=1` in the protected `hub.env`,
install/resume the schedule, and inspect its first publication stage and remote
hash. Full-run pushes are best-effort, so a zero overall exit status is not proof
of publication; read the `push` stage result. `run-daily --capture-only` never
commits or pushes the vault.

Vault Git history is **not a ledger backup**. Set `TEAMMEM_SNAPSHOTS` outside the
vault for consistent SQLite snapshots and arrange separately protected off-host
backup and restore checks. The ledger and aggregate tables are authoritative;
the Markdown projection alone cannot restore them. Never publish credentials,
ledger files, snapshots, or raw import archives to the rendered-vault remote.

## Public engine and operator responsibilities

This repository supplies the generic hub engine, configuration templates,
rendering/publication primitives, and portable setup/scheduling instructions.
No private overlay is required to install or use it. Each operator owns their
people/project mappings, consent and access choices, credentials, private
repositories, backups, network/VPN access, monitoring, and any automatic service
or code-recovery deployment. TeamMem does not bundle an automatic recovery agent
or provision those operator-specific services.

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
| GitLab | `TEAMMEM_GITLAB_URL`, `TEAMMEM_GITLAB_TOKEN`, `TEAMMEM_GITLAB_GROUP` | `gitlab` member IDs and emails; `gitlab_repos`; `enabled: true`; optional boolean `collect_mr_commits` (default `true`) | Token that can see the configured group with [`read_api`](https://docs.gitlab.com/security/tokens/access_token_scopes/). `read_api` authorizes API reads but does not grant group/project membership or expand what the token identity can see. The adapter uses the official [group projects](https://docs.gitlab.com/api/groups/), [branches](https://docs.gitlab.com/api/branches/), [commits](https://docs.gitlab.com/api/commits/), [merge requests](https://docs.gitlab.com/api/merge_requests/), [issues](https://docs.gitlab.com/api/issues/), and [users](https://docs.gitlab.com/api/users/) APIs | Projects in the configured group hierarchy, including subgroups but excluding projects merely shared into that hierarchy, plus their issue lifecycle observations and repository creations inside the lookback. Repository commit polling paginates every reachable branch and requests that branch with `ref_name` plus `TEAMMEM_SINCE_DAYS`, excluding tag-only commits. By default, MRs merged inside that lookback also contribute all unseen MR commits, including in-window commits from deleted or squashed source branches and older commits; set `collect_mr_commits: false` to disable only this supplement. Known `gitlab_repos` receive project attribution; other in-scope projects remain visibly unmapped |
| Slack | `TEAMMEM_SLACK_BOT_TOKEN` | `slack` member IDs; `slack_channels`; `enabled: true` | Bot token only. For public channels grant `channels:read` and `channels:history`; for private project channels grant `groups:read` and `groups:history`. Add the app visibly to every allowlisted channel. See [`conversations.info`](https://docs.slack.dev/reference/methods/conversations.info/), [`conversations.history`](https://docs.slack.dev/reference/methods/conversations.history/), and the deliberately unused [`conversations.replies`](https://docs.slack.dev/reference/methods/conversations.replies/) | Human top-level messages in allowlisted public or private project channels containing the app; no DMs, multi-person DMs, unlisted channels, bot messages, or thread replies |
| Feishu | `TEAMMEM_FEISHU_APP_ID`, `TEAMMEM_FEISHU_APP_SECRET` | `feishu` member IDs; `feishu_channels`; `enabled: true` | Custom app with bot capability, installed in the tenant and visibly added to each allowlisted group. Use app-identity group-read permission (`im:chat:readonly`), message read (`im:message:readonly`), and group-message history (`im:message.group_msg`). See official [tenant token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal), [group information](https://open.feishu.cn/document/server-docs/group/chat/get-2), and [conversation history](https://open.feishu.cn/document/server-docs/im-v1/message/list) documentation | Human messages only in allowlisted group chat IDs; no direct chats or unlisted groups |
| Discord | `TEAMMEM_DISCORD_BOT_TOKEN` | `discord` member IDs; `discord_channels`; `enabled: true` | Bot installed in the guild with `VIEW_CHANNEL` and `READ_MESSAGE_HISTORY` for each allowlisted channel, plus the `MESSAGE_CONTENT` privileged intent in the Developer Portal. See [Get Channel Messages](https://docs.discord.com/developers/resources/message#get-channel-messages), [permissions](https://docs.discord.com/developers/topics/permissions), and [Message Content Intent](https://docs.discord.com/developers/events/gateway#message-content-intent) | Human content messages in allowlisted guild channels; no DM/group-DM channels, unlisted guild channels, bots, or webhooks |

For Slack, the adapter requests 15 messages per history page and globally waits
at least 60 seconds between all `conversations.history` calls across pages and
channels. `Retry-After` is authoritative when Slack returns it. Slack's tighter
limit applies to affected commercially distributed apps outside Marketplace
approval; Slack says internal customer-built apps are not affected. The adapter
uses this conservative policy for portable deployments. See Slack's
[official rate-limit notice](https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps/).
It never uses a user token and never requests thread replies.

Discord's messages endpoint returns no history without
`READ_MESSAGE_HISTORY`, while missing `MESSAGE_CONTENT` can empty content fields.
An empty channel result therefore produces a diagnostic warning to verify both.

Feishu is a first-class official provider. The GitHub and Slack quick start is
only an example; enabling Slack neither reconfigures nor replaces another
connector.

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

## Validate, run once, then schedule

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

Each enabled connector runs independently. Connector, import, journal, report,
documentation-sync, and push failures remain visible as failed steps without
discarding successful work, but they are warning-level for the aggregate exit
status and retry on the next run. Ledger, identity-reclaim, render, and snapshot
failures return non-zero so the scheduler reports failures that threaten the
ledger or its durable local projection. Required ledger, reclaim, and render
failures still skip dependent work; LLM failures may still permit deterministic
rendering from ledger evidence and cached summaries.

With an available LLM backend, the full run synthesizes or reuses daily person
journals, then reconciles both the previous and current Work Journal weeks.
Current-week reports are marked provisional Monday through Thursday, become a
Friday checkpoint, and may reconcile late evidence during the weekend.
Previous-week reconciliation catches late provider events and reviewed MemberKit
bundles. Each rendered report states the cutoff and precision stored with the
synthesis; it does not present newly captured but unsynthesized rows as covered.
Without an LLM backend, journal and report synthesis are skipped while
deterministic rendering continues from ledger evidence and cached summaries.

Use capture-only for an operator-managed intraday evidence tick:

```bash
teammem run-daily --capture-only
```

This mode collects enabled connectors, imports reviewed bundles, reclaims
identity and project mappings, writes an atomic snapshot when
`TEAMMEM_SNAPSHOTS` is configured, and skips
journal, report, documentation-sync, render, and push. It performs no LLM call
and no vault publication. If an enabled connector or the configured import fails,
capture-only returns non-zero after preserving successful work and, when
configured, snapshotting it.

Full and capture runs share a canonical-ledger lock. Capture mode fails fast if
another run is active. Full mode waits up to 30 minutes and prints content-free
progress before failing. Keep the ledger and its adjacent lock file on a local
filesystem with normal OS locking semantics.

Journal calls default to concurrency `2`; set `TEAMMEM_LLM_CONCURRENCY=1` for a
serial backend or another integer through `8` after observing provider capacity.
Only genuine person-day cache misses are submitted concurrently. Every event and
the full ordered person-day text remain in scope: there is no ranking, cap,
truncation, cross-person batching, hidden retry, compaction, or model change.

`run-daily` does not stay resident and does not install, change, or remove a
schedule. After that observed run succeeds, explicitly install the 18:20 daily
job and inspect it:

```bash
teammem schedule install --time 18:20
teammem schedule status
```

The time is the operator host's local timezone. Package installation alone does
nothing in the background; only `schedule install` writes and enables the job.
The schedule's invocation contains only the resolved `teammem` executable,
`--env-file`, the environment-file path, and `run-daily`. Credential values
remain in the separately protected environment file and are never copied into a
launchd, systemd, or Windows Task Scheduler definition.

The built-in definition always invokes the full `run-daily` command and installs
only one daily job. Extra intraday capture triggers are optional, explicit
operator-owned scheduler entries whose action includes `run-daily
--capture-only`; rerunning `teammem schedule install` does not create them.

Polling needs outbound provider HTTPS access and Git access when the operator
performs inbox or vault transport. It opens no inbound public port.

### macOS: launchd

On an always-on Mac mini, installation writes and loads this user LaunchAgent:

```text
~/Library/LaunchAgents/org.teammem.hub-daily.plist
```

Its output files are:

```text
~/.local/state/teammem/schedule.log
~/.local/state/teammem/schedule.err
```

Inspect or remove it through the CLI:

```bash
teammem schedule status
teammem schedule remove
```

The `StartCalendarInterval` uses local time. Apple's documented behavior is that
a calendar job missed while the Mac is asleep runs when the computer wakes; a
job missed while the Mac is powered off waits until the next designated time.
See Apple's
[Scheduling Timed Jobs](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html).
This is a per-user LaunchAgent, so keep the operator's GUI session logged in.
Keep the host normally available and rely on the connector lookback, not the
scheduler alone, to recover provider events after a gap.

Use this exact upgrade order:

```bash
teammem schedule remove
pipx upgrade teammem
teammem connectors check
teammem run-daily
teammem schedule install --time 18:20
```

For a source checkout, replace the `pipx upgrade` step with the reviewed
`git pull --ff-only` and editable reinstall shown above. Always run
`teammem schedule remove` before uninstalling either installation.

### Linux server or VPS: systemd user timer

Installation writes and enables these user units:

```text
~/.config/systemd/user/teammem-daily.service
~/.config/systemd/user/teammem-daily.timer
```

On an unattended server or VPS, an administrator must enable lingering for the
operator account so its user manager starts at boot and remains available after
logout:

```bash
sudo loginctl enable-linger "$USER"
teammem schedule install --time 18:20
```

That administrative choice is the operator's responsibility; `teammem` does not
run `sudo` or change linger state. The
[official `loginctl` manual](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html)
documents that `enable-linger` starts the user manager at boot and keeps it
after logout.

Inspect the timer, next run, and service logs with:

```bash
teammem schedule status
systemctl --user status teammem-daily.timer
systemctl --user list-timers teammem-daily.timer
journalctl --user -u teammem-daily.service
```

The timer contains `OnCalendar=*-*-* 18:20:00` without a timezone suffix, so
systemd uses the host's current local timezone. `Persistent=true` causes one
missed calendar activation to run when the user timer becomes active again. See
the official
[`systemd.timer`](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)
and
[`systemd.time`](https://www.freedesktop.org/software/systemd/man/latest/systemd.time.html)
manuals. Connector lookback then recovers provider events from the gap, while
ledger idempotency prevents duplicate attributed events on overlapping runs.

Remove the timer with:

```bash
teammem schedule remove
```

Use the same remove, upgrade, validate, manual-run, and reinstall order shown for
macOS. Remove the timer before uninstalling `teammem`.

### Windows: Task Scheduler

Windows scheduling is a current-user, least-privilege, logged-in-only Task
Scheduler task. It runs with `InteractiveToken`: a screen lock is fine, but
logout prevents runs. It does not wake a sleeping or powered-off computer, so
the machine must remain powered and normally available.

Create the private environment file, configure it, and make one observed pass
before installing the schedule:

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\\TeamMemory"
notepad "$env:APPDATA\\TeamMemory\\hub.env"
teammem --env-file "$env:APPDATA\\TeamMemory\\hub.env" run-daily
teammem schedule install --time 18:20
teammem schedule status
```

Package installation alone creates no task. The default is 18:20 in the local
Windows timezone. The task's `StartWhenAvailable` setting catches a missed daily
trigger when the interactive user is next available; it does not make a logged
out user available and it cannot run while the machine is powered off.

The generated XML directly starts the installed `teammem.exe` with the
environment-file path and `run-daily`. It is deliberately no password, no S4U,
and no shell wrapper: Task Scheduler stores no provider token, Git credential,
Windows credential, or environment-file contents. Password, service-account,
and logged-out operation are unsupported.

For scheduling evidence, enable and inspect Task Scheduler History, then check
the task's **Last Run Time** and **Last Run Result**. Use `teammem schedule
status` to validate the installed definition. Task Scheduler does not capture
direct-action stdout/stderr without a wrapper, so use the manual command below
for detailed application output:

```powershell
teammem --env-file "$env:APPDATA\\TeamMemory\\hub.env" run-daily
```

Remove the current-user task before uninstalling:

```powershell
teammem schedule remove
```

For an upgrade, remove the old task, upgrade the package, validate connectors,
run one manual daily pass, and install the task again so it records the current
executable and configuration paths:

```powershell
teammem schedule remove
python -m pip install --upgrade teammem
teammem connectors check
teammem --env-file "$env:APPDATA\\TeamMemory\\hub.env" run-daily
teammem schedule install --time 18:20
```

If status reports a conflict, do not delete a same-named task manually: inspect
its ownership and definition in Task Scheduler first. A foreign or altered task
is intentionally not treated as a TeamMem schedule. If the environment file is
rejected, keep it under the current user's `%APPDATA%\\TeamMemory` directory and
remove shared read access rather than loosening the security check.

### Local-filesystem requirement

Schedule lifecycle operations serialize changes with a directory lock. That
definition and scheduler-command behavior is hermetically tested for both
backends. The recorded separate-process live lock probe ran on macOS only. Linux
guidance follows the documented local semantics of
[`flock(2)`](https://man7.org/linux/man-pages/man2/flock.2.html) and the official
systemd manuals linked above; it is not a claim of a live Linux lock probe.

The built-in scheduler uses the fixed home-relative definition paths shown above
and rejects symlink traversal. It has no directory override. NFS and SMB locking
behavior varies by server, client, and mount configuration. If the operator's
home uses NFS, SMB, or another filesystem with uncertain `flock` semantics,
either run `teammem` under a local home or use an externally managed scheduler
that invokes:

```bash
teammem --env-file /absolute/path/to/hub.env run-daily
```

Do not assume the built-in schedule is safe on an unverified network home.

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
transport remains an explicit operator-owned step. The built-in schedule invokes
only `teammem run-daily`; it does not run `git pull`, `git archive`, or any
private MemberKit transport command.

If `TEAMMEM_INBOX`, `TEAMMEM_ARCHIVE`, and `TEAMMEM_QUARANTINE` are configured
for a scheduled run, the operator must refresh a disposable staging export
separately before that run. The transport checkout itself must remain clean and
must never be configured as the import path. If no separate refresh workflow is
in place, omit all three inbox paths from the scheduled hub configuration and
perform bundle staging/import during an observed manual run instead.

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
macOS it offers to install a LaunchAgent at 17:30 in the Mac's local timezone.
Press Enter to accept, enter another `HH:MM`, or enter `no` to decline. The
optional `MEMBERKIT_TIMEZONE` controls member-calendar attribution after the
command starts; it does not move the launchd trigger. Package installation alone
never installs the schedule.

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
