# Project, Area, and Count-Only Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the vault distinguish full projects, organizational areas, hidden labels, and four GitHub projects whose pages contain only weekly contributor commit counts.

**Architecture:** `projects.yaml` becomes the deterministic classification source while the immutable event ledger remains unchanged. GitHub count-only collection writes replacement snapshots to a separate aggregate table; the renderer combines classification, normal events, and aggregates without leaking GitHub commit details. GitLab retains its configured group sweep and additionally resolves only explicitly mapped repositories outside that group.

**Tech Stack:** Python 3.11+, SQLite, PyYAML, requests, pytest, Markdown, operator-owned YAML

**Spec:** `docs/superpowers/specs/2026-09-02-project-area-and-count-only-projection.md`

## Global Constraints

- `dev-agent` reads only its one operator-approved private GitLab path; do not
  configure its GitHub repository.
- GitHub count-only applies only to `collective-cognition-sdk`, `conversation-runtime-sdk`, `forge-guard`, and `team-memory-agent`.
- MemberKit events remain normal ledger events for every project.
- Count-only persistence stores only project, week start, person, and commit count.
- Existing event and summary rows are never deleted or rewritten by this migration.
- `local-agent-team` remains unchanged and is outside this plan; legacy
  unclassified labels continue to render as full projects.
- COC maps exactly three internal repositories and its existing Feishu channel.
- `team-coordination` and `turkey-rnd` render under `Areas/`.
- `IdeaProjects`, `System32`, `claude`, and `scripts` have no project or area projection.
- No commit, push, pin bump, or deployment command may run without the corresponding explicit user instruction.

---

## File map

### Public engine: repository root

- `teammem/identity.py`: parse and validate project/area/hidden classification.
- `teammem/metrics.py`: immutable aggregate and scope value objects.
- `teammem/store.py`: aggregate schema, transactional replacement, and queries.
- `teammem/connectors/base.py`: carry aggregate snapshots beside events.
- `teammem/connectors/github.py`: choose full-event or count-only collection per project.
- `teammem/connectors/gitlab.py`: collect explicitly mapped repositories outside the configured group.
- `teammem/services.py`: persist connector aggregate snapshots and report honest counts.
- `teammem/render.py`: manage `Areas/`, render count-only project pages, and avoid dead links.
- `docs/privacy.md`: document transient GitHub processing and aggregate persistence.
- `docs/operator-guide.md`: document the new YAML fields and rolling count window.
- `tests/test_identity.py`: classification and collision tests.
- `tests/test_store.py`: schema upgrade and replacement tests.
- `tests/test_github_connector.py`: count-only privacy and aggregation tests.
- `tests/test_gitlab_collector.py`: external mapped-repository tests.
- `tests/test_services.py`: aggregate persistence tests.
- `tests/test_render.py`: project/area/hidden/count-only render tests.
- `tests/test_bundles.py`: MemberKit non-regression test.

### Private overlay: `<private-overlay-worktree>`

- `team-memory-agent-overlay/config/projects.yaml`: production classification and mappings.
- `team-memory-agent-overlay/config/connectors.yaml`: enable GitHub with a four-week count window.
- `team-memory-agent-overlay/config/roster.yaml`: map GitHub login `xiongxhc` to `cx`.
- `team-memory-agent-overlay/tests/test_deployment_overlay.py`: semantic configuration assertions.
- `team-memory-agent-overlay/README.md`: operator-facing projection and credential notes.

---

### Task 1: Deterministic projection classification

**Files:**
- Modify: `teammem/identity.py`
- Modify: `tests/test_identity.py`
- Modify: `tests/fixtures/config/projects.example.yaml`

**Interfaces:**
- Consumes: existing `projects.yaml` dictionaries.
- Produces: `IdentityMaps.projection(slug: str) -> str` and resource mappings across projects and areas.

- [ ] **Step 1: Write failing classification tests**

Add tests that build this configuration directly:

```python
projects = {
    "projects": {
        "full": {"gitlab_repos": ["group/full"]},
        "counts": {
            "projection": "count-only",
            "github_repos": ["owner/counts"],
        },
    },
    "areas": {
        "coordination": {"feishu_channels": ["oc_coordination"]},
    },
    "hidden_projects": ["IdeaProjects"],
}
ids = IdentityMaps({"members": {}}, projects)
assert ids.projection("full") == "full"
assert ids.projection("counts") == "count-only"
assert ids.projection("coordination") == "area"
assert ids.projection("IdeaProjects") == "hidden"
assert ids.projection("unknown") == "unclassified"
assert ids.project_for_channel("oc_coordination") == "coordination"
```

Add parameterized failures for an invalid projection value, a slug present in
two categories, and a resource claimed across a project and area.

- [ ] **Step 2: Run the focused tests and confirm the new contract fails**

Run: `python -m pytest tests/test_identity.py -q`

Expected: failures report missing `projection()` and unsupported `areas` parsing.

- [ ] **Step 3: Implement the smallest classification parser**

Keep `RESOURCE_FIELDS` unchanged. Parse `projects`, `areas`, and
`hidden_projects`; validate category uniqueness before inserting resources.
Store the resolved mode in `self._projection_by_slug`. Default each ordinary
project to `full` and reject any explicit value outside `{full, count-only}`.

Implement:

```python
def projection(self, slug: str) -> str:
    return self._projection_by_slug.get(slug, "unclassified")
```

Make `resources(kind)` include resources from both projects and areas so the
existing Feishu connector can map area channels without a second lookup path.

- [ ] **Step 4: Run identity tests**

Run: `python -m pytest tests/test_identity.py -q`

Expected: all tests pass.

- [ ] **Step 5: Prepare the public commit boundary**

Proposed commit: `feat(config): classify projects, areas, and hidden labels`

Do not run `git commit` until the user explicitly authorizes a commit.

---

### Task 2: Aggregate value objects and SQLite storage

**Files:**
- Create: `teammem/metrics.py`
- Modify: `teammem/store.py`
- Modify: `tests/test_store.py`

**Interfaces:**
- Produces: `WeeklyCommitCount(project, week_start, person, commit_count)`.
- Produces: `CommitCountScope(project, week_start)`.
- Produces: `replace_weekly_commit_counts(conn, scopes, counts) -> int`.
- Produces: `weekly_commit_counts(conn, project, week_start) -> list[WeeklyCommitCount]`.

- [ ] **Step 1: Write failing migration and replacement tests**

Cover these exact cases:

```python
scopes = (CommitCountScope("team-memory-agent", "2026-08-31"),)
first = (
    WeeklyCommitCount("team-memory-agent", "2026-08-31", "cx", 9),
    WeeklyCommitCount("team-memory-agent", "2026-08-31", "sam", 3),
)
assert replace_weekly_commit_counts(conn, scopes, first) == 2
assert replace_weekly_commit_counts(conn, scopes, first) == 0
assert replace_weekly_commit_counts(conn, scopes, ()) == 2
assert weekly_commit_counts(conn, "team-memory-agent", "2026-08-31") == []
```

Also create a legacy database containing only the existing `events` and
`summaries` tables, reopen it with `open_db`, and assert the aggregate table is
created without changing legacy row counts.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `python -m pytest tests/test_store.py -q`

Expected: import failure for `teammem.metrics` or missing storage functions.

- [ ] **Step 3: Add immutable aggregate types**

Create `teammem/metrics.py`:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class WeeklyCommitCount:
    project: str
    week_start: str
    person: str
    commit_count: int

@dataclass(frozen=True)
class CommitCountScope:
    project: str
    week_start: str
