# Deployment

Start manually with fixture configuration and a temporary ledger. Do not introduce
a scheduler until the manual flow has been reviewed.

## Hub

The hub operator deploys `teammem`; individual members do not.

### Published package

Install the public command:

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

Edit both downloaded files before collecting or importing data. Upgrade or
uninstall the command with:

```bash
pipx upgrade teammem
pipx uninstall teammem
```

Uninstalling the command does not delete operator-owned configuration, ledgers,
archives, quarantine records, inbox checkouts, or generated views.

### Source checkout

Contributors developing adapters or running from a pinned checkout can instead use:

```bash
git clone https://github.com/xiongxhc/team-memory-agent.git
cd team-memory-agent
python3 -m venv .venv
.venv/bin/pip install -e .
cp config/roster.example.yaml config/roster.yaml
cp config/projects.example.yaml config/projects.yaml
```

Edit both copied files, then keep runtime paths and secrets outside source control.
The main path settings are:

```bash
export TEAMMEM_DB=/path/to/runtime/ledger.db
export TEAMMEM_CONFIG_DIR=/path/to/runtime/config
export TEAMMEM_VAULT=/path/to/rendered/team-vault
```

Optional GitLab and Feishu collectors use additional `TEAMMEM_*` values. Never
commit tokens, certificates, databases, inboxes, archives, quarantine files, or
generated views.

The operator must also:

1. Create a private Git inbox repository.
2. Grant each participating member permission to push.
3. Add each member's canonical slug to `roster.yaml`.
4. Give each member their slug and the inbox Git URL.
5. Maintain a clean checkout of the inbox transport repository.

Do not pass the Git working tree itself to `import-bundles`: accepted and rejected
files are consumed from the import directory. Import from a disposable export so
the checkout stays clean and can pull later revisions of the same member and date:

```bash
git -C /path/to/inbox-checkout pull --ff-only
STAGING_INBOX="$(mktemp -d)"
git -C /path/to/inbox-checkout archive HEAD | tar -x -C "$STAGING_INBOX"
```

Then validate or import from `$STAGING_INBOX`:

```bash
TEAMMEM_DB=/path/to/runtime/ledger.db \
TEAMMEM_CONFIG_DIR=/path/to/runtime/config \
  teammem import-bundles \
    --inbox "$STAGING_INBOX" \
    --archive /path/to/archive \
    --quarantine /path/to/quarantine \
    --dry-run
```

Remove `--dry-run` only after reviewing the result. Each new export can contain
previously accepted bundles; event and archive idempotency makes those retries safe
and inserts zero duplicate events. Delete the disposable staging directory after
the import. Never delete imported files from the Git checkout.

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

Members install only the independently distributed client:

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

Manage the schedule with:

```bash
memberkit schedule status
memberkit schedule install --time 17:30
memberkit schedule remove
```

Non-macOS users should run setup with `--no-schedule`, then configure their native
scheduler to invoke `memberkit scheduled-run`.

The member reviews the generated JSON under `~/.memberkit/out/`, removes private
events, and explicitly chooses one action:

```bash
memberkit review --date YYYY-MM-DD
memberkit push --date YYYY-MM-DD
memberkit dismiss --date YYYY-MM-DD
```

Transmission is always the separate `push` action. Scheduled code never imports
the push module, invokes Git, or transmits a bundle. See the
[MemberKit guide](member-guide.md) for the complete member workflow, local file
inventory, upgrades, uninstall order, and troubleshooting.
