#!/usr/bin/env bash
set -euo pipefail
umask 027

EDGE_DIR="${EDGE_DIR:-/opt/edge}"
if [ ! -d "${EDGE_DIR}" ] && [ -d "/opt/stack/edge" ]; then
  EDGE_DIR="/opt/stack/edge"
fi
OVR_DIR="${OVR_DIR:-/etc/ovr}"

DEPLOYMENT_ID=""
NODE_ID=""
SYSTEM_ID=""
BASE_DOMAIN=""
DEPLOY_MODE=""

LAN_MODE=""
LAN_IF=""
LAN_IP=""
LAN_GW=""
LAN_DNS=""
WIFI_SSID=""
WIFI_PASS=""

REMOTE_WRITE_URL=""
REMOTE_WRITE_USER=""
REMOTE_WRITE_PASSWORD=""
REMOTE_WRITE_PASSWORD_FILE=""
MAPBOX_TOKEN=""
MAPBOX_TOKEN_FILE=""
VM_WRITE_URL=""
VM_QUERY_URL=""
VM_WRITE_URL_SECONDARY=""
VM_WRITE_USERNAME=""
VM_WRITE_PASSWORD=""
VM_WRITE_PASSWORD_FILE=""
GHCR_OWNER=""
EDGE_VERSION=""
EDGE_GIT_SHA=""
VM_IMAGE=""
VMAGENT_IMAGE=""
GRAFANA_IMAGE=""
TELEGRAF_IMAGE=""
NODE_EXPORTER_IMAGE=""
VM_RETENTION=""

TARGETS_FILE=""
TARGETS_INLINE=()
TARGETS_INLINE_RAW=""

HAS_GX=""
GX_HOST=""

PROMPT_ALL="${PROMPT_ALL:-1}"
COMPOSE_CMD=()

usage() {
  cat <<'EOF'
Usage: sudo provisioning/edge/bootstrap_n100.sh [options]

Identity:
  --deployment-id <id>      Required deployment identifier (fleet/group)
  --node-id <id>            Required node identifier
  --base-domain <domain>    Base domain for public hostnames (default: overdrive.rocks)

Networking (NetworkManager):
  --lan-mode dhcp|static
  --lan-if <ifname>
  --lan-ip <cidr>           Required for static (e.g. 192.168.100.10/24)
  --lan-gw <ip>             Required for static
  --lan-dns <ip[,ip]>       Optional for static
  --wifi-ssid <ssid>
  --wifi-pass <password>

Remote write:
  --remote-write-url <url>
  --remote-write-user <user>
  --remote-write-password <pass>            (optional, will prompt if needed)
  --remote-write-password-file <path>       (optional, preferred)

Local VM writes (events):
  --vm-write-url <url>                      (optional)
  --vm-query-url <url>                      (optional)
  --vm-write-url-secondary <url>            (optional)
  --vm-write-username <user>                (optional)
  --vm-write-password <pass>                (optional, will prompt if needed)
  --vm-write-password-file <path>           (optional, preferred)

Map tiles:
  --mapbox-token <token>                    (optional)
  --mapbox-token-file <path>                (optional, preferred)

Targets:
  --targets <job=host:port[,job=host:port]>
  --targets-file <path>

GX (optional):
  --has-gx true|false (or --has-gx=true|false)
  --gx-host <hostname-or-ip>

Other:
  --no-prompt                Disable interactive prompts
EOF
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root (sudo)." >&2
    exit 1
  fi
}

need() {
  local value="$1"
  local msg="$2"
  if [ -z "${value}" ]; then
    echo "ERROR: ${msg}" >&2
    exit 1
  fi
}

read_site_env_value() {
  local key="$1"
  local file="$2"
  local line
  line=$(grep -m1 "^${key}=" "${file}" 2>/dev/null || true)
  if [ -n "${line}" ]; then
    printf '%s' "${line#*=}"
  fi
}

