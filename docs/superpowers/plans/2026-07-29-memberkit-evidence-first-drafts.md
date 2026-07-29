# MemberKit Evidence-First Drafts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve every eligible MemberKit v1 observation event, keep edited journals synchronized, and defer semantic summarization to TeamMem.

**Architecture:** Simplify `bundle.draft()` to the existing legacy v1 projection for every caller. Add one shared local bundle preflight that validates the frozen envelope, regenerates `journal_md`, and atomically persists it; both review and push use it, while review state records removed pending fingerprints before later schedules can restore them.

**Tech Stack:** Python 3.11+, SQLite read-only URI, `pathlib`, `json`, replacement-style atomic writes, pytest, Git.

## Global Constraints

- `teammem-bundle/v1` remains frozen: exact top-level keys and exact event keys; `kind` is `journal-highlight`; `refs` is `null`.
- Drafting emits every eligible observation row in chronological order; no scoring, session consolidation, semantic deduplication, or per-project cap.
- `--all` remains accepted as a compatibility alias and produces the same event set as default drafting.
- The existing millisecond epoch query, member-timezone date boundary, normalized event timestamp, `--force`, and no-auto-push behavior remain unchanged.
- Complete raw observation rows, facts, session metadata, file payloads, and direct messages are not added to the bundle.
- Review and push finish validation, exclusion reconciliation, journal regeneration, and atomic local persistence before push performs any Git/network-capable call.
- macOS, Linux, and Windows scheduler implementation files are outside this change.
- Commits use `Chris Xiong <xionghx713@gmail.com>` as author and committer, contain no `Co-Authored-By`, and are not pushed.

## File map and interfaces

- `packages/memberkit/memberkit/bundle.py` owns observation projection, `validate_bundle(data, member, date) -> dict`, `prepare_bundle(path, member, date) -> dict`, journal rendering, and atomic JSON persistence.
- `packages/memberkit/memberkit/cli.py` owns the direct draft/review commands and invokes the shared preflight before displaying review output.
- `packages/memberkit/memberkit/push.py` owns inbox Git transport and invokes the shared preflight before any transport operation.
- `packages/memberkit/memberkit/state.py` remains the fingerprint decision store; existing `refresh(date, discovered, current)` records removed pending events as excluded.
- `packages/memberkit/memberkit/schedule.py` continues calling the default `bundle.draft()` and preserves existing member-edited drafts.
- MemberKit tests cover component behavior; `tests/test_memberkit_integration.py` covers frozen-v1 hub import.

---

### Task 1: Restore Evidence-First Observation Projection

**Files:**
- Modify: `packages/memberkit/memberkit/bundle.py`
- Modify: `packages/memberkit/memberkit/cli.py`
- Test: `packages/memberkit/tests/test_bundle.py`
- Test: `packages/memberkit/tests/test_cli.py`
- Test: `packages/memberkit/tests/test_schedule.py`

**Interfaces:**
- Consumes: `draft(db_path: Path, member: str, date: str, *, all_observations: bool = False, timezone=None) -> dict`
- Produces: identical default and `all_observations=True` event arrays using the legacy title/narrative projection

- [ ] **Step 1: Write failing selection tests**

Add behavior tests whose hand-built fixtures contain eight same-project rows,
two rows sharing one session, and two identical titles:

```python
default = bundle.draft(db, "alex", "2026-07-24", timezone=zone)
compat = bundle.draft(
    db, "alex", "2026-07-24", all_observations=True, timezone=zone
)
assert len(default["events"]) == 8
assert default["events"] == compat["events"]
assert [event["summary"] for event in default["events"]].count("same") == 2
```

Update the CLI test to prove `draft` and `draft --all` request the same
observable bundle behavior, and update the scheduled-run test to assert all
eight events are written.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/pytest -q \
  packages/memberkit/tests/test_bundle.py \
  packages/memberkit/tests/test_cli.py \
  packages/memberkit/tests/test_schedule.py
```

Expected: failures show the default still selects/caps curated events and
scheduled drafting inherits that selection.

- [ ] **Step 3: Remove the curation path**

Delete `PROJECT_LIMIT`, scoring/generic/path-ranking constants and helpers, and
`_curated_events()`. Query only the legacy projection columns plus the stable
timestamp tie-breaker:

```python
selected = ["project", "title", "narrative", "created_at_epoch"]
order_by = "created_at_epoch, id" if "id" in columns else "created_at_epoch, rowid"
events = _legacy_events(rows, zone)
```

Retain the keyword argument for CLI compatibility but do not branch on it.
Change `--all` help to state that it is a compatibility alias.

- [ ] **Step 4: Run GREEN and the real-data count check**

Run the focused command from Step 2. Then run read-only real-data verification
with no MemberKit work-directory write:

```bash
MEMBERKIT_TIMEZONE=Asia/Dubai PYTHONPATH=packages/memberkit \
  .venv/bin/python -c '
