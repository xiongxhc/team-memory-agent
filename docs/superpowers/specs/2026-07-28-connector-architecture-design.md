# Provider-Neutral Connector Architecture

**Status:** Approved
**Date:** 2026-07-28

## Problem

Public Team Memory Agent must reflect how real teams work without assuming that
Feishu, or any other provider, is universally present. Team activity is spread
across source-control forges, workplace chat, community chat, and sources that
cannot or should not be collected centrally.

The current hub has hard-coded GitLab and Feishu collector commands. It has no
portable daily orchestrator, no connector registry, and no direct way for a member
to add a reviewed highlight that came from an unsupported source.

The desired outcome is:

- no chat provider is enabled by default;
- GitHub, GitLab, Slack, Feishu, and Discord are official hub adapters;
- all adapters normalize provider data into the existing `Event` ledger shape;
- the hub runs every explicitly enabled adapter through one portable daily command;
- MemberKit lets a member manually add a reviewed highlight from WhatsApp,
  Telegram, LINE, email, a meeting, or any other unsupported source;
- collection remains visible, allowlisted, local-first, and reviewable.

## Product Boundary

There are two independent schedules:

1. The **MemberKit schedule** runs on a member's machine. It drafts local
   highlights and reminds the member to review them. It never reads a team chat
   provider, pushes a bundle, or transmits data.
2. The **hub schedule** is owned by the operator and runs on an operator-controlled
   machine such as an always-on Mac mini, Linux server, or VPS. It invokes a
   portable `teammem run-daily` command. Package installation alone never creates
   a schedule; the operator installs one explicitly after configuration and a
   successful manual run.

Feishu is an official adapter because it is already proven by the internal
deployment. It is not the default public chat provider. The public quick start
will lead with GitHub and Slack, while keeping every network connector disabled
until the operator explicitly configures it.

The private internal deployment remains Feishu-based. Adding Slack to the public
package does not replace, reconfigure, or migrate that deployment.

## Considered Approaches

### 1. Extend the current CLI conditionals

Add more `if connector == ...` branches to `teammem collect`.

This is the smallest initial change, but configuration, validation, scheduling,
and tests become provider-specific conditionals. It does not create a stable
boundary for future adapters.

### 2. Built-in connector registry

Define one connector protocol and register the five official adapters inside the
`teammem` package. Keep provider HTTP implementations isolated and dependency-light.

This is the selected approach. It creates a clear contract without requiring
users to install, trust, and debug third-party executable plugins in the first
release.

### 3. External package plugins

Discover connectors from Python package entry points.

This offers maximum extensibility but introduces dependency conflicts, arbitrary
code execution, version compatibility, and support obligations before the core
connector contract is proven. Entry-point discovery is deferred until an external
adapter has a concrete maintainer and use case.

## Architecture

```text
                         ┌─ GitHub
                         ├─ GitLab
operator config ───────> connector registry ─┬─ Slack
                         ├─ Feishu            │
                         └─ Discord           │
                                             v
MemberKit reviewed bundle ──> inbox importer ─> Event ledger
                                                   │
                                                   v
                                      journals, reports, Markdown vault
```

### Connector contract

The hub adds a small `teammem.connectors` package:

```text
teammem/connectors/
├── base.py
├── registry.py
├── github.py
├── gitlab.py
├── slack.py
├── feishu.py
└── discord.py
```

Each connector has:

- a stable lowercase name;
- configuration validation that reports missing values without displaying
  secrets;
- an explicit check for whether the connector is enabled;
- a collection operation that accepts an injected clock and transport;
- a result containing normalized `Event` values and non-secret display metadata.

Provider pagination and response parsing stay inside the provider module. Tests
inject fixture transports and never require credentials or network access.

The registry is static and built into the package. It is the single source for CLI
choices, configuration validation, and daily-run iteration. Importing the registry
must not authenticate or make a network request.

### Normalized events

The SQLite `Event` shape remains unchanged:

- `person`: canonical roster slug or `_unmapped/<provider-id>`;
- `project`: configured project slug or `null`;
- `ts`: provider timestamp normalized to ISO 8601;
- `source`: `github`, `gitlab`, `slack-channel`, `feishu-channel`,
  `discord-channel`, or `bundle:<member>`;
- `kind`: `commit`, `pr`, `mr`, `message`, `meeting`, or
  `journal-highlight`;
- `summary`: a concise attributed fact;
- `refs`: non-secret provider identifiers and a URL when available;
- `raw`: the provider payload needed for replay and diagnosis;
- `hash`: a stable provider identity used by the existing
  `UNIQUE(person, source, hash)` constraint.

Forge pull requests and merge requests retain their provider-native `pr` and `mr`
kinds. Rendering and synthesis treat both as change requests when grouping work.
This avoids calling a GitHub pull request a merge request while preserving existing
GitLab rows.

### Identity and project mapping

The existing roster and project files gain additive provider fields:

```yaml
members:
  alex:
    emails: [alex@example.com]
    github: [alex-gh]
    gitlab: [alex-gl]
    slack: [U0123]
    feishu: [ou_example]
    discord: ["1234567890"]

projects:
  project-alpha:
    github_repos: [team/project-alpha]
    gitlab_repos: [team/project-alpha]
    slack_channels: [C0123]
    feishu_channels: [oc_example]
    discord_channels: ["9876543210"]
```

Internally, project resources are keyed by `(provider-kind, value)`, not by one
shared string map. This prevents an identifier from one provider colliding with
the same text from another provider. Existing GitLab and Feishu configuration
continues to load without migration.

Unknown users and repositories remain visible as `_unmapped` identities or
unmapped project activity. Chat is stricter: only explicitly listed channel IDs
are queried, so an unlisted shared channel is never collected merely because the
bot can see it.

## Official Adapter Behavior

### GitHub

- Uses explicitly mapped repositories and an operator-owned token.
- Collects default-branch commits and pull requests updated during the lookback
  window.
- Maps authors by GitHub login and commit email when available.
- Emits `github` events with `commit` or `pr` kinds.

### GitLab

- Preserves the existing group/project, commit, and merge-request behavior.
- Moves behind the connector interface without changing stored source names or
  deduplication identities.
- Emits `gitlab` events with `commit` or `mr` kinds.

### Slack

- Reads only channel IDs explicitly mapped to projects.
- Uses scheduled Web API polling with a bot token; it does not require a user
  token or an always-running listener.
- Collects human-authored top-level channel messages within the lookback window.
- Skips bot-generated messages by default.
- Emits `slack-channel` events with the `message` kind.
- Does not collect thread replies in this release. The README and deployment guide
  state this limitation explicitly rather than implying complete Slack history.

### Feishu

- Preserves the existing tenant-app authentication and message normalization.
- Changes collection from every bot-visible chat to only channel IDs explicitly
  mapped to projects.
- Skips non-user senders.
- Emits `feishu-channel` events with the `message` kind.

### Discord

- Reads only channel IDs explicitly mapped to projects and visible to the
  configured bot.
- Collects human-authored messages within the lookback window.
- Skips bot and webhook messages by default.
- Emits `discord-channel` events with the `message` kind.

Provider setup documentation must be verified against each provider's current
official documentation during implementation. It must list the minimum permissions
and explain exactly what the integration can see.

## Configuration

Non-secret connector configuration lives in
`$TEAMMEM_CONFIG_DIR/connectors.yaml`. An operator choosing GitHub and Slack would
write:

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

All connectors are disabled in `connectors.example.yaml`. Documentation may recommend
GitHub and Slack, but installation alone never enables network collection.

Tokens, app secrets, and bot tokens remain outside YAML and repositories. The hub
loads them from the process environment or an operator-owned
`~/.config/teammem/hub.env` file with mode `0600`; process environment values take
precedence. This explicit file gives launchd and systemd the same configuration as
a manual run without embedding secrets in scheduler definitions. Each enabled
connector fails validation when its required credentials or non-secret scope is
missing. Disabled connectors do not require credentials and are not imported by
the daily runner beyond their lightweight registry declaration.

