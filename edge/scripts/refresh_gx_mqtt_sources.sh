#!/usr/bin/env bash
set -uo pipefail
umask 022

# Note: not using set -e because we want to continue on individual device failures

EDGE_DIR="${EDGE_DIR:-/opt/ovr/edge}"
OVR_DIR="${OVR_DIR:-/etc/ovr}"

OUT_DIR="${OVR_DIR}/telegraf.d"
OUT_FILE="${OUT_DIR}/gx_mqtt_sources.conf"
LIST="${OVR_DIR}/targets_gx.txt"

# Venus wildcard expansion limit
VENUS_MAX=30

# Fallback to repo file if not in /etc/ovr
if [ ! -f "${LIST}" ]; then
  LIST="${EDGE_DIR}/telegraf/targets_gx.txt"
fi

if [ ! -f "${LIST}" ]; then
  echo "ERROR: Missing GX targets list ${LIST}" >&2
  exit 1
fi

mkdir -p "${OUT_DIR}"

# Check if MQTT port is reachable
mqtt_reachable() {
  local ip="$1"
  timeout 1 bash -c "cat < /dev/null > /dev/tcp/${ip}/1883" 2>/dev/null
}

# Discover portal ID by subscribing to system serial
discover_portal_id() {
  local ip="$1"
  local portal_id=""

  if command -v mosquitto_sub >/dev/null 2>&1; then
    # Subscribe briefly to get the portal ID from system serial
    portal_id=$(timeout 3 mosquitto_sub -h "${ip}" -t 'N/+/system/0/Serial' -C 1 2>/dev/null | \
      sed -n 's/.*"value":"\([^"]*\)".*/\1/p' || true)
  fi

  echo "${portal_id}"
}

# Discover CustomName from MQTT and normalize it
# Tries vebus CustomName first, returns normalized name or empty string
discover_custom_name() {
  local ip="$1"
  local portal_id="$2"
  local name=""
  local tmp_file

  if command -v mosquitto_sub >/dev/null 2>&1; then
    tmp_file=$(mktemp)

    # Subscribe in background, send keepalive, wait for data
    timeout 4 mosquitto_sub -h "${ip}" -t "N/${portal_id}/vebus/+/CustomName" -C 1 > "${tmp_file}" 2>/dev/null &
    local sub_pid=$!
    sleep 0.3
    mosquitto_pub -h "${ip}" -t "R/${portal_id}/keepalive" -m '' 2>/dev/null || true
    wait $sub_pid 2>/dev/null || true
    name=$(sed -n 's/.*"value":"\([^"]*\)".*/\1/p' "${tmp_file}")

    # Fallback: try settings SystemName
    if [ -z "${name}" ]; then
      timeout 4 mosquitto_sub -h "${ip}" -t "N/${portal_id}/settings/0/Settings/SystemSetup/SystemName" -C 1 > "${tmp_file}" 2>/dev/null &
      sub_pid=$!
      sleep 0.3
      mosquitto_pub -h "${ip}" -t "R/${portal_id}/keepalive" -m '' 2>/dev/null || true
      wait $sub_pid 2>/dev/null || true
      name=$(sed -n 's/.*"value":"\([^"]*\)".*/\1/p' "${tmp_file}")
    fi

    rm -f "${tmp_file}"

    # Normalize: lowercase, remove spaces between letters/numbers, dots and spaces to hyphens
    # "Pro 6005-1" -> "pro6005-1", "PRO 6005.2" -> "pro6005-2", "diskobox pro30" -> "diskobox-pro30"
    if [ -n "${name}" ]; then
      name=$(echo "${name}" | \
        tr '[:upper:]' '[:lower:]' | \
        sed 's/\([a-z]\) \([0-9]\)/\1\2/g' | \
        tr '.' '-' | \
        tr ' ' '-')
    fi
  fi

  echo "${name}"
}

# Send keepalive to GX device
send_keepalive() {
  local ip="$1"
  local portal_id="$2"

  if [ -z "${portal_id}" ]; then
    return 1
  fi

  if command -v mosquitto_pub >/dev/null 2>&1; then
    mosquitto_pub -h "${ip}" -t "R/${portal_id}/keepalive" -m '' 2>/dev/null && \
      echo "KEEPALIVE sent to ${portal_id} (${ip})" || \
      echo "KEEPALIVE failed for ${portal_id} (${ip})"
  else
    echo "WARN: mosquitto_pub not installed, cannot send keepalive"
  fi
}