preserve_if_empty() {
  local var_name="$1"
  local key="$2"
  local file="$3"
  if [ -z "${!var_name}" ]; then
    local value
    value="$(read_site_env_value "${key}" "${file}")"
    if [ -n "${value}" ]; then
      printf -v "${var_name}" '%s' "${value}"
    fi
  fi
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --deployment-id) DEPLOYMENT_ID="$2"; shift 2 ;;
      --node-id) NODE_ID="$2"; shift 2 ;;
      --system-id) SYSTEM_ID="$2"; shift 2 ;;
      --base-domain) BASE_DOMAIN="$2"; shift 2 ;;
      --hostname) HOSTNAME_SET="$2"; shift 2 ;;
      --lan-mode) LAN_MODE="$2"; shift 2 ;;
      --lan-if) LAN_IF="$2"; shift 2 ;;
      --lan-ip) LAN_IP="$2"; shift 2 ;;
      --lan-gw) LAN_GW="$2"; shift 2 ;;
      --lan-dns) LAN_DNS="$2"; shift 2 ;;
      --wifi-ssid) WIFI_SSID="$2"; shift 2 ;;
      --wifi-pass) WIFI_PASS="$2"; shift 2 ;;
      --remote-write-url) REMOTE_WRITE_URL="$2"; shift 2 ;;
      --remote-write-user) REMOTE_WRITE_USER="$2"; shift 2 ;;
      --remote-write-password) REMOTE_WRITE_PASSWORD="$2"; shift 2 ;;
      --remote-write-password-file) REMOTE_WRITE_PASSWORD_FILE="$2"; shift 2 ;;
      --vm-write-url) VM_WRITE_URL="$2"; shift 2 ;;
      --vm-query-url) VM_QUERY_URL="$2"; shift 2 ;;
      --vm-write-url-secondary) VM_WRITE_URL_SECONDARY="$2"; shift 2 ;;
      --vm-write-username) VM_WRITE_USERNAME="$2"; shift 2 ;;
      --vm-write-password) VM_WRITE_PASSWORD="$2"; shift 2 ;;
      --vm-write-password-file) VM_WRITE_PASSWORD_FILE="$2"; shift 2 ;;
      --mapbox-token) MAPBOX_TOKEN="$2"; shift 2 ;;
      --mapbox-token-file) MAPBOX_TOKEN_FILE="$2"; shift 2 ;;
      --targets)
        TARGETS_INLINE_RAW="$2"
        IFS=',' read -r -a items <<< "$2"
        for item in "${items[@]}"; do
          [ -n "${item}" ] && TARGETS_INLINE+=("${item}")
        done
        shift 2
        ;;
      --targets-file) TARGETS_FILE="$2"; shift 2 ;;
      --has-gx=*)
        HAS_GX="${1#*=}"
        shift
        ;;
      --has-gx)
        if [ -n "${2:-}" ] && [ "${2#--}" = "${2}" ]; then
          HAS_GX="$2"
          shift 2
        else
          HAS_GX="true"
          shift
        fi
        ;;
      --gx-host) GX_HOST="$2"; shift 2 ;;
      --no-prompt) PROMPT_ALL=0; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "ERROR: Unknown argument: $1" >&2; usage; exit 1 ;;
    esac
  done
}

parse_targets_inline() {
  local raw="$1"
  local -a items=()
  TARGETS_INLINE=()
  if [ -z "$raw" ]; then
    return 0
  fi
  IFS=',' read -r -a items <<< "$raw"
  for item in "${items[@]}"; do
    [ -n "${item}" ] && TARGETS_INLINE+=("${item}")
  done
}

load_existing_defaults() {
  local existing_env=""
  local candidate
  for candidate in "${OVR_DIR}/edge.env" "${OVR_DIR}/site.env"; do
    if [ -f "${candidate}" ]; then
      existing_env="${candidate}"
      break
    fi
  done
  if [ -n "${existing_env}" ]; then
    preserve_if_empty SYSTEM_ID SYSTEM_ID "${existing_env}"
    preserve_if_empty BASE_DOMAIN BASE_DOMAIN "${existing_env}"
    preserve_if_empty DEPLOY_MODE DEPLOY_MODE "${existing_env}"
    preserve_if_empty REMOTE_WRITE_URL VM_REMOTE_WRITE_URL "${existing_env}"
    preserve_if_empty REMOTE_WRITE_USER VM_REMOTE_WRITE_USERNAME "${existing_env}"
    preserve_if_empty REMOTE_WRITE_PASSWORD_FILE VM_REMOTE_WRITE_PASSWORD_FILE "${existing_env}"
    preserve_if_empty VM_WRITE_URL VM_WRITE_URL "${existing_env}"
    preserve_if_empty VM_QUERY_URL VM_QUERY_URL "${existing_env}"
    preserve_if_empty VM_WRITE_URL_SECONDARY VM_WRITE_URL_SECONDARY "${existing_env}"
    preserve_if_empty VM_WRITE_USERNAME VM_WRITE_USERNAME "${existing_env}"
    preserve_if_empty VM_WRITE_PASSWORD_FILE VM_WRITE_PASSWORD_FILE "${existing_env}"
    preserve_if_empty VM_WRITE_PASSWORD VM_WRITE_PASSWORD "${existing_env}"
    preserve_if_empty MAPBOX_TOKEN MAPBOX_TOKEN "${existing_env}"
    preserve_if_empty MAPBOX_TOKEN_FILE MAPBOX_TOKEN_FILE "${existing_env}"
    preserve_if_empty GX_HOST GX_HOST "${existing_env}"
    preserve_if_empty GHCR_OWNER GHCR_OWNER "${existing_env}"
    preserve_if_empty EDGE_VERSION EDGE_VERSION "${existing_env}"
    preserve_if_empty EDGE_GIT_SHA EDGE_GIT_SHA "${existing_env}"
    preserve_if_empty VM_IMAGE VM_IMAGE "${existing_env}"
    preserve_if_empty VMAGENT_IMAGE VMAGENT_IMAGE "${existing_env}"
    preserve_if_empty GRAFANA_IMAGE GRAFANA_IMAGE "${existing_env}"
    preserve_if_empty TELEGRAF_IMAGE TELEGRAF_IMAGE "${existing_env}"
    preserve_if_empty NODE_EXPORTER_IMAGE NODE_EXPORTER_IMAGE "${existing_env}"
    preserve_if_empty VM_RETENTION VM_RETENTION "${existing_env}"
  fi
}

