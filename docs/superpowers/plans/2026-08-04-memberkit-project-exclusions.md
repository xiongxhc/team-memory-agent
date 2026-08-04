# MemberKit Project Exclusions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a MemberKit user locally exclude configured projects and projected-summary patterns from newly generated drafts, while preserving frozen-v1 bundles, existing drafts, draft-state reversibility, and native schedules.

**Architecture:** Add a pure `memberkit.exclusions` module for the workdir-derived rule path, complete-file parsing, normalized rendering, and first-match filtering/counts. Direct draft, scheduled generation, and read-only preview keep using `bundle.draft()` as the single SQLite/read-only frozen-v1 projection path; they validate its output before applying the immutable rules snapshot. Only included events reach `DraftState.refresh()` and bundle persistence.

**Tech Stack:** Python 3.11+, standard-library `dataclasses`, `pathlib`, `re`, `sqlite3` read-only URI mode, JSON, existing atomic bundle writes, pytest.

## Global Constraints

- Rules are the UTF-8 file `<resolved MEMBERKIT_WORKDIR>/exclude-projects.txt`; do not add a configuration key, environment variable, editing command, remote synchronization, or schedule argument.
- A missing rules file means zero rules. An unreadable file, invalid UTF-8, syntax/control-character error, or regex compile failure fails closed before a new bundle or draft-state write.
- Parse and compile the complete rules file once per command invocation. Never apply a partially parsed prefix; concurrent edits affect only the next invocation.
- Supported syntax is exact project, trailing-star project prefix, and `exact-project ~ regular-expression`; project matching is case-sensitive and regex `re.search` is case-insensitive against the projected frozen-v1 `summary` only.
- Preserve source event order, event payload, `teammem-bundle/v1`, `--all` compatibility, member timezone selection, atomic bundle replacement, and state-first persistence semantics. Do not claim a cross-file transaction or rollback.
- Excluded events must not enter approved, excluded, or pending fingerprints. Existing valid, invalid, and member-edited drafts are never re-filtered because a rule changes.
- `scheduled_run()` must retain its existing public positional parameters and `list[str]` ready-date return value. macOS and Windows scheduled actions remain exactly `memberkit scheduled-run`; edits to rule contents require no reinstall.
- Read-only `exclusions list` and `exclusions preview` must not write bundle/state files, contact a network, invoke Git/LLM/notification/push adapters, or print matched observation content.
- Do not modify `schemas/teammem-bundle-v1.md`. Exclusions are local pre-bundle policy, not bundle metadata.
- Do not migrate `~/.memberkit/exclude-projects.txt`, existing drafts/state, or launchd during implementation. Release/package verification and wrapper cutover are a separate authorized deployment.
- Execution requires separate commit authorization. Do not stage, commit, or push while carrying out planning; any task commit below is an execution-time checkpoint only, and no task authorizes `git push`.

## File Map

- Create `packages/memberkit/memberkit/exclusions.py`: no-I/O-beyond-rule-file parser, normalized immutable rules, matcher, and count-only result.
- Create `packages/memberkit/tests/test_exclusions.py`: grammar, validation, normalization, matching, privacy, and deterministic-count unit tests.
- Modify `packages/memberkit/memberkit/cli.py`: direct-draft filter integration plus `exclusions list` and `exclusions preview` parser/dispatch/output.
- Modify `packages/memberkit/memberkit/schedule.py`: one snapshot per scheduled invocation, missing-draft filtering, excluded-count logging, without public API change.
- Modify `packages/memberkit/tests/test_cli.py`: direct draft, CLI inspection, side-effect, and state safety tests.
- Modify `packages/memberkit/tests/test_schedule.py` and `packages/memberkit/tests/test_schedule_runtime.py`: scheduled behavior, native action, Windows safe log, and notification regressions.
- Modify `packages/memberkit/tests/test_bundle.py`: projection sentinel proving regex receives final v1 summary rather than discarded narrative.
- Modify `packages/memberkit/README.md`, `docs/member-guide.md`, `README.md`, and `docs/privacy.md`: user syntax, workflow/reversibility, no-reinstall scheduling, migration boundary, and trusted-local-regex privacy boundary.

---

### Task 1: Pure Local Rule Parsing and First-Match Filtering

**Files:**
- Create: `packages/memberkit/memberkit/exclusions.py`
- Create: `packages/memberkit/tests/test_exclusions.py`

