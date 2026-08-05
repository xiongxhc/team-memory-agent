# Rolling Weekly Synthesis and Journal Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make current-week team reports useful every evening and reduce the observed 75-minute journal run without removing or compacting evidence.

**Architecture:** Separate pure report provenance from storage and rendering; isolate daily cache identity to one person-day; run only LLM calls in a bounded pool while all SQLite work stays on the main thread. Add capture-only orchestration and a ledger-wide run lock, then reconcile previous/current reports in every full run.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, `concurrent.futures`, `fcntl`/`msvcrt`, pytest, existing Anthropic HTTP and Claude CLI adapters.

**Spec:** [`2026-08-05-rolling-weekly-synthesis-performance-design.md`](../specs/2026-08-05-rolling-weekly-synthesis-performance-design.md)

## Global Constraints

- Keep every ledger event and the complete ordered person-day event text; no ranking, cap, truncation, pre-summary, batching, model switch, or compaction.
- `TEAMMEM_LLM_CONCURRENCY` defaults to `2`; valid values are integers `1..8`.
- Worker threads call only the LLM callable; all SQLite reads/writes stay on the main thread.
- Full-mode journal bounds are the previous report Monday through the operator-local current date; connector lookback and standalone `journal --since-days` remain unchanged.
- Public `schedule install` still creates one full daily run. Extra capture jobs are operator-owned and never installed implicitly.
- Preserve full-mode connector/import exit semantics; capture-only treats enabled connector/import failures as non-zero while still reclaiming and snapshotting.
- Commit commands in this plan are checkpoint suggestions only. Do not stage or commit until the user explicitly authorizes `commit`.
- Do not modify the dirty `feat/mr-commit-backfill` checkout or the private `local-agent-team` deployment during this public-engine plan.

## File Structure

- Create `teammem/run_lock.py`: canonical cross-platform process lock.
- Create `teammem/telemetry.py`: structured, content-free progress and percentile helpers.
- Modify `teammem/config.py`: bounded LLM concurrency configuration.
- Modify `teammem/store.py`: idempotent summary provenance migration and atomic summary records.
- Modify `teammem/queries.py`: report state, cutoff precision, and effective flags.
- Modify `teammem/summarize.py`: independent versions, prepared daily inputs, weekly canonical identity.
- Modify `teammem/services.py`: concurrent journal executor and provenance-aware report executor.
- Modify `teammem/render.py`: stored coverage/legacy warning and shared flags rendering.
- Modify `teammem/daily.py`: capture-only/full modes, progress events, and two-week report orchestration.
- Modify `teammem/cli.py`: `--capture-only` and streaming progress.
- Create `tests/test_run_lock.py` and `tests/test_telemetry.py`; extend focused existing test modules.
- Modify `README.md`, `docs/architecture.md`, `docs/deployment.md`, and `docs/privacy.md` only where behavior changes.

---

### Task 1: Atomic summary provenance and old-schema migration

**Files:**
- Modify: `teammem/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces `SummaryRecord`, `get_summary()`, and `put_summary()` for later journal, report, and renderer tasks.
- Keeps `get_or_make()` source-compatible for existing callers and nullable daily provenance.

- [ ] **Step 1: Write failing migration and record tests**

Add tests that hand-create the old `summaries` schema, insert a legacy weekly row, call `open_db()`, and assert data retention plus these nullable columns:

```python
PROVENANCE = {
    "evidence_cutoff", "cutoff_precision", "coverage_state",
    "source_input_hash", "effective_flags_json",
}

