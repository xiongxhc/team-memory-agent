# DeepSeek Harness Event-Log Semantics — Reference

Source: [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
(MIT, developer preview, open-sourced 2026-08-13). File paths below are relative
to that repo and pinned to the preview state — verify against upstream before
citing later. Status: reference record, no work item attached.

## Why this record exists

teammem is already event-sourced: the SQLite ledger holds one attributed fact
per row, the Markdown vault is a regenerable projection, and weekly reports
store their coverage state and source-input identity atomically with their
narrative. DeepSeek Harness (dsh) ships an agent-session log built on the same
philosophy, taken further and argued precisely in its design notes
(`.agents/notes/implemented/architecture/2026-06-11-event-sourced-sessions.md`,
`2026-07-05-reconstructable-requests.md`). This record captures where teammem's
design is independently confirmed, which of dsh's refinements are worth
borrowing if the ledger ever grows in those directions, and what dsh does NOT
solve so we don't assume it did.

## Where teammem already matches dsh

- **Log is source of truth; views are derived and disposable.** dsh derives the
  entire model-visible history from the event log (`deriveMessages()`), exactly
  as the vault renderer derives Markdown from ledger evidence. Their stated
  principle — "model-visible ⟺ durably referenced": anyone holding the log
  reconstructs every derived artifact byte-for-byte — is the same contract as
  teammem's deterministic render plus report provenance.
- **Storage is append-only, never rewritten.** dsh's persistence contract says
  "flushed events are never rewritten"; compaction shrinks only the derived
  view. teammem's ledger and content-hash bundle archive follow the same rule.
- **Replay-safe ingestion.** dsh validates `seq === index` contiguity on every
  seed; teammem's `UNIQUE(person, source, hash)` makes connector replays and
  bundle revisions idempotent. Same property, different key.

## Refinements worth borrowing (each cited to source)

1. **Watermark-anchored reconstruction, not wall-clock.** dsh has no
   "state as of time T" API on purpose — reconstruction is always anchored on
   the monotonic event seq, and time is only a filter used to map T → seq.
   Every derived projection carries its watermark
   (`ProjectionSnapshot { asOfSeq, values }`,
   `docs/subsystems/session-projection.md`). teammem reports already store an
   evidence cutoff; the refinement is to standardize a single monotonic ledger
   watermark (max rowid or an ingest counter) and stamp *every* derived
   artifact — journals, weekly reports, vault renders, snapshots — with it.
   That turns "why does this report disagree with the vault" from a debugging
   session into a watermark comparison.

2. **Append-only correction: supersede, don't upgrade in place.** dsh never
   edits a logged event. A correction is a new event carrying
   `surfaceOp: {op:'replace', start, end}` plus `sourceEventSeqs` citing every
   shadowed row (`packages/core/session/src/surface.ts`), and query results
   classify each event as `current | shadowed | log-only`. teammem's one
   in-place mutation is the legacy bare-SHA → project-scoped commit-identity
   upgrade during reconciliation. It is safe today; but if reconciliation ever
   grows more rewrite cases, the dsh pattern — append a superseding row citing
   the superseded one, and let projections resolve — preserves the audit trail
   that in-place upgrades quietly discard.

3. **Derivation-desync invariant check.** dsh does not merely trust that
   derived state matches the log: an interceptor independently re-folds the
   log and fails the request on divergence
   (`packages/core/agent-loop/src/invariant.ts`, "log-reconstruction desync").
   The teammem equivalent is cheap and valuable: an optional
   `verify-render` step that re-renders the vault from the ledger into a
   temporary tree and diffs it against the published one — mechanical
   detection of hand-edited vault files or a nondeterministic renderer.

4. **Search index as a disposable derived DB.** dsh's session search is a
   separate SQLite FTS5 database that is never the persistence DB, is rebuilt
   from the log, and never takes the crash-repairing write path
   (`packages/session-query/session-query-sqlite`). If vault/ledger full-text
   search ever becomes a feature, this is the shape: a derived index file
   beside the ledger, deletable at will, populated read-only.

5. **Forward-compat via `ignorable`, not versioned parsing.** Every dsh event
   may carry `ignorable: true`; a reader hitting an *unknown* event type
   refuses to reconstruct unless the event is marked ignorable
   (`packages/core/session/src/types.ts:404`). Bundle v1 is frozen, but a
   future bundle v2 event vocabulary should adopt this bit: old hubs skip
   unknown-but-ignorable events instead of quarantining the whole bundle, and
   still hard-refuse unknown load-bearing ones. Fail-open per event, fail-closed
   per contract.

6. **Repair by appending, cold-only.** dsh never truncates a crashed log; it
   appends deterministic synthetic closer events, and only on cold load —
   a live log is never repaired (`packages/core/session/src/repair.ts`).
   teammem's lock discipline mostly prevents the situation, but the principle
   generalizes: any future recovery for an interrupted collection should add
   rows recording the interruption, not delete partial evidence.

## What dsh does NOT solve (don't assume it did)

- **No retention or GC** — session logs grow forever; derivation cost growth is
  acknowledged with compaction of the *view* as the only mitigation. teammem's
  lookback windows and snapshot retention are ahead here.
- **No wall-clock time-travel API** — see item 1; T → seq mapping is the
  caller's job.
- **No multi-writer story** — their docs state that tolerating concurrent
  writers on one log needs a signal beyond the log, and none exists. teammem's
  single-lock, single-hub model is the same answer arrived at independently.
- **No log format migration yet** — `SESSION_FORMAT_VERSION = 0`; mismatch
  refuses outright. Not a model to copy for bundle versioning beyond the
  refuse-on-unknown default (item 5 is the better import).

## Compaction aside (for the synthesis pipeline, not the ledger)

dsh compaction is a three-event transaction (`compaction/start` as durable
lock → log-only `compaction/summary` → `compaction/end` released last), so a
crash mid-compaction leaves a detectable orphaned lock rather than a false
success (`packages/compaction/compaction/src/types.ts`). teammem's report
rows already store their provenance atomically; the transferable bit is the
lock-event-first ordering if any future multi-step synthesis needs to be
crash-evident inside the ledger itself.
