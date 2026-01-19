#!/usr/bin/env bash
set -euo pipefail

# Guardrail: prevent ':latest' in release/sandbox deployment files.
#
# Rationale:
# - ':latest' makes fleets non-identical and rollbacks unpredictable.
# - Release deploys should be reproducible: pin by tag or digest.

FILES=(
  "edge/compose.release.yml"
  "edge/edge.env.example"
  "cloud/compose.release.yml"
  "cloud/compose.sandbox.yml"
  "cloud/.env.example"
)

fail=0

for f in "${FILES[@]}"; do
  if [ ! -f "${f}" ]; then
    echo "::error::Expected file missing: ${f}" >&2
    fail=1
    continue
  fi

  if [[ "${f}" == *.yml ]]; then
    # Compose files: flag only if an image ref uses ':latest'
    if grep -nE '^[[:space:]]*image:[^#]*:latest\b' "${f}" > /tmp/no_latest_hits.txt; then
      echo "::error::Found an image using ':latest' in ${f}. Release files must pin images by tag or digest." >&2
      cat /tmp/no_latest_hits.txt
      echo
      fail=1
    fi
  else
    # Env files: flag only if an assignment uses ':latest'
    if grep -nE '^[[:space:]]*[A-Za-z0-9_]+=[^#]*:latest\b' "${f}" > /tmp/no_latest_hits.txt; then
      echo "::error::Found an env var value using ':latest' in ${f}. Release files must pin images by tag or digest." >&2
      cat /tmp/no_latest_hits.txt
      echo
      fail=1
    fi
  fi
done

if [ "${fail}" -ne 0 ]; then
  exit 1
fi

echo "No ':latest' found in release/sandbox deployment files."
