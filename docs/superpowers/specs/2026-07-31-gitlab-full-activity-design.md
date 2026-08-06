# GitLab Full Activity — Design

> **Superseded boundary (2026-08-04):** The branch-exclusion decision below is
> retained as historical context. Current collection includes commits from every
> reachable branch inside `TEAMMEM_SINCE_DAYS`, with older commits additionally
> backfilled from merge requests merged inside that lookback. The backfill is
> default-on and may be disabled with the boolean `collect_mr_commits: false`.

## Problem

At the time of this design, the GitLab adapter collected two kinds —
default-branch `commit` and `mr` — so the ledger recorded code landing but not the
movement around it: issues opened and closed, and repositories appearing in the
group. Teams that ran their planning in GitLab issues were invisible between
merges, and a new project's first days (often setup work with few default-branch
commits) produced no attributable events at all. The operator's goal was that all
GitLab movement in the configured group — change requests, issues, and repository
lifecycle — land in the ledger as attributed facts.

## Decision summary

- **Two new kinds, provider-native names:** `issue` and `repo`, both
  `source="gitlab"`. **Historical boundary, superseded:** branch commits were
  excluded until they surfaced through a merge. Current daily collection reads
  commits from every reachable branch inside `TEAMMEM_SINCE_DAYS`.
- **Issues produce stable lifecycle observations:** polling emits one initial
  creation fact with `hash = event_hash("issue", project_id, iid, "opened")`
  when `created_at` is inside the lookback, and one provider-reported closure
  fact with the existing `"closed"` hash when `closed_at` is inside the
  lookback. A current issue record cannot reconstruct repeated reopen/reclose
  history; that requires a GitLab state-events or webhook source.
- **Issue attribution uses provider-reported actors:** the initial creation fact
  belongs to the author and the closure fact belongs to `closed_by`. Assignment
  is not evidence of who closed an issue. A missing actor resolves to
  `_unmapped/(none)` rather than being silently dropped.
- **`repo` records repository creation from data already fetched:** the group
  projects listing carries `created_at`, so projects created inside the lookback
  emit one `repo` event with `hash = event_hash("repo", project_id, "created")`.
  The state field leaves room for later lifecycle states (e.g. `archived`) as
  new hashes. One extra call per *new* repository (`/users/{creator_id}`)
  resolves the creator's username for attribution. If that lookup fails or
  returns no usable username, that repository fact is deferred and a warning
  names only the public project path; the normal lookback retries it later.
- **Rendering treats both kinds as work items:** the three render kind filters
  widen to `("commit", "pr", "mr", "issue", "repo", "journal-highlight")`.
  Synthesis needs no change — slices pass the kind string to the LLM verbatim.
- **Issue/repository configuration:** the existing group `read_api` token already
  covers the issues and users APIs; the lookback stays `TEAMMEM_SINCE_DAYS`.
  **Historical configuration statement, superseded:** current collection also
  has the default-on boolean `collect_mr_commits` option for older merged-MR
  commit backfill.

## Event identity mappings (binding)

| Field | `issue` | `repo` |
|---|---|---|
| endpoint | `/projects/{id}/issues?updated_after=<since>` | group projects listing (no extra listing call) + `/users/{creator_id}` per new repo |
| person | initial creation: author username; closure: `closed_by` username | creator username via user lookup |
| ts | initial creation: `created_at`; closure: `closed_at` | `created_at` |
| summary | `[opened] <title>` or `[closed] <title>` | `[created] <path_with_namespace>` |
| refs | `{"iid": iid, "url": web_url}` | `{"id": project_id, "url": web_url}` |
| hash | existing `event_hash("issue", project_id, iid, "opened" | "closed")` identities | `event_hash("repo", project_id, "created")` |

## Flag interaction

New kinds mechanically widen the denominators of the existing gap/concentration
flags (events per person per week). That is consistent with their definition —
flags name work states, not people-grades — and stays inside the "visibility,
not scoring; no ranking views" constraint.

## Out of scope

- **Historical branch exclusion, superseded for commit visibility:** current
  polling captures commits from every reachable branch inside the lookback.
  Provider push/force-push lifecycle events remain out of scope, as do
  comments/notes, wiki, releases/tags, and milestones.
- Project `archived` transitions — the listing has no archived-at timestamp to
  place the event in the lookback window.
- Repeated issue reopen/reclose history — polling the current issue record
  captures one initial-creation fact and one provider-reported closure fact per
  issue; complete cycles require a state-events or webhook source.
- Confidential-issue filtering beyond what the token identity can see — the API
  boundary is the token's, as everywhere else in the adapter.
- GitHub parity (`issue` for GitHub, repo lifecycle) — separate change, same
  identity pattern.

## Success criteria

1. An issue produces its initial-creation and provider-reported closure facts
   when their provider timestamps are inside the lookback; closure attribution
   uses `closed_by`, and re-collection inserts zero rows for the pinned hashes.
2. A repository created inside the lookback produces one attributed `repo`
   event; repositories older than the lookback produce none, and all existing
   event identities (commit, mr) are byte-identical to before.
3. Issue and repo events render as work bullets on Person and Projects pages
   and count in the Work Journal kind tally.
4. `pytest -q tests` and `./scripts/check-public.sh` pass; the architecture,
   deployment, privacy, and README boundary tables name the new kinds.
