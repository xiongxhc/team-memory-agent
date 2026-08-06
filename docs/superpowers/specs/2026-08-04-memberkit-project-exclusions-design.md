# MemberKit Project Exclusions Design

**Status:** Implemented — approved 2026-08-04
**Date:** 2026-08-04
**Scope:** MemberKit draft generation and read-only exclusion inspection

## Problem

MemberKit currently projects every eligible claude-mem observation for the selected local date into a frozen `teammem-bundle/v1` event. Members can manually exclude events during review, but they cannot declare stable local rules for projects or recurring low-value summaries that should never enter a newly generated draft.

The immediate user need is to keep known-noise projects and summary patterns out of new drafts without changing the bundle protocol, rewriting existing drafts, losing reversibility, or adding a private wrapper around the released CLI. Rules must remain local to the member and must be safe for unattended `scheduled-run` execution.

## Goals

- Add a built-in local rules file at `MEMBERKIT_WORKDIR/exclude-projects.txt`.
- Preserve the existing user-authored rule syntax for exact projects, project prefixes, and project-scoped summary regular expressions.
- Validate the complete rules file before a command can write a bundle or draft state.
- Apply exclusions only while generating a missing draft or an explicitly forced draft.
- Keep excluded source observations out of MemberKit draft-state fingerprints so that rules remain reversible.
- Make the same rules apply to normal drafts, `draft --all`, and newly generated scheduled drafts.
- Provide read-only commands to inspect normalized rules and preview deterministic match counts.
- Preserve current schedules and the public `scheduled_run()` return contract.
- Migrate the existing local rules file away from the custom wrapper only after the released package is verified.

## Non-goals

- Matching against raw claude-mem narrative, facts, files, session metadata, or other source-only fields.
- Changing `teammem-bundle/v1`, adding exclusion metadata to bundle events, or introducing a new bundle schema version.
- Retrospectively scanning, filtering, or rewriting existing draft bundles.
- Synchronizing rules between members or treating local rules as team policy.
- Automatically pushing, approving, dismissing, or notifying about excluded events.
- Using an LLM to classify exclusions.
- Adding a new configuration key or requiring schedule reinstall when only rule contents change.
- Adding rule editing commands; members edit the UTF-8 text file directly.
- Refactoring the existing draft-state persistence format or adding a cross-file transaction between state and bundle files.

## Chosen Architecture

### Ownership

A new `memberkit.exclusions` module owns the rules path, parsing, validation, normalization, matching, and deterministic reporting. It has no dependency on Git, notification, LLM, scheduler, bundle persistence, or draft-state persistence.

The bundle layer continues to own source-database reads and frozen-v1 event projection. Exclusion matching receives already-projected v1 events and returns an included event list plus count-only match information. This ordering is mandatory: a regular expression sees exactly the event `summary` that a reviewer would otherwise receive, never the discarded source narrative.

The CLI owns user-facing `exclusions list` and `exclusions preview [--date]` commands and count reporting. The scheduling layer uses the same parser and matcher when it must generate a missing draft.

### Rules path

The path is always:

```text
<resolved MEMBERKIT_WORKDIR>/exclude-projects.txt
```

In code this is derived from `Config.workdir`; it is not a new environment or config field. The path is printed by `memberkit exclusions list`, including when the file is absent. A missing file is valid and means that there are no exclusion rules.

Each command reads and validates the file once and uses that in-memory snapshot for the entire invocation. A concurrent edit affects the next invocation, not the one already running.

### Interfaces

The implementation should expose small pure or read-only interfaces equivalent to:

```python
load_rules(path: Path) -> tuple[ExclusionRule, ...]
apply_rules(events: Sequence[BundleEvent], rules: Sequence[ExclusionRule]) -> ExclusionResult
```

`ExclusionRule` records the source line, rule kind, exact project or prefix, and compiled regular expression when applicable. It can render its normalized form for `list` without changing match semantics.

`ExclusionResult` contains:

- included events in their original order;
- the total number of excluded events;
- one count per rule in source-file order.

It must not retain or expose excluded summaries in CLI output. Internal event values may exist only for the duration of the local filtering operation.