apply_defaults() {
  if [ -z "${DEPLOYMENT_ID}" ]; then
    DEPLOYMENT_ID="OverdriveFleet"
  fi
  if [ -z "${BASE_DOMAIN}" ]; then
    BASE_DOMAIN="overdrive.rocks"
  fi
  if [ -z "${DEPLOY_MODE}" ]; then
    DEPLOY_MODE="dev"
  fi
  if [ -z "${REMOTE_WRITE_URL}" ]; then
    REMOTE_WRITE_URL="https://metrics.overdrive.rocks/api/v1/write"
  fi
  if [ -z "${REMOTE_WRITE_USER}" ]; then
    REMOTE_WRITE_USER="ovr"
  fi
  if [ -z "${REMOTE_WRITE_PASSWORD_FILE}" ]; then
    REMOTE_WRITE_PASSWORD_FILE="${OVR_DIR}/secrets/remote_write_password"
  fi
  if [ -z "${VM_WRITE_URL_SECONDARY}" ]; then
    VM_WRITE_URL_SECONDARY="https://metrics.overdrive.rocks/write"
  fi
  if [ -z "${VM_WRITE_USERNAME}" ]; then
    VM_WRITE_USERNAME="ovr"
  fi
  if [ -z "${VM_WRITE_PASSWORD_FILE}" ]; then
    VM_WRITE_PASSWORD_FILE="${OVR_DIR}/secrets/vm_write_password"
  fi
}

prompt_line() {
  local var_name="$1"
  local label="$2"
  local desc="$3"
  local default_value="${4:-}"
  local secret="${5:-0}"
  local mask_default="${6:-0}"
  local input=""
  local current="${!var_name}"

  if [ -n "$current" ]; then
    default_value="$current"
  fi

  local default_label=""
  if [ -n "$default_value" ]; then
    if [ "$mask_default" = "1" ]; then
      default_label="(set)"
    else
      default_label="$default_value"
    fi
  else
    default_label="(blank to skip)"
  fi

  if [ -n "$desc" ]; then
    echo "$desc"
  fi

  if [ "$secret" = "1" ]; then
    read -r -s -p "$label [$default_label]: " input
    echo ""
  else
    read -r -p "$label [$default_label]: " input
  fi

  if [ -n "$input" ]; then
    printf -v "$var_name" '%s' "$input"
  elif [ -n "$default_value" ]; then
    printf -v "$var_name" '%s' "$default_value"
  fi
}

prompt_optional() {
  prompt_line "$@"
}

prompt_required() {
  local var_name="$1"
  while true; do
    prompt_line "$@"
    if [ -n "${!var_name}" ]; then
      return 0
    fi
    echo "This value is required."
  done
}

prompt_choice() {
  local var_name="$1"
  local label="$2"
  local desc="$3"
  local default_value="${4:-}"
  shift 4
  local -a options=("$@")
  local value=""
  local ok=0

  while true; do
    prompt_line "$var_name" "$label" "$desc" "$default_value"
    value="${!var_name}"
    if [ -z "$value" ]; then
      return 0
    fi
    ok=0
    for opt in "${options[@]}"; do
      if [ "$value" = "$opt" ]; then
        ok=1
        break
      fi
    done
    if [ "$ok" -eq 1 ]; then
      return 0
    fi
    echo "Invalid choice. Options: ${options[*]}"
  done
}

list_ifaces() {
  if command -v ip >/dev/null 2>&1; then
    ip -br link | awk '$1 != "lo" {print $1}'
    return 0
  fi
  if [ -d /sys/class/net ]; then
    ls /sys/class/net | awk '$1 != "lo" {print $1}'
  fi
}

prompt_bool() {
  local var_name="$1"
  local label="$2"
  local desc="$3"
  local default_value="${4:-false}"
  local current="${!var_name}"
  local reply=""
  local default_label=""

  if [ -z "$current" ]; then
    current="$default_value"
  fi
  if [ "$current" = "true" ]; then
    default_label="Y/n"
  else
    default_label="y/N"
  fi

  while true; do
    if [ -n "$desc" ]; then
      echo "$desc"
    fi
    read -r -p "$label [$default_label]: " reply
    case "$reply" in
      "" )
        printf -v "$var_name" '%s' "$current"
        return 0
        ;;
      y|Y|yes|YES)
        printf -v "$var_name" 'true'
        return 0
        ;;
      n|N|no|NO)
        printf -v "$var_name" 'false'
        return 0
        ;;
      *)
        echo "Please answer y or n."
        ;;
    esac
  done
}

