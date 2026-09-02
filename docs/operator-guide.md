# Operator guide

This guide covers project classification, privacy-preserving projections, and
the local run boundary. The [privacy guide](privacy.md) describes the consent
and credential rules; the [deployment guide](deployment.md) covers installation
and provider permissions.

## Configure projects and areas

Project and resource mappings live in the operator-owned `projects.yaml` under
`TEAMMEM_CONFIG_DIR` (normally `~/.config/teammem`). The supported shape is:

```yaml
projects:
  project-alpha:
    projection: full             # optional; full is the default
    github_repos: [team/project-alpha]
    gitlab_repos: [team/project-alpha]
    slack_channels: [C0123]
    feishu_channels: [oc_example_alpha]
    discord_channels: ["9876543210"]
  project-beta:
    projection: count-only
    github_repos: [team/project-beta]
areas:
  coordination:
    slack_channels: [C0124]
hidden_projects:
  - IdeaProjects
```

Each resource list is optional. A project without `projection` is `full`, so
existing configuration keeps its detailed-project behavior. An explicit
project may use only `full` or `count-only`. A project label found in events but
not declared in the file is `unclassified`; it remains a detailed Project for
legacy compatibility. Labels declared under `areas` are Areas, not Projects.
Only labels explicitly listed under `hidden_projects` suppress their own pages.

The renderer owns the managed vault paths. Full and unclassified labels render
under `Projects/`; area labels render under `Areas/`; and count-only labels also
use `Projects/` but with aggregate-only content. `Person/`, `Projects/`,
`Areas/`, `Work Journal/`, and the generated root `README.md` are regenerated on
each render. Keep operator-authored material outside those managed paths.

## Count-only GitHub projects

Use count-only when contributors may be named in an aggregate but commit detail
must not enter the Team Memory ledger or rendered project pages:

```yaml
connectors:
  github:
    enabled: true
    count_weeks: 4
```

`count_weeks` defaults to `4` and accepts YAML integers from `1` through `52`;
booleans, fractions, and quoted numeric strings are rejected during configuration
loading. The window is aligned to UTC Monday starts. For a mapped `count-only` GitHub repository,
the connector reads commit responses transiently, resolves a contributor, and
increments a weekly total. It does not create an Event and does not persist the
commit payload, SHA, message, timestamp, reference, or raw response.
Responses whose UTC commit week falls outside the requested replacement window
are ignored with an operator-visible warning.

The durable count snapshot is the `weekly_commit_counts` table. Its exact
fields are:

| Field | Meaning |
|---|---|
| `project` | Canonical configured project slug |
| `week_start` | ISO date for the Monday starting the week |
| `person` | Resolved roster slug (or surfaced `_unmapped/...` identity) |
| `commit_count` | Positive number of commits in that project/person/week |

Count-only Project README and week files use this table only. They show total
commits, contributor count, and the contributor/count table; they do not show
commit messages, SHAs, references, event kinds, raw data, or event cutoffs.
Missing aggregate data is shown as `No commit count collected for this week.`

This setting is not a general event-retention switch. A reviewed MemberKit
bundle is validated and converted independently of project projection. A pushed
MemberKit event remains a normal, recordable ledger event with its `source`,
`summary`, and `project` preserved. It remains available in `Person/` and
`Work Journal/` views. If its project is count-only, the count-only Project page
still contains aggregate GitHub counts only; the event itself is not silently
deleted or rewritten.

## Historical GitLab project attribution

If the GitLab service origin changes, explicitly list each former origin whose
existing event URLs may still be used for project reclaim:

```yaml
connectors:
  gitlab:
    enabled: true
    reclaim_origins:
      - https://gitlab.previous.example
```

`TEAMMEM_GITLAB_URL` is always trusted as the current origin. The optional
`reclaim_origins` value is a list of HTTP or HTTPS origins only; paths,
credentials, queries, fragments, and malformed ports are rejected. Matching is
exact after normalizing the scheme, lowercase hostname, and effective port, so
`https://gitlab.previous.example` and
`https://GITLAB.PREVIOUS.EXAMPLE:443` are the same origin. TeamMem never infers
trusted origins from URLs already present in the ledger.

This option affects historical project attribution during `teammem reclaim`
and the daily reclaim stage only. It does not change the GitLab collection
endpoint, which remains `TEAMMEM_GITLAB_URL`. Reclaim updates only the
`events.project` field; dry runs write nothing and repeated runs are
idempotent.

## Run and inspect locally

Keep secrets in the protected environment file and paths in absolute form. Check
configuration without contacting providers:

```bash
teammem connectors list
teammem connectors check
```

Run once under operator observation before installing a schedule:

```bash
teammem run-daily
```

The run imports reviewed bundles, collects enabled providers, writes the ledger
and count snapshots, and regenerates the managed vault. It does not install or
modify a schedule. Use `teammem run-daily --capture-only` when only collection,
bundle import, reclaim, and optional snapshot writing are wanted; capture-only
does not synthesize, render, sync docs, commit, or push.

Inspect the generated tree and verify it without mutating it:

```bash
find "$TEAMMEM_VAULT" -maxdepth 2 -type f | sort
teammem render --verify
```

The ledger and the `weekly_commit_counts` snapshot are local operator data.
Restrict access to the database, MemberKit inbox/archive/quarantine, snapshots,
and rendered vault according to the team's retention and access policy. Do not
commit any of them.

## MemberKit import boundary

The hub imports only reviewed `teammem-bundle/v1` files from the configured
inbox. Bundle conversion does not load `IdentityMaps` and does not use a
project's projection mode. The bundle's `project` value therefore remains an
ordinary event field; classification is applied later by connectors and
rendering. Invalid bundles are quarantined, accepted bundles are archived by
content hash, and event insertion is idempotent.

After a run, inspect the operator summary for event and aggregate counts
separately. Aggregate row counts contain only numeric metadata; they are not a
proxy for persisted commit payloads. MemberKit evidence is still subject to
member review and the hub's normal ledger access controls.