def test_open_db_migrates_legacy_summaries_once(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE summaries (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, input_hash TEXT NOT NULL, text TEXT NOT NULL, model TEXT NOT NULL, created_ts TEXT NOT NULL, UNIQUE(kind,key))")
    conn.execute("INSERT INTO summaries(kind,key,input_hash,text,model,created_ts) VALUES('weekly-team','team|2026-07-27','h','old','m','t')")
    conn.commit(); conn.close()

    upgraded = open_db(path)
    columns = {row[1] for row in upgraded.execute("PRAGMA table_info(summaries)")}
    assert PROVENANCE <= columns
    assert get_summary(upgraded, "weekly-team", "team|2026-07-27").evidence_cutoff is None
    upgraded.close()
    open_db(path).close()
```

Add an atomic round-trip test for all fields of `SummaryRecord` and retain existing `get_or_make()` hit/miss tests.

- [ ] **Step 2: Run tests and verify RED**

Run: `../../.venv/bin/python -m pytest tests/test_store.py -q`

Expected: import/column assertions fail because provenance APIs do not exist.

- [ ] **Step 3: Implement the minimal schema and APIs**

Add nullable columns to `_SCHEMA`, then idempotently upgrade old ledgers inside one transaction using `PRAGMA table_info` and `ALTER TABLE ... ADD COLUMN`.

```python
@dataclass(frozen=True)
class SummaryRecord:
    kind: str
    key: str
    input_hash: str
    text: str
    model: str
    created_ts: str
    evidence_cutoff: str | None = None
    cutoff_precision: str | None = None
    coverage_state: str | None = None
    source_input_hash: str | None = None
    effective_flags_json: str | None = None

def get_summary(conn: sqlite3.Connection, kind: str, key: str) -> SummaryRecord | None: ...
def put_summary(conn: sqlite3.Connection, record: SummaryRecord) -> None: ...
```

`put_summary()` must upsert text and every provenance field in one `with conn:` block. Implement `get_or_make()` by reading/writing `SummaryRecord` with null provenance.

- [ ] **Step 4: Run focused and compatibility tests**

Run: `../../.venv/bin/python -m pytest tests/test_store.py tests/test_summarize.py -q`

Expected: PASS.

- [ ] **Step 5: Record the checkpoint command without executing it**

```bash
git add teammem/store.py tests/test_store.py
git commit -m "feat: store report provenance atomically"
```

---

### Task 2: Pure report state, cutoff, and effective flags

**Files:**
- Modify: `teammem/queries.py`
- Test: `tests/test_queries.py`

**Interfaces:**
- Consumes a target Monday, operator-local date, and included `(person, day)` keys.
- Produces one `ReportContext` used unchanged by weekly synthesis and stored for renderer use.

- [ ] **Step 1: Write failing report-context tests**

Define expected public types in tests:

```python
@dataclass(frozen=True)
class ReportState:
    target_monday: date
    coverage_state: str               # provisional | friday-checkpoint
    evidence_cutoff: str | None
    cutoff_precision: str             # instant | date | none
    cutoff_note: str | None

@dataclass(frozen=True)
class ReportContext:
    state: ReportState
    effective_flags: dict
```

Test:

- Monday–Thursday suppress `gaps` and `concentration` but preserve `unmapped` and `unmapped_channels`.
- Friday, Saturday, Sunday, and any previous week use `friday-checkpoint` and full flags.
- `2026-08-04T23:30:00-04:00` is chronologically later than `2026-08-05T01:00:00+04:00` despite lexical/date order.
- A naive timestamp on the latest included day yields date precision plus `some source timestamps omit offsets`.
- No included person-days yields cutoff `None` and precision `none`.
- Year-boundary target weeks remain correct.

- [ ] **Step 2: Run tests and verify RED**

Run: `../../.venv/bin/python -m pytest tests/test_queries.py -q`

Expected: imports fail for the new context API.

- [ ] **Step 3: Implement the pure context helpers**

```python
def report_context(
    conn: sqlite3.Connection,
    target_monday: date,
    operator_date: date,
    ids: IdentityMaps,
    included_person_days: set[tuple[str, str]],
) -> ReportContext: ...
```

Parse `Z` as `+00:00`; compare aware timestamps after UTC normalization. Never assign a timezone to naive bundle timestamps. Restrict cutoff evidence to included person-days. Return copied/filtered flags—never mutate `flags()` output.

- [ ] **Step 4: Run focused tests**

Run: `../../.venv/bin/python -m pytest tests/test_queries.py tests/test_render.py -q`

Expected: PASS with existing render behavior unchanged.

- [ ] **Step 5: Record the checkpoint command without executing it**

```bash
git add teammem/queries.py tests/test_queries.py
git commit -m "feat: derive report coverage and effective flags"
```

---

### Task 3: Run lock, concurrency configuration, and telemetry primitives

**Files:**
- Create: `teammem/run_lock.py`
- Create: `teammem/telemetry.py`
- Modify: `teammem/config.py`
- Create: `tests/test_run_lock.py`
- Create: `tests/test_telemetry.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces `acquire_run_lock()` for Task 7.
- Produces `Config.llm_concurrency: int` for Task 4.
- Produces `Reporter`, `ProgressEvent`, `Distribution`, and `nearest_rank()` for Tasks 4 and 7.

- [ ] **Step 1: Write failing lock and configuration tests**

```python
class RunLockedError(RuntimeError): ...

def acquire_run_lock(
    ledger_path: Path,
    *,
    wait_seconds: float,
    on_wait: Callable[[str], None] | None = None,
    platform: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[None]: ...
```

Test canonical realpath aliases map to the same adjacent lock; Unix contention in a child process; Windows one-byte locking through injected fake primitives; release after exception; capture-style `wait_seconds=0`; full-style bounded wait; and the exact content-free error `another run is active`.

In `test_config.py`, assert missing configuration gives `2`, values `1` and `8` load, and `0`, `9`, or non-integers raise `ValueError("TEAMMEM_LLM_CONCURRENCY must be an integer from 1 to 8")`.

In `test_telemetry.py`, require stable content-free lines and nearest-rank behavior:

```python
@dataclass(frozen=True)
class ProgressEvent:
    event: str
    stage: str | None = None
    fields: tuple[tuple[str, str | int | float | bool | None], ...] = ()

@dataclass(frozen=True)
class Distribution:
    count: int
    p50: float | None
    p95: float | None
    maximum: float | None

Reporter = Callable[[ProgressEvent], None]

def noop_reporter(event: ProgressEvent) -> None: ...
def nearest_rank(values: Sequence[float], percentile: int) -> float | None: ...
def distribution(values: Sequence[float]) -> Distribution: ...
def stream_reporter(run_id: str, stream: TextIO) -> Reporter: ...
```

Zero samples return null percentiles; one sample repeats that value; two samples use the nearest-rank definition. `stream_reporter` sorts field names, flushes each line, and never receives prompts or secrets.

- [ ] **Step 2: Run tests and verify RED**

Run: `../../.venv/bin/python -m pytest tests/test_run_lock.py tests/test_config.py tests/test_telemetry.py -q`

Expected: missing module/field failures.

- [ ] **Step 3: Implement the platform-isolated lock and bounded config**

Use lazy `fcntl`/`msvcrt` imports so Windows never imports Unix-only modules and vice versa. Keep the file descriptor open for the context lifetime; canonicalize Windows paths with `normcase(realpath(...))`; use injected clock/sleep for deterministic wait tests.

Add `llm_concurrency: int = 2` to `Config` and a `_bounded_integer(values, key, default, minimum, maximum)` loader. Implement telemetry as immutable values and pure formatting; no service imports belong in `telemetry.py`.

- [ ] **Step 4: Run lock, config, and Windows import tests**

Run: `../../.venv/bin/python -m pytest tests/test_run_lock.py tests/test_config.py tests/test_telemetry.py tests/test_schedule_windows.py tests/test_windows_schedule_smoke.py -q`

Expected: PASS.

- [ ] **Step 5: Record the checkpoint command without executing it**

```bash
git add teammem/run_lock.py teammem/telemetry.py teammem/config.py tests/test_run_lock.py tests/test_telemetry.py tests/test_config.py
git commit -m "feat: add run locking and synthesis telemetry"
```

---

### Task 4: Person-day-local cache identity and concurrent journal execution

**Files:**
- Modify: `teammem/summarize.py`
- Modify: `teammem/services.py`
- Modify: `teammem/slices.py`
- Test: `tests/test_summarize.py`
- Test: `tests/test_services.py`
- Test: `tests/test_slices.py`

**Interfaces:**
- Produces `PreparedDailyJournal` and `JournalRunResult` for Tasks 5 and 7.
- Keeps direct `daily_person_journal()` and CLI `run_journal()` behavior compatible.

- [ ] **Step 1: Write failing cache-locality and migration tests**

Define:

```python
DAILY_PROMPT_VERSION = "2"
DAILY_HASH_SCHEMA_VERSION = "local-projects-v1"
LEGACY_DAILY_PROMPT_VERSION = "2"

@dataclass(frozen=True)
class PreparedDailyJournal:
    person: str
    day: str
    key: str
    user_prompt: str
    input_hash: str
    legacy_input_hash: str
    event_count: int
    prompt_bytes: int

@dataclass(frozen=True)
class JournalFailure:
    person: str
    day: str
    detail: str

@dataclass(frozen=True)
class JournalMetrics:
    pairs: int
    cached: int
    migrated: int
    llm_calls: int
    concurrency: int
    prompt_events: Distribution
    prompt_bytes: Distribution
    queue_wait_seconds: Distribution
    backend_seconds: Distribution
    elapsed_seconds: float

@dataclass(frozen=True)
class JournalRunResult:
    metrics: JournalMetrics
    failures: tuple[JournalFailure, ...]

    @property
    def failed_person_days(self) -> tuple[tuple[str, str], ...]: ...

    @property
    def exit_code(self) -> int: ...
```

Tests must prove:

- adding project B to another person/day leaves project A's new hash unchanged;
- the LLM prompt still contains every ordered event line;
- a matching current-global legacy hash migrates without calling LLM;
- a legacy cache created with an older unknown global list safely regenerates;
- daily and report versions invalidate only their own kind;
- same-day distinct projects are the only `Known project names`.

- [ ] **Step 2: Run cache tests and verify RED**

Run: `../../.venv/bin/python -m pytest tests/test_summarize.py tests/test_slices.py -q`

Expected: new types/functions missing and unrelated project still changes the hash.

- [ ] **Step 3: Implement preparation and safe migration**

Add a person-day project query to `slices.py`. Build the canonical hash from both daily version constants, identity/date, local projects, and complete slice. Compute the pinned legacy hash from the supplied global project list only for best-effort matching. Migrate matching rows with a main-thread `put_summary()`; regenerate unverifiable rows.

- [ ] **Step 4: Write failing bounded-concurrency tests**

Use a barrier/locked fake LLM to assert maximum active calls equals `cfg.llm_concurrency`, SQLite hooks run only on the caller thread, successes persist in sorted `(person, day)` order, all futures drain, and one failed pair does not discard sibling successes. Assert prompt distributions include all prepared pairs while queue/backend distributions include only genuine calls.

- [ ] **Step 5: Run concurrency tests and verify RED**

Run: `../../.venv/bin/python -m pytest tests/test_services.py -q`

Expected: serial implementation violates the two-active-call assertion and has no rich result.

- [ ] **Step 6: Implement the three-phase executor**

Add:

```python
def execute_journal(
    cfg: Config,
    ids: IdentityMaps,
    *,
    start_day: str,
    end_day: str,
    created_ts: str,
    conn: sqlite3.Connection,
    llm: LLM,
    reporter: Reporter = noop_reporter,
    monotonic: Callable[[], float] = time.monotonic,
) -> JournalRunResult: ...
```

Its phases are:

1. prepare/cache/migrate serially;
2. submit only genuine misses to `ThreadPoolExecutor(max_workers=cfg.llm_concurrency)`;
3. drain all futures, collect queue/backend timing, and persist successes serially in key order;
4. aggregate redacted failures by person-day;
5. emit content-free progress through an optional reporter.

Keep `run_journal()` as the printing/exit-code wrapper used by the standalone CLI. Do not add retries or multi-person batches.

- [ ] **Step 7: Run focused journal tests**

Run: `../../.venv/bin/python -m pytest tests/test_slices.py tests/test_summarize.py tests/test_services.py tests/test_cli.py -q`

Expected: PASS.

- [ ] **Step 8: Record the checkpoint command without executing it**

```bash
git add teammem/slices.py teammem/summarize.py teammem/services.py tests/test_slices.py tests/test_summarize.py tests/test_services.py
git commit -m "perf: isolate and parallelize journal synthesis"
```

---

### Task 5: Provenance-aware weekly report service

**Files:**
- Modify: `teammem/summarize.py`
- Modify: `teammem/services.py`
- Test: `tests/test_summarize.py`
- Test: `tests/test_services.py`

**Interfaces:**
- Consumes `SummaryRecord`, `ReportContext`, and canonical daily input hashes.
- Produces `ReportRunResult` for Task 7 and a stored weekly record for Task 6.

- [ ] **Step 1: Write failing weekly identity and prompt tests**

```python
REPORT_PROMPT_VERSION = "3"

@dataclass(frozen=True)
class DailySummaryInput:
    person: str
    day: str
    input_hash: str
    text: str

@dataclass(frozen=True)
class ReportRunResult:
    target_monday: date
    status: str               # generated | cached | skipped | failed
    detail: str
    elapsed_seconds: float
```

Assert the weekly user prompt includes report state, cutoff/precision note, effective flags, and all daily text. Assert `source_input_hash` changes when a daily canonical hash changes even if daily text is identical. Assert Friday state change invalidates only the weekly cache.

- [ ] **Step 2: Run tests and verify RED**

Run: `../../.venv/bin/python -m pytest tests/test_summarize.py tests/test_services.py -q`

Expected: weekly input still drops daily hashes/provenance.

- [ ] **Step 3: Implement canonical weekly synthesis**

Change the pure interface to:

```python
def weekly_team_report(
    conn: sqlite3.Connection,
    *,
    monday_iso: str,
    dailies: Sequence[DailySummaryInput],
    context: ReportContext,
    llm: LLM,
    model: str,
    created_ts: str,
) -> SummaryRecord: ...
```

Hash canonical JSON of sorted `(person, day, input_hash, text)` plus canonical effective flags into `source_input_hash`. Hash report version + source hash + coverage metadata into final `input_hash`. On miss, invoke LLM, prepend the deterministic provisional/checkpoint line, and `put_summary()` with all provenance and canonical `effective_flags_json` in one transaction.

- [ ] **Step 4: Implement the revised report service**

```python
def execute_report(
    cfg: Config,
    ids: IdentityMaps,
    *,
    target_week: date,
    operator_date: date,
    conn: sqlite3.Connection,
    llm: LLM,
    monotonic: Callable[[], float] = time.monotonic,
) -> ReportRunResult: ...
```

Normalize `target_week` with `week_monday`; load daily `key,input_hash,text`; build `report_context()` from exactly those person-days. No dailies returns `skipped` and writes nothing. Keep `run_report()` as the standalone printing/exit wrapper; CLI `--week-of` supplies target while actual local today supplies operator date.

- [ ] **Step 5: Run focused report tests**

Run: `../../.venv/bin/python -m pytest tests/test_summarize.py tests/test_services.py tests/test_cli.py -q`

Expected: PASS.

- [ ] **Step 6: Record the checkpoint command without executing it**

```bash
git add teammem/summarize.py teammem/services.py tests/test_summarize.py tests/test_services.py tests/test_cli.py
git commit -m "feat: generate rolling reports with provenance"
```

---

### Task 6: Stored coverage, legacy warnings, and shared Flags rendering

**Files:**
- Modify: `teammem/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes weekly `SummaryRecord` provenance/effective flags from Tasks 1 and 5.
- Preserves Person pages, project pages, raw evidence appendix, and reference links.

- [ ] **Step 1: Write failing renderer tests**

Add fixtures for:

- a new weekly record whose stored coverage line remains unchanged after a later ledger event;
- a legacy null-provenance report rendering `> Legacy report — exact event cutoff unknown.`;
- provisional stored flags that hide gap/concentration, preserve unmapped data, and add the deferred-check notice;
- Friday/checkpoint stored flags rendering full gaps/concentration;
- byte-stable repeated render and existing appendix/reference behavior.

- [ ] **Step 2: Run tests and verify RED**

Run: `../../.venv/bin/python -m pytest tests/test_render.py -q`

Expected: renderer cannot see provenance and recomputes full flags.

- [ ] **Step 3: Implement provenance-aware rendering**

Use `get_summary()` for weekly rows. New rows already contain their coverage line and canonical `effective_flags_json`; legacy rows receive only the explicit unknown-cutoff warning. Never derive a legacy cutoff from current events. Render the stored effective flags for synthesized pages; when provisional, append `Gap and concentration checks are deferred until the Friday checkpoint.`

Keep daily summary text lookup and report/no-report appendix ordering otherwise unchanged.

- [ ] **Step 4: Run render and query tests**

Run: `../../.venv/bin/python -m pytest tests/test_render.py tests/test_queries.py -q`

Expected: PASS.

- [ ] **Step 5: Record the checkpoint command without executing it**

```bash
git add teammem/render.py tests/test_render.py
git commit -m "feat: render report provenance and provisional flags"
```

---

### Task 7: Capture-only, rolling orchestration, run progress, and CLI

**Files:**
- Modify: `teammem/daily.py`
- Modify: `teammem/cli.py`
- Test: `tests/test_daily.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes `acquire_run_lock()`, `execute_journal()`, and `execute_report()`.
- Keeps top-level `DailyResult.step("report")` compatibility while adding stable previous/current subresults.

- [ ] **Step 1: Write failing capture-only and lock tests**

Extend `run_daily()` contract:

```python
def run_daily(
    cfg, ids, settings, now, *,
    connectors=None,
    capture_only: bool = False,
    lock_factory=acquire_run_lock,
    reporter: Reporter = noop_reporter,
    monotonic: Callable[[], float] = time.monotonic,
) -> DailyResult: ...
```

Test capture-only runs connector/import/reclaim/configured snapshot, never resolves LLM or renders/pushes, and marks journal/report/docs-sync/render/push `skipped: capture-only`. Connector/import failure returns `1` but snapshot remains; full mode retains current `0`. Assert capture requests zero lock wait, full requests 1800 seconds, and collision occurs before `open_db()`.

- [ ] **Step 2: Write failing rolling-report tests**

Replace the Friday-only regression with:

- Monday full mode calls previous then current report weeks;
- journal bounds are previous Monday through operator-local current day;
- a failed person-day blocks only its containing report week;
- previous/current exceptions create stable subresults while the report stage stays non-fatal;
- missing backend skips both;
- local-offset and year-boundary dates are correct.

Add `subresults: tuple[StepResult, ...] = ()` to the expected `StepResult` interface.

- [ ] **Step 3: Run daily tests and verify RED**

Run: `../../.venv/bin/python -m pytest tests/test_daily.py -q`

Expected: no capture flag, lock, rolling report, or subresults.

- [ ] **Step 4: Implement daily orchestration**

Acquire the canonical ledger lock around every stage and close the connection before release. Capture branches after reclaim, appends explicit skipped synthesis/publication stages, and reaches snapshot. `_daily_result(..., capture_only=True)` treats enabled connector/import failures as fatal only in capture mode.

Full mode calls `execute_journal()` with explicit bounds, resolves report backend once, and independently attempts previous/current weeks unless their person-days failed. Aggregate their stable subresults under one top-level `report` step; remove the Friday gate.

Emit content-free run/stage/progress events immediately through `reporter`; include elapsed stage timing without adding it to vault files.

- [ ] **Step 5: Add CLI tests and implement CLI wiring**

Replace the bare parser with:

```python
p_daily = sub.add_parser("run-daily", help="run configured hub stages once")
p_daily.add_argument("--capture-only", action="store_true",
                     help="capture, import, reclaim, and snapshot without synthesis or publication")
```

Pass `capture_only`; create a content-free run ID; stream reporter lines to flushing stderr before the final `_print_daily()` result. Keep scheduled command generation unchanged (`run-daily` means full).

- [ ] **Step 6: Run orchestration, CLI, and scheduling regressions**

Run: `../../.venv/bin/python -m pytest tests/test_daily.py tests/test_cli.py tests/test_run_lock.py tests/test_schedule.py tests/test_schedule_windows.py tests/test_windows_schedule_smoke.py -q`

Expected: PASS.

- [ ] **Step 7: Record the checkpoint command without executing it**

```bash
git add teammem/daily.py teammem/cli.py tests/test_daily.py tests/test_cli.py
git commit -m "feat: add capture-only and rolling daily orchestration"
```

---

### Task 8: Public documentation, quality verification, and performance evidence

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/deployment.md`
- Modify: `docs/privacy.md`
- Test: `tests/test_public_scan.py` only if wording requires fixture updates

**Interfaces:**
- Documents public behavior only. The private two-LaunchAgent cutover is a separate post-release plan in `local-agent-team`.

- [ ] **Step 1: Update public documentation**

Document:

- full `run-daily` versus `run-daily --capture-only`;
- one default scheduled full run and explicit optional capture triggers;
- capture snapshot/non-zero failure behavior and no partial vault publication;
- cross-mode lock, full 30-minute wait, and capture fail-fast behavior;
- rolling provisional/Friday-checkpoint reports and previous-week reconciliation;
- `TEAMMEM_LLM_CONCURRENCY=2` and range `1..8`;
- no evidence compaction or event loss.

Do not include company hosts, private labels, local paths, channel IDs, credentials, or the private three-trigger times in public docs.

- [ ] **Step 2: Run the complete automated verification**

```bash
../../.venv/bin/python -m pytest -q
./scripts/check-public.sh
git diff --check
```

Expected baseline or better: `1057 passed, 1 skipped`; public scan and diff check PASS.

- [ ] **Step 3: Verify copied-ledger correctness without publishing**

On a copied ledger/config only:

- compare before/after ledger event counts, appendix counts, and refs;
- verify unrelated project addition schedules zero post-migration daily calls;
- verify current and previous report provenance/cutoffs;
- inspect mapped, unmapped, message-only, multilingual, and no-project Person entries;
- inspect the current Work Journal for actual team outcomes and actionable attention;
- run capture-only and confirm no render/commit/push;
- record capture and journal timings.

Acceptance targets on the deployment machine with responsive providers: capture-only ≤5 minutes; the observed 41 genuine calls at concurrency `2` ≤15 minutes; no calls beyond genuine misses plus explicitly reported safe migration fallback.

- [ ] **Step 4: Request independent code and specification review**

Reviewer must check correctness, privacy/public boundary, complete-evidence preservation, concurrency/SQLite safety, legacy schema migration, Windows imports, report quality, and every success criterion in the spec.

- [ ] **Step 5: Record the final public-engine checkpoint command without executing it**

```bash
git add README.md docs/architecture.md docs/deployment.md docs/privacy.md tests/test_public_scan.py
git commit -m "docs: explain rolling synthesis and capture-only operation"
```

## Post-plan private cutover boundary

After explicit authorization to commit/merge/release the public engine, create a separate private plan that:

1. updates the pinned OSS revision;
2. splits the current multi-trigger LaunchAgent into a capture job and evening full job;
3. preserves the existing protected environment, ledger, vault, and rollback;
4. performs a copied-data benchmark and manual full run;
5. cuts over scheduler labels transactionally and verifies `launchctl print` plus logs.

Do not perform those private mutations as part of this plan.
