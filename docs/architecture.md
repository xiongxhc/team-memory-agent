# Architecture

```text
operator-controlled Mac mini / Linux server / VPS

GitHub ─┐
GitLab ─┤
Slack  ─┼─> built-in connector registry ─┐
Feishu ─┤                               │
Discord ┘                               ├─> SQLite event ledger ─> Markdown views
                                         │
MemberKit -> reviewed v1 bundle -> inbox importer
```

The hub and MemberKit solve different parts of the data boundary:

- `teammem` runs on an always-on, operator-controlled Mac mini, Linux server, or
  VPS. It polls
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
| GitLab | Projects in the `TEAMMEM_GITLAB_GROUP` hierarchy, including subgroups but excluding projects merely shared into it; `gitlab_repos` maps known projects and unknown in-scope projects remain visible without project attribution | `gitlab`: `commit`, `mr` |
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

Feishu remains a first-class official adapter. The existing private deployment
continues to use Feishu and is not reconfigured, migrated, or replaced by the
GitHub + Slack public quick start. Public Slack remains an optional,
top-level-message-only adapter.

## Ledger and importer

The ledger stores one attributed fact per row. Its
`UNIQUE(person, source, hash)` constraint makes connector replays and bundle
revisions safe. Rendered vault files are projections: operators regenerate them
rather than editing them as source data.

The importer validates a complete bundle before inserting anything. Accepted
input is archived by content hash through a synced temporary file and atomic
replacement, so interrupted archive writes are safely repairable and multiple
reviewed revisions for one date are preserved. Invalid input is quarantined with
machine-readable error metadata.

The inbox path is a disposable export of the private Git transport repository,
not its working checkout. `run-daily` consumes only the already-exported staging
directory; it does not pull Git or create the export.

## Daily run

`teammem run-daily` executes enabled connectors independently, then runs the
configured local stages:

1. collect each enabled provider;
2. import reviewed MemberKit bundles when inbox, archive, and quarantine paths
   are all configured;
3. reclaim newly mapped identities and projects;
4. create daily journals and the Friday weekly report when an LLM backend is
   available;
5. optionally synchronize project documents;
6. deterministically render the Markdown vault;
7. optionally commit and push that vault through the existing Git boundary;
8. create and retain configured SQLite snapshots.

A failed connector does not discard events already collected by another
connector and does not prevent independent local work. Required local-state
failures skip dependent stages. Synthesis failures remain visible and non-zero,
but deterministic rendering may continue from ledger evidence and cached
summaries. The command prints one result per stage and exits non-zero when a
configured connector or required stage fails.

Hub scheduling is a separate, explicit lifecycle around the one-shot command.
The portable `teammem.schedule` facade selects a macOS user LaunchAgent, Linux
systemd user service/timer, or Windows Task Scheduler backend. `teammem schedule
install --time 18:20` writes and enables the selected definition; `schedule
status` inspects it and `schedule remove` disables and removes it. Package
installation and every other CLI command leave scheduler state unchanged.

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
