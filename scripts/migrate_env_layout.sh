#!/usr/bin/env bash
set -euo pipefail
umask 027

OVR_DIR="/etc/ovr"
EDGE_ENV="${OVR_DIR}/edge.env"
CLOUD_ENV="${OVR_DIR}/cloud.env"
LEGACY_SITE_ENV="${OVR_DIR}/site.env"
LEGACY_DIR="/etc/overdrive"

log() {
  echo "[migrate] $*"
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root (sudo)." >&2
    exit 1
  fi
}

ensure_dir() {
  local dir="$1"
  local mode="$2"
  if [ ! -d "$dir" ]; then
    mkdir -p "$dir"
    log "Created ${dir}"
  fi
  chmod "$mode" "$dir"
  chown root:root "$dir" 2>/dev/null || true
}

require_root
ensure_dir "${OVR_DIR}" 0755
ensure_dir "${OVR_DIR}/secrets" 0700

if [ -f "${LEGACY_SITE_ENV}" ] && [ ! -f "${EDGE_ENV}" ]; then
  cp "${LEGACY_SITE_ENV}" "${EDGE_ENV}"
  chmod 0600 "${EDGE_ENV}"
  chown root:root "${EDGE_ENV}" 2>/dev/null || true
  log "Copied ${LEGACY_SITE_ENV} -> ${EDGE_ENV}"
elif [ -f "${LEGACY_DIR}/site.env" ] && [ ! -f "${EDGE_ENV}" ]; then
  cp "${LEGACY_DIR}/site.env" "${EDGE_ENV}"
  chmod 0600 "${EDGE_ENV}"
  chown root:root "${EDGE_ENV}" 2>/dev/null || true
  log "Copied ${LEGACY_DIR}/site.env -> ${EDGE_ENV}"
fi

if [ -f "${EDGE_ENV}" ]; then
  chmod 0600 "${EDGE_ENV}"
  chown root:root "${EDGE_ENV}" 2>/dev/null || true
fi

if [ -f "${CLOUD_ENV}" ]; then
  chmod 0600 "${CLOUD_ENV}"
  chown root:root "${CLOUD_ENV}" 2>/dev/null || true
fi

if [ -f "${EDGE_ENV}" ]; then
  if [ -L "${LEGACY_SITE_ENV}" ]; then
    ln -sf "${EDGE_ENV}" "${LEGACY_SITE_ENV}"
    log "Refreshed ${LEGACY_SITE_ENV} -> ${EDGE_ENV}"
  elif [ -e "${LEGACY_SITE_ENV}" ]; then
    if cmp -s "${LEGACY_SITE_ENV}" "${EDGE_ENV}"; then
      rm -f "${LEGACY_SITE_ENV}"
      ln -s "${EDGE_ENV}" "${LEGACY_SITE_ENV}"
      log "Linked ${LEGACY_SITE_ENV} -> ${EDGE_ENV}"
    else
      log "Left ${LEGACY_SITE_ENV} in place (differs from edge.env)"
    fi
  else
    ln -s "${EDGE_ENV}" "${LEGACY_SITE_ENV}"
    log "Linked ${LEGACY_SITE_ENV} -> ${EDGE_ENV}"
  fi
fi

link_env() {
  local env_file="$1"
  shift
  local found=0
  local dir
  for dir in "$@"; do
    if [ -d "$dir" ]; then
      ln -sf "$env_file" "$dir/.env"
      log "Linked ${dir}/.env -> ${env_file}"
      found=1
    fi
  done
  if [ "$found" -eq 0 ]; then
    echo "Could not detect stack dir. To link manually run:"
    echo "  ln -sf ${env_file} <stack-dir>/.env"
  fi
}

if [ -f "${EDGE_ENV}" ]; then
  link_env "${EDGE_ENV}" /opt/edge /opt/stack/edge
else
  log "Missing ${EDGE_ENV}; create it to link EDGE .env"
fi

if [ -f "${CLOUD_ENV}" ]; then
  link_env "${CLOUD_ENV}" /opt/stack/cloud /opt/cloud
else
  log "Missing ${CLOUD_ENV}; create it to link CLOUD .env"
fi