prompt_for_values() {
  if [ "$PROMPT_ALL" -ne 1 ]; then
    return 0
  fi

  echo "== Identity =="
  prompt_required DEPLOYMENT_ID "Deployment ID" \
    "Fleet/group label used in metrics and configs." "${DEPLOYMENT_ID}"
  prompt_required NODE_ID "Node ID" \
    "Unique node name (e.g., n100-01)." "${NODE_ID}"
  prompt_optional BASE_DOMAIN "Base domain" \
    "Base domain for public hostnames." "${BASE_DOMAIN}"

  echo ""
  echo "== Networking (optional; skip if using ovru-netkit) =="
  prompt_choice LAN_MODE "LAN mode (dhcp=auto, static=fixed IP, skip=unchanged)" \
    "Configure LAN via NetworkManager. Choose dhcp, static, or skip." \
    "${LAN_MODE:-skip}" dhcp static skip
  if [ "${LAN_MODE}" = "skip" ]; then
    LAN_MODE=""
  fi
  if [ -n "${LAN_MODE}" ]; then
    mapfile -t ifaces < <(list_ifaces)
    if [ "${#ifaces[@]}" -gt 0 ]; then
      echo "Detected interfaces: ${ifaces[*]}"
    fi
    prompt_required LAN_IF "LAN interface" \
      "Interface name for LAN (e.g., enp3s0)." "${LAN_IF}"
    if [ "${LAN_MODE}" = "static" ]; then
      prompt_required LAN_IP "LAN IP CIDR" \
        "Static LAN IP with CIDR (e.g., 192.168.1.10/24)." "${LAN_IP}"
      prompt_required LAN_GW "LAN gateway" \
        "Static LAN gateway IP (e.g., 192.168.1.1)." "${LAN_GW}"
      prompt_optional LAN_DNS "LAN DNS" \
        "Optional LAN DNS servers (comma-separated)." "${LAN_DNS}"
    else
      prompt_optional LAN_DNS "LAN DNS override" \
        "Optional DNS servers for DHCP (comma-separated)." "${LAN_DNS}"
    fi
  fi

  prompt_optional WIFI_SSID "WiFi SSID" \
    "WiFi network name (leave blank to skip WiFi)." "${WIFI_SSID}"
  if [ -n "${WIFI_SSID}" ]; then
    prompt_optional WIFI_PASS "WiFi password" \
      "WiFi password (leave blank for open network)." "${WIFI_PASS}" 1 1
  fi

  echo ""
  echo "== Remote write (optional) =="
  prompt_optional REMOTE_WRITE_URL "Remote write URL" \
    "Remote write endpoint (leave blank to skip)." "${REMOTE_WRITE_URL}"
  if [ -n "${REMOTE_WRITE_URL}" ]; then
    prompt_required REMOTE_WRITE_USER "Remote write user" \
      "Remote write username." "${REMOTE_WRITE_USER}"
    local rw_default_file="${REMOTE_WRITE_PASSWORD_FILE:-${OVR_DIR}/secrets/remote_write_password}"
    prompt_optional REMOTE_WRITE_PASSWORD_FILE "Remote write password file" \
      "Path to password file (leave blank to be prompted for a password)." "${rw_default_file}"
    if [ -n "${REMOTE_WRITE_PASSWORD_FILE}" ] && [ -f "${REMOTE_WRITE_PASSWORD_FILE}" ]; then
      :
    else
      prompt_required REMOTE_WRITE_PASSWORD "Remote write password" \
        "Password for remote write (stored in a file)." "${REMOTE_WRITE_PASSWORD}" 1 1
    fi
  fi

  echo ""
  echo "== GX (optional) =="
  local gx_default="false"
  if [ "${HAS_GX}" = "true" ] || [ -n "${GX_HOST}" ]; then
    gx_default="true"
  fi
  prompt_bool HAS_GX "Is a GX device attached?" \
    "Set this to true when a GX device is connected to the GX port." "${gx_default}"
  if [ "${HAS_GX}" = "true" ]; then
    prompt_optional GX_HOST "GX host" \
      "GX hostname or IP address (optional)." "${GX_HOST}"
  fi

  echo ""
  echo "== Targets (optional) =="
  prompt_optional TARGETS_FILE "Targets file" \
    "Override targets.yml with a full file path (leave blank to skip)." "${TARGETS_FILE}"
  if [ -z "${TARGETS_FILE}" ]; then
    if [ -z "${TARGETS_INLINE_RAW}" ] && [ "${#TARGETS_INLINE[@]}" -gt 0 ]; then
      TARGETS_INLINE_RAW="$(IFS=,; echo "${TARGETS_INLINE[*]}")"
    fi
    prompt_optional TARGETS_INLINE_RAW "Targets inline" \
      "Inline targets (job=host:port,job=host:port) or blank to use defaults." "${TARGETS_INLINE_RAW}"
    parse_targets_inline "${TARGETS_INLINE_RAW}"
  fi
}

