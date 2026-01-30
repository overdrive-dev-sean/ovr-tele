#!/usr/bin/env bash
set -euo pipefail
umask 027

OVR_DIR="/etc/ovr"
EDGE_ENV="${OVR_DIR}/edge.env"
CLOUD_ENV="${OVR_DIR}/cloud.env"
EDGE_DIR="/opt/ovr/edge"
CLOUD_DIR="/opt/ovr/cloud"

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

ensure_env_perms() {
  local file="$1"
  if [ -f "$file" ]; then
    chmod 0600 "$file"
    chown root:root "$file" 2>/dev/null || true
  fi
}

link_env() {
  local env_file="$1"
  local dir="$2"
  local label="$3"

  if [ ! -f "$env_file" ]; then
    log "Missing ${env_file}; create it to link ${label} .env"
    return 0
  fi

  if [ ! -d "$dir" ]; then
    log "Missing ${dir}; clone the repo to /opt/ovr to link ${label} .env"
    return 0
  fi

  ln -sf "$env_file" "$dir/.env"
  log "Linked ${dir}/.env -> ${env_file}"
}

require_root
ensure_dir "${OVR_DIR}" 0755
ensure_dir "${OVR_DIR}/secrets" 0700
ensure_env_perms "${EDGE_ENV}"
ensure_env_perms "${CLOUD_ENV}"

link_env "${EDGE_ENV}" "${EDGE_DIR}" "EDGE"
link_env "${CLOUD_ENV}" "${CLOUD_DIR}" "CLOUD"
