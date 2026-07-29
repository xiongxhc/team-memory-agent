# MemberKit guide

MemberKit is the member-facing command-line package for Team Memory Agent. It is
not a skill, does not require the hub package, and does not require a repository
checkout. It reads configured local observations, prepares a JSON draft, and waits
for the member to decide what to share.

## What you need

- macOS for automatic schedule installation; the manual command works on other
  platforms;
- Python 3.11 or newer;
- [`pipx`](https://pipx.pypa.io/) to keep the command in an isolated environment;
- Git, with your name and email configured;
- a local
  [`claude-mem`](https://github.com/thedotmack/claude-mem) observations database;
- your roster slug and an inbox Git URL from the team-memory operator;
- permission to push to that inbox.

Check the local prerequisites:

```bash
python3 --version
pipx --version
git --version
git config user.name
git config user.email
test -f ~/.claude-mem/claude-mem.db && echo "claude-mem database found"
```

## Install

Install the published package:

```bash
pipx install teammem-memberkit
memberkit --help
```

Only contributors installing from a source checkout should use:

```bash
python3 -m venv .venv
.venv/bin/pip install -e packages/memberkit
```

## Configure once

Interactive setup asks for the values supplied by the operator:

```bash
memberkit setup
```

Example prompts:

```text
Member slug: alex
Inbox Git URL: git@forge.example:team/team-memory-inbox.git
Daily reminder time [17:30], or 'no' to decline:
```

Press Enter at the final prompt to accept 17:30 in the Mac's local timezone, enter
another `HH:MM`, or enter `no`. Package installation alone never creates a
schedule.

The equivalent non-interactive setup is:

```bash
memberkit setup \
  --member alex \
  --inbox-url git@forge.example:team/team-memory-inbox.git \
  --timezone Asia/Dubai \
  --time 17:30
```

To configure without a schedule:

```bash
memberkit setup \
  --member alex \
  --inbox-url git@forge.example:team/team-memory-inbox.git \
  --no-schedule
```

Advanced overrides are available through setup options or `MEMBERKIT_*`
environment variables:

| Setting | Default | Purpose |
|---|---|---|
| `MEMBERKIT_DB` | `~/.claude-mem/claude-mem.db` | Read-only local observations database |
| `MEMBERKIT_WORKDIR` | `~/.memberkit` | Drafts, state, inbox clone, and schedule logs |
| `MEMBERKIT_TIMEZONE` | detected local timezone | Observation dates, scheduled day selection, bounds, and event timestamps |

Setup stores the required values in `~/.config/teammem/memberkit.env` with mode
`0600`. Use an IANA name such as `Asia/Dubai` or `America/Los_Angeles`.
An invalid explicit name is rejected. A process `MEMBERKIT_TIMEZONE` overrides
the private file for that invocation.

## Daily workflow

The installed macOS schedule runs `memberkit scheduled-run`. It creates
local drafts for yesterday and today and shows a notification for every unfinished
date. It does not invoke Git or transmit anything. New drafts contain a
deterministic local curation of normally three to seven highlights per project,
or fewer when there are fewer distinct work-session outcomes. The per-project cap
is seven. Curation makes no LLM or network call.

Review a date:

```bash
memberkit review --date YYYY-MM-DD
```

The date defaults to today in the configured member timezone:

```bash
memberkit review
```

The JSON file is:

```text
~/.memberkit/out/bundle-<member>-<YYYY-MM-DD>.json
```

If a useful observation seems to be missing, inspect the raw compatibility mode:

```bash
memberkit draft --all --force --date YYYY-MM-DD
```

`--all` restores the original one-row-per-observation projection in timestamp
order. `--force` is a separate, explicit overwrite choice. Without `--force`,
`memberkit draft` does not replace any existing file, including valid,
member-edited, malformed, or partially written JSON. Raw mode is for local
inspection and deliberately preserves unfiltered legacy title/narrative
summaries. It may expose sensitive observation text. Curated path filtering does
not apply, so review and redact every raw event before pushing. Raw mode retains
the same rows, summaries, and epoch order, while `ts` is normalized from each
epoch into the member's local timezone to satisfy the bundle-date contract.

To redact an item:

1. Open that file in a text editor.
2. Delete the complete item from its `events` list.
3. Keep the file as valid JSON.
4. Run `memberkit review --date YYYY-MM-DD` again.

The `events` list is authoritative. MemberKit regenerates `journal_md` from that
list during push, so editing only `journal_md` does not redact an event.

Push only after review:

```bash
memberkit push --date YYYY-MM-DD
```

The first push clones the configured inbox under `~/.memberkit/inbox`. MemberKit
writes only the reviewed bundle, creates a Git commit, and pushes it. If Git asks
for credentials or reports a permission error, confirm your inbox access with the
operator.

Dismiss a date without sharing it:

```bash
memberkit dismiss --date YYYY-MM-DD
```

Removed and dismissed events remain excluded from later catch-up drafts.

## Timing and catch-up

On macOS, the default launchd trigger is 17:30 in the Mac's local timezone. It is
static: `MEMBERKIT_TIMEZONE` does not dynamically move the trigger. Once the
command runs, direct and scheduled drafts use the configured member timezone—or
the detected local timezone when none is configured—for scheduled yesterday/today
selection, observation bounds, and event timestamps.

When the Mac and member zones are the same, the familiar catch-up rule applies:
events after the 17:30 run stay attributable to their original member date when
that draft is explicitly regenerated, while unfinished dates remain in later
host-local reminders. When the zones differ, 17:30 host-local may be another hour
for the member, but calendar attribution still follows the member-local timestamp:

- events seen before the daily run appear in that day's draft;
- events created after the daily run but before midnight remain attributable to
  the earlier date when the member explicitly regenerates that existing draft;
- events created at or after midnight belong to the new calendar day;
- unfinished older dates remain in later reminders until pushed or dismissed.

The schedule never overwrites an existing draft, whether valid, manually edited,
malformed, or partially written. After reviewing an existing draft, use explicit
`memberkit draft --force --date YYYY-MM-DD` if you want to regenerate it with
later observations. A malformed draft remains pending so the member can repair
it. To discard one, delete that local draft file first, then run:

```bash
memberkit dismiss --date YYYY-MM-DD
```

## Schedule management

On macOS:

```bash
memberkit schedule status
memberkit schedule install --time 17:30
memberkit schedule remove
```

Changing the time replaces the existing MemberKit LaunchAgent. The value is
interpreted in the Mac's local timezone. MemberKit installs only one schedule:

```text
~/Library/LaunchAgents/org.teammem.memberkit-daily.plist
```

On another operating system, configure its scheduler to run:

```bash
memberkit scheduled-run
```

The command is portable, but v0.1 does not install non-macOS schedules
automatically.

## Local files

| Path | Contents |
|---|---|
| `~/.config/teammem/memberkit.env` | Private MemberKit configuration |
| `~/.memberkit/out/` | Local review drafts |
| `~/.memberkit/state.json` | Pending, approved, and excluded fingerprints |
| `~/.memberkit/inbox/` | Local clone used only by explicit push |
| `~/.memberkit/schedule.log` | Scheduled-run standard output |
| `~/.memberkit/schedule.err` | Scheduled-run errors |

The configured source database is opened read-only. Scheduled runs never push,
commit, or transmit.

To verify drafting against your configured real database without writing the
normal `~/.memberkit` directory, use a temporary work directory:

```bash
MEMBERKIT_WORKDIR="$(mktemp -d)" memberkit draft --date YYYY-MM-DD
```

This opens the configured observation database read-only and writes the draft and
review-state files only under the printed temporary directory. It is a local
inspection, not a push.

## Upgrade or remove

Upgrade:

```bash
pipx upgrade teammem-memberkit
```

Before uninstalling, remove the schedule while the command still exists:

```bash
memberkit schedule remove
pipx uninstall teammem-memberkit
```

Uninstalling the package does not delete the member-owned configuration, drafts,
state, or inbox clone. Remove those local files separately only after deciding they
are no longer needed.

## Troubleshooting

Check the installation and schedule:

```bash
memberkit --help
memberkit schedule status
```

Run one draft pass in the terminal to see errors directly:

```bash
memberkit scheduled-run
```

Common causes are a missing `~/.claude-mem/claude-mem.db`, invalid JSON in an
existing draft, missing Git identity, or missing inbox permission. On macOS, also
check `~/.memberkit/schedule.err`.