**Interfaces:**
- Produces: `rules_path(workdir: Path) -> Path`
- Produces: `RuleFileError(ValueError)` with `path`, optional `line`, and sanitized category-only `str()` output.
- Produces: `ExclusionRule(source_line: int, kind: Literal["exact", "prefix", "regex"], project: str, pattern: str | None, compiled: re.Pattern[str] | None)` with `normalized() -> str` and `matches(event: dict) -> bool`.
- Produces: `ExclusionResult(included: list[dict], excluded_count: int, rule_counts: tuple[int, ...])`.
- Produces: `load_rules(path: Path) -> tuple[ExclusionRule, ...]` and `apply_rules(events: Sequence[dict], rules: Sequence[ExclusionRule]) -> ExclusionResult`.
- Consumed by Tasks 2–4: immutable rule tuple and result count fields; no caller receives excluded event values.

- [ ] **Step 1: Write failing parser, sanitizer, and matcher tests**

Create `test_exclusions.py` with a small frozen-v1-shaped event fixture and tests that define the complete contract:

```python
def test_rules_normalize_and_assign_first_matching_rule(tmp_path):
    path = tmp_path / "exclude-projects.txt"
    path.write_bytes(
        b"  # comment\\r\\n"
        b"team-memory-agent\\r\\n"
        b"scratch*\\r\\n"
        b"estidama-sdk ~ ^test(s)? passed$\\r\\n"
        b"estidama-sdk ~ passed\\r\\n"
    )
    rules = exclusions.load_rules(path)
    result = exclusions.apply_rules([
        event("team-memory-agent", "keep no source narrative"),
        event("scratch-one", "other"),
        event("estidama-sdk", "Tests Passed"),
        event(None, "Tests Passed"),
    ], rules)

    assert [rule.normalized() for rule in rules] == [
        "team-memory-agent", "scratch*",
        "estidama-sdk ~ ^test(s)? passed$", "estidama-sdk ~ passed",
    ]
    assert result.included == [event(None, "Tests Passed")]
    assert result.excluded_count == 3
    assert result.rule_counts == (1, 1, 1, 0)
```

Add parameterized failures for invalid UTF-8, empty operands, `*`, `foo*bar`, `foo**`, wildcard regex project, invalid regex, tab/C0/DEL, and a later invalid line after a valid line. Assert every error includes `str(path)`, the known line where available, and a category such as `invalid regular expression`, but excludes the source line and pattern. Add exact/prefix case-sensitive tests, regex-project case-sensitive/summary case-insensitive tests, bare `~` project tests, duplicate ordering, and a title/narrative sentinel event proving only `summary` is inspected.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
python -m pytest -q packages/memberkit/tests/test_exclusions.py
```

Expected: collection fails with `ModuleNotFoundError: No module named 'memberkit.exclusions'`.

- [ ] **Step 3: Implement complete-file parsing and pure filtering**

Create `exclusions.py`. Decode the entire existing file with `read_bytes().decode("utf-8")`, split only on `"\n"`, remove one terminal `"\r"` from each resulting line to support CRLF, reject every remaining C0/DEL code point before trimming, and only return after all regexes compile. Do not use `splitlines()`: it treats forbidden vertical-tab/form-feed and other Unicode separators as line boundaries. Use a single delimiter matching `r" +~ +"` and preserve regex text except delimiter-adjacent outer spaces:

```python
def load_rules(path: Path) -> tuple[ExclusionRule, ...]:
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return ()
    except UnicodeDecodeError as exc:
        raise RuleFileError(path, None, "invalid UTF-8") from exc
    except OSError as exc:
        raise RuleFileError(path, None, "unreadable rules file") from exc

    rules: list[ExclusionRule] = []
    encoded_lines = text.split("\n")
    for source_line, encoded_line in enumerate(encoded_lines, start=1):
        has_lf = source_line < len(encoded_lines)
        raw = encoded_line[:-1] if has_lf and encoded_line.endswith("\r") else encoded_line
        _reject_controls(path, source_line, raw)
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rules.append(_parse_rule(path, source_line, line))
    return tuple(rules)

def apply_rules(events: Sequence[dict], rules: Sequence[ExclusionRule]) -> ExclusionResult:
    included: list[dict] = []
    counts = [0] * len(rules)
    for event in events:
        index = next((i for i, rule in enumerate(rules) if rule.matches(event)), None)
        if index is None:
            included.append(event)
        else:
            counts[index] += 1
    return ExclusionResult(included, sum(counts), tuple(counts))
