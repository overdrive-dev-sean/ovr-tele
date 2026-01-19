#!/usr/bin/env bash
set -euo pipefail

# Naming guardrails
#
# Purpose:
# - Prevent accidental reintroduction of legacy names after the repo restructure.
# - Keep edge/cloud naming consistent and future-proof.
#
# This script is intended to run in CI on every PR.

EXCLUDES=(
  ":!docs/edge/LEGACY_STACK_README.md"  # historical reference (intentionally outdated)
  ":!scripts/ci/naming_guard.sh"        # exclude self
)

# NOTE: These are fixed-string matches (git grep -F), not regex.
# Keep patterns specific enough to avoid false positives.
FORBIDDEN_PATTERNS=(
  "fleet-api"
  "fleet-map"
  "event-service/"     # legacy directory path reference
  "event-service:"     # legacy compose service key reference
  "provisioning/edge-os"
  "edge-os/"
  "ovr-edge-"          # legacy GHCR image prefix
  "ovr-cloud-"         # legacy GHCR image prefix
)

fail=0

have_git=0
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  have_git=1
fi

for pat in "${FORBIDDEN_PATTERNS[@]}"; do
  if [ "$have_git" -eq 1 ]; then
    # git grep returns exit code 1 when no matches are found
    if git grep -n -F "${pat}" -- . "${EXCLUDES[@]}" > /tmp/naming_guard_hits.txt; then
      echo "::error::Forbidden legacy name detected: '${pat}'"
      cat /tmp/naming_guard_hits.txt
      echo
      fail=1
    fi
  else
    # fallback for non-git environments
    if grep -R -n -F --exclude='LEGACY_STACK_README.md' --exclude='naming_guard.sh' "${pat}" . > /tmp/naming_guard_hits.txt; then
      echo "::error::Forbidden legacy name detected: '${pat}'"
      cat /tmp/naming_guard_hits.txt
      echo
      fail=1
    fi
  fi
done

if [ "${fail}" -ne 0 ]; then
  echo "Naming guardrails failed. Please use the current names (api/map/events, provisioning/edge, GHCR images without 'ovr-')." >&2
  exit 1
fi

echo "Naming guardrails passed."