## Hub Commands and Daily Operation

The CLI supports:

```text
teammem connectors list
teammem connectors check
teammem collect <github|gitlab|slack|feishu|discord>
teammem collect --enabled
teammem run-daily
teammem schedule install [--time HH:MM]
teammem schedule status
teammem schedule remove
```

`teammem run-daily` executes:

1. validate enabled connector configuration;
2. run each enabled central connector independently;
3. import reviewed MemberKit bundles when inbox paths are configured;
4. reclaim newly mapped identities and projects;
5. generate daily journals;
6. generate the weekly report on the configured weekday;
7. optionally synchronize project documentation;
8. render the Markdown vault;
9. optionally commit and push the vault through the existing vault Git boundary;
10. create a local ledger snapshot when a snapshot directory is configured.

`teammem run-daily` is a run-once, idempotent operation. It never installs,
changes, or removes a schedule.

After a successful manual run, the operator may explicitly install a daily
schedule. The default is 18:20 in the machine's local timezone:

- macOS and always-on Mac mini deployments use a LaunchAgent;
- Linux server and VPS deployments use a persistent systemd user timer;
- other platforms use the documented `teammem run-daily` cron or scheduler entry.

The persistent scheduler launches the command once per day and catches up a missed
run when the host becomes available. The collection lookback and event
deduplication safely recover events that occurred while the host was offline.
`teammem setup` may offer to install the schedule interactively, but the operator
must affirm that choice. `pipx install teammem` never creates a background job.

Connector failures are isolated. A failed network connector does not discard
events already collected by another connector and does not prevent import or local
rendering. The command prints a per-step summary and exits non-zero if any enabled
connector or required local step failed. The existing lookback window and event
deduplication make the next run a safe retry.

LLM synthesis remains optional. When no configured LLM backend is available,
journal and weekly-report stages are marked skipped and deterministic rendering
continues successfully. When an available backend fails during synthesis, the
failure is reported and the command exits non-zero, but rendering may continue
from ledger evidence and previously cached summaries.

A required local-state failure, such as being unable to open the ledger, reclaim
identities, or write the rendered vault, stops downstream stages that depend on
that state. It is never downgraded to a warning. Optional documentation sync,
vault push, and snapshots run only when configured and report their own status.

## MemberKit Fallback

MemberKit adds a v1-compatible command:

```text
memberkit add --summary TEXT [--project SLUG] [--date YYYY-MM-DD] [--time HH:MM]
```

The command:

- defaults the date to today and combines it with the current local clock time
  unless `--time` is supplied;
- creates the day's local draft when it does not exist;
- appends one `journal-highlight` event to a valid existing draft;
- refuses to overwrite malformed or partially edited JSON;
- regenerates only the local `journal_md` preview;
- marks the date as pending review;
- never invokes Git or transmits data.

The scheduled refresh preserves manually added events through the existing
member-visible draft and pending-state behavior. The member can edit or delete the
highlight before running the separate `memberkit push` command.

Bundle v1 deliberately has no structured origin field and requires `refs: null`.
Therefore a manual WhatsApp, Telegram, LINE, email, or meeting highlight arrives
at the hub as `source: bundle:<member>`. A member may include an origin label in
the human-readable summary when it is useful. Structured origin provenance is
reserved for a separately designed `teammem-bundle/v2`; this work does not alter
the frozen v1 protocol.

MemberKit does not scrape, authenticate to, or automatically read any unsupported
application.

## Privacy and Security

- No connector is enabled by package installation.
- Chat collection is limited to explicitly allowlisted shared project channels.
- Direct messages and unlisted channels are outside the product boundary.
- The integration must be visibly present in every collected chat channel.
- Provider credentials stay outside repositories and rendered Markdown.
- The optional hub environment file is created with user-only permissions and is
  referenced by the scheduler rather than copied into a plist or systemd unit.
- Validation and logs never print tokens or app secrets.
- Raw provider payloads remain in the operator-controlled local ledger; rendered
  views expose only the existing normalized summaries and references.
- MemberKit scheduling remains draft-only and cannot import the push module.
- Manually added MemberKit highlights remain local until explicit review and push.

