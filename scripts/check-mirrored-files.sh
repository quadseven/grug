#!/usr/bin/env bash
# Drift-detection lint for files that MUST stay in lockstep between
# services/api/ and services/webhook/. Closes #66; extended for #139.
#
# v1 trade-off (PRD #21 + ADR-0001): both Lambda services duplicate
# shared modules instead of importing from a shared package. Until
# rule-of-three triggers extraction (ADR-0001), this script catches
# accidental one-side edits at PR time.
#
# Two mirror modes:
#
#   MIRRORED_WITH_HEADER  — files carrying a `# MIRRORED — sibling at
#                           services/<other>/<path>; ...` header on
#                           line 1 (added by #138). Body (line 2+)
#                           must be byte-identical; header must point
#                           at the correct counterpart.
#
#   MIRRORED_BYTE_IDENTICAL — files fully byte-identical (typically
#                             empty __init__.py or low-content). No
#                             header required.
#
# Files that intentionally differ (FastAPI app, lambda handler entry
# points, etc.) are NOT in either list — they're allowlisted to diverge
# by omission.

set -euo pipefail

cd "$(dirname "$0")/.."

MIRRORED_WITH_HEADER=(
  "activity_log.py"
  "adapters/install_store.py"
  "adapters/pg_base.py"
  "adapters/pg_install_store.py"
  "cf_auth.py"
  "code_review_prompt.py"
  "enforcement.py"
  "github_checks_client.py"
  "github_reviews_client.py"
  "github_rulesets_client.py"
  "llm_client.py"
  "observability.py"
  "personas/registry.py"
  "personas/code_reviewer/dedup.py"
  "personas/code_reviewer/diff_parser.py"
  "review_types.py"
  "personas/code_reviewer/dispatch.py"
  "personas/code_reviewer/judge.py"
  "personas/code_reviewer/persona.py"
  "personas/code_reviewer/reactions.py"
  "personas/code_reviewer/iac_scan.py"
  "personas/code_reviewer/sast.py"
  "personas/code_reviewer/sca.py"
  "personas/code_reviewer/webhook_dispatch.py"
  "personas/tpm/dor_checks.py"
  "personas/tpm/webhook_dispatch.py"
  "ports/token_cache.py"
  "readiness.py"
  "secrets_loader.py"
)

MIRRORED_BYTE_IDENTICAL=(
  "adapters/__init__.py"
  "conftest.py"
  "github_app_auth/__init__.py"
  "personas/__init__.py"
  "personas/code_reviewer/__init__.py"
  "personas/tpm/__init__.py"
  "ports/__init__.py"
)

ADR_REF="docs/adr/0001-mirror-with-rule-of-three-deferral.md"

BODY_DIVERGED=()
HEADER_BAD=()
MISSING=()

# --- MIRRORED_WITH_HEADER: header check + body diff ---
for f in "${MIRRORED_WITH_HEADER[@]}"; do
  api="services/api/$f"
  webhook="services/webhook/$f"
  if [ ! -f "$api" ]; then MISSING+=("$api"); continue; fi
  if [ ! -f "$webhook" ]; then MISSING+=("$webhook"); continue; fi

  expected_api_header="# MIRRORED — sibling at services/webhook/$f; keep in lockstep. See $ADR_REF."
  expected_webhook_header="# MIRRORED — sibling at services/api/$f; keep in lockstep. See $ADR_REF."
  actual_api_header=$(head -n 1 "$api")
  actual_webhook_header=$(head -n 1 "$webhook")

  if [ "$actual_api_header" != "$expected_api_header" ]; then
    HEADER_BAD+=("$api")
  fi
  if [ "$actual_webhook_header" != "$expected_webhook_header" ]; then
    HEADER_BAD+=("$webhook")
  fi

  # Compare body (line 2+) — strip header before diff
  if ! diff -q <(tail -n +2 "$api") <(tail -n +2 "$webhook") >/dev/null 2>&1; then
    BODY_DIVERGED+=("$f")
  fi
done

# --- MIRRORED_BYTE_IDENTICAL: full byte equality ---
for f in "${MIRRORED_BYTE_IDENTICAL[@]}"; do
  api="services/api/$f"
  webhook="services/webhook/$f"
  if [ ! -f "$api" ]; then MISSING+=("$api"); continue; fi
  if [ ! -f "$webhook" ]; then MISSING+=("$webhook"); continue; fi
  if ! diff -q "$api" "$webhook" >/dev/null 2>&1; then
    BODY_DIVERGED+=("$f")
  fi
done

EXIT=0

if [ ${#MISSING[@]} -gt 0 ]; then
  echo "ERROR: missing mirror files (one side absent):"
  for m in "${MISSING[@]}"; do echo "  - $m"; done
  echo
  echo "Either remove from scripts/check-mirrored-files.sh OR create the missing copy."
  EXIT=2
fi

if [ ${#HEADER_BAD[@]} -gt 0 ]; then
  echo "ERROR: MIRRORED header missing or wrong on line 1:"
  for h in "${HEADER_BAD[@]}"; do
    # Derive the relative path + expected sibling-side header for this exact file
    case "$h" in
      services/api/*)
        rel="${h#services/api/}"
        expected="# MIRRORED — sibling at services/webhook/$rel; keep in lockstep. See $ADR_REF."
        ;;
      services/webhook/*)
        rel="${h#services/webhook/}"
        expected="# MIRRORED — sibling at services/api/$rel; keep in lockstep. See $ADR_REF."
        ;;
      *)
        expected="# MIRRORED — sibling at services/<other>/<path>; keep in lockstep. See $ADR_REF."
        ;;
    esac
    echo
    echo "=== $h ==="
    echo "  expected: $expected"
    echo "  actual:   $(head -n 1 "$h")"
  done
  echo
  echo "Mirror-discipline header is load-bearing — see ADR-0001."
  EXIT=1
fi

if [ ${#BODY_DIVERGED[@]} -gt 0 ]; then
  # Build a newline-joined string of files-with-header so we can grep WITHOUT
  # piping (per memory feedback_pipefail_grep_q_sigpipe.md: `... | grep -q X`
  # under `set -euo pipefail` trips SIGPIPE when grep matches early and exits).
  with_header_list=$(printf '%s\n' "${MIRRORED_WITH_HEADER[@]}")

  echo "ERROR: paired files diverged between services/api/ + services/webhook/:"
  for d in "${BODY_DIVERGED[@]}"; do
    echo
    echo "=== $d ==="
    if grep -Fxq -- "$d" <<<"$with_header_list"; then
      diff -u <(tail -n +2 "services/api/$d") <(tail -n +2 "services/webhook/$d") | head -50
    else
      diff -u "services/api/$d" "services/webhook/$d" | head -50
    fi
  done
  echo
  echo "Both copies must stay in lockstep (PRD #21 + ADR-0001 mirror discipline)."
  echo "Apply the same change to both. Until shared-package extraction lands,"
  echo "this lint catches accidental one-side edits."
  EXIT=1
fi

if [ "$EXIT" -eq 0 ]; then
  total=$(( ${#MIRRORED_WITH_HEADER[@]} + ${#MIRRORED_BYTE_IDENTICAL[@]} ))
  echo "OK: $total mirrored file pairs verified (${#MIRRORED_WITH_HEADER[@]} with header + ${#MIRRORED_BYTE_IDENTICAL[@]} byte-identical)."
fi

exit "$EXIT"