The bundle layer should share one v1 projection path between draft generation and preview. Preview must not reimplement title/narrative fallback or any source query semantics independently.

## Rules File Grammar

The rules file is UTF-8 text. `LF` and `CRLF` line endings are accepted. Line numbers are one-based.

After removing the line ending:

- An empty or spaces-only line is ignored.
- A line whose first non-space character is `#` is a whole-line comment and is ignored.
- Leading and trailing spaces around a rule are ignored.
- Inline comments are not supported; `#` inside a project or regular expression is literal.
- ASCII C0 control characters (`U+0000` through `U+001F`) and `DEL` (`U+007F`) are invalid. Newline and an optional `CR` used only as the line ending are delimiters, not rule content. Other valid UTF-8, including non-ASCII Unicode characters outside that explicit range, is allowed.

The supported rule forms are:

```text
exact-project
project-prefix*
exact-project ~ regular-expression
```

The regular-expression delimiter is a `~` with at least one space on both sides. The parser splits on the first such delimiter. A bare `~` without surrounding spaces is part of an exact project name.

### Exact project

An exact-project rule matches when `event["project"] == configured_project`. Matching is case-sensitive. The project must be non-empty and must not contain `*`. An event whose project is `null` does not match any project rule.

### Project prefix

A project-prefix rule contains exactly one `*`, and it must be the last character of the normalized line. The non-wildcard prefix must be non-empty. It matches when the event project is not `null` and `event["project"].startswith(prefix)`. Matching is case-sensitive.

Examples:

```text
scratch*
test-repo-
```

The first line is a prefix rule. The second line has no wildcard and is therefore an exact-project rule. `*`, `foo*bar`, `foo**`, and `foo* ~ pattern` are invalid.

### Project-scoped summary regular expression

A regular-expression rule first requires a non-`null`, exact, case-sensitive project match. Only then is its compiled regular expression searched against the projected frozen-v1 event `summary` using case-insensitive search semantics equivalent to `re.search(pattern, summary, re.IGNORECASE)`.

Both operands must be non-empty after surrounding spaces are removed. The project operand cannot contain `*`. The regular expression is preserved as written except for its surrounding delimiter spaces. It is never run against the original source title, narrative, facts, files, session, journal, or entire serialized event.

Examples:

```text
team-memory-agent ~ ^task [0-9]+$
acme-sdk ~ test(s)? passed
```

### Validation and normalization

The complete file is decoded, parsed, and all regular expressions are compiled before draft generation can cause any bundle or draft-state write. Validation stops the invocation on the first error, but nothing parsed before that error is applied.

Invalid input includes:

- invalid UTF-8;
- an empty project operand;
- an empty regular expression;
- any unsupported `*` placement in an exact/prefix rule or in a regex rule's project operand; `*` remains ordinary regex syntax inside the regex operand;
- a wildcard inside a regex rule's project operand;
- an invalid regular expression;
- `U+0000` through `U+001F` or `U+007F` within line content.

Errors identify the rules path, one-based line number when available, and error category. They do not echo the invalid line or regular-expression pattern. For example:

```text
exclude-projects.txt:12: invalid regular expression
```

`memberkit exclusions list` displays valid rules in source order with one-based rule ordinals and canonical spacing:

```text
1  exact-project
2  project-prefix*
3  exact-project ~ regular-expression
```

Blank lines and comments do not consume ordinals. Duplicate rules are valid and retained because source order controls match attribution. A duplicate that follows an equivalent rule will normally report zero matches.

## Matching and Counting Semantics

Each projected event is evaluated against rules in file order. An event is excluded if any rule matches. For reporting, the event is assigned only to the first matching rule; later overlapping rules do not increment their counts.

This first-match attribution affects reports only. Exclusion itself is boolean and does not add metadata to the event or bundle.

Event ordering is preserved for all included events. Matching does not deduplicate, rank, consolidate, or otherwise change the source event stream.

Count output discloses eligible, excluded, and remaining totals plus per-rule counts where requested. It never prints matched event summaries, source narratives, or observation payloads. `list` necessarily displays the member-authored normalized rule text; validation errors avoid repeating invalid content.