configure_network() {
  if [ -z "${LAN_MODE}" ] && [ -z "${WIFI_SSID}" ]; then
    return 0
  fi

  command -v nmcli >/dev/null 2>&1 || {
    echo "ERROR: nmcli not found. Install NetworkManager first." >&2
    exit 1
  }

  if [ -n "${LAN_MODE}" ]; then
    need "${LAN_IF}" "--lan-if is required when --lan-mode is set."

    if nmcli -t -f NAME con show | grep -qx ovr-lan; then
      nmcli con delete ovr-lan >/dev/null 2>&1 || true
    fi

    if [ "${LAN_MODE}" = "dhcp" ]; then
      nmcli con add type ethernet ifname "${LAN_IF}" con-name ovr-lan \
        ipv4.method auto ipv6.method ignore
    elif [ "${LAN_MODE}" = "static" ]; then
      need "${LAN_IP}" "--lan-ip is required for static mode."
      need "${LAN_GW}" "--lan-gw is required for static mode."
      nmcli con add type ethernet ifname "${LAN_IF}" con-name ovr-lan \
        ipv4.addresses "${LAN_IP}" ipv4.gateway "${LAN_GW}" ipv4.method manual ipv6.method ignore
      if [ -n "${LAN_DNS}" ]; then
        nmcli con mod ovr-lan ipv4.dns "${LAN_DNS}"
      fi
    else
      echo "ERROR: --lan-mode must be dhcp or static." >&2
      exit 1
    fi
    nmcli con up ovr-lan || true
  fi

  if [ -n "${WIFI_SSID}" ]; then
    if nmcli -t -f NAME con show | grep -qx ovr-wifi; then
      nmcli con delete ovr-wifi >/dev/null 2>&1 || true
    fi
    if [ -n "${WIFI_PASS}" ]; then
      nmcli dev wifi connect "${WIFI_SSID}" password "${WIFI_PASS}" name ovr-wifi
    else
      nmcli dev wifi connect "${WIFI_SSID}" name ovr-wifi
    fi
  fi
}

install_deps() {
  if command -v docker >/dev/null 2>&1; then
    return 0
  fi
  apt-get update
  apt-get install -y docker.io docker-compose curl jq ca-certificates
  systemctl enable --now docker
}

