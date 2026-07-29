# MemberKit Curated Drafts Implementation Plan

**Goal:** Produce reviewable, deterministic daily curated drafts without changing
the frozen bundle contract or the explicit push boundary.

**Architecture:** Keep legacy projection as a dedicated `--all` path. Add a pure
candidate normalization/ranking/curation layer above the read-only SQLite query;
write only valid new drafts, while normal and scheduled runs preserve existing
ones.

**Tech:** Python, SQLite read-only URI, pytest.

## Task 1: Characterize the legacy path and epoch units

**Files:** `packages/memberkit/memberkit/bundle.py`,
`packages/memberkit/tests/test_bundle.py`

- [ ] Add failing fixtures whose `created_at_epoch` values are milliseconds.
      Assert the intended local-day rows are selected and adjacent-day rows are
      excluded.
- [ ] Add `draft(..., all_observations=True)` (or equivalent keyword mode) tests that
      assert `--all` returns precisely one event per eligible legacy row, in
      ascending `created_at_epoch` order, with the current title/narrative
      projection and frozen v1 event shape. Assert `ts` is the contract-required
      member-local ISO 8601 conversion of that epoch, not the source timestamp
      string.
- [ ] Correct query bounds to milliseconds and retain `mode=ro`; keep the legacy
      projection in a separately testable helper so curated rules cannot change
      it.
- [ ] Run: `pytest -q packages/memberkit/tests/test_bundle.py`

## Task 2: Add pure, deterministic curation

**Files:** `packages/memberkit/memberkit/bundle.py`,
`packages/memberkit/tests/test_bundle.py`

- [ ] Write failing unit tests for whitespace normalization and exact normalized
      deduplication (earliest row wins), meaningful title/subtitle selection,
      generic-title fallback to a useful narrative sentence, and no exposure of
      session IDs, paths, raw metadata, or internal facts in emitted events.
- [ ] Add fixtures spanning two projects and sessions. Assert each session bucket
      contributes at most one best outcome; each project/day has three to seven
      when enough distinct session outcomes exist, fewer when sparse, a hard cap
      of seven, outcome/security/decision/blocker/release candidates outrank
      mechanics, and final events are chronological.
- [ ] Implement pure helpers accepting rows and returning selected safe event
      inputs; use an explicit keyword scoring table and stable timestamp/index
      tie-breaker. Serialize only v1 fields.
- [ ] Run: `pytest -q packages/memberkit/tests/test_bundle.py`

## Task 3: Preserve drafts and expose CLI modes

**Files:** `packages/memberkit/memberkit/cli.py`,
`packages/memberkit/memberkit/schedule.py`, `packages/memberkit/memberkit/bundle.py`,
`packages/memberkit/tests/test_cli.py`, `packages/memberkit/tests/test_schedule.py`

- [ ] Add failing CLI tests for curated default, `draft --all` legacy output,
      and rejection/preservation of an existing valid or malformed draft unless
      `--force` is supplied. Assert `--force` replaces only in the explicit CLI
      case.
- [ ] Add scheduler tests proving it uses curated default, does not import push,
      and leaves valid/manual and malformed drafts byte-for-byte unchanged.
- [ ] Add an explicit shared draft-validation helper if needed; do not weaken
      malformed-draft protection. Thread `--all` only through draft selection,
      never bundle schema or schedule.
- [ ] Run: `pytest -q packages/memberkit/tests/test_cli.py packages/memberkit/tests/test_schedule.py`

## Task 4: Document and verify safely

**Files:** `packages/memberkit/README.md`, `docs/member-guide.md`,
`docs/privacy.md`, `packages/memberkit/tests/test_bundle.py` (or a focused
read-only verification test)

- [ ] Document curated defaults, `draft --all`, `--force`, frozen-v1 redaction,
      and that scheduling drafts/reminds only.
- [ ] Add a documented read-only real-DB verification invocation that sets a
      temporary work directory and does not write `~/.memberkit`; never claim it
      validates a live database until it has actually been run.
- [ ] Run the focused suite, then:

      `pytest -q packages/memberkit/tests tests/test_memberkit_integration.py`

- [ ] Build/package and public scan when available:

      `(cd packages/memberkit && python -m build)`

      `./scripts/check-public.sh`

## Acceptance checks

- Curated output is local, deterministic, chronological, provider/network/LLM
  free, and contains only frozen v1 public fields.
- Each session bucket contributes at most one best outcome; per `(project, day)`
  output is capped at seven and favors meaningful outcomes. Sparse project/days
  remain sparse.
- Millisecond epoch date filtering is correct.
- An optional validated `MEMBERKIT_TIMEZONE` controls direct and scheduled day
  selection and timestamp serialization; process environment overrides the
  private file, while detected local time remains the default.
- `--all` precisely preserves the old row selection, title/narrative summaries,
  and epoch order while normalizing `ts` to the same member timezone used for
  day selection.
- No automatic path overwrites a valid/manual or malformed draft; only explicit
  `draft --force` replaces a draft.
