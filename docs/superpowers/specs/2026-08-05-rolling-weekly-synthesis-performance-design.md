# Rolling Weekly Synthesis and Journal Runtime Design

## Problem

The current Person pages answer what an individual did because they render cached
person-by-day LLM journals. The current-week Work Journal does not answer what the
team accomplished until Friday because `run-daily` skips weekly synthesis on every
other weekday. Until the Friday cache exists, the renderer exposes the latest five
raw work events per person as the main page. Those event fragments are evidence,
not a management report.

The same full daily pipeline also took about 75 minutes on 2026-08-04. Collection,
import, render, and snapshot used only a few minutes. Journal synthesis launched 154
serial Claude CLI calls and consumed about 69 call-minutes. Only about 41 person-day
slices had changed. The other 113 calls were cache fan-out: every daily prompt and
cache hash included the ledger's global project list, so adding three projects
invalidated every cached journal in the seven-day window.

These are one product problem: the service spends most of its time regenerating
person-level prose while still withholding the useful team-level synthesis until
Friday.

## User-facing outcome

- Person pages continue to answer: **What did this person accomplish?**
- Work Journal pages answer every evening: **What did the team accomplish, what
  needs attention, and where was coordination heavy without a concrete artifact?**
- Monday through Thursday reports are visibly provisional and state their evidence
  cutoff. Friday produces a visible checkpoint, while weekend and late evidence can
  still reconcile the same established seven-day report window.
- The previous week is reconciled during nightly synthesis so late GitLab events or
  MemberKit bundles can update a stale report.
- Intraday ticks acknowledge newly captured evidence without waiting for LLM work.
- Runtime optimizations do not remove, rank, truncate, or compact ledger evidence.

## Scope and ownership

The public `team-memory-agent` repository owns all reusable engine behavior:

- capture-only orchestration;
- rolling current- and previous-week reporting;
- person-day cache isolation and migration;
- bounded LLM concurrency;
- stage and synthesis timing telemetry;
- portable CLI/configuration and tests.

The private `local-agent-team` repository owns only its operational overlay:

- one capture LaunchAgent invokes the public capture-only mode at the intraday times;
- the existing evening LaunchAgent invokes the public full mode;
- the pinned OSS revision and deployment wrapper are updated only after the public
  change is reviewed, merged, and released.

No engine logic is duplicated in the private repository. MemberKit remains a source
of reviewed member bundles and is unchanged by this design.

## Design

### 1. Two explicit run modes

Keep `teammem run-daily` as the full, backwards-compatible command. Add:

```text
teammem run-daily --capture-only
```

Capture-only runs:

1. acquire the ledger run lock;
2. open the ledger;
3. run all enabled connectors;
4. import MemberKit bundles;
5. reclaim identities and channel projects;
6. write the normal atomic ledger snapshot;
7. stop.

It explicitly records journal, report, docs sync, render, and push as skipped with the
reason `capture-only`. It never calls an LLM and never publishes a partially
regenerated vault. Snapshot remains enabled because it is cheap and protects evidence
captured before an evening full-run failure.

Capture-only returns non-zero if any enabled connector or bundle import fails, while
still reclaiming and snapshotting successfully captured evidence. Full mode preserves
the existing provider-failure policy in this release.

Full mode retains the existing stages and additionally performs the rolling report
behavior below.

The public scheduler continues to install one full daily run by default. Operators
who add extra intraday triggers use `--capture-only`. A single launchd job cannot use
different arguments for different calendar entries, so the private deployment uses
two jobs: a capture job at its configured intraday times and the existing full job at
its configured evening time.

### 1.1 Cross-mode run lock

Every `run-daily` invocation acquires one cross-platform lock derived from the
canonical real ledger path (`realpath` plus platform case normalization) before
opening the ledger. The lock covers capture-only, full, scheduled, and manual runs.
Unix uses `fcntl`; Windows uses `msvcrt`; both keep the locked file descriptor open for
the process lifetime.