write_edge_env() {
  mkdir -p "${OVR_DIR}" "${OVR_DIR}/secrets"
  chmod 0700 "${OVR_DIR}/secrets"

  if [ -n "${REMOTE_WRITE_URL}" ]; then
    if [ -z "${REMOTE_WRITE_USER}" ]; then
      echo "ERROR: --remote-write-user is required when remote write is enabled." >&2
      exit 1
    fi
    if [ -z "${REMOTE_WRITE_PASSWORD_FILE}" ]; then
      REMOTE_WRITE_PASSWORD_FILE="${OVR_DIR}/secrets/remote_write_password"
    fi
    if [ ! -f "${REMOTE_WRITE_PASSWORD_FILE}" ]; then
      if [ -z "${REMOTE_WRITE_PASSWORD}" ]; then
        read -r -s -p "Remote write password: " REMOTE_WRITE_PASSWORD
        echo ""
      fi
      printf "%s" "${REMOTE_WRITE_PASSWORD}" > "${REMOTE_WRITE_PASSWORD_FILE}"
      chmod 0600 "${REMOTE_WRITE_PASSWORD_FILE}"
    fi
  fi

  if [ -n "${VM_WRITE_USERNAME}" ] || [ -n "${VM_WRITE_PASSWORD}" ] || [ -n "${VM_WRITE_PASSWORD_FILE}" ]; then
    if [ -z "${VM_WRITE_PASSWORD_FILE}" ]; then
      VM_WRITE_PASSWORD_FILE="${OVR_DIR}/secrets/vm_write_password"
    fi
    if [ -n "${VM_WRITE_PASSWORD}" ] && [ ! -f "${VM_WRITE_PASSWORD_FILE}" ]; then
      printf "%s" "${VM_WRITE_PASSWORD}" > "${VM_WRITE_PASSWORD_FILE}"
      chmod 0600 "${VM_WRITE_PASSWORD_FILE}"
    fi
  fi

  if [ -n "${MAPBOX_TOKEN}" ]; then
    if [ -z "${MAPBOX_TOKEN_FILE}" ]; then
      MAPBOX_TOKEN_FILE="${OVR_DIR}/secrets/mapbox_token"
    fi
    if [ ! -f "${MAPBOX_TOKEN_FILE}" ]; then
      printf "%s" "${MAPBOX_TOKEN}" > "${MAPBOX_TOKEN_FILE}"
      chmod 0600 "${MAPBOX_TOKEN_FILE}"
    fi
  fi

  if [ -n "${MAPBOX_TOKEN_FILE}" ] && [ -f "${MAPBOX_TOKEN_FILE}" ]; then
    MAPBOX_TOKEN=""
  fi

  if [ -n "${VM_WRITE_PASSWORD_FILE}" ] && [ -f "${VM_WRITE_PASSWORD_FILE}" ]; then
    VM_WRITE_PASSWORD=""
  fi

  if [ -z "${VM_WRITE_URL}" ]; then
    VM_WRITE_URL="http://victoria-metrics:8428/write"
  fi

  if [ -z "${VM_QUERY_URL}" ]; then
    VM_QUERY_URL="http://victoria-metrics:8428"
  fi

  local env_file="${OVR_DIR}/edge.env"
  {
    cat <<EOF
DEPLOYMENT_ID=${DEPLOYMENT_ID}
NODE_ID=${NODE_ID}
EOF
    if [ -n "${SYSTEM_ID}" ]; then
      echo "SYSTEM_ID=${SYSTEM_ID}"
    fi
    cat <<EOF
STACK_NAME=n100
BASE_DOMAIN=${BASE_DOMAIN:-overdrive.rocks}
DEPLOY_MODE=${DEPLOY_MODE:-dev}
EOF
    if [ -n "${GHCR_OWNER}" ]; then
      echo "GHCR_OWNER=${GHCR_OWNER}"
    fi
    if [ -n "${EDGE_VERSION}" ]; then
      echo "EDGE_VERSION=${EDGE_VERSION}"
    fi
    if [ -n "${EDGE_GIT_SHA}" ]; then
      echo "EDGE_GIT_SHA=${EDGE_GIT_SHA}"
    fi
    if [ -n "${VM_IMAGE}" ]; then
      echo "VM_IMAGE=${VM_IMAGE}"
    fi
    if [ -n "${VMAGENT_IMAGE}" ]; then
      echo "VMAGENT_IMAGE=${VMAGENT_IMAGE}"
    fi
    if [ -n "${GRAFANA_IMAGE}" ]; then
      echo "GRAFANA_IMAGE=${GRAFANA_IMAGE}"
    fi
    if [ -n "${TELEGRAF_IMAGE}" ]; then
      echo "TELEGRAF_IMAGE=${TELEGRAF_IMAGE}"
    fi
    if [ -n "${NODE_EXPORTER_IMAGE}" ]; then
      echo "NODE_EXPORTER_IMAGE=${NODE_EXPORTER_IMAGE}"
    fi
    if [ -n "${VM_RETENTION}" ]; then
      echo "VM_RETENTION=${VM_RETENTION}"
    fi
    cat <<EOF

VM_WRITE_URL=${VM_WRITE_URL}
VM_QUERY_URL=${VM_QUERY_URL}
VM_WRITE_URL_SECONDARY=${VM_WRITE_URL_SECONDARY}
VM_WRITE_USERNAME=${VM_WRITE_USERNAME}
VM_WRITE_PASSWORD_FILE=${VM_WRITE_PASSWORD_FILE:-${OVR_DIR}/secrets/vm_write_password}
VM_WRITE_PASSWORD=${VM_WRITE_PASSWORD}

VM_REMOTE_WRITE_URL=${REMOTE_WRITE_URL}
VM_REMOTE_WRITE_USERNAME=${REMOTE_WRITE_USER}
VM_REMOTE_WRITE_PASSWORD_FILE=${REMOTE_WRITE_PASSWORD_FILE:-${OVR_DIR}/secrets/remote_write_password}

MAPBOX_TOKEN=${MAPBOX_TOKEN}
MAPBOX_TOKEN_FILE=${MAPBOX_TOKEN_FILE:-${OVR_DIR}/secrets/mapbox_token}

GX_HOST=${GX_HOST}
GX_SSH_PORT=
GX_USER=root
GX_PASSWORD_FILE=${OVR_DIR}/secrets/gx_password
EOF
  } > "${env_file}"

  chmod 0600 "${env_file}"
}

sanitize_host_label() {
  local host="$1"
  local value="$host"
  local stripped=""

  if [[ "$value" =~ ^\\[(.*)\\](:[0-9]+)?$ ]]; then
    value="${BASH_REMATCH[1]}"
  fi

  if [[ "$value" =~ ^[^:]+:[0-9]+$ ]]; then
    value="${value%:*}"
  fi

  while [ -n "$value" ] && [ "${value##*.}" = "" ]; do
    value="${value%.}"
  done

  if [[ "$value" =~ ^[0-9]+(\\.[0-9]+){3}$ ]]; then
    printf '%s' "$value"
    return 0
  fi

  if [[ "$value" == *.* ]]; then
    value="${value%%.*}"
  fi

  stripped="$value"
  printf '%s' "$stripped"
}

