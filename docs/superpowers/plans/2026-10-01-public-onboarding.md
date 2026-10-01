# Public hub onboarding corrections

## Problem and scope

Operators cannot complete unattended publication from the public instructions,
and an available Claude backend receives placeholder model identifiers. The
public engine must stand on its own without private deployment code.

## Acceptance criteria

- Document a separate private rendered-vault repository, empty versus existing
  remote setup, selected branch/upstream, service-user unattended read/write
  authentication, verified first push, and only then `TEAMMEM_PUSH=1`.
- Never force or overwrite remote history; never embed credentials in URLs.
  Distinguish private projection publication from authoritative ledger backups.
- Claude synthesis requires explicit nonblank, non-placeholder operator models
  when its API or CLI backend is available. Errors identify the configuration
  keys without making a provider call. Config loading, collection, rendering,
  dry runs, no-backend skips, and the existing Codex path remain usable.
- Generic engine/setup documentation belongs here. Operator-specific identity,
  credentials, networks, monitoring and recovery remain operator responsibilities.

## Implementation and verification

1. Baseline public hub suite: 892 passed on Python 3.12.
2. Add regression tests for absent/blank/legacy models, API and CLI resolution,
   no backend, direct CLI errors, and daily collection/render continuation;
   observe failures before implementation.
3. Remove placeholder defaults, validate at synthesis resolution, and turn
   standalone command configuration errors into concise exit-2 diagnostics.
4. Complete deployment instructions and align README/operator guidance.
5. Run focused tests, full hub CI tier, and public-source scan; parent performs
   independent MemberKit/chat/integration checks and reviews before commits.

No provider calls, publication, schedules, private configuration, or live hosts
are changed by this work.

## Verification evidence

- New regression cases: 26 failed / 4 passed before implementation; the suite
  now has 34 passing cases, including requested-model-only and dry-run coverage.
- Focused config/services/CLI/daily suite: 183 passed.
- Full public hub CI tier: 926 passed on Python 3.12.
- Public-content scan and whitespace checks passed. Empty-remote clone/orphan
  branch commands were exercised against a disposable local bare repository.
- Provider model entitlement and remote credentials remain operator checks; no
  live provider or publication operation was performed.