Capture-only fails fast on collision so intraday runs cannot queue. Full mode waits up
to 30 minutes, streaming lock-wait progress, so a capture that is finishing cannot
permanently skip the evening synthesis. A still-contended full run exits non-zero with
a content-free `another run is active` diagnostic and is recovered by the next full
scheduled run. This prevents the two private jobs or a manual command from overlapping
even though scheduler-level `IgnoreNew` protection applies only within one job.

### 2. Cache inputs are local to one person-day

The daily prompt keeps the complete ordered event slice for that person and local
date. `Known project names` changes from every project ever seen in the ledger to the
distinct non-null projects present in that same person-day slice.

The new cache hash contains only:

- `DAILY_PROMPT_VERSION`;
- `DAILY_HASH_SCHEMA_VERSION`;
- person identity and display name;
- local date;
- that person-day's relevant project names;
- the complete ordered person-day event text.

Adding an unrelated project can no longer invalidate other people or dates.

Daily and weekly cache evolution are independent. Replace the shared
`PROMPT_VERSION` with:

- `DAILY_PROMPT_VERSION` for daily model instructions;
- `DAILY_HASH_SCHEMA_VERSION` for the person-day input/hash shape; and
- `REPORT_PROMPT_VERSION` for weekly instructions and report semantics.

Changing weekly-report behavior therefore cannot invalidate daily journals or defeat
their migration path.

#### Compatibility migration

Changing the hash formula must not trigger one final full-regeneration storm. For
each cached person-day, the engine computes both:

- the new local-input hash; and
- the legacy hash using the old global-project prompt shape and an exact pinned
  `LEGACY_DAILY_PROMPT_VERSION` constant.

If the stored hash matches the legacy hash, the existing text is retained and its
hash is migrated to the new value without an LLM call. If neither hash matches, the
engine cannot prove that the legacy evidence is unchanged and safely regenerates the
slice. The historical global project set was not stored, so a universal no-call
migration is impossible. This migration is explicitly best-effort: it avoids calls
for caches that still match the current legacy prompt, while bounding safe fallback
regeneration to the explicit previous-Monday-through-current-day reconciliation
window. It is limited to the known prompt version and removed only in a later explicit
compatibility release.

### 3. Bounded LLM concurrency with serial database writes

`TEAMMEM_LLM_CONCURRENCY` controls parallel synthesis and defaults to `2`. Values
must be integers from 1 through 8. `1` preserves serial behavior for constrained
operators.

`run_journal` performs three phases:

1. **Prepare serially:** build prompts/hashes and classify cache hits, migrations,
   and genuine misses using the SQLite connection on the main thread.
2. **Generate concurrently:** submit only genuine misses to a bounded thread pool.
   Worker threads call the injected LLM only; they never touch SQLite.
3. **Persist serially:** write successful results on the main thread in stable
   `(person, day)` order.

This works for both independent Claude CLI subprocesses and the HTTP backend. All
futures are drained. Compatibility migrations are committed before LLM work;
successful generations are sorted and persisted on the main thread even when a
sibling request fails; worker exceptions are aggregated and redacted by person-day.
This matches the existing behavior where earlier serial calls remain cached if a
later call fails.

The first release adds no hidden retry loop: HTTP 429, Claude CLI failure, and the
existing 600-second per-call timeout fail visibly. Operators encountering backend
limits can set concurrency to `1`. Retry/backoff policy is separate because it changes
runtime and provider-load semantics.

No multi-person batching is introduced; it would broaden privacy, retry, and cache
failure boundaries.

### 4. Rolling current-week team report

Every successful full synthesis run generates or reuses the current week's team
report. The Friday-only gate is removed from full mode.

The report keeps the proven structure:

```markdown
## Shipped
## Needs attention
## Coordination-heavy / low artifact
```

The prompt remains grounded in all cached person-day narratives for the report week
plus deterministic flag facts. It must consolidate repeated commit/MR/chat evidence
into team outcomes grouped by project, name contributors, distinguish shipped work
from in-progress coordination, and never rank impact by event count.

The LLM user prompt explicitly contains the deterministic `report_state` and exact
maximum event timestamp cutoff. `REPORT_PROMPT_VERSION` covers these semantics. The
model therefore knows whether it is writing a provisional report or Friday checkpoint;
changing this weekly contract does not touch daily cache versions.