strip_host_port() {
  local host="$1"
  local value="$host"

  if [[ "$value" =~ ^\\[.*\\]:[0-9]+$ ]]; then
    value="${value#\\[}"
    value="${value%\\]:*}"
  elif [[ "$value" =~ ^[^:]+:[0-9]+$ ]]; then
    value="${value%:*}"
  fi

  printf '%s' "$value"
}

generate_targets() {
  mkdir -p "${OVR_DIR}"
  local gx_hostname=""
  local gx_target_host=""

  if [ -n "${GX_HOST}" ]; then
    gx_target_host="$(strip_host_port "${GX_HOST}")"
    gx_hostname="$(sanitize_host_label "${gx_target_host}")"
  fi

  if [ -n "${TARGETS_FILE}" ]; then
    cp "${TARGETS_FILE}" "${OVR_DIR}/targets.yml"
    return 0
  fi

  if [ "${#TARGETS_INLINE[@]}" -gt 0 ]; then
    {
      for entry in "${TARGETS_INLINE[@]}"; do
        job="${entry%%=*}"
        target="${entry#*=}"
        if [ -z "${job}" ] || [ -z "${target}" ]; then
          continue
        fi
        local system_id="${NODE_ID}"
        local gx_host_label=""
        if [ "${job}" = "gx_fast" ] || [ "${job}" = "gx_slow" ]; then
          if [ -n "${gx_hostname}" ]; then
            system_id="${gx_hostname}"
          else
            local target_host
            target_host="$(strip_host_port "${target}")"
            system_id="$(sanitize_host_label "${target_host}")"
            gx_host_label="${target_host}"
          fi
          if [ -z "${gx_host_label}" ]; then
            gx_host_label="${gx_target_host}"
          fi
        fi
        cat <<EOF
- targets:
    - "${target}"
  labels:
    job: ${job}
    system_id: "${system_id}"
    node_id: "${NODE_ID}"
EOF
        if [ -n "${gx_host_label}" ]; then
          cat <<EOF
    gx_host: "${gx_host_label}"

EOF
        else
          echo ""
        fi
      done
    } > "${OVR_DIR}/targets.yml"
    return 0
  fi

  if [ -n "${GX_HOST}" ] && [ -f "${EDGE_DIR}/examples/targets.yml.example" ]; then
    sed \
      -e "s/__NODE_ID__/${NODE_ID}/g" \
      -e "s/__GX_HOST__/${gx_target_host}/g" \
      -e "s/__GX_HOSTNAME__/${gx_hostname}/g" \
      "${EDGE_DIR}/examples/targets.yml.example" \
      > "${OVR_DIR}/targets.yml"
  else
    cat > "${OVR_DIR}/targets.yml" <<EOF
- targets:
    - "host.docker.internal:9100"
  labels:
    job: node_exporter
    system_id: "${NODE_ID}"
    node_id: "${NODE_ID}"

- targets:
    - "events:8088"
  labels:
    job: event_service
    system_id: "${NODE_ID}"
    node_id: "${NODE_ID}"
EOF
    if [ -n "${GX_HOST}" ]; then
      cat >> "${OVR_DIR}/targets.yml" <<EOF

- targets:
    - "${gx_target_host}:9480"
  labels:
    job: gx_fast
    system_id: "${gx_hostname}"
    gx_host: "${gx_target_host}"

- targets:
    - "${gx_target_host}:9481"
  labels:
    job: gx_slow
    system_id: "${gx_hostname}"
    gx_host: "${gx_target_host}"
EOF
    fi
  fi
}

generate_vmagent_config() {
  local src="${EDGE_DIR}/vmagent/scrape.yml"
  local dst="${OVR_DIR}/vmagent.scrape.yml"
  if [ ! -f "${src}" ]; then
    echo "ERROR: Missing vmagent template at ${src}" >&2
    exit 1
  fi
  sed \
    -e "s/__DEPLOYMENT_ID__/${DEPLOYMENT_ID}/g" \
    -e "s/__NODE_ID__/${NODE_ID}/g" \
    "${src}" > "${dst}"
}

ensure_vmagent_remote_write_configs() {
  local src_dir="${EDGE_DIR}/vmagent"
  local dst_dir="${OVR_DIR}"
  local file
  for file in stream_aggr.yml remote_write_local_relabel.yml remote_write_cloud_relabel.yml; do
    if [ -f "${src_dir}/${file}" ] && [ ! -f "${dst_dir}/${file}" ]; then
      cp "${src_dir}/${file}" "${dst_dir}/${file}"
      chmod 0644 "${dst_dir}/${file}"
    fi
  done
}