```

- [ ] **Step 4: Add the schema and transactional replacement**

Append the `weekly_commit_counts` table from the spec to `_SCHEMA`. Validate
that every count belongs to a supplied scope and is positive before opening the
transaction. Inside one transaction, read the old rows, delete each scope, and
insert the new rows. Return the number of rows whose persisted tuple changed,
including removed rows, so a repeated identical snapshot returns zero.

Query rows in `commit_count DESC, person ASC` order.

- [ ] **Step 5: Run store tests**

Run: `python -m pytest tests/test_store.py -q`

Expected: all tests pass and legacy event counts remain unchanged.

- [ ] **Step 6: Prepare the public commit boundary**

Proposed commit: `feat(store): persist weekly contributor commit counts`

Do not run `git commit` until the user explicitly authorizes a commit.

---

### Task 3: GitHub count-only collection

**Files:**
- Modify: `teammem/connectors/base.py`
- Modify: `teammem/connectors/github.py`
- Modify: `tests/test_github_connector.py`

**Interfaces:**
- Consumes: `IdentityMaps.projection()` and `ConnectorSettings.options["count_weeks"]`.
- Produces: `CollectionResult.commit_counts` and `CollectionResult.commit_count_scopes`.

- [ ] **Step 1: Write failing count-only connector tests**

Use one count-only repository with three commits: two from `alex-gh` and one
from `sam-gh`. Assert:

```python
assert result.events == ()
assert result.commit_counts == (
    WeeklyCommitCount("project-alpha", "2026-07-13", "alex", 2),
    WeeklyCommitCount("project-alpha", "2026-07-13", "sam", 1),
)
assert all("sha-" not in repr(row) for row in result.commit_counts)
assert not any(path.endswith("/pulls") for path, _ in calls)
```

Add a second test proving `projection: full` preserves the current commit and
pull-request `Event` output. Add a four-week test proving all project/week scopes
are returned even when a week has zero commits. Add an invalid `count_weeks`
test for `0`, `53`, and a non-integer string.

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `python -m pytest tests/test_github_connector.py -q`

Expected: missing `CollectionResult` aggregate fields.

- [ ] **Step 3: Extend the connector result contract**

Add tuple defaults to `CollectionResult`:

```python
commit_counts: tuple[WeeklyCommitCount, ...] = ()
commit_count_scopes: tuple[CommitCountScope, ...] = ()
```

Existing connector constructors remain valid.

- [ ] **Step 4: Implement count-only aggregation**

For every mapped GitHub repository:

1. Read its project projection.
2. For `full`, execute the existing commit and pull-request path unchanged.
3. For `count-only`, fetch commits only, map login then author email through the
   roster, derive the Monday from the commit timestamp date, and increment an
   in-memory `(project, week_start, person)` counter.
4. Return sorted aggregates and the complete requested project/week scope set.

Parse `count_weeks` with default `4` and bounds `1..52`. Set `since` to midnight
UTC on the oldest requested Monday. Do not construct `Event` objects for the
count-only path.

- [ ] **Step 5: Run connector tests**

Run: `python -m pytest tests/test_github_connector.py -q`

Expected: all full and count-only tests pass.

- [ ] **Step 6: Prepare the public commit boundary**

Proposed commit: `feat(github): aggregate count-only project activity`

Do not run `git commit` until the user explicitly authorizes a commit.

---

### Task 4: Persist aggregate connector results

**Files:**
- Modify: `teammem/services.py`
- Modify: `tests/test_services.py`
- Modify: `tests/test_daily.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `CollectionResult.commit_counts`, `commit_count_scopes`, and `replace_weekly_commit_counts()`.
- Produces: `CollectionRun.aggregate_rows` and `CollectionRun.aggregate_changes`.

- [ ] **Step 1: Write failing service tests**

Create a fixture connector returning zero events, two aggregate rows, and one
scope. Assert a normal run persists the two rows, an identical second run has
zero changes, and dry-run leaves the table empty. Assert CLI output contains
`2 aggregate rows / 2 changed` and prints no commit detail.

- [ ] **Step 2: Run the focused service tests and confirm failure**

Run: `python -m pytest tests/test_services.py tests/test_daily.py tests/test_cli.py -q`

Expected: missing aggregate fields on `CollectionRun` or no persisted rows.

- [ ] **Step 3: Persist counts in the existing collection transaction boundary**

Extend `CollectionRun` with integer `aggregate_rows` and `aggregate_changes`.
When not dry-running, call `replace_weekly_commit_counts` on the same SQLite
connection after event reconciliation. When dry-running, print only aggregate
project/week/person/count tuples and never provider payload fields.

