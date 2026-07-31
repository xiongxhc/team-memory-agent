# GitLab Full Activity — Design

## Problem

The GitLab adapter collects two kinds — default-branch `commit` and `mr` — so the
ledger records code landing but not the movement around it: issues opened and
closed, and repositories appearing in the group. Teams that run their planning in
GitLab issues are invisible between merges, and a new project's first days (often
setup work with few default-branch commits) produce no attributable events at
all. The operator's goal is that all GitLab movement in the configured group —
change requests, issues, and repository lifecycle — lands in the ledger as
attributed facts.

## Decision summary

- **Two new kinds, provider-native names:** `issue` and `repo`, both
  `source="gitlab"`. Branch pushes stay excluded — branch work surfaces at merge
  via MRs, the deliberate boundary carried since the first GitLab collector.
- **Issues follow the MR state-transition identity exactly:**
  `hash = event_hash("issue", project_id, iid, state)` with no timestamp, so each
  state transition (`opened` → `closed`) is exactly one event and re-collections
  of the same state deduplicate on `UNIQUE(person, source, hash)`. A reopened
  issue re-produces the `opened` identity and is deduplicated; a later close is a
  new fact.
- **Issue attribution names the worker, not just the reporter:** an `opened`
  issue is attributed to its author; a `closed` issue to its assignee, falling
  back to the author when unassigned. This is a deliberate deviation from the
  MR mapping (author for all states) because closing an issue is the assignee's
  work. Ghost authors/assignees resolve to `_unmapped/(none)` — never dropped.
- **`repo` records repository creation from data already fetched:** the group
  projects listing carries `created_at`, so projects created inside the lookback
  emit one `repo` event with `hash = event_hash("repo", project_id, "created")`.
  The state field leaves room for later lifecycle states (e.g. `archived`) as
  new hashes. One extra call per *new* repository (`/users/{creator_id}`)
  resolves the creator's username for attribution; if that lookup fails the
  event still lands, attributed `_unmapped/(none)`.
- **Rendering treats both kinds as work items:** the three render kind filters
  widen to `("commit", "pr", "mr", "issue", "repo", "journal-highlight")`.
  Synthesis needs no change — slices pass the kind string to the LLM verbatim.
- **No new configuration:** the existing group `read_api` token already covers
  the issues and users APIs; the lookback stays `TEAMMEM_SINCE_DAYS`.

## Event identity mappings (binding)

| Field | `issue` | `repo` |
|---|---|---|
| endpoint | `/projects/{id}/issues?updated_after=<since>` | group projects listing (no extra listing call) + `/users/{creator_id}` per new repo |
| person | `opened`: author username; `closed`: assignee username, fallback author | creator username via user lookup |
| ts | `closed_at` or `updated_at` | `created_at` |
| summary | `[<state>] <title>` | `[created] <path_with_namespace>` |
| refs | `{"iid": iid, "url": web_url}` | `{"id": project_id, "url": web_url}` |
| hash | `event_hash("issue", project_id, iid, state)` | `event_hash("repo", project_id, "created")` |

## Flag interaction

New kinds mechanically widen the denominators of the existing gap/concentration
flags (events per person per week). That is consistent with their definition —
flags name work states, not people-grades — and stays inside the "visibility,
not scoring; no ranking views" constraint.

## Out of scope

- Branch pushes, comments/notes, wiki, releases/tags, milestones — future kinds
  if wanted, same pattern.
- Project `archived` transitions — the listing has no archived-at timestamp to
  place the event in the lookback window.
- Confidential-issue filtering beyond what the token identity can see — the API
  boundary is the token's, as everywhere else in the adapter.
- GitHub parity (`issue` for GitHub, repo lifecycle) — separate change, same
  identity pattern.

## Success criteria

1. A closed issue with an assignee produces one event attributed to the
   assignee with a pinned, stable hash; re-collection inserts zero rows.
2. A repository created inside the lookback produces one attributed `repo`
   event; repositories older than the lookback produce none, and all existing
   event identities (commit, mr) are byte-identical to before.
3. Issue and repo events render as work bullets on Person and Projects pages
   and count in the Work Journal kind tally.
4. `pytest -q tests` and `./scripts/check-public.sh` pass; the architecture,
   deployment, privacy, and README boundary tables name the new kinds.
