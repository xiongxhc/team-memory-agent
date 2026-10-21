# Project, Area, and Count-Only Projection Design

**Status:** approved in conversation on 2026-09-02

## Problem

The generated `Projects/` view currently treats every non-null ledger project
label as a durable software project. That makes filesystem-derived labels and
broad organizational chats look equivalent to repository-backed projects, and
it exposes event-level detail where a weekly contribution count is sufficient.

## Outcomes

For a fixed ledger, configuration, commit-count snapshot, and operator date:

1. A full project has `Projects/<slug>/README.md` and one file per rendered week.
2. An area has `Areas/<slug>/README.md` and one file per rendered week.
3. A hidden label creates neither a project nor an area page.
4. Historical event rows are never deleted by classification or rendering.
5. Count-only projects show a weekly total and contributor names with counts.
6. GitHub details for count-only projects are never persisted.
7. MemberKit events remain normal ledger evidence and continue through journals
   and synthesis.

## Approved classification

### Full projects

- `coc` uses exactly three operator-approved internal GitLab repositories plus
  the existing operator-approved COC Feishu channel. Exact private resource
  identifiers remain only in the private overlay.
- `dev-agent` uses exactly one operator-approved internal GitLab repository.
  Its GitHub repository must not be configured as a second TeamMem source.
- Existing project classifications not named in this design remain unchanged.
- `local-agent-team` is explicitly out of scope and remains unchanged.
- Unclassified legacy labels retain the current full-project projection. This
  change does not silently decide the remaining ambiguous entries; a later
  classification pass can make the projection strict after each has an
  approved destination.

### Count-only projects

The following public GitHub repositories remain projects, but GitHub collection
stores weekly aggregates only:

- `xiongxhc/collective-cognition-sdk`
- `xiongxhc/conversation-runtime-sdk`
- `xiongxhc/forge-guard`
- `xiongxhc/team-memory-agent`

The `xiongxhc` profile README repository is not a project.

### Areas

- `team-coordination`
- `turkey-rnd`

The COC channel is removed from `team-coordination` and assigned to `coc`.

### Hidden labels

- `IdeaProjects`
- `System32`
- `claude`
- `scripts`

Their existing and future ledger evidence remains queryable and usable in
person/team journals. Rendering does not create broken links for hidden labels.

## Configuration contract

`projects.yaml` gains explicit projection metadata while retaining its existing
resource mappings:

```yaml
projects:
  dev-agent:
    projection: full
    gitlab_repos: [internal-group/dev-agent]  # placeholder operator path
  team-memory-agent:
    projection: count-only
    github_repos: [xiongxhc/team-memory-agent]

areas:
  team-coordination:
    feishu_channels: []

hidden_projects:
  - IdeaProjects
  - System32
  - claude
  - scripts
```

`projection` defaults to `full` for backward compatibility. Only
`full` and `count-only` are accepted. Resources must be unique across projects
and areas. A slug cannot appear in more than one of `projects`, `areas`, and
`hidden_projects`.

`IdentityMaps` owns this deterministic classification. It exposes resource
lookups as today plus `projection(slug)`, returning `full`, `count-only`,
`area`, `hidden`, or `unclassified`. The renderer treats `unclassified` as the
legacy full-project behavior in this migration; only an explicit `hidden`
classification suppresses a page.

## Count-only collection and storage

The GitHub connector fetches default-branch commits for explicitly mapped
count-only repositories. It uses the commit timestamp and author identity only
in memory to group rows by project, ISO-week Monday, and roster person.

The ledger gains a separate aggregate table:

```sql
CREATE TABLE IF NOT EXISTS weekly_commit_counts (
  project      TEXT NOT NULL,
  week_start   TEXT NOT NULL,
  person       TEXT NOT NULL,
  commit_count INTEGER NOT NULL CHECK (commit_count >= 0),
  PRIMARY KEY (project, week_start, person)
);
```

No repository path is stored in this table because it remains operator-owned
configuration. No commit title, message, SHA, URL, diff, pull request, or raw
provider payload enters either `weekly_commit_counts` or `events` for a
count-only GitHub mapping.

Each collection returns complete snapshots for the requested project/week
scopes. Persistence replaces every returned scope transactionally: delete the
old rows for that project/week, then insert the new non-zero contributor rows.
This makes correction, force-push, and identity-remapping behavior deterministic
without retaining commit identifiers.

The initial operator configuration requests four weeks so existing weekly pages
can be populated. Normal daily runs refresh that rolling window. GitHub details
exist only in request memory and are discarded after aggregation.

MemberKit is unchanged. A MemberKit event whose `project` is one of the four
count-only projects is inserted into `events` normally. It may contribute to
person journals and team synthesis, but it never increments the GitHub commit
count.

## Internal GitLab scope

The existing configured group remains the normal discovery boundary. After
group enumeration, the GitLab connector resolves any explicitly mapped GitLab
repository not returned by that group through the encoded project endpoint and
collects it once. This adds the three COC repositories without scanning every
repository in the root `backend` and `frontend` groups.

The runtime token must have read API access to each explicitly mapped external
path. A missing mapped repository is a collection failure or explicit warning;
it must not silently produce an empty project.

## Rendering

`Areas/` becomes a managed projection beside `Person/`, `Projects/`, and
`Work Journal/`.

Full-project pages retain the existing useful weekly content. Count-only weekly
pages use this shape:

```markdown
# team-memory-agent — Week 2026-08-31-04

12 commits · 2 contributors

| Contributor | Commits |
|---|---:|
| Chris Xiong | 9 |
| Contributor B | 3 |
```

Rows sort by descending count, then display name. The total is the sum of the
contributor rows. A count-only project with ledger evidence but no collected
aggregate renders an explicit `No commit count collected for this week.` state;
it never falls back to event-detail rendering.

Project and area indexes link only to their own classifications. Work journals
link full, count-only, and legacy-unclassified project activity to `Projects/`,
area activity to `Areas/`, and render hidden labels as plain text so no dead
links are created.

## Migration and safety

- Opening an existing database creates `weekly_commit_counts` without rewriting
  `events` or `summaries`.
- Existing detailed GitHub rows, if any, remain historical ledger evidence;
  migration does not delete or compact them.
- Classification affects projection only and never mutates event rows.
- The renderer remains deterministic and `render --verify` compares `Areas/`
  as a managed path.
- The private overlay configuration is changed only after the public engine
  supports and tests the new schema.
- Commit, push, pin bump, and agent-box deployment remain separate operator
  gates. No gate is implied by approving this design.

## Verification

Acceptance requires all of the following:

1. Store tests prove idempotent schema upgrade and transactional count
   replacement, including a now-empty scope.
2. GitHub connector tests prove count-only mappings return no events and expose
   no commit detail in their aggregate result.
3. Existing full GitHub connector behavior remains backward compatible.
4. GitLab tests prove explicitly mapped repositories outside the configured
   group are collected once and unmapped root-group repositories are not swept.
5. Identity tests reject category/resource collisions and preserve default-full
   behavior.
6. Renderer tests prove full, count-only, area, hidden, empty-count, historical,
   and broken-link behavior.
7. Bundle/import tests prove MemberKit events for count-only projects are still
   inserted.
8. Overlay semantic tests pin the exact COC, dev-agent, OSS, area, and hidden
   mappings.
9. The complete public test suite and private overlay tests pass locally.
10. A render against a disposable copy of the production ledger has no missing,
    unexpected, or differing managed files after the second render.
