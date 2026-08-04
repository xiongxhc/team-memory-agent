#!/bin/sh
set -eu

bad_files=$(git ls-files -- \
  '*.db' '*.pem' '*.key' '.env' '*/.env' \
  'hub.env' '*/hub.env' 'memberkit.env' '*/memberkit.env')
if [ -n "$bad_files" ]; then
  echo "runtime or credential-like files found:"
  echo "$bad_files"
  exit 1
fi

matches=$(git grep -nI -E \
  -e '(/Users/[A-Za-z0-9._-]+/)' \
  -e '(^|[^0-9])(10\.[0-9]+\.[0-9]+\.[0-9]+|192\.168\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+)([^0-9]|$)' \
  -e '(^|[^0-9A-Fa-f:])([Ff][CcDd][0-9A-Fa-f]{2}|[Ff][Ee][89AaBb][0-9A-Fa-f]):' \
  -e 'gitlab\.company\.local' \
  -e 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY' \
  -e '(gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})' \
  -e '"(api[_-]?(key|token)|access[_-]?token|auth[_-]?token|client[_-]?secret|password|private[_-]?key|token|secret)"[[:space:]]*:[[:space:]]*"[A-Za-z0-9_./+=$:@-]{16,}"' \
  -e '^[[:space:]]*(api[_-]?(key|token)|access[_-]?token|auth[_-]?token|client[_-]?secret|password|private[_-]?key|token|secret):[[:space:]]*[A-Za-z0-9_./+=$:@-]{16,}' \
  -e '^[[:space:]]*[A-Z0-9_]*(API_KEY|API_TOKEN|TOKEN|SECRET|PASSWORD|PRIVATE_KEY)[A-Z0-9_]*[[:space:]]*=[[:space:]]*"?[A-Za-z0-9_./+=$:@-]{16,}' \
  -- || test $? -eq 1)
if [ -n "$matches" ]; then
  printf '%s\n' "$matches"
  echo "private identity, infrastructure, or credential-like content found"
  exit 1
fi

private_wording_matches=
for candidate in \
  README.md docs/deployment.md docs/architecture.md docs/privacy.md teammem/render.py; do
  if [ -f "$candidate" ]; then
    private_wording_status=0
    candidate_matches=$(git grep -niI -E \
      -e 'existing private deployment' \
      -e 'private internal deployment' \
      -e 'company vault' \
      -- "$candidate") || private_wording_status=$?
    case "$private_wording_status" in
      0|1) ;;
      *) exit "$private_wording_status" ;;
    esac
    if [ -n "$candidate_matches" ]; then
      private_wording_matches="${private_wording_matches}${private_wording_matches:+
}${candidate_matches}"
    fi
  fi
done

if [ -n "$private_wording_matches" ]; then
  printf '%s\n' "$private_wording_matches"
  echo "private deployment wording found"
  exit 1
fi

git_identity_status=0
git_identity_matches=$(git grep -nI -E \
  -e '(^|[[:space:]])GIT_(AUTHOR|COMMITTER)_(NAME|EMAIL)[[:space:]]*=' \
  -- 'docs/superpowers/plans/*.md') || git_identity_status=$?
case "$git_identity_status" in
  0|1) ;;
  *) exit "$git_identity_status" ;;
esac
if [ -n "$git_identity_matches" ]; then
  printf '%s\n' "$git_identity_matches"
  echo "hard-coded Git author identity found"
  exit 1
fi

obsolete_schedule_claims=$(git grep -niI -E \
  -e '(teammem|team memory agent|hub|package|command)[^.]*(has no|lacks)[^.]*(built-in[[:space:]]+)?(hub[[:space:]]+)?schedul(e|ing)' \
  -e '(teammem|team memory agent|hub|package|command)[^.]*does not( yet)?[[:space:]]+(provide|include|support)[^.]*(built-in[[:space:]]+)?(hub[[:space:]]+)?schedul(e|ing)' \
  -e '(teammem|team memory agent|hub|package|command)[^.]*(defers?|will defer)[^.]*(built-in[[:space:]]+)?(hub[[:space:]]+)?schedule installation' \
  -e '(built-in[[:space:]]+)?hub[[:space:]]+schedule[[:space:]-]+installation[^.]*(will come|comes?)[^.]*(later|future)' \
  -e '(built-in[[:space:]]+)?hub[[:space:]]+schedule[[:space:]-]+installation[^.]*(belongs to|is deferred to)[^.]*(later|future|external|separate|scheduler)' \
  -e '(built-in[[:space:]]+)?hub[[:space:]]+schedule[[:space:]-]+installation[^.]*is not part of' \
  -- \
  README.md \
  docs/deployment.md \
  docs/architecture.md \
  docs/privacy.md \
  || test $? -eq 1)
if [ -n "$obsolete_schedule_claims" ]; then
  printf '%s\n' "$obsolete_schedule_claims"
  echo "obsolete hub-scheduling claim found"
  exit 1
fi

# The Windows scheduler is deliberately current-user and interactive-only.  Keep
# the public operator contract explicit, rather than allowing a later doc edit
# to quietly imply a background-service or credentialed mode.
windows_contract_file=docs/deployment.md
if [ -f "$windows_contract_file" ] && [ -f pyproject.toml ]; then
  for required_windows_claim in \
    'logged-in-only' \
    'screen lock' \
    'logout prevents runs' \
    'StartWhenAvailable' \
    'machine must remain powered' \
    'no password' \
    'no S4U' \
    'no shell wrapper'; do
    if ! grep -Fq "$required_windows_claim" "$windows_contract_file"; then
      echo "missing Windows scheduling contract: $required_windows_claim"
      exit 1
    fi
  done

  windows_claim_text=$(for windows_doc in \
    README.md docs/deployment.md docs/architecture.md docs/privacy.md; do
    if [ -f "$windows_doc" ]; then
      grep -nH . "$windows_doc" || test $? -eq 1
    fi
  done)
  windows_positive_claims=$(printf '%s\n' "$windows_claim_text" | grep -viE \
    'does not (run after logout|use S4U|store a password|use a shell wrapper|invoke a shell wrapper|have a shell wrapper)|no (password|S4U|shell wrapper)|without (a )?(password|S4U|shell wrapper)' \
    || test $? -eq 1)
  unsupported_windows_claims=$(printf '%s\n' "$windows_positive_claims" | grep -niE \
    -e '(Windows[^.]*runs after logout|runs after logout[^.]*Windows)' \
    -e '(Windows[^.]*uses S4U|uses S4U[^.]*Windows)' \
    -e '(Windows[^.]*stores? (a )?password|stores? (a )?password[^.]*Windows)' \
    -e '(Windows[^.]*(uses?|invokes?|has) (a )?shell wrapper|(uses?|invokes?|has) (a )?shell wrapper[^.]*Windows)' \
    || test $? -eq 1)
  if [ -n "$unsupported_windows_claims" ]; then
    printf '%s\n' "$unsupported_windows_claims"
    echo "unsupported Windows scheduling claim found"
    exit 1
  fi
fi

if [ -n "${TEAMMEM_PUBLIC_DENY_REGEX:-}" ]; then
  private_matches=$(git grep -nI -E \
    -e "$TEAMMEM_PUBLIC_DENY_REGEX" -- || test $? -eq 1)
  if [ -n "$private_matches" ]; then
    printf '%s\n' "$private_matches"
    echo "operator-supplied private identifier found"
    exit 1
  fi
fi

echo "public-content scan passed"
