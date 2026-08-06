# Version Changelog Implementation Plan

> **Provenance update (2026-08-06):** This plan predates the 2026-08-04
> `v0.4.0` tag. Both manifests at that tag declare `0.4.0`; this documentation
> change does not alter the current checkout's package versions or create a
> release. The completed `0.4.0` work now belongs in its dated section, while
> post-tag and pending-integration work remains under `Unreleased`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an accurate source-controlled version history for TeamMem and MemberKit, including every published release through `v0.4.0`, post-release work, and clearly labelled pending-integration changes.

**Architecture:** Keep one root changelog because the release workflow versions and publishes both packages together. Organize every version by package and change type, then expose the file from the root README and both packages' project metadata.

**Tech Stack:** Markdown, TOML project metadata, Git tags and GitHub release descriptions

## Global Constraints

- Follow `docs/superpowers/specs/2026-08-03-changelog-design.md`.
- Preserve the published `v0.1.0`, `v0.2.0`, `v0.3.0`, and `v0.4.0` history; do not edit existing GitHub releases.
- Keep TeamMem and MemberKit changes visibly separate.
- Do not change package versions or create a release.
- Do not add changelog generation or validation automation.
- Do not commit unless the user explicitly requests a commit.

---

### Task 1: Backfill the public changelog

**Files:**
- Create: `CHANGELOG.md`

**Interfaces:**
- Consumes: Git tags `v0.1.0` through `v0.4.0`; their published GitHub release descriptions; commits in `v0.4.0..master`; and separately pending integration commits that are explicitly labelled as such
- Produces: the canonical human-readable version history used by repository readers and future GitHub releases

- [ ] **Step 1: Create the changelog header and release ordering**

Create `CHANGELOG.md` with this exact top-level order:

```markdown
# Changelog

Notable changes to Team Memory Agent and MemberKit are documented here.

## Unreleased

## 0.4.0 — 2026-08-04

## 0.3.0 — 2026-07-30

## 0.2.0 — 2026-07-30

## 0.1.0 — 2026-07-27
```

- [ ] **Step 2: Add post-`v0.4.0` and pending-integration TeamMem changes**

Under `Unreleased`, add a `### TeamMem` section. Document rolling synthesis and
capture-only operation as post-release work. Label GitLab all-branch/merged-MR
backfill, capitalized/lowercase docs-sync compatibility, and public-boundary
cleanup as pending integration for the same release train; do not present them
as shipped or as present on `master`.

```markdown
The prior implementation text in this plan described the `v0.4.0` contents
before the tag existed. Those entries now belong under the dated `0.4.0`
section, alongside MemberKit project exclusions.
```

Do not include the `hub.env` CLI-test isolation change because it has no installed
runtime behavior.

- [ ] **Step 3: Backfill version 0.4.0**

Move all changes through tag `v0.4.0` into that dated section: GitLab
issue/repository collection and reliability hardening; Person weekly folders
and path compatibility; tolerant bundle timestamps; and MemberKit standing
project exclusions. Do not leave these entries under `Unreleased`.

- [ ] **Step 4: Backfill version 0.3.0**

Record only behavior that shipped in the `v0.3.0` tag:

```markdown
### MemberKit

#### Added

- Native Windows Task Scheduler support for install, status, replacement,
  removal, missed-run catch-up, and overlap prevention.
- `memberkit setup` supports protected Windows configuration and explicit
  schedule opt-in.

#### Changed

- The same scheduling commands dispatch to launchd on macOS and Task Scheduler
  on Windows. Linux users invoke `memberkit scheduled-run` from their own
  scheduler.
- Scheduling remains opt-in. Scheduled runs prepare local drafts and reminders
  but never approve, push, or transmit member data.

#### Fixed

- Managed Windows tasks validate ownership and use transactional recovery,
  bounded logs, value-safe diagnostics, current-user `InteractiveToken`, least
  privilege, and native Windows CI verification.
```

- [ ] **Step 5: Backfill version 0.2.0 with the historical clarification**

Use separate TeamMem and MemberKit sections. Include the official GitHub,
GitLab, Slack, Feishu, and Discord connectors; their normalized event interface;
explicit hub scheduling; evidence-first MemberKit drafts; regenerated review
journals; explicit-only push; and manual reviewed fallback for unsupported
sources. End the MemberKit section with this note:

```markdown
> The v0.2.0 release description announced native Windows MemberKit scheduling
> before that lifecycle was complete. Version 0.3.0 completed and verified it.
```

- [ ] **Step 6: Backfill version 0.1.0**

Use separate TeamMem, MemberKit, and Protocol sections to record:

- local-first SQLite activity ledger and regenerated Markdown reports;
- independently installable, member-reviewed MemberKit;
- opt-in local scheduling with no automatic transmission;
- frozen `teammem-bundle/v1` import protocol;
- Apache-2.0 licensing.

- [ ] **Step 7: Check changelog completeness and formatting**

Run:

```bash
grep -nE '^## |^### |^#### ' CHANGELOG.md
git diff --check -- CHANGELOG.md
```

Expected: versions appear newest first, both packages are distinguishable, and
`git diff --check` prints nothing.

### Task 2: Make the changelog discoverable

**Files:**
- Modify: `README.md:368-375`
- Modify: `pyproject.toml:24-28`
- Modify: `packages/memberkit/pyproject.toml:23-27`

**Interfaces:**
- Consumes: root `CHANGELOG.md` from Task 1
- Produces: repository and PyPI metadata links to the canonical changelog

- [ ] **Step 1: Link the changelog from README navigation**

Add the changelog to the existing `See ...` documentation list near the end of
`README.md` using:

```markdown
[changelog](https://github.com/xiongxhc/team-memory-agent/blob/master/CHANGELOG.md),
```

Keep the surrounding sentence grammatical and preserve the existing architecture,
privacy, deployment, MemberKit, and bundle-contract links.

- [ ] **Step 2: Add the TeamMem package metadata link**

In root `[project.urls]`, add:

```toml
Changelog = "https://github.com/xiongxhc/team-memory-agent/blob/master/CHANGELOG.md"
```

- [ ] **Step 3: Add the MemberKit package metadata link**

In `packages/memberkit/pyproject.toml` `[project.urls]`, add the same `Changelog`
URL. Both PyPI project pages should therefore expose the shared monorepo history.

- [ ] **Step 4: Validate TOML and link targets**

Run:

```bash
python3 -c 'import tomllib; from pathlib import Path; files=[Path("pyproject.toml"), Path("packages/memberkit/pyproject.toml")]; assert all(tomllib.loads(p.read_text())["project"]["urls"]["Changelog"].endswith("/CHANGELOG.md") for p in files)'
test -f CHANGELOG.md
grep -n 'CHANGELOG.md' README.md pyproject.toml packages/memberkit/pyproject.toml
git diff --check
```

Expected: every command exits zero; the grep reports all three discoverability
locations; `git diff --check` prints nothing.

- [ ] **Step 5: Run the public repository validation**

Run:

```bash
./scripts/check-public.sh
.venv/bin/pytest -q tests packages/memberkit/tests
```

Expected: public-boundary checks pass and the complete test suite passes.

- [ ] **Step 6: Review the final diff without committing**

Run:

```bash
git diff -- CHANGELOG.md README.md pyproject.toml packages/memberkit/pyproject.toml docs/superpowers/specs/2026-08-03-changelog-design.md docs/superpowers/plans/2026-08-03-changelog.md
git status --short
```

Expected: only the approved design, plan, changelog, README, and metadata files
are changed or untracked. Report the result and wait for explicit commit or push
authorization.
