#!/usr/bin/env bash
set -euo pipefail
umask 027

ENV_FILE_DEFAULT="/etc/ovr/firstboot.env"
ENV_FILE="${ENV_FILE:-$ENV_FILE_DEFAULT}"
WIFI_SSID="${WIFI_SSID:-}"
WIFI_PASS="${WIFI_PASS:-}"
WIFI_PASS_FILE="${WIFI_PASS_FILE:-}"
WIFI_IF="${WIFI_IF:-}"
CON_NAME="${CON_NAME:-ovr-wifi}"
NONINTERACTIVE=0

usage() {
  cat <<'EOF'
Usage: sudo provisioning/edge/setup-wifi.sh [options]

Options:
  --ssid <ssid>         WiFi SSID
  --pass <password>     WiFi password (blank for open)
  --pass-file <path>    Read WiFi password from file
  --iface <ifname>      WiFi interface (optional)
  --name <name>         Connection name (default: ovr-wifi)
  --env-file <path>     Load defaults from env file
  --noninteractive      Fail if required values are missing
  -h, --help            Show this help
EOF
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root (sudo)." >&2
    exit 1
  fi
}

trim_line() {
  local s="$1"
  s="${s%$'\r'}"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

load_env() {
  if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
  fi
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --ssid) WIFI_SSID="$2"; shift 2 ;;
      --pass) WIFI_PASS="$2"; shift 2 ;;
      --pass-file) WIFI_PASS_FILE="$2"; shift 2 ;;
      --iface) WIFI_IF="$2"; shift 2 ;;
      --name) CON_NAME="$2"; shift 2 ;;
      --env-file) ENV_FILE="$2"; shift 2 ;;
      --noninteractive) NONINTERACTIVE=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "ERROR: Unknown argument: $1" >&2; usage; exit 1 ;;
    esac
  done
}

resolve_password() {
  if [ -z "$WIFI_PASS" ] && [ -n "$WIFI_PASS_FILE" ] && [ -f "$WIFI_PASS_FILE" ]; then
    WIFI_PASS="$(<"$WIFI_PASS_FILE")"
    WIFI_PASS="$(trim_line "$WIFI_PASS")"
  fi
}

list_wifi_ifaces() {
  nmcli -t -f DEVICE,TYPE dev status | awk -F: '$2=="wifi" {print $1}'
}

choose_iface() {
  local -a options=("$@")
  local choice
  if [ "${#options[@]}" -eq 0 ]; then
    return 1
  fi
  echo "Select WiFi interface:"
  for i in "${!options[@]}"; do
    printf '  %s) %s\n' "$((i + 1))" "${options[$i]}"
  done
  while true; do
    read -r -p "Select (1-${#options[@]}): " choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && ((choice >= 1 && choice <= ${#options[@]})); then
      printf '%s' "${options[$((choice - 1))]}"
      return 0
    fi
  done
}

ensure_wifi_iface() {
  if [ -n "$WIFI_IF" ]; then
    return 0
  fi
  mapfile -t ifaces < <(list_wifi_ifaces)
  if [ "${#ifaces[@]}" -eq 0 ]; then
    echo "ERROR: no WiFi interfaces detected by NetworkManager." >&2
    exit 1
  fi
  if [ "${#ifaces[@]}" -eq 1 ]; then
    WIFI_IF="${ifaces[0]}"
    return 0
  fi
  if [ "$NONINTERACTIVE" -eq 1 ]; then
    echo "ERROR: multiple WiFi interfaces detected; set WIFI_IF." >&2
    exit 1
  fi
  WIFI_IF="$(choose_iface "${ifaces[@]}")"
}

bring_iface_up() {
  if [ -n "$WIFI_IF" ]; then
    ip link set dev "$WIFI_IF" up >/dev/null 2>&1 || true
    nmcli dev set "$WIFI_IF" managed yes >/dev/null 2>&1 || true
  fi
}

prompt_missing() {
  if [ "$NONINTERACTIVE" -eq 1 ]; then
    return 0
  fi
  if [ -z "$WIFI_SSID" ]; then
    read -r -p "WiFi SSID: " WIFI_SSID
  fi
  if [ -z "$WIFI_PASS" ]; then
    read -r -s -p "WiFi password (blank for open): " WIFI_PASS
    echo ""
  fi
}

validate_required() {
  if [ -z "$WIFI_SSID" ]; then
    echo "ERROR: WiFi SSID is required." >&2
    exit 1
  fi
}

main() {
  require_root
  load_env
  parse_args "$@"

  if ! command -v nmcli >/dev/null 2>&1; then
    echo "ERROR: nmcli not found. Install NetworkManager first." >&2
    echo "Hint: sudo bash /opt/ovr/provisioning/edge/install-packages.sh" >&2
    exit 1
  fi

  resolve_password
  prompt_missing
  validate_required
  ensure_wifi_iface
  bring_iface_up

  if nmcli -t -f NAME con show | grep -qx "$CON_NAME"; then
    nmcli con delete "$CON_NAME" >/dev/null 2>&1 || true
  fi

  if [ -n "$WIFI_PASS" ]; then
    nmcli dev wifi connect "$WIFI_SSID" password "$WIFI_PASS" name "$CON_NAME" ${WIFI_IF:+ifname "$WIFI_IF"}
  else
    nmcli dev wifi connect "$WIFI_SSID" name "$CON_NAME" ${WIFI_IF:+ifname "$WIFI_IF"}
  fi

  nmcli con mod "$CON_NAME" connection.autoconnect yes || true
  if [ -n "$WIFI_IF" ]; then
    nmcli con mod "$CON_NAME" connection.interface-name "$WIFI_IF" || true
  fi

  nmcli -t -f DEVICE,STATE,CONNECTION dev status || true
}

main "$@"