```

`_parse_rule()` must distinguish a delimiter only when `~` has space on both sides; validate prefix placement before constructing a rule; compile regexes using `re.IGNORECASE`; and raise `RuleFileError` without embedding user-authored rule text. `matches()` must return `False` for `event["project"] is None` for every rule kind.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
python -m pytest -q packages/memberkit/tests/test_exclusions.py
```

Expected: all parser/matcher tests pass; no package imports pull scheduler, push, or hub modules.

- [ ] **Step 5: Commit the isolated rules module (only after separate authorization)**

```bash
git add packages/memberkit/memberkit/exclusions.py packages/memberkit/tests/test_exclusions.py
git commit -m "feat(memberkit): add local exclusion rule parser"
```

Do not push.

### Task 2: Filter Direct Drafts Before Draft-State Persistence

**Files:**
- Modify: `packages/memberkit/memberkit/cli.py:7-17,137-163`
- Modify: `packages/memberkit/tests/test_cli.py`
- Modify: `packages/memberkit/tests/test_bundle.py`
- Test: `packages/memberkit/tests/test_state.py` (regression only)

**Interfaces:**
- Consumes: `exclusions.rules_path(cfg.workdir) -> Path`, `load_rules(path) -> tuple[ExclusionRule, ...]`, and `apply_rules(data["events"], rules) -> ExclusionResult` from Task 1.
- Consumes: unchanged `bundle.draft(...) -> dict`, `bundle.validate_bundle(...) -> dict`, `bundle.render_journal(...) -> str`, `bundle.write_bundle(...) -> None`, and `DraftState.refresh(...) -> list[dict]`.
- Produces: direct `draft` output line `excluded <N> events`; `N` is printed even when zero.

- [ ] **Step 1: Write failing direct-draft and reversibility tests**

Add a shared local SQLite fixture with eligible rows for an exact match, prefix match, regex match, `project NULL`, and a narrative-only row. Write `exclude-projects.txt` under `cfg.workdir`, then assert normal, `--all`, and forced drafting use exactly the included events:

```python
assert cli.main(["draft", "--date", "2026-07-27"]) == 0
payload = json.loads(out.read_text(encoding="utf-8"))
state = DraftState(cfg.workdir / "state.json").snapshot()
assert [item["summary"] for item in payload["events"]] == ["Keep", "Regex project null"]
assert payload["journal_md"] == "## 2026-07-27\n\n### general\n- Regex project null\n\n### kept\n- Keep"
assert all(event_fingerprint(item, "2026-07-27") not in state["pending"]["2026-07-27"]
           for item in excluded_events)
```

Add a force regeneration test that deletes the rule file after the first run and proves the previously filtered source event returns. Add an all-filtered direct draft assertion for `events == []` and `journal_md == "## 2026-07-27"`. Snapshot existing bundle/state bytes, then provide a valid first rule followed by an invalid regex and assert `SystemExit`/`RuleFileError`, unchanged bytes, and no output/temp directory when there was no destination. Assert existing output without `--force` is refused before rules or database reads.

In `test_bundle.py`, use a source row where title is `"Visible title"` and narrative contains a regex-only sentinel; assert the projected event contains only the title. This guards Task 1’s matcher integration against bypassing frozen-v1 projection.

- [ ] **Step 2: Run direct-draft tests and verify RED**

Run:

```bash
python -m pytest -q packages/memberkit/tests/test_exclusions.py packages/memberkit/tests/test_cli.py packages/memberkit/tests/test_bundle.py packages/memberkit/tests/test_state.py
```

Expected: the new CLI tests fail because all projected events are sent directly to `DraftState.refresh()` and no excluded count is printed.

- [ ] **Step 3: Insert the rules snapshot at the direct generation boundary**

Import the exclusions module in `cli.py`. Preserve the existing refusal check before any rules/source read. For a missing or forced output, load all rules, project through the existing bundle function, validate the whole unfiltered frozen-v1 payload, then replace only its event list before `DraftState.refresh()`:

```python
rules = exclusions.load_rules(exclusions.rules_path(cfg.workdir))
data = bundle.draft(cfg.db, cfg.member, date_text,
                    all_observations=args.all, timezone=timezone)
bundle.validate_bundle(data, cfg.member, date_text)
result = exclusions.apply_rules(data["events"], rules)
data["events"] = DraftState(cfg.workdir / "state.json").refresh(
    date_text, result.included, current=None
)
data["journal_md"] = bundle.render_journal(data["events"], date_text)
bundle.validate_bundle(data, cfg.member, date_text)
bundle.write_bundle(out, data)
print(f"excluded {result.excluded_count} events")
```

