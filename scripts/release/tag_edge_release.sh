#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   scripts/release/tag_edge_release.sh 1.2.3
#   scripts/release/tag_edge_release.sh 1.2.3-rc.1

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  echo "Usage: $0 <version>" >&2
  exit 1
fi

TAG="edge/v${VERSION}"

git rev-parse --git-dir >/dev/null 2>&1 || { echo "Not a git repo" >&2; exit 1; }

# Ensure clean working tree
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Working tree not clean. Commit or stash changes before tagging." >&2
  exit 1
fi

if git rev-parse "${TAG}" >/dev/null 2>&1; then
  echo "Tag already exists: ${TAG}" >&2
  exit 1
fi

git tag -a "${TAG}" -m "Edge release ${VERSION}"
echo "Created tag: ${TAG}"

echo "Next: push it"
echo "  git push origin ${TAG}"
