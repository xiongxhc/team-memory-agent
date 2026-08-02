# Review Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make GitLab and MemberKit evidence truthful and idempotent, make synthesis failures diagnosable, and preserve the private hub's established daily failure policy.

**Architecture:** Keep the frozen event and bundle formats. GitLab issue polling emits lifecycle facts only when the provider gives an authoritative lifecycle timestamp and actor; repository events with unresolved transient creators are deferred with warnings. Existing reclaim logic runs before connector insertion and collapses already-created unmapped/mapped duplicates. CLI diagnostics include bounded excerpts from both streams. The daily result distinguishes fatal local-integrity failures from warning-level remote/optional failures.

**Tech Stack:** Python 3.11+, SQLite, pytest, provider REST adapters.

## Global Constraints

- Preserve existing commit, merge-request, issue-open, issue-closed, and repository event hashes so the live ledger does not gain one-time migration duplicates.
- Never turn an edited or commented old issue into a recent opened fact.
- Do not use deprecated GitLab `assignee` as evidence of who closed an issue.
- `teammem-bundle/v1` timestamps must be parseable but need not share the bundle's textual calendar date.
- Connector, import, synthesis, docs-sync, and push failures are warning-level; ledger, reclaim, render, and snapshot failures are fatal.
- Do not expose credentials, prompts, or unbounded subprocess output.
- Do not commit or push without an explicit user instruction.

---

### Task 1: Frozen bundle-v1 timestamp compatibility

**Files:**
- Modify: `teammem/bundles.py`
- Modify: `packages/memberkit/memberkit/bundle.py`
- Modify: `schemas/teammem-bundle-v1.md`
- Modify: `docs/superpowers/specs/2026-07-29-memberkit-evidence-first-drafts-design.md`
- Test: `tests/test_bundles.py`
- Test: `packages/memberkit/tests/test_bundle.py`

**Interfaces:**
- Consumes: frozen `teammem-bundle/v1` with member-local or offset evidence timestamps.
- Produces: unchanged `Bundle`, MemberKit validation, and event identities.

- [ ] **Step 1: Add failing hub and MemberKit acceptance tests**

  Add literal fixtures whose bundle date is `2026-07-28` and whose valid timestamps are `2026-07-27T20:30:00Z` and `2026-07-27T23:30:00`; assert both validators accept them. Keep malformed timestamp rejection.

- [ ] **Step 2: Run RED tests**

  Run: `PYTHONPATH=packages/memberkit <venv-python> -m pytest -q tests/test_bundles.py packages/memberkit/tests/test_bundle.py`

  Expected: only the two cross-date cases fail with `timestamp is outside bundle date` or `ts is outside bundle date`.

- [ ] **Step 3: Remove only the calendar-date equality checks**

  Continue parsing timestamps with `datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))`; remove the comparison to the bundle date from both validators.

- [ ] **Step 4: Correct public schema/design prose**

  State that `date` is the member-selected review day while `ts` is preserved evidence time and may show a neighbouring calendar date after timezone conversion.

- [ ] **Step 5: Run GREEN tests**

  Run the Task 1 command and require zero failures.

---

### Task 2: Truthful GitLab lifecycle facts and stable repository attribution

**Files:**
- Modify: `teammem/connectors/gitlab.py`
- Modify: `teammem/reclaim.py`
- Modify: `teammem/services.py`
- Modify: `docs/superpowers/specs/2026-07-31-gitlab-full-activity-design.md`
- Modify: `README.md`
- Modify: `docs/deployment.md`
- Modify: `docs/privacy.md`
- Test: `tests/test_gitlab_collector.py`
- Test: `tests/test_reclaim.py`
- Test: `tests/test_services.py`

**Interfaces:**
- Consumes: GitLab group projects, issues, users, and ISO-8601 timestamps.
- Produces: normalized `issue` and `repo` events plus non-sensitive `CollectionResult.warnings`.

- [ ] **Step 1: Add failing lifecycle tests**

  Cover: an old open issue updated only by a comment emits nothing; a new issue uses `created_at`; a closed issue uses `closed_by` even when another person is assigned; a created-and-closed issue emits opened and closed facts; a fractional exact-boundary project timestamp is included.

- [ ] **Step 2: Add failing attribution/reclaim tests**

  Cover: a failed repository creator lookup emits no unstable event and returns a warning; a later successful lookup emits one event; collecting after roster mapping reclaims `_unmapped/<username>` before inserting; reclaim collapses an existing unmapped/mapped `(source, hash)` pair into one mapped row.

- [ ] **Step 3: Run RED tests**

  Run: `<venv-python> -m pytest -q tests/test_gitlab_collector.py tests/test_reclaim.py tests/test_services.py`

  Expected: the new lifecycle, warning, boundary, and duplicate-collapse assertions fail against the reviewed implementation.