# Check if entry is a venus wildcard
is_venus_wildcard() {
  [ "$1" = "venus" ]
}

declare -a brokers
declare -A seen_ips  # Track IPs we've already processed (avoid duplicates)
added=0
skipped=0

# Process a single hostname, returns 0 on success, 1 on failure
process_host() {
  local hostname="$1"
  local fqdn="${hostname}.local"
  local ip

  ip=$(timeout 1 getent hosts "${fqdn}" 2>/dev/null | awk '{print $1}' | head -1)

  if [ -z "${ip}" ]; then
    return 1
  fi

  # Skip if we've already seen this IP (dedup)
  if [ -n "${seen_ips[${ip}]:-}" ]; then
    return 0  # Not a failure, just a dup
  fi
  seen_ips[${ip}]=1

  if ! mqtt_reachable "${ip}"; then
    echo "SKIP ${hostname} (${ip}): MQTT not reachable"
    skipped=$((skipped+1))
    return 1
  fi

  # Discover portal ID and send keepalive
  local portal_id
  portal_id=$(discover_portal_id "${ip}")
  if [ -z "${portal_id}" ]; then
    echo "WARN ${hostname} (${ip}): could not discover portal ID"
    return 1
  fi

  send_keepalive "${ip}" "${portal_id}"

  # Discover CustomName for system_id (fallback to portal_id)
  local custom_name system_id
  custom_name=$(discover_custom_name "${ip}" "${portal_id}")
  system_id="${custom_name:-${portal_id}}"

  brokers+=("${system_id}|${ip}|${portal_id}")
  echo "OK ${hostname} (${ip}) portal=${portal_id} system_id=${system_id}"
  added=$((added+1))
  return 0
}

while read -r entry; do
  # Skip empty lines and comments
  [[ -z "${entry}" ]] && continue
  [[ "${entry}" =~ ^# ]] && continue

  if is_venus_wildcard "${entry}"; then
    # Venus wildcard: scan venus, venus-2, ... until first miss
    # Use short timeout (0.5s) for mDNS lookups to fail fast on non-existent hosts
    process_host "venus" || true
    for i in $(seq 2 ${VENUS_MAX}); do
      # Stop on first unresolvable hostname (they're sequential)
      if ! timeout 0.5 getent hosts "venus-${i}.local" >/dev/null 2>&1; then
        break
      fi
      process_host "venus-${i}" || true
    done
  else
    # Explicit hostname
    if ! process_host "${entry}"; then
      echo "SKIP ${entry}: hostname not resolvable"
      skipped=$((skipped+1))
    fi
  fi
done < "${LIST}"

# Generate telegraf config
PROCESSOR_SCRIPT="${EDGE_DIR}/telegraf/processors/victron_mqtt.star"

tmp_file="$(mktemp)"
{
  echo "# Auto-generated by refresh_gx_mqtt_sources.sh. Do not edit."
  echo "# Source: ${LIST}"
  echo "# Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo ""

  if [ "${#brokers[@]}" -eq 0 ]; then
    echo "# No GX MQTT brokers available."
  else
    # Add starlark processor for semantic metric names
    if [ -f "${PROCESSOR_SCRIPT}" ]; then
      cat <<EOF
# Transform Victron MQTT topics into semantic metric names
[[processors.starlark]]
  namepass = ["mqtt_consumer*"]
  source = '''
$(cat "${PROCESSOR_SCRIPT}")
'''

EOF
    fi

    for entry in "${brokers[@]}"; do
      IFS='|' read -r system_id ip portal_id <<< "${entry}"
      cat <<EOF
[[inputs.mqtt_consumer]]
  servers = ["tcp://${ip}:1883"]
  topics = ["N/${portal_id:-+}/#"]
  qos = 0
  connection_timeout = "5s"
  client_id = "ovr-${system_id}"
  data_format = "json"
  topic_tag = "topic"
  [inputs.mqtt_consumer.tags]
    system_id = "${system_id}"
    gx_host = "${ip}"
    portal_id = "${portal_id:-unknown}"

EOF
    done
  fi
} > "${tmp_file}"

mv "${tmp_file}" "${OUT_FILE}"
chmod 0644 "${OUT_FILE}"

echo "SUMMARY: added=${added} skipped=${skipped} -> ${OUT_FILE}"
