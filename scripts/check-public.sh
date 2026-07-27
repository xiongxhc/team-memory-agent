#!/bin/sh
set -eu

bad_files=$(find . -type f \
  \( -name '*.db' -o -name '*.pem' -o -name '*.key' -o -name '.env' \) \
  ! -path './.git/*' ! -path './.venv/*' ! -path '*/__pycache__/*')
if [ -n "$bad_files" ]; then
  echo "runtime or credential-like files found:"
  echo "$bad_files"
  exit 1
fi

files=$(find . -type f \
  ! -path './.git/*' ! -path './.venv/*' ! -path '*/__pycache__/*' \
  ! -path '*/.pytest_cache/*' ! -path '*.egg-info/*')

if grep -En \
  '(/Users/[A-Za-z0-9._-]+/|10\.[0-9]+\.[0-9]+\.[0-9]+|gitlab\.company\.local|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|=[A-Za-z0-9_/-]{32,})' \
  $files; then
  echo "private path, infrastructure, or credential-like content found"
  exit 1
fi

echo "public-content scan passed"