- [ ] **Step 4: Implement provider-time parsing and lifecycle filtering**

  Add one small ISO parser using `datetime.fromisoformat(value.replace("Z", "+00:00"))`. Emit an opened fact only when `created_at >= since`, with the author and existing opened hash. Emit a closed fact only when `closed_at >= since`, with `closed_by` and the existing closed hash. Emit both for issues created and closed in the window. Do not infer reopen events from `updated_at`; repeated reopen/reclose cycles remain explicitly outside this polling contract rather than creating migration duplicates in the live ledger.

- [ ] **Step 5: Defer unstable repository attribution**

  On creator lookup exceptions or unusable responses, omit that repository event for the run and add a warning naming only the public project path. Retry naturally during the lookback. Keep `_unmapped/<username>` for a successful lookup whose username is absent from the roster.

- [ ] **Step 6: Reclaim before inserting and collapse historical duplicates**

  In non-dry connector collection, run `reclaim(connection, ids)` before `insert_events`. In `reclaim`, before updating an unmapped person to a mapped slug, delete only unmapped rows whose exact `(source, hash)` already exists for that mapped slug, then update the remaining claimable rows.

- [ ] **Step 7: Correct lifecycle documentation**

  Replace “state transitions” with “issue lifecycle observations”; state that polling captures one initial-creation fact and one provider-reported closure fact per issue, while repeated reopen/reclose history requires a state-events or webhook source.

- [ ] **Step 8: Run GREEN tests**

  Run the Task 2 command and require zero failures.

---

### Task 3: Bounded Claude CLI diagnostics

**Files:**
- Modify: `teammem/summarize.py`
- Test: `tests/test_summarize.py`

**Interfaces:**
- Consumes: subprocess `stdout`, `stderr`, and exit status.
- Produces: one `ValueError` whose detail is deterministic, single-line, and at most 300 characters.

- [ ] **Step 1: Add a failing two-stream test**

  Return nonzero with a long stderr warning and a meaningful stdout model error. Assert both `stderr:` and `stdout:` excerpts survive, whitespace is collapsed, and the detail after the status prefix is bounded.

- [ ] **Step 2: Run the RED test**

  Run: `<venv-python> -m pytest -q tests/test_summarize.py`

- [ ] **Step 3: Implement one diagnostic formatter**

  Collapse control/newline whitespace, take a bounded excerpt from each non-empty stream, label them deterministically in stderr-then-stdout order, and retain a `(no output)` fallback.

- [ ] **Step 4: Run GREEN tests**

  Run the Task 3 command and require zero failures.

---

### Task 4: Daily warning and fatal exit policy

**Files:**
- Modify: `teammem/daily.py`
- Modify: `docs/deployment.md`
- Modify: `README.md`
- Test: `tests/test_daily.py`

**Interfaces:**
- Consumes: ordered `StepResult` values.
- Produces: `DailyResult.exit_code`, zero for warning-level optional/remote failures and one for local-integrity failures.

- [ ] **Step 1: Add failing policy tests**

  Parameterize failed stages. Assert `ledger`, `reclaim`, `render`, and `snapshot` yield exit 1; connector names, `import`, `journal`, `report`, `docs-sync`, and `push` yield exit 0 while retaining `status == "failed"` and visible detail.

- [ ] **Step 2: Run RED tests**

  Run: `<venv-python> -m pytest -q tests/test_daily.py`

- [ ] **Step 3: Implement explicit fatal-stage evaluation**

  Use a fixed local-integrity set `{"ledger", "reclaim", "render", "snapshot"}` when deriving the exit code. Do not rewrite individual step statuses or suppress their diagnostics.

- [ ] **Step 4: Document the policy**

  Explain that remote/optional failures remain visible warnings and retry next run, while failures that threaten the ledger or durable local projection make the scheduler report failure.

- [ ] **Step 5: Run GREEN tests**

  Run the Task 4 command and require zero failures.

---

### Task 5: Repository-wide verification

**Files:**
- Verify only.

- [ ] **Step 1: Run all focused tests from Tasks 1-4 together**

- [ ] **Step 2: Run the public validation script**

  Run: `./scripts/check-public.sh`

- [ ] **Step 3: Run the complete test suite with MemberKit on `PYTHONPATH`**

  Run: `PYTHONPATH=packages/memberkit <venv-python> -m pytest -q`

  Record separately any known baseline failures caused by the operator's live default hub environment; do not hide or silently fix unrelated failures.

- [ ] **Step 4: Review the complete branch diff and confirm no secrets, IDs, credentials, or private paths were introduced**
