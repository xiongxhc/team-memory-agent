# Architecture

```text
operator-controlled Mac mini / Linux server / VPS / Windows machine

GitHub ─┐
GitLab ─┤
Slack  ─┼─> built-in connector registry ─┐
Feishu ─┤                               │
Discord ┘                               ├─> SQLite event ledger ─> Markdown views
                                         │
MemberKit -> reviewed v1 bundle -> inbox importer
```

The hub and MemberKit solve different parts of the data boundary:

- `teammem` runs on an always-on, operator-controlled Mac mini, Linux server,
  VPS, or Windows machine. It polls
  explicitly enabled central providers, imports reviewed bundles, owns the
  ledger, and regenerates shared views.
- `teammem-memberkit` runs on each participating member's workstation. It drafts
  local highlights and transmits nothing until that member explicitly runs
  `memberkit push`.
- `teammem-bundle/v1` is their frozen file contract. The two Python
  distributions share no runtime imports.

Package installation enables no network connector and creates no hub schedule.
`teammem run-daily` is one run only on the operator machine.

## Connector boundary

The static `teammem.connectors` registry contains five official adapters:
GitHub, GitLab, Slack, Feishu, and Discord. Registry import and
`teammem connectors list` are local-only operations. Each adapter validates its
own configuration, accepts an injected clock and transport for hermetic tests,
and returns normalized `Event` values plus non-secret channel display metadata.

All connector enable flags live in `connectors.yaml` and default to `false`.
Provider credentials live in the process environment or the user-only
`~/.config/teammem/hub.env`, never in YAML. Process values take precedence over
the file.

Project resources and people use provider-namespaced mappings. The same text can
therefore identify a GitHub repository and a Slack channel without collision.
Unknown central identities stay visible as `_unmapped/...` rather than being
silently discarded.

| Provider | Query boundary | Event source and kinds |
|---|---|---|
| GitHub | Only `github_repos` explicitly mapped to projects | `github`: `commit`, `pr` |
| GitLab | Projects in the `TEAMMEM_GITLAB_GROUP` hierarchy, including subgroups but excluding projects merely shared into it; commits on every reachable branch inside `TEAMMEM_SINCE_DAYS`, plus default-on collection of all unseen MR commits from MRs merged inside that lookback, including in-window commits from deleted or squashed source branches and older commits; human non-system notes on in-window MRs and issues become `comment` events (summary capped at 120 chars, authors in `exclude_note_authors` skipped); `gitlab_repos` maps known projects and unknown in-scope projects remain visible without project attribution | `gitlab`: `commit`, `mr`, `issue`, `repo`, `comment` |
| Slack | Only `slack_channels` whose metadata identifies a public or private project channel containing the app | `slack-channel`: `message` |
| Feishu | Only `feishu_channels` whose metadata identifies a group chat containing the app | `feishu-channel`: `message` |
| Discord | Only `discord_channels` whose metadata includes a guild ID | `discord-channel`: `message` |

Chat is stricter than forge collection: an unlisted channel is never discovered
or collected merely because an app can see it. Slack direct and multi-person
direct messages are rejected, and only human top-level messages are emitted.
The adapter never calls `conversations.replies`. It uses 15-message history
pages and globally paces requests at least 60 seconds apart across pages and
channels. Slack's tighter limit applies to affected commercially distributed
apps outside Marketplace approval; Slack says internal customer-built apps are
not affected. The adapter uses the conservative behavior for portability and
treats `Retry-After` as authoritative. Feishu direct chats and Discord
DM/group-DM channels are rejected. Discord also skips bot and webhook messages;
empty history carries a warning because it can mean missing
`READ_MESSAGE_HISTORY` or `MESSAGE_CONTENT` access.

Feishu remains a first-class official adapter. GitHub and Slack form only the
public quick-start example; operators may enable any supported adapters
independently.

GitLab branch activity is collected by paginating repository branches, then
paginating each branch's commits with `ref_name` and the lookback boundary.
Commits reachable only from tags are excluded, and `(project_id, sha)` is
emitted once per collection. `collect_mr_commits` defaults to `true` and
supplements that result with all unseen MR commits from MRs merged inside the
same lookback, regardless of the original commit timestamp or whether its source
branch still exists. Setting it to `false` disables only that supplement.

## Ledger and importer

The ledger stores one attributed fact per row. Its
`UNIQUE(person, source, hash)` constraint makes connector replays and bundle
revisions safe. Rendered vault files are projections: operators regenerate them
rather than editing them as source data.

GitLab commit hashes include the provider project ID and commit SHA. This keeps
identical SHAs by the same author in different projects distinct. During normal
collection, reconciliation upgrades a matching legacy bare-SHA commit row to
the project-scoped identity instead of inserting a duplicate.

The importer validates a complete bundle before inserting anything. Accepted
input is archived by content hash through a synced temporary file and atomic
replacement, so interrupted archive writes are safely repairable and multiple
reviewed revisions for one date are preserved. Invalid input is quarantined with
machine-readable error metadata.

