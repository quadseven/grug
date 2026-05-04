#!/usr/bin/env bash
# Drift-detection lint for files that MUST stay byte-identical between
# services/api/ and services/webhook/. Closes #66.
#
# v1 trade-off (PRD #21): both Lambda services duplicate shared modules
# instead of importing from a shared package. Until extraction lands,
# this script catches accidental one-side edits at PR time.
#
# Files that intentionally differ (FastAPI app, lambda handler entrypoint,
# logger name) are NOT in MIRRORED_FILES — they're allowlisted to diverge.
#
# Add new mirrored files to MIRRORED_FILES below. Add intentionally-
# diverging files NOWHERE — they're skipped by omission.

set -euo pipefail

cd "$(dirname "$0")/.."

MIRRORED_FILES=(
  "adapters/__init__.py"
  "adapters/install_store.py"
  "conftest.py"
  "github_app_auth/__init__.py"
  "github_checks_client.py"
  "observability.py"
  "personas/__init__.py"
  "personas/tpm/__init__.py"
  "personas/tpm/dor_checks.py"
  "ports/__init__.py"
  "ports/token_cache.py"
  "secrets_loader.py"
)

DIVERGED=()
MISSING=()

for f in "${MIRRORED_FILES[@]}"; do
  api="services/api/$f"
  webhook="services/webhook/$f"
  if [ ! -f "$api" ]; then
    MISSING+=("$api")
    continue
  fi
  if [ ! -f "$webhook" ]; then
    MISSING+=("$webhook")
    continue
  fi
  if ! diff -q "$api" "$webhook" >/dev/null 2>&1; then
    DIVERGED+=("$f")
  fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
  echo "ERROR: missing mirror files (one side absent):"
  for m in "${MISSING[@]}"; do echo "  - $m"; done
  echo
  echo "Either remove from MIRRORED_FILES in scripts/check-mirrored-files.sh"
  echo "OR create the missing copy."
  exit 2
fi

if [ ${#DIVERGED[@]} -gt 0 ]; then
  echo "ERROR: paired files diverged between services/api/ + services/webhook/:"
  for d in "${DIVERGED[@]}"; do
    echo
    echo "=== $d ==="
    diff -u "services/api/$d" "services/webhook/$d" | head -50
  done
  echo
  echo "Both copies must stay byte-identical (PRD #21 v1 duplicate-file pattern)."
  echo "Apply the same change to both. Until shared-package extraction lands,"
  echo "this lint catches accidental one-side edits."
  exit 1
fi

echo "OK: ${#MIRRORED_FILES[@]} mirrored file pairs are byte-identical."