from pathlib import Path
from memberkit.bundle import draft
d = draft(Path("/Users/cx/.claude-mem/claude-mem.db"), "cx", "2026-07-28")
print(len(d["events"]))
print(sum(e["project"] == "team-memory-agent" for e in d["events"]))
'
```

Expected current database result: all eligible day events are present, including
all 211 currently stored `team-memory-agent` events (the earlier cutoff had
202).

- [ ] **Step 5: Commit**

```bash
git add packages/memberkit/memberkit/bundle.py \
  packages/memberkit/memberkit/cli.py \
  packages/memberkit/tests/test_bundle.py \
  packages/memberkit/tests/test_cli.py \
  packages/memberkit/tests/test_schedule.py
GIT_AUTHOR_NAME='Chris Xiong' GIT_AUTHOR_EMAIL='xionghx713@gmail.com' \
GIT_COMMITTER_NAME='Chris Xiong' GIT_COMMITTER_EMAIL='xionghx713@gmail.com' \
  git commit -m 'fix: preserve all MemberKit evidence'
```

### Task 2: Make Review Persist the Authoritative Journal

**Files:**
- Modify: `packages/memberkit/memberkit/bundle.py`
- Modify: `packages/memberkit/memberkit/cli.py`
- Test: `packages/memberkit/tests/test_bundle.py`
- Test: `packages/memberkit/tests/test_cli.py`
- Test: `packages/memberkit/tests/test_state.py`

**Interfaces:**
- Produces: `validate_bundle(data: object, member: str, date: str) -> dict`
- Produces: `write_bundle(path: Path, data: dict) -> None`
- Produces: `prepare_bundle(path: Path, member: str, date: str) -> dict`
- Consumes: `DraftState.refresh(date, discovered=[], current=data) -> list[dict]`

- [ ] **Step 1: Write failing validation and review tests**

Add table-driven validator cases for wrong top-level keys, member/date mismatch,
wrong event keys, blank summary, wrong `kind`, non-null `refs`, invalid
timestamp, and timestamp outside the bundle date. Add a review regression:

```python
original["events"] = [kept_event]
original["journal_md"] = "## 2026-07-27\n- removed private event"
path.write_text(json.dumps(original), encoding="utf-8")

assert cli.main(["review", "--date", "2026-07-27"]) == 0
saved = json.loads(path.read_text(encoding="utf-8"))
assert saved["journal_md"] == (
    "## 2026-07-27\n\n### project-alpha\n- kept"
)
assert removed_fingerprint in DraftState(state_path).snapshot()["excluded"]
```

Add a failure test that injects an atomic replacement error and asserts the
bundle bytes and state snapshot remain unchanged.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/pytest -q \
  packages/memberkit/tests/test_bundle.py \
  packages/memberkit/tests/test_cli.py \
  packages/memberkit/tests/test_state.py
```

Expected: review prints a fresh journal but leaves stale bytes on disk and does
not immediately record the removed fingerprint.

- [ ] **Step 3: Implement shared validation and atomic persistence**

Implement strict validation in `bundle.py` without changing frozen v1. Validate
exact key sets, types, non-empty summary, ISO timestamp, and same calendar date.
Regenerate only after validation:

```python
def prepare_bundle(path: Path, member: str, date: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    validated = validate_bundle(data, member, date)
    validated["journal_md"] = render_journal(validated["events"], date)
    write_bundle(path, validated)
    return validated
```

`write_bundle()` serializes to a same-directory temporary file, flushes and
`fsync()`s it, replaces the destination with `os.replace()`, and removes an
unreplaced temporary file in `finally`.

In CLI review, validate first, call `state.refresh(..., current=data)` to stage
the exclusions after the regenerated bundle is persisted, and ignore its return
value because the authoritative edited `events` array must not be filtered by
prior approval state:

```python
data = bundle.prepare_bundle(out, cfg.member, date_text)
DraftState(cfg.workdir / "state.json").refresh(
    date_text, discovered=[], current=data
)
```

This order means a failed bundle replacement cannot mutate review state.

- [ ] **Step 4: Run GREEN**

Run the focused command from Step 2 and verify pristine output.

- [ ] **Step 5: Commit**

```bash
git add packages/memberkit/memberkit/bundle.py \
  packages/memberkit/memberkit/cli.py \
  packages/memberkit/tests/test_bundle.py \
  packages/memberkit/tests/test_cli.py \
  packages/memberkit/tests/test_state.py
GIT_AUTHOR_NAME='Chris Xiong' GIT_AUTHOR_EMAIL='xionghx713@gmail.com' \
GIT_COMMITTER_NAME='Chris Xiong' GIT_COMMITTER_EMAIL='xionghx713@gmail.com' \
  git commit -m 'fix: synchronize reviewed MemberKit journals'
```

