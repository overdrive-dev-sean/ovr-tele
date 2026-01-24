#!/usr/bin/env bash
set -euo pipefail
umask 022

EDGE_DIR="${EDGE_DIR:-/opt/ovr/edge}"
OVR_DIR="${OVR_DIR:-/etc/ovr}"

OUT_DIR="${EDGE_DIR}/telegraf/telegraf.d"
LIST="${OVR_DIR}/targets_acuvim.txt"
TPL="${EDGE_DIR}/telegraf/templates/acuvim_modbus.tpl"

# Fallback to repo file if not present in /etc/ovr
if [ ! -f "${LIST}" ]; then
  LIST="${EDGE_DIR}/telegraf/targets_acuvim.txt"
fi

if [ ! -f "${TPL}" ]; then
  echo "ERROR: Missing template ${TPL}" >&2
  exit 1
fi

if [ ! -f "${LIST}" ]; then
  echo "ERROR: Missing IP list ${LIST}" >&2
  exit 1
fi

is_up() {
  local ip="$1"
  timeout 1 bash -lc "cat < /dev/null > /dev/tcp/${ip}/502" >/dev/null 2>&1
}

ensure_mode_0644() {
  local f="$1"
  local mode
  mode="$(stat -c '%a' "$f" 2>/dev/null || echo "")"
  if [ "$mode" != "644" ]; then
    chmod 0644 "$f"
  fi
}

added=0
updated=0
removed=0

while read -r ip; do
  [[ -z "${ip}" ]] && continue
  [[ "${ip}" =~ ^# ]] && continue

  fname="${OUT_DIR}/acuvim_${ip//./_}.conf"
  last_octet="${ip##*.}"
  device="acuvim_${last_octet}"

  if is_up "${ip}"; then
    tmp="$(mktemp)"
    sed \
      -e "s/__IP__/${ip}/g" \
      -e "s/__DEVICE__/${device}/g" \
      "${TPL}" > "${tmp}"
    chmod 0644 "${tmp}"

    if [ -f "${fname}" ]; then
      if cmp -s "${tmp}" "${fname}"; then
        rm -f "${tmp}"
        ensure_mode_0644 "${fname}"
      else
        mv "${tmp}" "${fname}"
        ensure_mode_0644 "${fname}"
        echo "UPDATED ${ip} -> $(basename "${fname}")"
        updated=$((updated+1))
      fi
    else
      mv "${tmp}" "${fname}"
      ensure_mode_0644 "${fname}"
      echo "ENABLED ${ip} -> $(basename "${fname}")"
      added=$((added+1))
    fi
  else
    if [ -f "${fname}" ]; then
      rm -f "${fname}"
      echo "DISABLED ${ip} (removed $(basename "${fname}"))"
      removed=$((removed+1))
    fi
  fi
done < "${LIST}"

if [ $((added+updated+removed)) -gt 0 ]; then
  echo "SUMMARY: added=${added} updated=${updated} removed=${removed}"
fi