## Testing

Tests remain hermetic and cover:

- registry enumeration without imports that authenticate or access the network;
- all connectors disabled by default;
- additive loading of old GitLab/Feishu roster and project configuration;
- provider-namespaced identity and project mappings;
- pagination, lookback boundaries, event normalization, and stable hashes for all
  five adapters;
- configured-channel allowlisting and bot-message exclusion for all chat adapters;
- the Slack adapter ignoring thread-only replies and never requiring a user token;
- fixture GitHub pull requests and GitLab merge requests grouped consistently;
- duplicate collection inserting zero additional ledger rows;
- one connector failing while other collection, import, and local rendering
  continue;
- a non-zero daily-run status and actionable per-step error summary;
- `memberkit add` creating a valid v1 bundle without network or Git;
- scheduled refresh preserving a manual highlight;
- member deletion, dismissal, and push-state behavior for manual highlights;
- public-source scanning for credentials, private hosts, and company-specific
  defaults.

Live tests remain separately gated by explicit environment flags and credentials.
They are never required by the default test suite.

## Delivery Order

1. Introduce the connector contract, provider-namespaced mappings, configuration
   loader, registry, and portable daily runner.
2. Move the existing GitLab and Feishu implementations behind the contract without
   changing their event identities.
3. Add GitHub and Slack as the first new public adapters and update the quick start.
4. Add Discord through the same proven contract.
5. Add `memberkit add`, preserve manual events across scheduled refresh, and update
   member documentation.
6. Add explicit launchd and systemd schedule management after the portable daily
   command is proven.
7. Update the README and deployment guide in the same change as the implemented
   commands. The README quick start must distinguish package installation,
   configuration, one manual `run-daily` verification, and explicit schedule
   installation.
8. Run hermetic tests, package builds, public-source scans, and a clean-install
   smoke test for both distributions.

## Acceptance Criteria

The design is complete when:

1. A clean installation performs no network collection and installs no hub
   schedule.
2. `teammem connectors list` shows GitHub, GitLab, Slack, Feishu, and Discord with
   clear enabled and configuration status.
3. Each official adapter converts fixtures into the existing Event ledger and
   survives repeated collection without duplicates.
4. Chat adapters cannot collect an unconfigured channel.
5. One `teammem run-daily` invocation completes every configured local stage and
   reports isolated connector failures.
6. GitHub and Slack form the primary public quick-start path; Feishu remains an
   optional official adapter.
7. `memberkit add` creates a local, editable, v1-compatible highlight and never
   transmits it.
8. Manual highlights from unsupported sources render in the team vault only after
   the member explicitly pushes and the operator imports the bundle.
9. Existing GitLab/Feishu configuration and historical ledger rows remain valid.
10. The README tells operators that the hub must run on an operator-controlled
    Mac mini, Linux server, or VPS, and clearly separates:
    `pipx install teammem` → configuration → manual `teammem run-daily` verification
    → explicit `teammem schedule install`.
11. The deployment guide contains copyable macOS LaunchAgent and Linux systemd
    instructions, schedule status/removal commands, the 18:20 local-time default,
    missed-run behavior, log locations, upgrade order, and safe uninstall order.
12. Documentation explains permissions, privacy boundaries, failure handling,
    installation, and removal for both hub and member workflows without presenting
    unimplemented commands as available.
13. Slack documentation states that the initial scheduled bot-token adapter
    collects top-level allowlisted-channel messages, not thread replies, and that
    it does not alter the private Feishu deployment.

## Non-Goals

- Automatically scraping WhatsApp, Telegram, LINE, email, meeting recordings, or
  arbitrary local files.
- Reading direct messages or unlisted channels.
- Installing an operator scheduler without the operator's explicit command or
  affirmative setup choice.
- A long-running Slack Events API or Socket Mode listener.
- Slack user-token collection or Slack thread-reply capture.
- External third-party connector discovery in this release.
- Changing the frozen `teammem-bundle/v1` schema.
- Ranking people, scoring performance, or treating message volume as productivity.