### Task 3: Preflight Push Before Git or Network

**Files:**
- Modify: `packages/memberkit/memberkit/push.py`
- Test: `packages/memberkit/tests/test_push.py`

**Interfaces:**
- Consumes: `prepare_bundle(path, cfg.member, date) -> dict`
- Consumes: review-state exclusion reconciliation from Task 2
- Preserves: `push(cfg: Config, date: str) -> Path`

- [ ] **Step 1: Write failing push-order tests**

Use an invalid bundle and replace `_run` with a function that fails the test if
called:

```python
calls = []
monkeypatch.setattr(push, "_run", lambda cmd: calls.append(cmd))
with pytest.raises(SystemExit):
    push.push(cfg, "2026-07-24")
assert calls == []
assert not (cfg.workdir / "inbox").exists()
```

Add a valid edited bundle with a stale private journal and valid ISO timestamp.
Push it without review, then assert the local source and destination contain the
same regenerated journal and the removed pending fingerprint is excluded. Add a
Git-failure test proving included events are not approved.

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
.venv/bin/pytest -q packages/memberkit/tests/test_push.py
```

Expected: current push clones before complete event validation and does not
rewrite the local source journal.

- [ ] **Step 3: Implement local preflight ordering**

At the beginning of `push()`:

```python
data = bundle.prepare_bundle(src, cfg.member, date)
state = DraftState(cfg.workdir / "state.json")
state.refresh(date, discovered=[], current=data)
```

Only then derive `clone` or invoke `_run`/`_git`. Copy the already validated
serialized data to the destination. Keep `record_push()` after the verified
no-op or successful remote push only.

- [ ] **Step 4: Run GREEN and MemberKit integration tests**

Run:

```bash
.venv/bin/pytest -q \
  packages/memberkit/tests/test_push.py \
  tests/test_memberkit_integration.py
```

- [ ] **Step 5: Commit**

```bash
git add packages/memberkit/memberkit/push.py \
  packages/memberkit/tests/test_push.py
GIT_AUTHOR_NAME='Chris Xiong' GIT_AUTHOR_EMAIL='xionghx713@gmail.com' \
GIT_COMMITTER_NAME='Chris Xiong' GIT_COMMITTER_EMAIL='xionghx713@gmail.com' \
  git commit -m 'fix: preflight MemberKit bundles before push'
```

### Task 4: Align Documentation and Verify the Full Branch

**Files:**
- Modify: `README.md`
- Modify: `packages/memberkit/README.md`
- Modify: `docs/member-guide.md`
- Modify: `docs/privacy.md`
- Modify: `tests/test_memberkit_integration.py` only for missing end-to-end behavior

**Interfaces:**
- Documents: default evidence-first draft, authoritative `events`, review rewrite,
  explicit push, downstream TeamMem synthesis, and `--all` compatibility alias

- [ ] **Step 1: Update public instructions**

Remove every statement that MemberKit selects three to seven highlights or that
`--all` bypasses a curated default. State that a busy day can contain hundreds
of short v1 events, review rewrites `journal_md`, and TeamMem summarizes after
import. Preserve the warning that human review is mandatory.

- [ ] **Step 2: Add or update the end-to-end regression**

The integration test must draft more than seven same-project observations,
remove one, run review, import the remaining frozen-v1 events, and assert
idempotent second import without relying on generated journal prose.

- [ ] **Step 3: Run complete verification**

```bash
.venv/bin/pytest -q
./scripts/check-public.sh
git diff --check
(cd packages/memberkit && ../../.venv/bin/python -m build --no-isolation)
```

Also verify the Windows code is unchanged from the approved design commit:

```bash
git diff d0160d7 -- teammem/schedule.py teammem/schedule_windows.py \
  teammem/windows_security.py tests/test_schedule_windows.py
```

Expected: empty diff for the Windows-owned files.

- [ ] **Step 4: Inspect scope and commit identity**

```bash
git status --short
git log --format='%h %an <%ae> | %cn <%ce> | %s' d0160d7..HEAD
```

Expected: only planned files changed and every commit uses
`Chris Xiong <xionghx713@gmail.com>`.

- [ ] **Step 5: Commit documentation and integration coverage**

```bash
git add README.md packages/memberkit/README.md docs/member-guide.md \
  docs/privacy.md tests/test_memberkit_integration.py \
  docs/superpowers/specs/2026-07-29-memberkit-evidence-first-drafts-design.md \
  docs/superpowers/plans/2026-07-29-memberkit-evidence-first-drafts.md
GIT_AUTHOR_NAME='Chris Xiong' GIT_AUTHOR_EMAIL='xionghx713@gmail.com' \
GIT_COMMITTER_NAME='Chris Xiong' GIT_COMMITTER_EMAIL='xionghx713@gmail.com' \
  git commit -m 'docs: explain evidence-first MemberKit review'
