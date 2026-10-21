# Team Memory Agent

> Operators deploy by pinning a reviewed revision; downstream systems
> integrate by reading the rendered vault output, never the ledger or engine
> internals.

Team Memory Agent turns scattered team activity into a local, queryable event
ledger and regenerated Markdown reports. It combines central signals such as forge
commits and shared project-channel messages with member-reviewed local highlights.

The generated vault is a disposable projection of that ledger:

```text
Projects/
├── README.md
└── <project>/
    ├── README.md
    └── Week YYYY-MM-DD-DD.md
```

`Projects/README.md` is the current activity index. Each project README is a
compact latest-week snapshot and complete history index; its weekly files hold
project-filtered evidence grouped by contributor. Project history covers every
event-bearing ledger week. Weeks outside the rendered Work Journal window do
not link to a missing team report, and future-dated activity is explicitly
flagged for timestamp review. Generated paths are deterministic and portable
across supported operating systems.

The privacy boundary is simple: MemberKit prepares a JSON bundle locally, the
member reviews it, and nothing is transmitted until that member explicitly runs
`memberkit push`. Scheduled runs create drafts and reminders only.

## Components

| Component | Installed by | Purpose |
|---|---|---|
| `teammem` | Hub operator | Collect, import, query, and render |
| `teammem-memberkit` | Individual member | Draft, review, and push local evidence |
| `teammem-bundle/v1` | Both packages | Frozen member-bundle JSON protocol |
| `teammem-meeting/v1`, `teammem-meeting/v2` | Meeting producer and hub | Versioned meeting artifact protocols |