Keep the existing state-first then atomic-bundle-write order. Do not load rules in `review`, `push`, or `dismiss`; those commands retain their current event semantics.

- [ ] **Step 4: Run direct-draft tests and verify GREEN**

Run:

```bash
python -m pytest -q packages/memberkit/tests/test_exclusions.py packages/memberkit/tests/test_cli.py packages/memberkit/tests/test_bundle.py packages/memberkit/tests/test_state.py
```

Expected: direct draft validates before matching, prints a count without summaries, preserves existing unforced bytes, produces an empty valid direct bundle when all events match, and records only included fingerprints.

- [ ] **Step 5: Commit direct-draft integration (only after separate authorization)**

```bash
git add packages/memberkit/memberkit/cli.py packages/memberkit/tests/test_cli.py packages/memberkit/tests/test_bundle.py
git commit -m "feat(memberkit): filter newly generated drafts"
```

Do not push.

### Task 3: Apply One Rules Snapshot to Newly Scheduled Drafts

**Files:**
- Modify: `packages/memberkit/memberkit/schedule.py:254-353`
- Modify: `packages/memberkit/tests/test_schedule.py`
- Modify: `packages/memberkit/tests/test_schedule_runtime.py`
- Test: `packages/memberkit/tests/test_schedule_windows.py`

**Interfaces:**
- Consumes: Task 1 `tuple[ExclusionRule, ...]` and `ExclusionResult`.
- Produces privately: `PendingPreparation(pending_dates: list[str], excluded_counts: tuple[tuple[str, int], ...])` from `_prepare_pending(config, now, timezone, rules)`.
- Preserves publicly: `scheduled_run(config, now=None, notify=True, timezone=None, *, platform=None, macos_runner=None, windows_api=None, windows_runner=None) -> list[str]`.

- [ ] **Step 1: Write failing schedule, byte-preservation, and Windows log tests**

Extend scheduled fixtures so yesterday contains an excluded and an included event, today has only excluded events, and a pre-existing valid/member-edited/malformed file exists for one candidate date. Assert only missing yesterday is written, all-filtered today has no bundle/state pending entry, and existing file bytes remain exactly unchanged.

```python
pending = scheduled_run(cfg, datetime(2026, 7, 28, 17, 30), notify=False)
assert pending == ["2026-07-27"]
assert summaries(created_yesterday) == ["included scheduled event"]
assert not (cfg.workdir / "out" / "bundle-alex-2026-07-28.json").exists()
assert existing.read_bytes() == member_edited_bytes
```

Write an invalid rule file before the run, seed prior state and two output bytes, monkeypatch `_notify_pending`, and assert it raises before either date changes or notification occurs. Add a Windows test asserting `schedule.log` contains `excluded=1` but not `"private summary"`; `schedule.err` must still contain only exception class/category. Retain the existing launchd/Task Scheduler tests asserting argument list equals `[..., "scheduled-run"]`.

- [ ] **Step 2: Run scheduler tests and verify RED**

Run:

```bash
python -m pytest -q packages/memberkit/tests/test_schedule.py packages/memberkit/tests/test_schedule_runtime.py packages/memberkit/tests/test_schedule_windows.py
```

Expected: new tests fail because the scheduler neither loads a snapshot nor filters generated event arrays/counts.

- [ ] **Step 3: Load once, filter only missing drafts, and log only counts**

At the beginning of `scheduled_run()`, after timezone normalization and before `_prepare_pending`, load once:

```python
rules = exclusions.load_rules(exclusions.rules_path(config.workdir))
prepared = _prepare_pending(config, normalized_now, timezone, rules)
pending_dates = [date for date in prepared.pending_dates if _is_strict_iso_date(date)]
```

Within the missing-file branch of `_prepare_pending`, keep `bundle.draft()` and its first `validate_bundle()` call unchanged, filter `discovered["events"]`, and pass `result.included` to `state.refresh()`. If the returned events are empty, do not create a bundle; capture the count for operational output anyway. Do not load/apply rules for an existing file, but only after successful invocation-level rule validation may the existing `state.refresh(..., current=current)` behavior run.

For each non-Windows run, print `f"{date_text}: excluded {count} events"` for every generated/missing candidate to normal scheduled process output. For Windows, add one date-to-count entry per generated/missing candidate to the existing bounded success record without member, project, summary, or rule text:

