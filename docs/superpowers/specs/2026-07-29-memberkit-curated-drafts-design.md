# MemberKit Curated Drafts Design

**Status:** Approved
**Date:** 2026-07-29

## Problem

Members need a short, truthful daily review draft rather than an unbounded
projection of every local observation. The output must remain local,
deterministic, provider-neutral, and safe to review before the existing explicit
push boundary.

## Scope and invariants

- `teammem-bundle/v1` is frozen. Every selected item remains a
  `journal-highlight` with `refs: null`; no internal fact, session identifier,
  file path, metadata, or source payload is exposed.
- Curation reads the existing observations SQLite database in read-only mode. It
  makes no LLM or network call and never writes `~/.memberkit` during tests or
  verification.
- The normal path produces three to seven truthful highlights for each
  `(project, local day)` total; a sparse project/day produces fewer. Seven is a
  hard per-project/day cap.
- `memberkit draft --all` is the compatibility mode: it preserves the same
  one-row-per-observation selection, title/narrative summary, and
  `created_at_epoch` order. Its `ts` is normalized from that epoch into the
  member timezone because frozen v1 requires every event date to match the
  bundle date.
- Existing valid member-edited or manually-created drafts are preserved by both
  `draft` and `scheduled-run`; only `memberkit draft --force` may replace one.

## Data selection

The query selects only rows inside the local-calendar day window and must correct
the current epoch-unit defect: `created_at_epoch` is milliseconds, so bounds are
epoch seconds multiplied by 1000. It reads only fields required for a safe
highlight: project, memory session, title, subtitle, narrative, type, timestamp,
and epoch. SQLite connection mode remains `mode=ro`.

The same resolved member timezone used for those day bounds converts every
selected `created_at_epoch` to an ISO 8601 `ts` with an offset. The source
`created_at` string is not serialized because it may be UTC even when the
selected event belongs to the member's previous local date.

Normalize candidates before grouping and deduplication:

1. Collapse whitespace and trim title, subtitle, and narrative-derived text.
2. Prefer a meaningful title; if the title is generic, use meaningful subtitle;
   otherwise use the first useful narrative sentence, capped to the existing
   public summary limit.
3. Drop blank, generic-only, or duplicate normalized summaries.
4. Use `(project, memory_session_id)` as a consolidation bucket (with stable null
   sentinels only for local selection, never serialized into the bundle), keeping
   at most its best outcome.

“Generic” is a small explicit, case-insensitive set/pattern for mechanical
labels (for example `update`, `progress`, `work`, `notes`) rather than a model
judgment. Exact normalized deduplication is deterministic and keeps the earliest
chronological candidate.

Within each session bucket, rank outcome-bearing candidates above mechanics:
security, decision, blocker, release/shipment, and concrete outcome signals in
that order; ties retain chronological order. Consolidate each bucket to its best
distinct outcome, then rank the distinct session outcomes for the project/day and
select at most seven. Render selected events chronologically. The scheduler calls
this curated default.

## Draft and CLI behavior

`memberkit draft` uses curated selection; `memberkit draft --all` keeps the
legacy row selection, title/narrative summary, and epoch order. Both normalize
`ts` to member-local time and generate the same frozen v1 envelope and journal.
`memberkit draft --force` is required to overwrite any existing draft. Without
it, a valid draft is left byte-for-byte intact and an invalid/member-edited file
is never repaired or replaced automatically. `scheduled-run` likewise preserves
existing valid drafts and refuses malformed files while leaving them intact.

The schedule remains local draft preparation and reminder only. It uses curated
selection by default, never imports push behavior, and never communicates with a
provider or the hub.

## Verification boundary

Tests create fixture SQLite databases with millisecond epochs and temporary
MemberKit work directories. A read-only real-DB verification command may inspect
the configured database but must use an explicit temporary `MEMBERKIT_WORKDIR`;
it must not create or mutate `~/.memberkit`. Documentation distinguishes that
read-only check from actual drafting.