## Command Data Flows

### `memberkit draft [--date DATE] [--force]`

For a missing draft, or when `--force` explicitly authorizes replacement:

1. Resolve configuration, local date, output path, and rules path.
2. Read and validate the entire rules file into one immutable snapshot.
3. Read the source database in read-only mode.
4. Project eligible observations into frozen-v1 events using the existing projection and ordering.
5. Validate the complete projected frozen-v1 bundle before matching.
6. Apply exclusions to the validated projected events; `project: null` is non-matching for every rule kind.
7. Call `DraftState.refresh` only with included events; this retains its existing behavior of persisting the refreshed state.
8. Render `journal_md` only from included events.
9. Validate the final filtered bundle and atomically replace it through the existing bundle writer.
10. Print the excluded total, including zero.

Rule loading, full-file validation, source projection, and exclusion matching must finish before `DraftState.refresh` can write state. An invalid rules file therefore cannot partially change either state or a bundle. After state refresh begins, the existing persistence order and failure behavior remain unchanged: state is saved first, and the bundle is then atomically replaced. This feature does not claim or introduce a cross-file transaction between those two files.

If every event is excluded, direct draft still writes a valid frozen-v1 bundle with an empty `events` list. Its `journal_md` contains the normal date heading and no project sections, consistent with the existing renderer and protocol.

When the destination already exists and `--force` is absent, the command retains its current refusal behavior. It does not scan or apply rules to the existing file.

`memberkit draft --all` remains a compatibility alias for the raw-observation selection mode, not an exclusion bypass. It uses the same rules snapshot and matching stage.

### `memberkit scheduled-run`

The standard scheduler action remains exactly `memberkit scheduled-run` on every supported platform. No shell wrapper, launchd edit, Task Scheduler edit, or reinstall is needed when the rules file changes.

At invocation start, scheduled-run loads and validates one rules snapshot before processing yesterday and today. This occurs before any existing-draft state refresh, so an invalid file cannot cause a partial draft-state write.

For each candidate date:

- If no draft exists, use the same projection and exclusion path as direct draft, then refresh state and write only the included events and journal.
- If no included events remain, retain the current scheduled behavior of not creating an empty pending draft.
- If a draft already exists, do not apply exclusions, replace events, or rewrite it because rules changed.
- If an existing draft is invalid, preserve it byte-for-byte and keep the date pending under existing behavior.
- If an existing draft is valid or member-edited, exclusion rules do not alter its event set. Existing scheduled-run state-refresh behavior may continue after successful rule validation.

Internally, preparation may return structured count information for CLI logging, but the public `scheduled_run()` return value remains the existing list of ready dates. The CLI prints or logs one excluded total for each newly generated date. On Windows the existing scheduled log receives the count; on macOS the scheduled process output receives it. Counts do not trigger notifications, and excluded events never cause a push.

An invalid rules file aborts the invocation before any bundle or state write. The sanitized error is visible in normal command output and platform logs. No success notification is sent for that failed run.

### `memberkit review` and `memberkit push`

Review and push do not load or apply exclusion rules to an existing bundle. They never remove an event merely because a new or changed rule would match it. This preserves review decisions and prevents a rules edit from becoming a retroactive mutation.

The exclusion feature does not change review/push preflight behavior: valid bundles may still have `journal_md` regenerated from their unchanged event set by the existing preparation path, and invalid bundles remain byte-preserved on failure. Therefore the event payload is preserved with respect to exclusions; the design does not promise that review/push cease their existing journal normalization.

### `memberkit exclusions list`

This read-only command:

1. Resolves and prints the rules file path.
2. Treats a missing file as zero rules.
3. Validates the complete existing file.
4. Prints valid normalized rules in source order.

It does not open the source database, read or write bundles or state, contact a network, run an LLM, invoke Git, notify, or push.

### `memberkit exclusions preview [--date DATE]`

This read-only command:

1. Resolves the date using the same local-date and timezone rules as `draft`; `--date` is optional and defaults to today in the resolved member timezone.
2. Loads and validates the rules snapshot.
3. Reads the source database in read-only mode using the same source selection as `draft`.
4. Uses the shared frozen-v1 projection.
5. Validates the complete projected frozen-v1 bundle.
6. Applies the shared matcher.
7. Prints the rules path, eligible total, each rule's first-match count, excluded total, and remaining total.

