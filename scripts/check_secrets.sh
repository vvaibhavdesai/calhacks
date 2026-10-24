#!/usr/bin/env bash
# A10 local secret check (pre-commit style). Same intent as CI's `secret-scan`
# job, runnable before you push.
#
#   scripts/check_secrets.sh            # scan git history + tracked files
#   scripts/check_secrets.sh --staged   # scan only what is staged (pre-commit)
#
# Use as a git pre-commit hook (opt-in, per clone):
#   printf '#!/bin/sh\nexec scripts/check_secrets.sh --staged\n' > .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# It NEVER prints a matched secret value: gitleaks runs with --redact and the
# fallback scanner prints file:line and the pattern name only. It only reads
# files git tracks (or has staged), so the gitignored .env is never opened.
#
# Uses gitleaks when installed (same pinned version as CI: 8.21.2,
# https://github.com/gitleaks/gitleaks/releases); otherwise falls back to a
# small built-in pattern scan, which is weaker -- install gitleaks for parity.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
mode="${1:-all}"
status=0

# 1. Env files must never be tracked or staged (only *.env.example and the
#    public apps/web fixture-mode files are allowed).
if [[ "$mode" == "--staged" ]]; then
  files=$(git diff --cached --name-only --diff-filter=ACMR)
else
  files=$(git ls-files)
fi
bad_env=$(printf '%s\n' "$files" | grep -E '(^|/)\.env($|\.)' \
  | grep -vE '\.example$|^apps/web/\.env\.fixtures(-[a-z0-9]+)?$' || true)
if [[ -n "$bad_env" ]]; then
  echo "check_secrets: env file(s) must not be committed:" >&2
  printf '%s\n' "$bad_env" | sed 's/^/  /' >&2
  status=1
fi

# 2. Content scan.
if command -v gitleaks >/dev/null 2>&1; then
  if [[ "$mode" == "--staged" ]]; then
    gitleaks protect --staged --config .gitleaks.toml --redact --no-banner || status=1
  else
    gitleaks detect --source . --config .gitleaks.toml --redact --no-banner || status=1
  fi
else
  echo "check_secrets: gitleaks not installed; using the weaker built-in scan." >&2
  patterns=(
    'anthropic-key:sk-ant-[A-Za-z0-9_-]{20,}'
    'aws-access-key:AKIA[0-9A-Z]{16}'
    'github-token:gh[pousr]_[A-Za-z0-9]{30,}'
    'slack-token:xox[baprs]-[A-Za-z0-9-]{10,}'
    'private-key:-----BEGIN [A-Z ]*PRIVATE KEY-----'
    'url-password:[a-z+]+://[A-Za-z0-9_.-]+:[A-Za-z0-9_.~%!*-]{6,}@'
  )
  while IFS= read -r f; do
    [[ -f "$f" ]] || continue
    for p in "${patterns[@]}"; do
      name="${p%%:*}"
      rx="${p#*:}"
      if [[ "$mode" == "--staged" ]]; then
        hits=$(git show ":$f" 2>/dev/null | grep -nE -- "$rx" | cut -d: -f1 || true)
      else
        hits=$(grep -nE -- "$rx" "$f" | cut -d: -f1 || true)
      fi
      for line in $hits; do
        # Committed local-dev defaults are documented as non-secrets
        # (.env.example, docker-compose dev defaults, CI-only passwords).
        if [[ "$name" == "url-password" ]] && \
           grep -qE 'encore_dev_password|encore_app_dev_password|encore_ci_password|\$\{' \
             <(sed -n "${line}p" "$f"); then
          continue
        fi
        echo "check_secrets: possible $name in $f:$line (value not shown)" >&2
        status=1
      done
    done
  done <<< "$files"
fi

if [[ $status -eq 0 ]]; then
  echo "check_secrets: OK"
fi
exit $status
