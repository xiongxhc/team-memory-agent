# MemberKit Manual Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let members add a local reviewed highlight from WhatsApp, Telegram, LINE, email, meetings, or any unsupported source without changing bundle v1 or transmitting automatically.

**Architecture:** A focused bundle helper appends one v1 `journal-highlight` to a valid local draft and records it in existing pending state. The CLI exposes `memberkit add`; scheduled refresh preserves the visible manual event, while push remains the only transmission path.

**Tech Stack:** Python 3.11+, JSON, datetime, existing MemberKit bundle/state modules, pytest

## Global Constraints

- `teammem-bundle/v1` remains frozen: `kind="journal-highlight"` and `refs=null`.
- Manual origin is not stored as a structured field; the hub source remains `bundle:<member>`.
- MemberKit never authenticates to or scrapes unsupported applications.
- `memberkit add` never imports the push module, invokes Git, or transmits.
- Malformed or partially edited drafts are never overwritten.
- Members can edit or delete manual highlights before explicit push.

---

### Task 1: Safe Manual Highlight Append

**Files:**
- Create: `packages/memberkit/memberkit/manual.py`
- Modify: `packages/memberkit/memberkit/bundle.py`
- Test: `packages/memberkit/tests/test_manual.py`
- Modify: `packages/memberkit/memberkit/state.py`

**Interfaces:**
- Produces: `add_highlight(cfg, summary, project, date, time=None, now=None) -> Path`
- Produces: `valid_draft(data: object, member: str, date: str) -> bool`
- Produces: a complete valid `teammem-bundle/v1` draft

- [ ] **Step 1: Write failing creation and append tests**

```python
def test_add_creates_local_v1_draft_and_pending_state(tmp_path):
    path = add_highlight(
        cfg, "Agreed rollout in customer WhatsApp group",
        "project-alpha", "2026-07-28", "14:30",
    )
    data = json.loads(path.read_text())
    assert data["events"] == [{
        "ts": "2026-07-28T14:30:00",
        "kind": "journal-highlight",
        "summary": "Agreed rollout in customer WhatsApp group",
        "project": "project-alpha",
        "refs": None,
    }]
    assert DraftState(cfg.workdir / "state.json").pending_dates() == ["2026-07-28"]
    assert not (cfg.workdir / "inbox").exists()
```

Add tests for appending to a valid existing draft, exact duplicate suppression,
blank-summary rejection, invalid date/time, and `project=None`.

- [ ] **Step 2: Write the malformed-draft preservation test**

```python
def test_add_refuses_and_preserves_malformed_member_edit(tmp_path):
    path = cfg.workdir / "out" / "bundle-alex-2026-07-28.json"
    path.parent.mkdir(parents=True)
    original = b'{"events": [member edit in progress'
    path.write_bytes(original)
    with pytest.raises(ValueError, match="repair or remove"):
        add_highlight(cfg, "Safe text", None, "2026-07-28", "14:30")
    assert path.read_bytes() == original
```

- [ ] **Step 3: Run tests and confirm missing helper**

Run: `pytest -q packages/memberkit/tests/test_manual.py`

Expected: import fails.

- [ ] **Step 4: Implement strict append behavior**

Build the timestamp from the requested bundle date plus local `HH:MM`. When time
is omitted, use `now.astimezone()`'s clock. Validate an existing draft with the
same shape checks used by scheduled refresh; centralize those checks in
`memberkit.bundle` so schedule and manual paths cannot drift.

Add `bundle.valid_draft(data, member, date)` and use it before changing an existing
file. Use `DraftState.refresh(date, [event], current)` to preserve current edits,
deduplicate the new event, and mark the date pending. Write JSON atomically through
a sibling temporary file followed by `os.replace`.

- [ ] **Step 5: Run manual and state tests**

Run: `pytest -q packages/memberkit/tests/test_manual.py packages/memberkit/tests/test_state.py`

Expected: all pass.

- [ ] **Step 6: Commit manual bundle support**

```bash
git add packages/memberkit/memberkit/manual.py packages/memberkit/memberkit/bundle.py packages/memberkit/memberkit/state.py packages/memberkit/tests/test_manual.py
git commit -m "feat: add local manual highlights"
```