MemberKit is a standalone command-line package, not a skill. Members install only
`teammem-memberkit`; they do not clone this repository or install the hub.
It can locally exclude configured projects or summary patterns from newly
generated drafts; see the [MemberKit guide](docs/member-guide.md#project-exclusions).

## Member quick start

### 1. Get the two team-specific values

Ask the hub operator for:

- your roster slug, such as `alex`;
- the Git URL of the team-memory inbox, plus permission to push to it.

MemberKit v0.4 reads local observations from
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
[`teammem-bundle/v1` contract](https://github.com/xiongxhc/team-memory-agent/blob/main/schemas/teammem-bundle-v1.md)
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
[MemberKit guide](https://github.com/xiongxhc/team-memory-agent/blob/main/docs/member-guide.md)
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

The five-connector hub is version 0.4.0. The following source-checkout path keeps
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
file. `teammem.env.example` in the repository root lists every supported
variable with placeholder values.

The complete provider table, current official permission links, and all runtime
paths are in the
[deployment guide](https://github.com/xiongxhc/team-memory-agent/blob/main/docs/deployment.md).

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

With an available LLM backend, every full run synthesizes both the current and
previous report weeks. The current Work Journal is marked **Provisional** from
Monday through Thursday, becomes a **Friday checkpoint** on Friday, and can
reconcile later evidence over the weekend. Rechecking the previous week lets
late provider events or reviewed MemberKit bundles update a stale report.
Reports consolidate evidence into `Shipped`, `Needs attention`, and
`Coordination-heavy / low artifact`; they do not use raw event count as an impact
score. Without an LLM backend, journal and report synthesis are skipped while
deterministic rendering continues from ledger evidence and any cached summaries.

For an operator-managed intraday trigger, use the capture mode:

```bash
teammem run-daily --capture-only
```

It collects enabled sources, imports reviewed bundles, reclaims mappings, writes
an atomic ledger snapshot when `TEAMMEM_SNAPSHOTS` is configured, and stops. Journal/report synthesis,
documentation sync, rendering, and Git publication are skipped, so capture mode
cannot partially publish a vault. An enabled connector or import failure makes
capture mode return non-zero after preserving successfully captured evidence.
The full and capture modes share one ledger lock: capture fails fast if another
run owns it, while a full run waits for at most 30 minutes.

Vault writers share a separate publication lock beside the canonical vault path
(`vault.lock` for a vault named `vault`), including writers using different
ledgers. The vault's parent directory must be writable; a missing parent is
created on first use. Daily holds its ledger lock first, then holds the vault
lock across docs-sync, verified render, commit and push. Standalone docs-sync,
render and meeting publication use the same vault lock; meeting publication
retains it through remote verification and receipt writing, including refusal
restoration. Contention waits up to 30 minutes and returns a retryable nonzero
result if the lock remains unavailable. Dry-run and render verification do not
create publication locks. The adjacent lock file is never vault content and may
remain after a run; do not remove it while writers are running.

Journal synthesis defaults to two concurrent LLM calls. Set
`TEAMMEM_LLM_CONCURRENCY` to an integer from `1` through `8` (`1` restores serial
calls). The runtime optimization preserves the complete ordered text of every
person-day slice. It adds no ranking, event cap, truncation, cross-person
batching, retry loop, compaction, or model change.

Only after the observed run succeeds, explicitly install the daily job. The
default and example below are 18:20 in the operator machine's local timezone:

```bash
teammem schedule install --time 18:20
teammem schedule status
```

Without meeting configuration, the built-in schedule invokes only `teammem run-daily`; it does not pull or
export a private MemberKit inbox and it does not install extra capture triggers.
Any intraday `--capture-only` trigger is an explicit operator-managed scheduler
choice. On Windows it is a current-user, logged-in-only
Task Scheduler task: a screen lock is fine, but logout prevents runs. See the
[deployment guide](https://github.com/xiongxhc/team-memory-agent/blob/main/docs/deployment.md)
for macOS/Linux/Windows paths, logs, missed-run behavior, safe inbox staging,
upgrades, and removal.

### What the built-in connectors can see

| Connector | Collection boundary |
|---|---|
| GitHub | Commits and pull requests from explicitly mapped repositories |
| GitLab | Commits on every reachable branch inside `TEAMMEM_SINCE_DAYS`, merge requests, issue lifecycle observations, human comments on in-window merge requests and issues, and repository creations in the operator-configured group hierarchy, including subgroups but excluding projects merely shared into it; for merge requests merged inside the lookback, all unseen MR commits are also collected, including in-window commits from deleted or squashed source branches and older commits; mapped repositories get project attribution and other in-scope repositories remain visibly unmapped |
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

Feishu remains a first-class official connector. Slack is optional and
top-level-message-only; enabling it neither disables nor reconfigures any other
connector.

GitLab merged-MR commit backfill is enabled by default. Set
`collect_mr_commits: false` under `gitlab` in `connectors.yaml` to opt out of
all unseen MR commits; daily commits from every reachable branch are still
collected. Commit identities include the GitLab project ID, so the same author
and SHA in different projects remain distinct ledger facts.

GitLab comment collection turns each human, non-system note on an in-window
merge request or issue into a `comment` event attributed to its author, with
the summary capped at 120 characters; the full note body stays in the local
ledger only. GitLab group/project access-token bot accounts
(`group_N_bot_…`/`project_N_bot_…`) are always skipped — every token an
automated reviewer posts through gets its own bot identity, so a name list
cannot keep up. List additional bot accounts with human-registerable names
under `exclude_note_authors` under `gitlab` in `connectors.yaml`.

### Meetings

Meeting support is off until it is configured, and it is configured as one
complete set: `TEAMMEM_MEETING_INBOX`, `TEAMMEM_MEETING_ARCHIVE`,
`TEAMMEM_MEETING_QUARANTINE`, `TEAMMEM_MEETING_AUDIO_STORE`,
`TEAMMEM_MEETING_RECEIPTS`, `TEAMMEM_MEETING_ALERTS`, and
`TEAMMEM_VAULT_WEB_BASE`. Setting one makes all of them required; setting none
leaves a hub that collects, renders, and publishes exactly as before. Every
directory must be absolute, must not be nested inside another or inside the
vault, and is created at mode 0700 — an existing group- or world-accessible one
is refused, because bundles carry transcripts and the store carries encrypted
recordings.

```bash
teammem process-meetings --now 2026-09-02T16:00:00Z
teammem process-meetings --dry-run
```

One run imports every ready bundle from the meeting inbox, renders the vault,
verifies it against the ledger, pushes, confirms the configured remote holds the
commit, and only then writes that occurrence's receipt. It asks for no approval
at any step. A receipt therefore means the note is published: rendered locally
and committed locally are not receipts, and a failed publication writes no receipt
for that occurrence.

Each published revision gets its own receipt at
`<receipts>/<occurrence-segment>/<revision>.json`, which a newer revision never
replaces, and the same bytes at `<receipts>/<occurrence-segment>.json`, which
always names the latest published revision. The revision file is written first,
so the latest file never names a revision whose own receipt is missing.

One real run at a time processes meetings: it holds a lock beside the ledger
(`ledger.db.meetings.lock` for `ledger.db`) from import through receipts. A run
that finds the lock held, such as a manual run during a timer run, touches
nothing and exits `1` with a `lock` refusal; the next run picks the work up.

Occurrences are processed independently. A malformed bundle is quarantined with
its reason and the next one still publishes; a refused publication leaves that
occurrence without a receipt and retryable on the next run, without holding up
any other meeting. The command exits `0` when everything published, `1` when a
bundle was quarantined, a publication was refused, or accepted-source cleanup
remains pending, and `2` when the configuration itself is wrong. `--dry-run`
reports what a real run would import
and publish and writes nothing at all: no ledger row, no audio file, no vault
file, no commit, no push, no receipt.

An accepted source whose metadata archival or ciphertext destruction fails remains a
durable cleanup obligation in the same transaction as its revision. Replays can
register additional physical source copies. Every real run retries these
obligations, including a partial bundle without `READY` and sources beneath a
previously configured inbox root. Publication continues while cleanup failures
return `1` with `source_cleanup_pending` and a separate
`*.source-cleanup.json` alert in `TEAMMEM_MEETING_ALERTS`. These alerts contain
occurrence/revision/deadline information, never transcript text, titles, source
paths, digests, or raw exceptions. Resolving cleanup removes only its cleanup alert.

`TEAMMEM_VAULT_REMOTE` and `TEAMMEM_VAULT_REF` (default `origin` and
`refs/heads/main`) declare where receipts point. The run refuses before touching
anything if the vault publishes somewhere else, so a receipt cannot name a
commit on a remote nobody reads.

`teammem render` and the daily run publish the vault too, so they check meeting
content before they commit it. A meeting that fails a check — a note that
contradicts the ledger, a name that collides with a page the vault already owns,
ledger content that reached a note — is withheld from the vault and named on
stderr, while every other page renders, commits and pushes as it always did. A
withheld meeting stays in the ledger and is filed again as soon as a corrected
revision lands. If a check fails on vault content no meeting owns, the run
commits and pushes nothing and exits `1`.

A note that would itself carry its own meeting's stored transcript text (for
example a decision quoting a transcript line verbatim) can never pass for that
revision, so
`process-meetings` refuses it once, exits `1`, and writes a terminal
`teammem-meeting-refusal/v1` record at
`<receipts>/<occurrence-segment>/<revision>.refused.json` with a reason code and
invariant names only. Later runs skip that revision and print `held` on stderr
without failing; a higher revision publishes normally. A note that only matches
another meeting's transcript is withheld and retried every run with
`cross_meeting_transcript_text`, never held for good. While a revision is held,
`TEAMMEM_MEETING_ALERTS` carries `<occurrence-segment>.publication-refused.json`
(`teammem-meeting-publication-refused-alert/v1`: occurrence, revision, reason code,
action), restored on every run and removed once a newer revision publishes. See
`docs/schemas/teammem-meeting-v1.md`.

#### Audio retention

TeamMem computes one expiry for a recording, `ended_at` plus fourteen days, and
a bundle asking for longer is provenance only. The one-shot command destroys
every recording already due at the supplied instant:

```bash
teammem expire-meeting-audio --now 2026-09-16T14:52:00Z
teammem expire-meeting-audio --dry-run
teammem expire-meeting-audio --watch
```

On Linux and macOS, `--watch` runs retention immediately, then waits for the
earliest stored expiry or current incomplete transcript's warning boundary.
Durable imports wake it before source archival or publication, including direct
store imports and unchanged replays enrolling another source copy. It uses a
private mode-0700 directory beside the canonical ledger (`ledger.db.retention`
for `ledger.db`), a retention-only directory lock and a mode-0600 FIFO containing
only wake bytes. The FIFO is removed on orderly exit; the directory remains as
the stable lock identity. Do not remove that directory while a worker runs.
Only one worker per ledger runs, and SIGTERM/SIGINT release it cleanly.
`--watch` cannot combine with `--now` or `--dry-run`; Windows keeps one-shot mode.

Failed work retries after five seconds while earlier healthy deadlines still
take precedence. A sixty-second recovery check reconciles lost import wakes and
wall-clock changes. Neither interval changes the original expiry. Startup and
overdue passes report late/catch-up work and retain actual deletion timestamps.
OS suspension, power loss, a blocked/failing filesystem, scheduling latency and
a process crash between commit and notification can cause late deletion; the
worker catches up when execution resumes. This is not a hard-real-time physical
deletion guarantee, and local clock-injected tests do not establish live acceptance.

What goes is the ciphertext and nothing else. The meeting, its transcript, its
synthesis, its revision history, and the recording's hash, format, duration and
expiry all stay queryable, with a deletion timestamp beside them. TeamMem never
holds the recording key and never decrypts, so deleted bytes cannot be
regenerated and the deadline is exact in both directions. Every revision expires
on its own schedule, and two meetings with identical recordings are stored and
deleted separately. TeamMem writes retention deadlines and new deletion audit
timestamps as fixed-width UTC microseconds (for example,
`2026-09-16T14:52:00.000000Z`). A deletion timestamp records when deletion
actually happened; migration preserves that evidence instead of rewriting it to
the deadline.

The promise covers every copy TeamMem holds, including a rejected bundle moved
into `TEAMMEM_MEETING_QUARANTINE` with its ciphertext. Each retention pass zeroes
and removes a quarantined `audio.enc` once fourteen days have passed since the
earlier of the bundle's declared `ended_at` (when readable) and the file's own
modification time; nothing in a rejected bundle can extend that. The reason file
and metadata stay. The watcher reaches these copies on its next pass (at most its
sixty-second recovery interval), since they have no ledger row to wake it.

Expiry destroys the primary ciphertext even if source cleanup or metadata
archiving fails. It records deletion only after every recorded duplicate is also
gone; a failure remains visible and retryable without extending retention.
Deferred cleanup checks the captured physical root, directory and audio identities
and the recording's hash/size. Replaced files and symlinks are preserved and
reported. On POSIX, partial-source cleanup destroys bytes through the verified
file descriptor and may retain a zero-byte `audio.enc` placeholder; it never
unlinks a concurrent replacement by pathname. Archive completion means validated
metadata was archived and the exact owned ciphertext was destroyed through a
descriptor held across archival I/O. Source directories and metadata remain;
Windows removes the owned audio through its exact file handle, while POSIX
retains an empty audio placeholder. Completed source identity records suppress
reprocessing only while that recorded source is still ciphertext-free. A newly
delivered valid bundle at the same path or a higher revision is enrolled normally.

The additive migration creates cleanup tracking without scanning source files.
Legacy accepted bundles are enrolled when a real inbox run validates them again;
old source copies under former roots that were never recorded cannot be located
automatically. Operators must account for those historical locations separately.
Both meeting commands run dry-run migrations only in an ephemeral ledger snapshot
and leave source bytes, durable schema and alerts unchanged.

When a meeting's current transcript is still incomplete and its recording is
within `TEAMMEM_MEETING_WARN_DAYS` (default 3, `0` to warn about nothing) of
expiry, the run writes one alert file per occurrence into
`TEAMMEM_MEETING_ALERTS` naming the occurrence and the deadline, and exits `1`.
The alert carries no transcript text. It is removed again once the warning has
lapsed — the transcript completed, or the recording is gone — so every file in
the spool is a warning that still applies. Retention is never extended for a warned
meeting: backfill arrives as a higher revision from the producer, and a warning
nobody acts on still ends in deletion on time. The command exits `0` when there
was nothing to say, `1` when it warned or a deletion is left to retry, and `2`
when the configuration is wrong. These are one-shot exit codes; the worker keeps
retrying and reports failures while running. `--dry-run` reports what a real run would do
and writes nothing: no file removed, no row changed, no alert.

Publication URL or receipt/archive/alert configuration faults are reported with
a nonzero exit but do not prevent accessible due audio from being destroyed.
An unsafe alert spool receives no writes. The recorded final remains authoritative
after an audio-root setting change: its inode is destroyed, including same-inode
hard links, without inspecting old sibling names outside the current root.

Publishing and committing renders use a coherent ledger view and compare every
verified page's bytes with the actual Git tree. Ignored required pages or changed
bytes from clean filters are refused. Configured Git hooks still run; a hook-mutated
commit is refused and removed from the current ref only when ownership is proven
with a compare-and-swap. An interrupted commit or failed rollback leaves
`teammem-publication-pending` in the vault's private Git directory, blocking later
automatic commits and pushes. Recovery requires inspecting and repairing the
reachable history before explicitly removing that guard; a retry does not clear it.

The complete-short-transcript check matches ordered whole turns without changing
their case or punctuation. Its supported presentation grammar allows whitespace,
Markdown bullet/number/quote prefixes, inline or standalone colon-terminated
speaker labels (including Unicode names and fullwidth colons), and bold, italic,
inline-code or strike markers around turns,
including nested wrappers. Identifiers and near-word matches remain distinct
from Markdown emphasis. Intervening ordinary prose is not treated as formatting;
this is a bounded presentation check, not arbitrary encoding or paraphrase detection.

Configuring meetings adds `teammem-meetings` and `teammem-meeting-retention`
beside the daily job the next time the schedule is installed — publication every
minute, leaving processing time within the initial ten-minute URL target.
That polling interval is not proof of end-to-end delivery latency. Unix retention
runs `expire-meeting-audio --watch`. The systemd service uses `Type=simple` and
`Restart=always` with five-second backoff. Its existing timer remains for
one-second activation bootstrap and daily recovery one hour after the daily run,
with explicit `AccuracySec=1us`; the timer does not schedule individual expiries
or restart an already-active service. Installation explicitly restarts the worker
after loading definitions, including upgrades with an already-active timer.
Launchd retains the retention label with `RunAtLoad`, `KeepAlive` and a five-second
restart throttle. Disable/remove stops the worker before unlinking definitions;
failed installation restores previous definitions and manager states.
Removing the meeting settings and reinstalling takes both away again. The daily job's own definition
is byte-for-byte what it always was, and `teammem run-daily` neither reads nor
requires any meeting setting. This repository holds the generator; the
operator's deployment overlay holds the exact deployed copy of each generated
unit.

`teammem schedule status` reports only the existing daily job. Inspect retention
separately with `systemctl --user status teammem-meeting-retention.service` or
`launchctl print gui/$(id -u)/org.teammem.meeting-retention`. Generated definitions
and local tests are not evidence that any live host has installed or accepted
the worker; follow [the human checklist](docs/HUMAN-CHECKLIST.md).

### Reviewed bundle inbox

The operator creates a private inbox Git repository, grants each member push
access, and provides each member an inbox URL and roster slug. Import from a
disposable `git archive` export, never directly from the transport checkout:
accepted and quarantined files are consumed from the configured import directory.
See the [deployment guide](https://github.com/xiongxhc/team-memory-agent/blob/main/docs/deployment.md)
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
[architecture](https://github.com/xiongxhc/team-memory-agent/blob/main/docs/architecture.md),
[privacy](https://github.com/xiongxhc/team-memory-agent/blob/main/docs/privacy.md),
[deployment](https://github.com/xiongxhc/team-memory-agent/blob/main/docs/deployment.md),
[changelog](https://github.com/xiongxhc/team-memory-agent/blob/main/CHANGELOG.md),
the
[MemberKit guide](https://github.com/xiongxhc/team-memory-agent/blob/main/docs/member-guide.md),
and the
[`teammem-bundle/v1` contract](https://github.com/xiongxhc/team-memory-agent/blob/main/schemas/teammem-bundle-v1.md).

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