Preview does not inspect or mutate an existing output bundle. It does not write a new bundle, journal, or state; contact a network; run an LLM; invoke Git; notify; or push. Running it repeatedly against an unchanged source database and rules file produces the same counts and ordering.

## Draft State and Reversibility

Only included events are passed to `DraftState.refresh`. Consequently, filtered events never enter `approved`, `excluded`, or `pending` fingerprint collections and are not recorded as manually dismissed.

Removing or narrowing a rule and running `memberkit draft --force` can restore the underlying source observation because the source database remains authoritative. It will not restore an event that is independently absent because it was already approved, manually excluded, dismissed, deleted at the source, or falls outside current source-selection rules.

Changing rules alone never rewrites historical bundles. A member must explicitly force-draft a date to regenerate it. Existing valid, invalid, or member-edited drafts are not retroactively filtered by scheduled-run, review, push, list, or preview.

## Failure and Safety Behavior

- Missing rules file: behave as zero rules.
- Unreadable file, invalid UTF-8, invalid syntax, control character, or regex compilation failure: fail closed before bundle/state writes.
- Source database, initial projected-bundle validation, or exclusion-matching failure before state refresh: preserve existing output and state.
- State refresh, final bundle validation, or bundle-write failure after state refresh begins: retain the existing state-first persistence semantics; do not claim cross-file rollback.
- Bundle validation or atomic write failure: do not report the draft as ready and do not push.
- Rules edits during an invocation: use the already loaded snapshot; validate again next invocation.
- Overlapping rules: exclude once and count against the first match only.
- Sensitive observation content: never include summaries or narratives in list, preview, count, or validation-error output.

Rules are local, member-authored configuration. This release does not sandbox regular-expression execution or accept rules from a remote/team-controlled source. A pathological local expression can make preview or a scheduled run slow; members should preview new regex rules before relying on unattended execution. That trust boundary must be documented; remote rule synchronization would require a separate threat model.

## Local Migration

The current local file `~/.memberkit/exclude-projects.txt` is already at the default workdir path and remains in place. Migration must not edit it, the existing seven clean draft bundles, MemberKit draft state, or any launchd configuration during implementation.

The deployment sequence is deliberately separate from code merge and package publication:

1. Implement, review, and release the built-in feature.
2. Install the released package in a controlled local verification environment.
3. Compare `exclusions list` and `exclusions preview` results with the current wrapper's intended behavior without generating or rewriting drafts.
4. Verify a disposable or new-date draft path with the released package.
5. Only after verification, replace the custom wrapper with the native CLI.
6. If the installed schedule still points to the wrapper, reinstall the schedule as a separate explicit local deployment action so its action points to native `memberkit scheduled-run`.

Ordinary future edits to `exclude-projects.txt` do not require schedule reinstall. Retaining the wrapper and its existing schedule until released-package verification provides rollback.

## Documentation Changes Required During Implementation

- `packages/memberkit/README.md`: rules syntax, default path, draft behavior, read-only commands, and schedule behavior.
- `docs/member-guide.md`: examples, exact case/regex semantics, reversibility, force-draft workflow, and platform-independent schedule note.
- Root `README.md`: concise MemberKit capability and link to the member guide.
- Release notes/changelog: feature availability, no schema change, no retroactive rewrite, and migration guidance. Update `CHANGELOG.md` only if it exists on the implementation branch; otherwise carry the release-note requirement into the integration/release handoff rather than modifying another worktree.
- Privacy/security documentation: summary-only regex scope, count-only output, local trusted-rule boundary, and no automatic push.

The frozen-v1 schema document must not be modified to imply that exclusions are protocol metadata; exclusions are a local pre-bundle generation policy.

## Test Plan

### Parser and validation

