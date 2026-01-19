#!/usr/bin/env bash
set -euo pipefail
umask 027

CONFIG_FILE_DEFAULT="/etc/systemd/network/10-ovr-lan.network"
CONFIG_FILE="$CONFIG_FILE_DEFAULT"
MODE=""
IFACE=""
IP_CIDR=""
GW=""
DNS=""
NONINTERACTIVE=0
FORCE=0

usage() {
  cat <<'EOF'
Usage: sudo provisioning/edge/bringup-network.sh [options]

Options:
  --iface <name>        Interface name (e.g., enp3s0)
  --mode dhcp|static    Network mode
  --ip <cidr>           Static IP (e.g., 192.168.1.10/24)
  --gw <ip>             Static gateway (e.g., 192.168.1.1)
  --dns <ip[,ip]>       DNS servers (comma-separated)
  --config <path>       Override networkd config file
  --noninteractive      Fail if required values are missing
  --force               Overwrite config without prompting
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

list_ifaces() {
  ip -br link | awk '$1 != "lo" {print $1}'
}

choose_iface() {
  local -a options=("$@")
  local choice
  if [ "${#options[@]}" -eq 0 ]; then
    return 1
  fi
  echo "Select interface:"
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

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --iface) IFACE="$2"; shift 2 ;;
      --mode) MODE="$2"; shift 2 ;;
      --ip) IP_CIDR="$2"; shift 2 ;;
      --gw) GW="$2"; shift 2 ;;
      --dns) DNS="$2"; shift 2 ;;
      --config) CONFIG_FILE="$2"; shift 2 ;;
      --noninteractive) NONINTERACTIVE=1; shift ;;
      --force) FORCE=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) echo "ERROR: Unknown argument: $1" >&2; usage; exit 1 ;;
    esac
  done
}

prompt_missing() {
  if [ "$NONINTERACTIVE" -eq 1 ]; then
    return 0
  fi

  if [ -z "$IFACE" ]; then
    mapfile -t ifaces < <(list_ifaces)
    if [ "${#ifaces[@]}" -eq 1 ]; then
      IFACE="${ifaces[0]}"
    else
      IFACE="$(choose_iface "${ifaces[@]}")"
    fi
  fi

  if [ -z "$MODE" ]; then
    read -r -p "Mode [dhcp/static] (default: dhcp): " MODE
  fi
  MODE="${MODE:-dhcp}"

  if [ "$MODE" = "static" ]; then
    if [ -z "$IP_CIDR" ]; then
      read -r -p "Static IP CIDR (e.g. 192.168.1.10/24): " IP_CIDR
    fi
    if [ -z "$GW" ]; then
      read -r -p "Gateway (e.g. 192.168.1.1): " GW
    fi
    if [ -z "$DNS" ]; then
      read -r -p "DNS servers (comma-separated, blank for none): " DNS
    fi
  else
    if [ -z "$DNS" ]; then
      read -r -p "DNS servers (comma-separated, blank to use DHCP): " DNS
    fi
  fi
}

validate_required() {
  if [ -z "$IFACE" ]; then
    echo "ERROR: interface not set. Use --iface or run interactively." >&2
    exit 1
  fi
  if [ -z "$MODE" ]; then
    MODE="dhcp"
  fi
  if [ "$MODE" != "dhcp" ] && [ "$MODE" != "static" ]; then
    echo "ERROR: invalid mode: $MODE" >&2
    exit 1
  fi
  if [ "$MODE" = "static" ]; then
    if [ -z "$IP_CIDR" ] || [ -z "$GW" ]; then
      echo "ERROR: --ip and --gw are required for static mode." >&2
      exit 1
    fi
  fi
}

write_config() {
  local dns_line

  mkdir -p "$(dirname "$CONFIG_FILE")"
  if [ -f "$CONFIG_FILE" ]; then
    if [ "$FORCE" -ne 1 ] && [ "$NONINTERACTIVE" -eq 0 ]; then
      read -r -p "Overwrite ${CONFIG_FILE}? [Y/n] " reply
      case "$reply" in
        n|N|no|NO) echo "Aborted."; exit 1 ;;
      esac
    fi
    cp -a "$CONFIG_FILE" "${CONFIG_FILE}.bak.$(date +%Y%m%d%H%M%S)"
  fi

  if [ "$MODE" = "dhcp" ]; then
    cat > "$CONFIG_FILE" <<EOF
[Match]
Name=${IFACE}

[Network]
DHCP=yes
EOF
  else
    cat > "$CONFIG_FILE" <<EOF
[Match]
Name=${IFACE}

[Network]
Address=${IP_CIDR}
Gateway=${GW}
EOF
  fi

  if [ -n "$DNS" ]; then
    printf '%s\n' "$DNS" | tr ',' '\n' | while IFS= read -r dns_line; do
      dns_line="$(trim_line "$dns_line")"
      [ -z "$dns_line" ] && continue
      printf 'DNS=%s\n' "$dns_line"
    done >> "$CONFIG_FILE"
  fi
}

apply_config() {
  if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    echo "Warning: NetworkManager is active. It may conflict with systemd-networkd."
  fi

  ip link set dev "$IFACE" up || true
  systemctl enable --now systemd-networkd
  systemctl restart systemd-networkd
}

show_status() {
  echo "== Interface status =="
  ip -4 addr show dev "$IFACE" || true
  echo "== Routes =="
  ip route || true
  if command -v getent >/dev/null 2>&1; then
    if ! getent hosts deb.debian.org >/dev/null 2>&1; then
      echo "DNS lookup failed. If IP works, set resolvers:"
      echo "  echo -e \"nameserver 1.1.1.1\\nnameserver 8.8.8.8\" | sudo tee /etc/resolv.conf"
    fi
  fi
}

main() {
  require_root
  parse_args "$@"
  prompt_missing
  validate_required
  write_config
  apply_config
  show_status
}

main "$@"