Keep `fetched` and `inserted` as event counts for backward compatibility. Add
the aggregate fields to stage telemetry using numeric values only.

- [ ] **Step 4: Run service, daily, and CLI tests**

Run: `python -m pytest tests/test_services.py tests/test_daily.py tests/test_cli.py -q`

Expected: all tests pass, including existing event-only fixtures.

- [ ] **Step 5: Prepare the public commit boundary**

Proposed commit: `feat(collect): store aggregate connector snapshots`

Do not run `git commit` until the user explicitly authorizes a commit.

---

### Task 5: Render Areas and count-only project pages

**Files:**
- Modify: `teammem/render.py`
- Modify: `tests/test_render.py`

**Interfaces:**
- Consumes: `IdentityMaps.projection()` and `weekly_commit_counts()`.
- Produces: managed `Areas/` tree and aggregate-only project pages.

- [ ] **Step 1: Write failing render tests**

Seed one full project event, one area event, one hidden-label event, one
count-only MemberKit event, and two aggregate count rows. Assert:

```python
assert (vault / "Projects/full/README.md").exists()
assert (vault / "Areas/coordination/README.md").exists()
assert not (vault / "Projects/IdeaProjects").exists()
count_page = (vault / "Projects/counts/Week 2026-07-13-17.md").read_text()
assert "Alex Rivera | 2" in count_page
assert "Sam Lee | 1" in count_page
assert "3 commits · 2 contributors" in count_page
assert "private MemberKit summary" not in count_page
```

Also assert the MemberKit summary remains visible in the corresponding person
journal, the area link targets `Areas/`, the hidden label is plain text, and
`render --verify` includes tampering under `Areas/` in its diff.

Add an empty-count test asserting the explicit no-count sentence and absence of
event-detail fallback.

Seed an unclassified `local-agent-team` event and assert its existing
`Projects/local-agent-team/` page and project link remain present.

- [ ] **Step 2: Run render tests and confirm failure**

Run: `python -m pytest tests/test_render.py -q`

Expected: missing `Areas/` and count-only detail leakage.

- [ ] **Step 3: Add classification-aware links and managed paths**

Add `Areas` to `MANAGED`. Replace the unconditional project link helper with a
helper that accepts `ids` and returns:

- a `Projects/` link for `full`, `count-only`, and `unclassified`;
- an `Areas/` link for `area`;
- escaped plain text for `hidden`.

Validate filename collisions independently within Projects and Areas.

- [ ] **Step 4: Split projection row sets**

Keep all event rows available to person and team journals. Build ordinary
project page inputs from `full` and legacy `unclassified` event rows, area page
inputs only from `area` rows, and count-only page inputs only from aggregate
rows. Hidden rows produce no folder.

Use the existing project folder and week-file layout for areas. Preserve old
weeks outside the active render window using the same historical discovery rule
as Projects.

- [ ] **Step 5: Render count-only tables deterministically**

Resolve the display name with `ids.display_name(person)`. Sort by negative count
then display name. Render total commits and unique contributor count before the
table. A missing snapshot renders the exact empty-state sentence from the spec.

- [ ] **Step 6: Run render tests**

Run: `python -m pytest tests/test_render.py -q`

Expected: all tests pass and existing full-project snapshots remain stable.

- [ ] **Step 7: Prepare the public commit boundary**

Proposed commit: `feat(render): separate areas and count-only projects`

Do not run `git commit` until the user explicitly authorizes a commit.

---

### Task 6: Collect explicitly mapped GitLab repositories outside the root group

**Files:**
- Modify: `teammem/connectors/gitlab.py`
- Modify: `tests/test_gitlab_collector.py`

**Interfaces:**
- Consumes: `IdentityMaps.resources("gitlab-repo")`.
- Produces: the existing GitLab `Event` shapes exactly once per repository.

- [ ] **Step 1: Write failing external-repository tests**

Return `team/project-in-group` from the group listing and configure
`outside-one/project-alpha` plus `outside-two/project-beta`. Make the encoded project
endpoints return those two projects. Assert all three are processed once and no
root `/groups/backend/projects` or `/groups/frontend/projects` call occurs.