The weekly service stores a deterministic coverage line immediately above the LLM
text:

```markdown
> Provisional — event timestamps through 2026-08-04T18:22:36+04:00.
```

On Friday:

```markdown
> Friday checkpoint — event timestamps through 2026-08-07T18:30:00+04:00;
> later evidence reconciles on the next full run.
```

Saturday and Sunday keep the same checkpoint wording while advancing the evidence
cutoff if new activity arrives. This preserves the existing Monday-through-Sunday
aggregation used by `week_range`; the `Week YYYY-MM-DD-DD` filename remains a
workweek-anchor compatibility label rather than a claim that weekend evidence is
excluded.

The cutoff is derived by parsing timestamps, never by string `max()`. Offset-aware
timestamps are compared as chronological instants after UTC normalization. Frozen
bundle v1 also accepts naive timestamps, which cannot be assigned an invented timezone:
if a latest report day contains any naive timestamp, the report uses date precision and
states `some source timestamps omit offsets` rather than claiming an exact instant.
The summaries schema gains atomic weekly provenance metadata:

```text
evidence_cutoff, cutoff_precision, coverage_state, source_input_hash,
effective_flags_json
```

Coverage state, cutoff, and source identity participate in the weekly cache hash and
are stored in the same transaction as the cached report. The renderer uses only this
stored provenance, so a later journal failure or newly arrived unsynthesized event
cannot make an old report look newer than its evidence.

The exact `effective_flags` passed to synthesis is stored as canonical JSON and reused
by deterministic rendering; the narrative and Flags section therefore cannot diverge
after later ledger changes.

Weekly `source_input_hash` includes every included daily summary key, canonical daily
input hash, and text, plus the effective flags. A changed daily evidence hash therefore
invalidates the weekly report even if the regenerated daily prose happens to be
text-identical.

#### Existing-ledger schema migration

New databases create the nullable weekly provenance columns directly on `summaries`.
Existing databases are upgraded by an idempotent migration inside one SQLite
transaction: inspect `PRAGMA table_info(summaries)` and `ALTER TABLE ... ADD COLUMN`
only for missing columns. The fields remain null for daily summaries.

Existing weekly rows have no reconstructable exact cutoff and retain null provenance
until regenerated under `REPORT_PROMPT_VERSION`. The renderer may keep their useful
text but must prepend `Legacy report — exact event cutoff unknown`; it must never infer
a cutoff from the current ledger. An LLM failure before first regeneration therefore
leaves the legacy warning visible rather than overstating coverage.

Raw work events remain below as `Appendix — activity by person`; project counts and
source references remain deterministic evidence. The Work Journal never uses the raw
fallback as its primary current-week view when a working LLM backend is available.

### 5. Partial-week attention semantics

`Needs attention` is available on every rolling report, but partial weeks must not
produce false absence judgments. One `report_state` helper computes the coverage
state and one `effective_flags` value used by both weekly synthesis and deterministic
rendering:

- blockers, security findings, unresolved incidents, decisions requiring follow-up,
  and unmapped evidence can appear immediately;
- gap and concentration findings are withheld from the provisional narrative and
  deterministic Flags section until the Friday checkpoint;
- the provisional Flags section states that gap and concentration checks are
  deferred until the Friday checkpoint.

This preserves early warning value without declaring someone inactive on Monday.

### 6. Previous-week reconciliation

Each full run evaluates two report weeks in stable order:

1. previous week;
2. current week.

Full-mode daily person synthesis uses explicit local-date bounds from the previous
report week's Monday through the operator's current local date, inclusive. This is
independent of connector lookback and of the standalone `journal --since-days` option.
Existing cache hits make this scan cheap. A late event regenerates only its affected
person-day; the canonical daily evidence hash then invalidates only that weekly report.

The report service accepts the target week separately from the operator's current
date. That prevents previous-week reconciliation from being mislabeled provisional or
from acquiring a fabricated creation date.

