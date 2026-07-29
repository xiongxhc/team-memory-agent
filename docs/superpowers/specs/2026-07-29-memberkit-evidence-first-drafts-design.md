# MemberKit Evidence-First Drafts Design

**Status:** Approved
**Date:** 2026-07-29
**Supersedes:** [MemberKit Curated Drafts Design](2026-07-29-memberkit-curated-drafts-design.md)

## Problem

MemberKit currently ranks observations, keeps one candidate per session, and
caps each project at seven events. This discards evidence before the member or
TeamMem can judge it. Separately, deleting an event can leave the derived
`journal_md` stale until push.

MemberKit should preserve and transport member-approved evidence. TeamMem should
own semantic deduplication and concise synthesis after import.

## Chosen behavior

### Preserve every eligible v1 event

For the requested member-local day, direct and scheduled drafts emit one event
for every eligible observation row in chronological order. Eligibility and the
wire projection retain the pre-curation behavior:

- the row falls inside the existing half-open local-day millisecond epoch range;
- it has a title or non-blank narrative;
- `ts` is normalized from `created_at_epoch` into the resolved member timezone;
- `kind` is `journal-highlight`;
- `summary` is the title when present, otherwise the existing bounded narrative
  fallback;
- `project` is copied and `refs` remains `null`.

MemberKit performs no scoring, session consolidation, semantic deduplication, or
per-project cap. Identical summaries from separate observations remain separate
events. `memberkit draft` uses this behavior by default. The recently added
`--all` option remains temporarily as a compatibility alias that produces the
same event set.

This is not a full raw export. Observation IDs, sessions, subtitles, types,
facts, source metadata, complete narratives, files, direct messages, and
provider payloads remain local. The frozen `teammem-bundle/v1` shape does not
change.

### Keep `events` authoritative

Every MemberKit path follows the frozen protocol rule:

1. Generate or load `events`.
2. Validate the complete bundle and every event.
3. Regenerate `journal_md` only from the validated events.
4. Persist the bundle atomically.

Existing timestamp, timezone, `--force`, no-auto-push, and scheduler behavior
remain unchanged.

### Review persists removals and the regenerated journal

After a member deletes unwanted objects from the JSON `events` array,
`memberkit review --date YYYY-MM-DD`:

1. parses and fully validates the local v1 bundle;
2. verifies the configured member, requested date, event fields, non-empty
   summaries, allowed `kind` and `refs`, and same-day timestamps;
3. reconciles pending fingerprints with the edited event list, recording absent
   pending events as excluded;
4. regenerates `journal_md` from the remaining events;
5. atomically rewrites the same bundle; and
6. displays only the regenerated journal and remaining events.

A malformed bundle or failed atomic write leaves the original file and review
state unchanged. The recorded exclusions prevent a later scheduled draft from
restoring member-deleted events.

### Push repeats the local preflight

Push does not trust that review was run or that the file remained unchanged. It
performs the same full validation, journal regeneration, and atomic local
rewrite before any clone, pull, commit, or push. It does not mutate review
state before Git. Only the validated regenerated bundle is copied to the inbox.

Validation or local-write failure performs no network-capable Git operation. A
later Git failure retains the corrected local bundle and leaves review state
unchanged, so pending evidence remains pending. After successful delivery or a
verified no-op, `record_push` reconciles included events as approved and omitted
pending events as excluded in the same state update.

### TeamMem summarizes after import

A busy day may therefore contribute all 202 approved events. TeamMem imports
them idempotently into its ledger and performs downstream synthesis and
deduplication for the human-facing journal. Any three-to-seven target belongs to
that generated presentation, never to the accepted evidence set.

## Alternatives rejected

- **Keep the MemberKit cap:** shorter review, but permanently loses evidence
  before TeamMem can synthesize it.
- **Add an LLM curator to MemberKit:** still summarizes at the wrong boundary
  while adding provider, authentication, grounding, and fallback complexity.
- **Export complete raw observations:** would expose substantially more local
  data and violate the frozen v1 privacy boundary.

## Error behavior

- Missing database, invalid timezone, or query failure creates no partial draft.
- An existing direct draft is unchanged unless `--force` is explicit.
- A malformed scheduled draft is preserved and reported for attention.
- Invalid review or push input reports an actionable local error without
  rewriting the file or changing state.
- Review and push use replacement-style atomic writes so a local-write failure
  preserves the previous bytes.
- Push completes all validation and local persistence before Git/network work.
- A Git failure leaves review state unchanged while retaining the corrected
  local bundle.

## Verification

- More than seven same-project observations all remain.
- Multiple rows from one session and identical summaries all remain.
- Default, `--all`, and scheduled discovery produce the same event set.
- Local-day millisecond bounds and timezone-normalized timestamps remain correct.
- No internal observation fields enter the v1 bundle.
- Deleting an event and running review updates the same file's `journal_md`,
  display, and exclusion state.
- A later scheduled run cannot restore an excluded event.
- Pushing without a prior review still regenerates the local and destination
  journals before delivery and reconciles review state only after delivery.
- A failed Git operation leaves approvals, exclusions, and pending fingerprints
  unchanged.
- Invalid bundles and atomic-write failures make zero Git/network calls.
- Frozen-v1 import remains idempotent.
- Existing macOS, Linux, Windows, timezone, reminder, and no-auto-push tests
  continue to pass.

## Rollout

This change replaces only the unreleased MemberKit curation behavior on the
current branch. The Windows Task Scheduler implementation and timezone fixes
remain intact. Documentation must describe evidence-first drafts and downstream
TeamMem synthesis instead of a MemberKit three-to-seven cap.

Existing curated files remain valid v1 bundles. MemberKit does not recover
previously omitted events unless the member explicitly regenerates that date
with `memberkit draft --force`.
