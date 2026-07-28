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