ensure_acuvim_targets() {
  local file="${OVR_DIR}/targets_acuvim.txt"
  if [ ! -f "${file}" ]; then
    cat > "${file}" <<'EOF'
# One IP per line, example:
# 10.10.4.10
EOF
    chmod 0644 "${file}"
  fi
  if [ ! -e "${EDGE_DIR}/telegraf/targets_acuvim.txt" ]; then
    ln -s "${file}" "${EDGE_DIR}/telegraf/targets_acuvim.txt"
  fi
}

check_required_files() {
  local -a missing=()

  if [ -n "${REMOTE_WRITE_URL}" ] && [ -n "${REMOTE_WRITE_PASSWORD_FILE}" ]; then
    if [ ! -f "${REMOTE_WRITE_PASSWORD_FILE}" ]; then
      missing+=("${REMOTE_WRITE_PASSWORD_FILE} (remote write password)")
    fi
  fi

  if [ -n "${VM_WRITE_USERNAME}" ] || [ -n "${VM_WRITE_PASSWORD_FILE}" ]; then
    if [ -n "${VM_WRITE_PASSWORD_FILE}" ] && [ ! -f "${VM_WRITE_PASSWORD_FILE}" ]; then
      missing+=("${VM_WRITE_PASSWORD_FILE} (VM write password)")
    fi
  fi

  if [ -n "${MAPBOX_TOKEN_FILE}" ] && [ ! -f "${MAPBOX_TOKEN_FILE}" ]; then
    missing+=("${MAPBOX_TOKEN_FILE} (mapbox token)")
  fi

  if [ "${HAS_GX}" = "true" ]; then
    local gx_file="${OVR_DIR}/secrets/gx_password"
    if [ ! -f "${gx_file}" ]; then
      missing+=("${gx_file} (GX password)")
    fi
  fi

  if [ "${#missing[@]}" -gt 0 ]; then
    echo "ERROR: missing required secret file(s):" >&2
    printf '  - %s\n' "${missing[@]}" >&2
    exit 1
  fi
}

select_compose_cmd() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
    return 0
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
    return 0
  fi
  return 1
}

ensure_edge_env_symlink() {
  if [ -d "${EDGE_DIR}" ]; then
    ln -sf "${OVR_DIR}/edge.env" "${EDGE_DIR}/.env"
  fi
}

deploy_stack() {
  set -a
  # shellcheck disable=SC1090
  . "${OVR_DIR}/edge.env"
  set +a

  if ! select_compose_cmd; then
    echo "ERROR: docker compose not found. Install Docker Compose (v2 plugin or v1 binary)." >&2
    exit 1
  fi

  if [ ! -d "${EDGE_DIR}" ]; then
    echo "ERROR: Missing edge directory at ${EDGE_DIR}. Expected edge runtime at /opt/edge (or legacy /opt/stack/edge)." >&2
    exit 1
  fi

  local deploy_mode="${DEPLOY_MODE:-dev}"
  case "${deploy_mode}" in
    release)
      echo "Deploying EDGE using release compose (DEPLOY_MODE=release)..."
      (cd "${EDGE_DIR}" && "${COMPOSE_CMD[@]}" -f compose.release.yml up -d)
      ;;
    dev)
      echo "Deploying EDGE using dev compose (DEPLOY_MODE=dev)..."
      (cd "${EDGE_DIR}" && "${COMPOSE_CMD[@]}" -f compose.dev.yml up -d --build)
      ;;
    *)
      echo "WARNING: Unknown DEPLOY_MODE='${deploy_mode}'. Falling back to dev."
      (cd "${EDGE_DIR}" && "${COMPOSE_CMD[@]}" -f compose.dev.yml up -d --build)
      ;;
  esac
}

main() {
  require_root
  parse_args "$@"
  if [ ! -t 0 ]; then
    PROMPT_ALL=0
  fi
  load_existing_defaults
  apply_defaults
  prompt_for_values

  need "${DEPLOYMENT_ID}" "--deployment-id is required."
  need "${NODE_ID}" "--node-id is required."


  if [ -z "${HAS_GX}" ]; then
    HAS_GX="false"
  fi

  configure_network
  install_deps
  write_edge_env
  ensure_edge_env_symlink
  check_required_files
  generate_targets
  generate_vmagent_config
  ensure_vmagent_remote_write_configs
  ensure_acuvim_targets
  deploy_stack

  if [ "${HAS_GX}" = "true" ]; then
    if [ -z "${GX_HOST}" ]; then
      echo "WARNING: --gx-host not set. GX deployment is pending."
    else
      echo "GX detected. You can deploy the exporter using: ${EDGE_DIR}/gx/install_dbus_exporter.sh"
    fi
  fi

  echo "Bootstrap complete."
}

main "$@"
