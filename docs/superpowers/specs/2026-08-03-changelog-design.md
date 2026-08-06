# Version Changelog — Design

> **Provenance update (2026-08-06):** This design was written before the
> `v0.4.0` release. Tag `v0.4.0` was created on 2026-08-04 and its tagged
> TeamMem and MemberKit manifests are both version `0.4.0`. This changelog
> work documents that release without changing the version fields in the
> current development checkout or creating another release.

## Problem

Team Memory Agent has useful GitHub release descriptions for `v0.1.0` through
`v0.4.0`, but no source-controlled version history. The current `master` also
contains post-`v0.4.0` TeamMem changes. Readers therefore cannot reliably tell
what changed in a release or whether a change belongs to the hub or MemberKit.

## Decision summary

- Add one root `CHANGELOG.md` covering the public monorepo.
- Use an `Unreleased` section followed by dated sections for `0.4.0`, `0.3.0`,
  `0.2.0`, and `0.1.0`, newest first.
- Within each version, separate `TeamMem` and `MemberKit` whenever both packages
  changed. Use `Added`, `Changed`, and `Fixed` only where they improve scanning.
- Backfill historical versions from the published GitHub release descriptions
  and the corresponding tagged source. Preserve the historical fact that the
  `v0.2.0` release description announced Windows MemberKit scheduling before it
  was complete; state clearly that `v0.3.0` completed and verified it.
- Preserve the reconciliation fact that the `v0.4.0` tagged tree contains the
  listed TeamMem changes even though the published release description
  characterized the hub package as parity-only.
- Document user-visible behavior, compatibility implications, and operational
  changes. Do not reproduce commit-by-commit development mechanics.
- Treat the changelog as the source for future concise GitHub release
  descriptions. Existing published release descriptions remain unchanged.

## Unreleased content

### TeamMem

- Rolling current/previous-week synthesis, report provenance, bounded journal
  concurrency, and capture-only operation with a ledger-wide lock.
- GitLab paginated branch enumeration and default-on collection of all unseen
  MR commits from merge requests merged inside the lookback, with a
  `collect_mr_commits: false` opt-out for the MR supplement only. Commit
  identities are project-scoped and matching legacy bare-SHA rows reconcile.
- Documentation sync accepts capitalized and lowercase
  `Architecture`/`Summary` filenames but writes lowercase destinations.
- Public-boundary documentation and scanning remove
  private-deployment wording from canonical public content.

### MemberKit

- No post-`v0.4.0` MemberKit behavior is included in this release train.

## Historical content

- `0.4.0` records GitLab issue/repository collection and reliability hardening,
  per-week Person vault indexes, tolerant bundle timestamps, and MemberKit
  standing project exclusions.
- `0.3.0` records native Windows MemberKit scheduling, protected setup,
  lifecycle safety, and the rule that installation and scheduled runs never
  push automatically.
- `0.2.0` records provider-neutral connectors, hub scheduling, the
  evidence-first MemberKit workflow, and manual fallback for unsupported
  sources. It also records that Windows MemberKit scheduling was announced but
  not complete until `0.3.0`.
- `0.1.0` records the first public ledger/reporting release, independently
  installable MemberKit, opt-in local scheduling, frozen bundle-v1 protocol,
  and Apache-2.0 licensing.

## Maintenance and release flow

1. Every user-visible change adds an entry under `Unreleased` in the appropriate
   package subsection.
2. Before a release, move the entries into a dated version section and ensure
   the TeamMem and MemberKit version numbers match while the workflow requires
   lockstep versions.
3. Use the version section as the basis for the GitHub release description.
4. The existing release workflow continues to validate matching versions and
   publish both packages. Changelog generation or enforcement is not added in
   this change.

## Out of scope

- Decoupling TeamMem and MemberKit versions or publication workflows.
- Editing historical GitHub release descriptions.
- Automatically generating the changelog from commit messages.
- Adding release automation or changelog-validation CI.

## Success criteria

1. A root `CHANGELOG.md` contains accurate `Unreleased`, `0.4.0`, `0.3.0`,
   `0.2.0`, and `0.1.0` sections in newest-first order.
2. TeamMem and MemberKit changes are clearly distinguished.
3. The Person folder/week layout and its generated-path compatibility impact
   are explicit under `0.4.0`.
4. Historical entries match the tagged implementation and published release
   descriptions, including the `0.2.0` Windows clarification.
5. README navigation and both package metadata files link to the changelog.
6. No package version is bumped and no release is created.