The inbox path is a disposable export of the private Git transport repository,
not its working checkout. `run-daily` consumes only the already-exported staging
directory; it does not pull Git or create the export.

## Daily run

`teammem run-daily` is the full mode. It executes enabled connectors
independently, then runs the configured local stages:

1. collect each enabled provider;
2. import reviewed MemberKit bundles when inbox, archive, and quarantine paths
   are all configured;
3. reclaim newly mapped identities and projects;
4. create daily journals and reconcile the previous and current weekly reports
   when an LLM backend is available;
5. optionally synchronize project documents;
6. deterministically render the Markdown vault;
7. optionally commit and push that vault through the existing Git boundary;
8. create and retain configured SQLite snapshots.

A failed connector does not discard events already collected by another
connector and does not prevent independent local work. Required local-state
failures skip dependent stages. Synthesis failures remain visible as failed
stage results, but deterministic rendering may continue from ledger evidence and
cached summaries. In full mode, connector, import, synthesis,
documentation-sync, and
push failures remain warning-level for the aggregate exit status; lock, ledger,
reclaim, render, and snapshot failures return non-zero.

`teammem run-daily --capture-only` follows the same ledger boundary through
collection, bundle import, identity/project reclaim, and, when
`TEAMMEM_SNAPSHOTS` is configured, an atomic SQLite snapshot, then stops.
Journal, report, documentation-sync, render, and
push stages are explicitly skipped with `capture-only`. Capture mode never calls
an LLM and never publishes a partially regenerated vault. An enabled connector
or import failure is fatal to the capture-mode exit status, while successfully
captured evidence is retained and, when configured, snapshotted.

Both modes acquire one lock adjacent to the canonical real ledger path. The file
descriptor remains held for the entire run. Capture mode fails fast with
`another run is active`; full mode streams lock-wait progress and waits at most
30 minutes before returning non-zero. Unix uses `fcntl`; Windows imports
`msvcrt` only on that platform.

Daily cache identity is local to one person-day: identity, local date, project
names in that slice, and the complete ordered event text. A compatibility check
migrates verifiable old cache rows without an LLM call; an unverifiable row is
regenerated safely. Only genuine misses enter a bounded worker pool, and worker
threads call the LLM only. SQLite preparation, migration, and persistence remain
serial on the main thread. `TEAMMEM_LLM_CONCURRENCY` defaults to `2` and accepts
`1..8`. This changes execution and cache isolation only: it adds no ranking,
caps, truncation, cross-person batching, retries, compaction, or model change.

Each successful full synthesis run reconciles the previous and current report
weeks. Monday through Thursday current-week reports are provisional; Friday is
a checkpoint; Saturday and Sunday may reconcile the same established seven-day
window. A report stores its exact coverage state, evidence cutoff precision,
source-input identity, and deterministic flags atomically with its narrative.
The renderer uses that stored provenance, so newly ingested but unsynthesized
evidence cannot make an older report appear current. The Work Journal presents
team outcomes under `Shipped`, `Needs attention`, and
`Coordination-heavy / low artifact`, while retaining deterministic evidence and
reference appendices below the synthesis.

Hub scheduling is a separate, explicit lifecycle around the one-shot command.
The portable `teammem.schedule` facade selects a macOS user LaunchAgent, Linux
systemd user service/timer, or Windows Task Scheduler backend. `teammem schedule
install --time 18:20` writes and enables the selected definition; `schedule
status` inspects it and `schedule remove` disables and removes it. Package
installation and every other CLI command leave scheduler state unchanged.

The public scheduler installs exactly one full daily run. Operators may add
separate intraday jobs that invoke `teammem run-daily --capture-only`, but the
package never creates those triggers implicitly.

The scheduled process is exactly the resolved executable plus `--env-file`, the
private environment-file path, and `run-daily`. Credential values are not copied
into scheduler definitions. On Windows, the generated Task Scheduler XML holds
only the executable path, environment-file path, and current-user SID; it uses a
direct executable action, `InteractiveToken`, and least privilege. The default
calendar time is 18:20 in the host's local timezone. The scheduler provides
process activation only: it does not pull the private MemberKit Git inbox or
produce a disposable export. When bundle paths are configured, the operator
refreshes that staging export separately before the run or omits the paths.

## Unsupported-source fallback

MemberKit's reviewed draft is the manual fallback for WhatsApp, Telegram, LINE,
email, meetings, and any unsupported source:

```text
edit an existing local v1 draft -> memberkit review -> memberkit push
```

The member adds a valid `journal-highlight` object to the existing draft's
authoritative `events` list. MemberKit does not authenticate to or scrape those
applications. Bundle v1 has no structured origin field, so the member can include
a useful origin label in the human-readable summary. The hub sees the accepted
event as `source=bundle:<member>`.

Member scheduling remains local and draft-only. The portable
`memberkit scheduled-run` command prepares yesterday/today drafts and keeps
reminding for older pending dates. Invalid member-edited drafts are never
regenerated or overwritten.