Add a test where an explicitly mapped endpoint returns an error and assert the
connector run fails rather than silently omitting COC.

- [ ] **Step 2: Run GitLab tests and confirm failure**

Run: `python -m pytest tests/test_gitlab_collector.py -q`

Expected: external mapped repositories have no events.

- [ ] **Step 3: Extract one-project collection without changing event shapes**

Move the existing per-project branch, merge-request, issue, comment, and
repository-creation logic into an inner helper that accepts the GitLab project
dictionary. Keep `seen_commits` shared across every processed project.

After the group list is processed, compare case-folded paths against
`ids.resources("gitlab-repo")`. Fetch each missing path through
`/projects/{quote(path, safe='')}` and pass its dictionary to the same helper.
Do not enumerate either root group.

- [ ] **Step 4: Run GitLab tests**

Run: `python -m pytest tests/test_gitlab_collector.py tests/test_gitlab_reconciliation.py -q`

Expected: all tests pass with existing event hashes and reconciliation intact.

- [ ] **Step 5: Prepare the public commit boundary**

Proposed commit: `feat(gitlab): collect explicitly mapped external repos`

Do not run `git commit` until the user explicitly authorizes a commit.

---

### Task 7: Preserve MemberKit behavior and document privacy

**Files:**
- Modify: `tests/test_bundles.py`
- Modify: `docs/privacy.md`
- Modify: `docs/operator-guide.md`

**Interfaces:**
- Consumes: count-only classifications only as renderer/connector metadata.
- Guarantees: bundle conversion remains independent of projection mode.

- [ ] **Step 1: Add the MemberKit regression test**

Load a bundle with `project: team-memory-agent`, convert it with
`bundle_events`, insert it into the ledger, and assert its source, summary, and
project are preserved. Do not pass `IdentityMaps` into bundle validation or
conversion.

- [ ] **Step 2: Run bundle and importer tests**

Run: `python -m pytest tests/test_bundles.py tests/test_importer.py -q`

Expected: all tests pass without production-code changes to MemberKit or bundle
import.

- [ ] **Step 3: Update operator and privacy documentation**

Document the YAML schema, default-full compatibility, managed `Areas/` path,
count-only table fields, four-week connector option, and the fact that GitHub
commit payloads are processed transiently but never persisted for count-only
projects. State explicitly that MemberKit events remain recordable.

- [ ] **Step 4: Prepare the public commit boundary**

Proposed commit: `docs: explain project projection and count privacy`

Do not run `git commit` until the user explicitly authorizes a commit.

---

### Task 8: Apply the approved private overlay classification

**Files:**
- Modify: `<private-overlay-worktree>/team-memory-agent-overlay/config/projects.yaml`
- Modify: `<private-overlay-worktree>/team-memory-agent-overlay/config/connectors.yaml`
- Modify: `<private-overlay-worktree>/team-memory-agent-overlay/config/roster.yaml`
- Modify: `<private-overlay-worktree>/team-memory-agent-overlay/tests/test_deployment_overlay.py`
- Modify: `<private-overlay-worktree>/team-memory-agent-overlay/README.md`

**Interfaces:**
- Consumes: public engine classification and count-only contracts.
- Produces: exact production mappings without changing the live ledger.

- [ ] **Step 1: Write failing semantic overlay assertions**

Load the three YAML files and assert:

- `coc` contains exactly the three operator-approved private GitLab paths and
  the operator-approved private channel;
- `team-coordination` no longer claims that channel;
- `dev-agent` contains only its operator-approved private GitLab path and no
  GitHub path;
- the four approved GitHub repositories are `projection: count-only`;
- `team-coordination` and `turkey-rnd` occur under `areas`, not `projects`;
- the four hidden labels match the spec exactly;
- `local-agent-team` remains an ordinary legacy project without a new source or
  projection rule;
- GitHub is enabled with `count_weeks: 4`;
- roster member `cx` includes GitHub login `xiongxhc`.

- [ ] **Step 2: Run the overlay semantic test and confirm failure**

Run from the private overlay repository root:

`python -m pytest -q team-memory-agent-overlay/tests/test_deployment_overlay.py`

Expected: failures identify the absent COC/OSS/area classification.

- [ ] **Step 3: Apply the exact YAML mappings**

Move the two organizational entries without changing their channel lists except
for removing COC from `team-coordination`. Add the operator-approved private COC
and dev-agent paths, four count-only GitHub paths, and four hidden labels. Add
`github: [xiongxhc]` to roster member `cx`. Enable GitHub and set
`count_weeks: 4`.

- [ ] **Step 4: Document the runtime credential prerequisite**

In the private README, state that the hub GitLab token needs read API access and
provider visibility for every explicitly mapped GitLab repository not returned
by the configured group sweep. Require an observed credential-backed provider
dry-run before deployment, keep exact private resource identifiers in the
private overlay, and state that `TEAMMEM_GITHUB_TOKEN` needs read access only to
the configured count-only public repositories. Do not put credential values in
YAML, documentation, tests, shell history, or commits.

- [ ] **Step 5: Run all private overlay checks locally**

Run from the private overlay repository root:

```bash
python -m pytest -q \
  team-memory-agent-overlay/tests/test_daily_wrapper.py \
  team-memory-agent-overlay/tests/test_deployment_overlay.py
bash -n team-memory-agent-overlay/bin/teammem-daily.sh
python -c 'import plistlib; plistlib.load(open("team-memory-agent-overlay/deploy/com.cx.teammem-daily.plist", "rb"))'
```

Expected: all commands exit zero.

- [ ] **Step 6: Prepare the private commit boundary**

Proposed commit: `feat(teammem): classify projects, areas, and OSS counts`

Do not run `git commit` until the user explicitly authorizes a commit.

---

### Task 9: End-to-end verification on disposable production data

**Files:**
- Test only; do not modify the canonical ledger or live vault.

**Interfaces:**
- Consumes: completed public engine and private overlay tasks.
- Produces: local test evidence and a deployment-ready change set.

- [ ] **Step 1: Run the complete public suite**

Run from the public repository root:

```bash
python -m pytest -q
python -m build
git diff --check
```

Expected: tests pass, wheel and source distribution build, and diff check is
clean.

- [ ] **Step 2: Copy production inputs into a disposable directory**

Use `mktemp -d` for the test root. Copy `ledger.db`, the config directory, and
the vault into that root. Point `TEAMMEM_DB`, `TEAMMEM_CONFIG_DIR`, and
`TEAMMEM_VAULT` only at the disposable copies.

- [ ] **Step 3: Render twice and verify determinism**

Run the candidate engine against the disposable database and vault:

```bash
teammem render --today 2026-09-02
teammem render --today 2026-09-02
teammem render --today 2026-09-02 --verify
```

Expected: verify reports empty missing, unexpected, and differing lists.

- [ ] **Step 4: Check the semantic output tree**

Assert COC and dev-agent exist under Projects, the two areas exist under Areas,
the four hidden folders do not exist, and every count-only week page lacks SHA,
commit-message, and raw-payload fields. Confirm the canonical ledger and vault
hashes did not change during the disposable run.

- [ ] **Step 5: Review staged scope before any commit**

Run `git status --short` and `git diff --stat` independently in both repositories.
Only the files named in this plan may be staged. Report the proposed public and
private commit boundaries to the user and wait for literal commit authorization.

- [ ] **Step 6: Keep delivery gates separate**

After an authorized commit, report both SHAs. A later literal `push` authorizes
remote publication; a later explicit deploy instruction authorizes pin bump and
agent-box deployment. Neither is implied by this plan.

---

## Self-review record

- Spec coverage: every approved classification, privacy boundary, COC mapping,
  MemberKit correction, preservation rule, and deferred `local-agent-team`
  decision maps to Tasks 1 through 9.
- Placeholder scan: no deferred implementation markers or unspecified error
  handling remain.
- Type consistency: `WeeklyCommitCount`, `CommitCountScope`,
  `replace_weekly_commit_counts`, and `IdentityMaps.projection` use the same
  names across producer and consumer tasks.