The daily workflow keeps one backwards-compatible `report` step containing structured
subresults for `previous` and `current`. Both attempts run independently. The aggregate
step is `failed` if either subresult fails but remains non-fatal under the existing
pipeline policy; its redacted detail and telemetry identify each target week.

Journal failures are tracked by person-day and report week. A failed current-week
person-day blocks only current-week report regeneration; the unaffected previous week
still runs, and vice versa. An unchanged previous week causes zero LLM calls. This
closes the current defect where
Friday's report can remain stale after weekend capture, delayed bundles, or backfill.

The standalone `teammem report --week-of DATE` remains available for deliberate older
reconciliation.

### 7. Timing and cache telemetry

The CLI creates a content-free run ID and streams stage-start/stage-end progress through
an injected reporter callback; library callers default to a no-op reporter. A killed
or hung run therefore still leaves its mode, local start timestamp, last entered stage,
and completed journal count in the operator log. Durations use an injected monotonic
clock in tests and never enter generated vault files.

Journal progress and completion report:

- total person-day pairs;
- cache hits;
- compatibility migrations;
- genuine LLM calls;
- configured concurrency;
- completed/total genuine calls after each completion;
- prompt event-count and byte-size p50, p95, and max;
- worker queue wait and backend latency separately;
- LLM latency p50 and p95 when calls occurred;
- total journal elapsed time.

Percentiles use the nearest-rank method and are omitted for zero samples. Connector
stage detail continues to report fetched/inserted counts and elapsed time. Previous-
and current-week reports have separate elapsed values inside the aggregate report
result. Secrets, prompts, and message contents are never included in telemetry.

This makes the next slow run diagnosable directly from the daily log instead of by
reconstructing Claude session timestamps.

## Quality-preserving performance boundary

This change deliberately does **not**:

- cap events per person-day;
- select only high-scoring observations;
- truncate large event slices;
- summarize chunks before the daily journal;
- batch multiple people into one prompt;
- switch models;
- reduce weekly evidence to the five rendered appendix bullets.

Those changes could alter information coverage and require a separate quality design.
The first optimization release changes invalidation, scheduling, and execution
parallelism only.

## Failure behavior

- Capture-only connector failures remain visible but do not invoke local synthesis.
- A missing LLM backend leaves capture successful and records journal/report as skipped,
  preserving the existing deterministic fallback.
- A journal failure prevents regeneration only for the report week containing that
  failed person-day; the renderer may continue using that week's last valid cached
  report and stored cutoff while the unaffected report week still runs.
- A previous-week report failure does not suppress an independently valid current-week
  report attempt; both failures are reported separately.
- An active run lock makes capture overlap fail fast and full mode wait up to 30 minutes
  before failing; the lock is released on normal return, exception, and interruption.
- SQLite access remains main-thread-only during journal concurrency.
- Full-mode render, push, and snapshot fatality rules remain unchanged.

## Configuration and documentation

Document in README, deployment, and architecture guides:

- `TEAMMEM_LLM_CONCURRENCY=2` default and valid range;
- `run-daily --capture-only` semantics;
- the default public schedule remains one full daily run;
- multi-tick operators should use capture-only intraday and one evening full run;
- private launchd multi-tick operation requires separate capture/full jobs protected by
  the engine-wide run lock;
- rolling reports are provisional before the Friday checkpoint and reconcile the
  previous week;
- no source events are discarded by the optimization.

## Testing

### Deterministic and unit tests

- opening a copied pre-upgrade database adds nullable provenance columns exactly once
  inside a transaction;
- a legacy weekly row renders `exact event cutoff unknown` when regeneration fails;
- unrelated new projects do not invalidate existing person-day summaries;
- a legacy global-project hash migrates without invoking the LLM;
- a cache created with an older global project set safely falls back to regeneration;
- daily and report version changes invalidate only their own cache kind;
- a genuinely changed person-day invokes exactly one LLM call;
- the LLM receives the complete ordered event slice before and after optimization;
- concurrency never exceeds the configured bound;
- SQLite operations occur only on the main thread;
- result persistence order is deterministic;
- partial failures retain successful summaries and identify failed person-days;
- invalid concurrency configuration is rejected;
- zero-, one-, and two-call telemetry uses the defined percentile behavior;
- the default Claude CLI and HTTP callables are safe for concurrent invocation.