```python
excluded = ",".join(
    f"{date_text}:{count}"
    for date_text, count in prepared.excluded_counts
) or "none"
f"invoked={invoked} dates={rendered_dates} excluded={excluded}"
```

Do not alter the public return list, notifications, scheduling definitions, or lazy platform-backend imports.

- [ ] **Step 4: Run scheduler tests and verify GREEN**

Run:

```bash
python -m pytest -q packages/memberkit/tests/test_exclusions.py packages/memberkit/tests/test_schedule.py packages/memberkit/tests/test_schedule_runtime.py packages/memberkit/tests/test_schedule_windows.py
```

Expected: one invalid rules snapshot prevents all state/bundle writes; missing drafts filter consistently; all-filtered scheduled dates remain unwritten; existing drafts remain byte-preserved; Windows receives safe count-only logging; native schedule actions are unchanged.

- [ ] **Step 5: Commit scheduled integration (only after separate authorization)**

```bash
git add packages/memberkit/memberkit/schedule.py packages/memberkit/tests/test_schedule.py packages/memberkit/tests/test_schedule_runtime.py
git commit -m "feat(memberkit): apply exclusions to scheduled drafts"
```

Do not push.

### Task 4: Read-Only Inspection Commands and Member Documentation

**Files:**
- Modify: `packages/memberkit/memberkit/cli.py:19-58,114-182`
- Modify: `packages/memberkit/tests/test_cli.py`
- Modify: `packages/memberkit/README.md`
- Modify: `docs/member-guide.md`
- Modify: `README.md`
- Modify: `docs/privacy.md`

**Interfaces:**
- Consumes: Task 1 parser/matcher and unchanged `bundle.draft()`/`validate_bundle()` for preview.
- Produces: `memberkit exclusions list` and `memberkit exclusions preview [--date YYYY-MM-DD]` CLI commands.
- Produces list output: resolved rules path plus source-order `"{ordinal}  {rule.normalized()}"` lines.
- Produces preview output: rules path, `eligible`, one `rule <ordinal> excluded <count>` per rule, `excluded`, and `remaining`; it prints no matched events.

- [ ] **Step 1: Write failing CLI side-effect and output tests**

Add parser/dispatch tests using a temporary `Config` and isolated `cfg.workdir`. For a missing file, assert `list` prints the resolved `exclude-projects.txt` path and `0 rules` without creating the workdir or opening the database. For valid rules, assert normalized ordered output. For preview, snapshot non-existent or seeded bundle/state paths, monkeypatch `bundle.write_bundle`, `DraftState.refresh`, `schedule._notify_pending`, and `memberkit.push.push` to fail if called, then assert deterministic count-only output:

```python
assert cli.main(["exclusions", "preview", "--date", "2026-07-27"]) == 0
captured = capsys.readouterr().out
assert "eligible 4" in captured
assert "rule 1 excluded 2" in captured
assert "excluded 3" in captured and "remaining 1" in captured
assert "private observation summary" not in captured
assert state_path.read_bytes() == original_state
assert out_path.read_bytes() == original_bundle
```

Add a timezone default test by monkeypatching `cli.datetime` or supplying a fixed configured `ZoneInfo`, then assert omitted `--date` calls `bundle.draft()` with the member-local calendar date. Add help/import regression tests for `memberkit exclusions --help` on simulated Darwin, Windows, and Linux imports without loading a platform backend.

- [ ] **Step 2: Run CLI tests and verify RED**

Run:

```bash
python -m pytest -q packages/memberkit/tests/test_cli.py packages/memberkit/tests/test_exclusions.py
```

Expected: argparse rejects the `exclusions` command.

- [ ] **Step 3: Add parser/dispatch and write the exact user-facing documentation**

Add an `exclusions` parent parser with required `list` and `preview` subcommands; give only preview the existing `--date` shape. Handle these commands after `Config.load()` but before draft output-path/state handling. `list` calls only `rules_path`/`load_rules`; preview calls only `load_rules`, confirms `cfg.db` exists, calls `bundle.draft(cfg.db, cfg.member, date_text, timezone=timezone)`, validates it, applies rules, and prints counts.

Document this exact member workflow in the package README and member guide:

```text
Create MEMBERKIT_WORKDIR/exclude-projects.txt with one exact project, trailing-star
project prefix, or `project ~ regular-expression` per line. Exact/prefix projects
are case-sensitive. Regex project matching is case-sensitive; its search against
the final frozen-v1 summary is case-insensitive. Preview rules before relying on
an unattended schedule: `memberkit exclusions preview --date YYYY-MM-DD`.
```

