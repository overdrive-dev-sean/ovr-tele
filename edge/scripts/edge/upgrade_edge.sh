#!/usr/bin/env bash
set -euo pipefail

# Upgrade an edge node to a specific EDGE_VERSION.
#
# Example:
#   sudo /opt/edge/scripts/edge/upgrade_edge.sh 1.2.3
#
# Notes:
# - Assumes the edge runtime directory is at /opt/edge (override with EDGE_DIR)
# - Uses compose.release.yml in EDGE_DIR (override with COMPOSE_FILE)

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  echo "Usage: $0 <edge_version>" >&2
  exit 1
fi

EDGE_DIR="${EDGE_DIR:-/opt/edge}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.release.yml}"
GHCR_OWNER="${GHCR_OWNER:-overdrive-dev-sean}"

if [ ! -d "$EDGE_DIR" ]; then
  # Back-compat fallback (older layout)
  if [ -d "/opt/stack/edge" ]; then
    EDGE_DIR="/opt/stack/edge"
  else
    echo "ERROR: Edge directory not found at $EDGE_DIR (and /opt/stack/edge missing)" >&2
    exit 1
  fi
fi

cd "$EDGE_DIR"

export EDGE_VERSION="$VERSION"
export GHCR_OWNER

echo "Upgrading edge stack in ${EDGE_DIR} to EDGE_VERSION=${EDGE_VERSION}"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "ERROR: docker compose not found" >&2
  exit 1
fi

"${COMPOSE[@]}" -f "$COMPOSE_FILE" pull
"${COMPOSE[@]}" -f "$COMPOSE_FILE" up -d

echo "Done. Quick health check:"
curl -fsS http://127.0.0.1:8088/health | sed 's/^/  /'
