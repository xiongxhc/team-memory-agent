# Changelog

Notable changes to Team Memory Agent and MemberKit are documented here.

## Unreleased

### TeamMem

#### Added

- Full runs reconcile the current and previous report weeks. Current-week Work
  Journals are provisional Monday through Thursday, become Friday checkpoints,
  and can incorporate late evidence over the weekend.
- `teammem run-daily --capture-only` records enabled source and reviewed-bundle
  evidence, reclaims mappings, and, when configured, snapshots the ledger
  without synthesis, documentation sync, rendering, or publication.
- GitLab collection paginates every repository branch and collects its commits
  inside the configured lookback while excluding tag-only commits. For merge
  requests merged inside the lookback, default-on backfill collects all unseen
  MR commits, including in-window commits from deleted or squashed source
  branches and older commits. Operators may set `collect_mr_commits: false` to
  disable only that supplement.

#### Changed

- Journal synthesis defaults to two concurrent LLM calls (configurable from one
  through eight) without ranking, capping, truncating, batching, retrying, or
  compacting evidence.
- Full and capture runs share one ledger lock. Capture mode fails fast when it
  is held; a full run waits for at most 30 minutes.
- Documentation sync accepts either `Architecture.md`/`Summary.md` or lowercase
  source filenames, while continuing to write lowercase destination names for
  case-sensitive links.
- Public documentation and the public-boundary scan describe operator-configured
  deployments without private-deployment wording and prevent that wording from
  returning to canonical public documentation.

#### Fixed

- GitLab commit identities include the project ID, preserving the same author
  and SHA in different projects. Normal collection reconciles matching legacy
  bare-SHA rows instead of duplicating them on the next lookback run.

## 0.4.0 — 2026-08-04

### TeamMem

#### Added

- GitLab collection includes issue opening and closing facts and repository
  creation facts across the configured group hierarchy.
- Person vault pages use `Person/<Display Name>/README.md` indexes plus one
  `Week <label>.md` file per week.

#### Changed

- Person indexes show the latest week inline and link to full-ledger weekly
  history. Weekly raw activity detail is capped at 12 items with an overflow
  count.
- Generated person paths change from `Person/<Name>.md` to
  `Person/<Name>/README.md`; external links and bookmarks must be updated. No
  database migration is required.
- Optional connector, import, synthesis, report, docs-sync, and publishing
  failures remain visible but no longer make the aggregate daily run fail.
  Ledger, identity reclaim, rendering, and snapshot failures remain fatal.
- Bundle-v1 accepts a valid evidence timestamp on a neighboring calendar date;
  the bundle date remains the member-selected review day.

#### Fixed

- GitLab issue attribution, closure timestamps, repository creator lookup,
  legacy-row reconciliation, retries, and duplicate identity reclaim are
  deterministic and preserve idempotency.
- Claude CLI synthesis failures include compact, sanitized, bounded diagnostics
  from stderr and stdout.

> Reconciliation note: the `v0.4.0` tagged tree contains the TeamMem changes
> listed above even though the published release description characterized the
> hub package as parity-only.

### MemberKit

#### Added

- Local project-exclusion rules support exact project names, project prefixes,
  and project-scoped regular expressions, with list and preview commands before
  an unattended schedule relies on them.

#### Changed

- Exclusions filter newly generated or forced drafts, including `draft --all`,
  without rewriting existing drafts or changing the explicit review/push flow.

#### Fixed

- Bundle-v1 validation accepts valid evidence timestamps on neighboring
  calendar dates while preserving the selected review day. This adds no new
  collection source, scheduling behavior, review flow, or automatic push.

## 0.3.0 — 2026-07-30

### MemberKit

#### Added

- Native Windows Task Scheduler support for install, status, replacement,
  removal, missed-run catch-up, and overlap prevention.
- `memberkit setup` supports protected Windows configuration and explicit
  schedule opt-in.

#### Changed

- The same scheduling commands dispatch to launchd on macOS and Task Scheduler
  on Windows. Linux users invoke `memberkit scheduled-run` from their own
  scheduler.
- Scheduling remains opt-in. Scheduled runs prepare local drafts and reminders
  but never approve, push, or transmit member data.

#### Fixed

- Managed Windows tasks validate ownership and use transactional recovery,
  bounded logs, value-safe diagnostics, current-user `InteractiveToken`, least
  privilege, and native Windows CI verification.

## 0.2.0 — 2026-07-30

### TeamMem

#### Added

- Official GitHub, GitLab, Slack, Feishu, and Discord connectors use one
  normalized event interface.
- Hub daily scheduling is explicitly installed by the operator; installation
  never schedules implicitly.

### MemberKit

#### Changed

- Evidence-first drafts preserve eligible source observations and regenerate
  review journals from current events.
- Bundles are pushed only through an explicit member action; scheduled runs
  never push automatically.
- Manually reviewed highlights from unsupported sources remain a fallback for
  WhatsApp, Telegram, LINE, email, meetings, and other unsupported sources.

> The v0.2.0 release description announced native Windows MemberKit scheduling
> before that lifecycle was complete. Version 0.3.0 completed and verified it.

## 0.1.0 — 2026-07-27

The project is licensed under Apache-2.0.

### TeamMem

#### Added

- Local-first SQLite activity ledger and regenerated Markdown reports.

### MemberKit

#### Added

- Independently installable, member-reviewed MemberKit.
- Opt-in local scheduling with no automatic transmission.

### Protocol

#### Added

- Frozen `teammem-bundle/v1` import protocol.