### Pipeline tests

- capture-only performs connectors/import/reclaim/snapshot and skips synthesis and
  publication without resolving an LLM backend;
- capture-only returns non-zero on connector/import failure while retaining the
  snapshot of successfully captured evidence;
- capture/full and manual/scheduled overlap is rejected by the shared run lock;
- path aliases resolve to the same lock; capture fails fast while full waits and then
  runs after a short capture releases it;
- a Monday full run generates the rolling team report;
- Monday-through-Thursday reports are provisional;
- Friday reports are timestamped checkpoints, not end-of-day completion claims;
- Friday evening and Sunday reconciliation preserve chronological offset-aware
  cutoffs, while naive latest timestamps produce an explicit date-precision warning;
- no-event Monday and no-dailies runs use an explicit fallback without inventing a
  report;
- local-offset and year-boundary dates select the correct previous/current report weeks;
- provisional attention suppresses gap/concentration but retains immediate risks and
  unmapped evidence;
- a late prior-week event regenerates one person-day and the prior weekly report;
- a changed daily input with text-identical regenerated prose still invalidates the
  weekly report;
- a journal failure after late capture preserves the cached report's older stored
  cutoff;
- a person-day failure blocks only its own report week;
- previous/current report failures remain independent and format stable subresults;
- unchanged prior/current weeks produce zero LLM calls;
- the raw evidence appendix and references remain present.

### Verification on copied production data

Before live deployment:

1. copy the production ledger and configuration into an isolated test location;
2. render the current and previous Work Journal before the change;
3. run the new full pipeline with concurrency `2`;
4. verify no unrelated-project cache fan-out and record stage/call timings;
5. verify the regenerated current report answers team outcomes and attention items;
6. inspect mapped, unmapped, message-only, multilingual, and no-project person-days to
   verify equal or better narrative coverage after removing the global project list;
7. compare ledger event counts, appendix counts, and references before/after;
8. run the full unit suite and public-boundary scan.

For the copied 2026-08-04-shaped workload, the acceptance targets are:

- capture-only completes within 5 minutes on the deployment machine when providers
  respond normally;
- after best-effort migration is accounted for, LLM calls equal genuine changed
  person-days, with no unrelated-project fan-out;
- concurrency `2` completes the observed 41-call journal workload within 15 minutes,
  excluding provider outage or rate limiting;
- report quality passes the source-grounded human review above.

These are deployment benchmark targets, not universal hardware/provider SLAs.

Live deployment remains a separate, reversible operation after code review, merge,
release, private pin update, and one manual copied-data acceptance run.

## Success criteria

1. A Monday evening Work Journal contains a provisional synthesized team report rather
   than beginning with raw person event fragments.
2. The report states team-level shipped outcomes, actionable attention items, and
   coordination-heavy work using the existing proven three-section format.
3. Friday is no longer the first time `Needs attention` exists; only immature
   absence/concentration judgments wait until Friday.
4. A late event in the previous week updates only its affected person-day and weekly
   report, even when regenerated prose is text-identical.
5. After safe migration, adding an unrelated project causes zero person-day LLM calls.
6. Journal concurrency defaults to two and never performs SQLite work in workers.
7. Capture-only performs no synthesis or vault publication, takes a snapshot, and
   reports incomplete capture with a non-zero exit.
8. Ledger events, daily prompt event text, rendered evidence counts, and references are
   not reduced by the optimization.
9. Scheduled capture, scheduled full, and manual runs cannot overlap on one ledger.
10. The copied 2026-08-04-shaped 41-call workload meets the 15-minute concurrency-two
    benchmark target when the provider responds normally; telemetry makes the result
    auditable.
11. All tests and the public repository boundary scan pass before integration.

## Out of scope

- lossy input compaction or event ranking;
- changing MemberKit curation or bundle format;
- additional connectors;
- changing the Person folder structure;
- automatic production deployment or scheduler mutation as part of the public merge;
- retroactively synthesizing every historical week without an explicit operator command.