- Missing file produces an empty rules tuple.
- Empty lines, spaces-only lines, comments, surrounding spaces, `LF`, and `CRLF` behave exactly as specified.
- Exact, trailing-star prefix, and project-scoped regex rules normalize and match correctly.
- Exact and prefix project matching is case-sensitive.
- Regex project matching is case-sensitive while regex search is case-insensitive.
- A frozen-v1 event whose project is `null` does not match exact, prefix, or project-scoped regex rules and never raises a matching error.
- Regex sees only the final projected v1 summary, including title/narrative fallback behavior, and cannot match discarded raw narrative when the projected summary differs.
- Bare `~` remains valid project text; only the spaced delimiter starts a regex rule.
- Empty project, empty regex, prefix-only `*`, multiple/middle wildcards, wildcard regex project, invalid regex, invalid UTF-8, tab, other control characters, and `DEL` fail.
- Errors contain path/category and line number where available but not the invalid pattern or line.
- Duplicate and overlapping rules preserve order and first-match attribution.
- Included events retain their input order.

### Direct draft

- Missing draft validates the projected v1 bundle, then applies rules before state refresh, journal generation, final validation, and write.
- `--force` regenerates from source with the current rules snapshot.
- Existing draft without `--force` is unchanged.
- `--all` obeys exclusions.
- All-filtered direct draft writes a valid empty v1 bundle whose journal contains only the normal date heading.
- Excluded events never appear in approved, excluded, or pending fingerprints.
- Removing a rule plus force-draft restores an otherwise eligible observation.
- Invalid rules cause no bundle or state write, including when earlier lines were valid.
- CLI output reports an excluded total without summaries.

### Scheduled run

- Rules load once per invocation and apply to missing yesterday/today drafts.
- All-filtered scheduled dates do not create empty pending files.
- Existing valid, invalid, and member-edited drafts are not exclusion-filtered or replaced.
- Invalid rules abort before any bundle or draft-state write and do not send success notification.
- Existing return value remains the ready-date list.
- Per-new-date excluded totals reach normal output and Windows scheduled logs without summaries.
- macOS launchd and Windows Task Scheduler actions remain native `memberkit scheduled-run` with no wrapper or rule-file argument.
- Editing only the rule contents affects the next run without schedule reinstall.

### Read-only commands

- `exclusions list` prints the resolved path and normalized ordered rules; missing file prints zero rules.
- `exclusions preview [--date]` uses shared source selection, projection, and matching and reports eligible/per-rule/excluded/remaining counts; omitted date defaults to today in the member timezone.
- Preview assigns overlap to the first rule and produces stable output for stable inputs.
- List and preview do not modify bundle/state bytes or timestamps.
- List and preview never invoke Git, network, LLM, notification, or push adapters.
- Output contains no matched summaries or raw source content.

### Regression and platform coverage

- Existing bundle validation, review, push, state, scheduling, and frozen-v1 tests continue to pass.
- CLI help and command dispatch work on macOS, Windows, and Linux imports without loading platform-incompatible scheduler modules.
- Test homes and workdirs are isolated so ambient `~/.memberkit` state cannot affect results.
- The pre-change baseline is **1014 passed, 1 skipped**; implementation verification must report the new full-suite result against that baseline.

## Acceptance Criteria

The design is complete when implementation can demonstrate all of the following:

1. A member can place valid rules in `MEMBERKIT_WORKDIR/exclude-projects.txt` and both direct and scheduled generation exclude matching projected v1 events.
2. Exact, trailing-star prefix, and project-scoped case-insensitive summary-regex semantics match this specification exactly.
3. A malformed file produces a sanitized line-numbered error before any bundle or state write.
4. Existing drafts are never retroactively filtered because rules changed.
5. Excluded events do not enter state fingerprints and can return after rule removal plus explicit force-draft when otherwise eligible.
6. `draft --all` obeys the same exclusions.
7. `exclusions list` and `exclusions preview [--date]` are deterministic and side-effect free and disclose counts rather than observation content.
8. Standard scheduled actions remain `memberkit scheduled-run`; changing rules requires no wrapper or reinstall.
9. Current local drafts, state, rules, and launchd configuration remain untouched until a separately authorized released-package migration.
10. The complete suite passes with new parser, integration, state, schedule, CLI, privacy, and cross-platform coverage.