### Task 2: CLI, Scheduled Preservation, and Privacy Regression

**Files:**
- Modify: `packages/memberkit/memberkit/cli.py`
- Modify: `packages/memberkit/memberkit/schedule.py`
- Modify: `packages/memberkit/tests/test_cli.py`
- Modify: `packages/memberkit/tests/test_schedule.py`
- Modify: `packages/memberkit/tests/test_push.py`

**Interfaces:**
- Adds: `memberkit add --summary TEXT [--project SLUG] [--date YYYY-MM-DD] [--time HH:MM]`

- [ ] **Step 1: Write failing CLI tests**

```python
def test_add_cli_writes_local_draft_without_importing_push(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.config, "load", lambda: cfg)
    assert cli.main([
        "add", "--summary", "Meeting decision", "--project", "project-alpha",
        "--date", "2026-07-28", "--time", "09:15",
    ]) == 0
    assert "memberkit.push" not in sys.modules
    assert not (cfg.workdir / "inbox").exists()
```

- [ ] **Step 2: Write scheduled-preservation and redaction tests**

Create a manual event, run `scheduled_run` with a new claude-mem observation, and
assert both remain. Delete the manual event from the JSON, rerun scheduled refresh,
and assert it remains excluded rather than reappearing.

- [ ] **Step 3: Run focused tests**

Run: `pytest -q packages/memberkit/tests/test_cli.py packages/memberkit/tests/test_schedule.py packages/memberkit/tests/test_push.py`

Expected: parser rejects `add`.

- [ ] **Step 4: Add the CLI command**

Print the local path and `review before pushing: memberkit review --date <date>`.
Never mention that the source was automatically collected. Keep push imported only
inside the existing push branch.

- [ ] **Step 5: Share draft validation between manual and scheduled paths**

Replace schedule's `_valid_existing_draft` with Task 1's
`bundle.valid_draft(data, member, date)`. Preserve the existing byte-for-byte
invalid-draft behavior.

- [ ] **Step 6: Run the complete MemberKit suite**

Run: `pytest -q packages/memberkit/tests`

Expected: all pass.

- [ ] **Step 7: Commit CLI and schedule integration**

```bash
git add packages/memberkit/memberkit packages/memberkit/tests
git commit -m "feat: expose reviewed MemberKit fallback"
```

### Task 3: Member Documentation and End-to-End Import

**Files:**
- Modify: `README.md`
- Modify: `packages/memberkit/README.md`
- Modify: `docs/member-guide.md`
- Modify: `docs/privacy.md`
- Modify: `tests/test_memberkit_integration.py`

**Interfaces:**
- Documents: manual unsupported-source workflow
- Verifies: local add → explicit push fixture → hub import → vault render

- [ ] **Step 1: Add end-to-end integration coverage**

Construct a manual v1 draft, copy it into a temporary inbox as the push boundary,
import it twice, and render the vault:

```python
assert first.inserted == 1
assert second.inserted == 0
assert "Meeting decision" in person_page.read_text()
assert row["source"] == "bundle:alex"
```

- [ ] **Step 2: Document the fallback workflow**

Add copyable examples:

```bash
memberkit add \
  --summary "Confirmed the rollout plan in the customer group" \
  --project project-alpha
memberkit review
memberkit push
```

Explain that this works for WhatsApp, Telegram, LINE, email, meetings, and other
unsupported sources because the member types a selected highlight; MemberKit does
not connect to those applications.

- [ ] **Step 3: Document v1 provenance limits**

State that the hub records `bundle:<member>` as the source. A member may include
“WhatsApp”, “email”, or another origin in the summary, but v1 has no structured
origin field and this release does not introduce one.

- [ ] **Step 4: Run MemberKit, integration, build, and public scans**

Run:

```bash
pytest -q packages/memberkit/tests tests/test_memberkit_integration.py tests/test_importer.py
(cd packages/memberkit && python -m build)
./scripts/check-public.sh
```

Expected: all pass; the built MemberKit wheel exposes `memberkit add`.

- [ ] **Step 5: Commit fallback documentation**

```bash
git add README.md packages/memberkit/README.md docs/member-guide.md docs/privacy.md tests/test_memberkit_integration.py
git commit -m "docs: explain manual source fallback"
```