Document that rules affect newly generated or forced drafts only; `draft --all` also filters; direct all-filtered drafts are valid empty files whereas scheduled all-filtered dates create no pending draft; review/push do not retroactively filter. State that only a released-package verification may replace the wrapper or reinstall an existing wrapper-based schedule, while ordinary rule edits require no reinstall. In `docs/privacy.md`, state summary-only matching, count-only command/log output, no automatic push, and the local trusted-rule/pathological-regex boundary. In root README, add a concise MemberKit exclusion capability sentence linking to `docs/member-guide.md`. Do not create or modify a changelog because none exists; include release-note/migration requirements in release handoff.

- [ ] **Step 4: Run CLI and documentation-adjacent regression tests and verify GREEN**

Run:

```bash
python -m pytest -q packages/memberkit/tests/test_exclusions.py packages/memberkit/tests/test_cli.py packages/memberkit/tests/test_bundle.py packages/memberkit/tests/test_schedule.py packages/memberkit/tests/test_schedule_runtime.py
```

Expected: list and preview are deterministic and side-effect free, output is count-only, help remains import-safe, direct/scheduled behavior remains covered, and no existing review/push tests regress.

- [ ] **Step 5: Commit CLI inspection and documentation (only after separate authorization)**

```bash
git add packages/memberkit/memberkit/cli.py packages/memberkit/tests/test_cli.py \
  packages/memberkit/README.md docs/member-guide.md README.md docs/privacy.md
git commit -m "feat(memberkit): add exclusion inspection commands"
```

Do not push.

## Full Verification and Release Boundary

- [ ] **Step 1: Install the same editable packages used by CI**

```bash
python -m pip install -e '.[dev]'
python -m pip install -e 'packages/memberkit[dev]'
```

- [ ] **Step 2: Run the complete local CI-equivalent suites**

```bash
python -m pytest -q packages/memberkit/tests
python -m pytest -q tests --ignore=tests/test_memberkit_integration.py
python -m pytest -q tests/test_memberkit_integration.py
./scripts/check-public.sh
```

Expected: report the new result explicitly against the pre-change baseline `1014 passed, 1 skipped`; do not claim platform smoke from compilation alone.

- [ ] **Step 3: Run the Windows CI subset and smoke only on Windows**

```powershell
python -m pytest -q packages/memberkit/tests -k windows
python scripts/memberkit-windows-schedule-smoke.py
python scripts/memberkit-windows-schedule-smoke.py --cleanup-only
```

Expected: task definitions still invoke only `scheduled-run`; cleanup executes even if the smoke fails. Record a sandbox/platform limitation if Windows is unavailable rather than claiming this ran.

- [ ] **Step 4: Keep deployment separate from code verification**

Do not touch `~/.memberkit/exclude-projects.txt`, current drafts/state, the wrapper, or launchd as part of code merge/package publication. After a released package exists, use a controlled environment to compare `memberkit exclusions list` and `memberkit exclusions preview` with wrapper intent, test a disposable/new-date draft, then obtain explicit authorization before replacing the wrapper or reinstalling an existing wrapper-based schedule.

## Self-Review

- **Spec coverage:** Task 1 covers file grammar, full-file validation, normalization, first-match counting, no-summary disclosure, and frozen-summary-only matching. Task 2 covers direct/default/`--all`/force order, empty direct output, state reversibility, and no retroactive non-force behavior. Task 3 covers one scheduler snapshot, missing-date filtering, existing-file preservation, no empty scheduled file, stable return contract, safe count logs, notification failure boundary, and native actions. Task 4 covers read-only list/preview, timezone defaults, output privacy, documentation, no schema/changelog modification, and release migration. Full verification covers suite, integration, public scan, and Windows smoke. No design requirement is left unassigned.
- **Placeholder scan:** This document contains no `TODO`, `TBD`, “implement later,” “add appropriate error handling,” or undefined interface references. Each task gives explicit files, interfaces, RED/GREEN commands, implementation snippets, and a task-scoped conventional commit command.
- **Type consistency:** Every integration consumes `tuple[ExclusionRule, ...]` from `load_rules()` and `ExclusionResult.included`, `.excluded_count`, `.rule_counts` from `apply_rules()`. `scheduled_run()` remains `-> list[str]`; only private `_prepare_pending()` returns `PendingPreparation`.

Plan complete and saved to `docs/superpowers/plans/2026-08-04-memberkit-project-exclusions.md`. Execution requires separate authorization before any commit, and no push is authorized.
