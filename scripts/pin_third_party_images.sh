#!/usr/bin/env bash
set -euo pipefail

# Pin third-party images by digest from a known-good running stack.
#
# Why:
# - Avoids :latest drift.
# - Produces truly reproducible deployments across your fleet.
#
# Usage:
#   # On an EDGE node:
#   ./scripts/pin_third_party_images.sh edge edge/compose.dev.yml
#
#   # On the CLOUD VPS:
#   ./scripts/pin_third_party_images.sh cloud cloud/compose.dev.yml
#
# Output:
#   Prints KEY=value lines you can paste into:
#   - edge/edge.env
#   - cloud/.env

STACK="${1:-}"
COMPOSE_FILE="${2:-}"

if [ -z "${STACK}" ] || [ -z "${COMPOSE_FILE}" ]; then
  echo "Usage: $0 <edge|cloud> <compose-file>" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose (v2) not found" >&2
  exit 1
fi

case "${STACK}" in
  edge)
    SERVICES=("victoria-metrics" "vmagent" "grafana" "telegraf" "node-exporter")
    VARS=("VM_IMAGE" "VMAGENT_IMAGE" "GRAFANA_IMAGE" "TELEGRAF_IMAGE" "NODE_EXPORTER_IMAGE")
    ;;
  cloud)
    SERVICES=("victoria-metrics" "grafana")
    VARS=("VM_IMAGE" "GRAFANA_IMAGE")
    ;;
  *)
    echo "Unknown stack '${STACK}'. Use 'edge' or 'cloud'." >&2
    exit 1
    ;;
esac

echo "# Pinned third-party images for ${STACK} (paste into your env file)"
echo "# Generated from running containers in compose file: ${COMPOSE_FILE}"
echo

for i in "${!SERVICES[@]}"; do
  svc="${SERVICES[$i]}"
  var="${VARS[$i]}"

  cid=$(docker compose -f "${COMPOSE_FILE}" ps -q "${svc}" 2>/dev/null || true)
  if [ -z "${cid}" ]; then
    echo "# WARN: service '${svc}' not running (no container id). Start the stack first." >&2
    echo "${var}="
    continue
  fi

  img=$(docker inspect --format '{{.Config.Image}}' "${cid}" 2>/dev/null || true)
  if [ -z "${img}" ]; then
    echo "# WARN: could not determine image for '${svc}'" >&2
    echo "${var}="
    continue
  fi

  digest=$(docker image inspect --format '{{index .RepoDigests 0}}' "${img}" 2>/dev/null || true)
  if [ -z "${digest}" ]; then
    # Fallback: if the image was built locally, RepoDigests may be empty.
    # In that case, at least emit the original image ref.
    echo "# WARN: no RepoDigest for '${img}' (built locally or not pulled). Using tag ref instead." >&2
    echo "${var}=${img}"
    continue
  fi

  echo "${var}=${digest}"
done
