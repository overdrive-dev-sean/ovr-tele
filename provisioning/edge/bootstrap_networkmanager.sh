#!/usr/bin/env bash
set -euo pipefail
umask 027

if [ -z "${BASH_VERSION:-}" ]; then
  echo "Please run with bash. If this file has CRLF line endings, run:"
  echo "  sed -i 's/\\r$//' /path/to/bootstrap_networkmanager.sh"
  exit 1
fi

LOG_PREFIX="[nm-bootstrap]"
LOG_FILE="${LOG_FILE:-/var/log/ovr-firstboot.log}"

append_log() {
  if [ -w "$LOG_FILE" ] || [ -w "$(dirname "$LOG_FILE")" ]; then
    printf '%s\n' "$1" >> "$LOG_FILE" 2>/dev/null || true
  fi
}

log() {
  local msg="${LOG_PREFIX} $*"
  echo "$msg"
  append_log "$msg"
}

err() {
  local msg="${LOG_PREFIX} ERROR: $*"
  echo "$msg" >&2
  append_log "$msg"
}

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    err "run as root (sudo)."
    exit 1
  fi
}

init_log_file() {
  local dir
  dir="$(dirname "$LOG_FILE")"
  mkdir -p "$dir"
  if [ ! -f "$LOG_FILE" ]; then
    touch "$LOG_FILE"
    chmod 0640 "$LOG_FILE" || true
  fi
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

ensure_packages() {
  local -a missing=()
  if ! have_cmd nmcli; then
    missing+=("network-manager")
  fi
  if ! have_cmd ip; then
    missing+=("iproute2")
  fi
  if [ "${#missing[@]}" -eq 0 ]; then
    return 0
  fi

  log "Installing packages: ${missing[*]}"
  apt-get update
  apt-get install -y "${missing[@]}"
}

backup_ifupdown() {
  local file="/etc/network/interfaces"
  local backup="/etc/network/interfaces.bak"
  local desired=$'auto lo\niface lo inet loopback\n'

  if [ -f "$file" ]; then
    local current
    current="$(cat "$file")"
    if [ "$current" != "$desired" ]; then
      if [ ! -f "$backup" ]; then
        cp "$file" "$backup"
        log "Backed up ${file} -> ${backup}"
      fi
    fi
  fi

  printf "%s" "$desired" > "$file"
  log "Wrote ${file} (loopback only)"
}

write_nm_config() {
  mkdir -p /etc/NetworkManager/conf.d
  cat > /etc/NetworkManager/conf.d/10-ifupdown-managed.conf <<'EOF'
[ifupdown]
managed=true
EOF

  cat > /etc/NetworkManager/conf.d/99-managed-all.conf <<'EOF'
[keyfile]
unmanaged-devices=
EOF

  log "Wrote NetworkManager managed config"
}

disable_networkd_if_active() {
  if systemctl is-active --quiet systemd-networkd 2>/dev/null; then
    log "Disabling systemd-networkd to avoid conflicts"
    systemctl disable --now systemd-networkd
  fi
}

restart_nm() {
  systemctl enable --now NetworkManager
  systemctl restart NetworkManager
  if ! systemctl is-active --quiet NetworkManager; then
    err "NetworkManager failed to start."
    exit 1
  fi
}

iface_exists() {
  [ -d "/sys/class/net/$1" ]
}

find_conn_for_iface() {
  local ifname="$1"
  nmcli -t -f NAME,connection.interface-name connection show 2>/dev/null | \
    awk -F: -v dev="$ifname" '$2==dev {print $1; exit}'
}

ensure_conn() {
  local ifname="$1"
  local conn_name="$2"
  local ipv4_method="$3"
  local ipv6_method="$4"
  local desired_name="$conn_name"

  if ! iface_exists "$ifname"; then
    log "Skipping ${ifname} (interface not present)"
    return 0
  fi

  local existing=""
  existing="$(find_conn_for_iface "$ifname")"
  if [ -z "$existing" ]; then
    if nmcli -t -f NAME connection show | grep -qx "$conn_name"; then
      existing="$conn_name"
    fi
  fi

  if [ -n "$existing" ]; then
    if [ "$existing" != "$desired_name" ]; then
      if ! nmcli -t -f NAME connection show | grep -qx "$desired_name"; then
        nmcli connection modify "$existing" connection.id "$desired_name"
        existing="$desired_name"
      fi
    fi
    nmcli connection modify "$existing" \
      connection.interface-name "$ifname" \
      connection.autoconnect yes \
      ipv4.method "$ipv4_method" \
      ipv6.method "$ipv6_method"
    log "Updated connection ${existing} (${ifname})"
  else
    nmcli connection add type ethernet ifname "$ifname" con-name "$conn_name" \
      ipv4.method "$ipv4_method" ipv6.method "$ipv6_method" \
      connection.autoconnect yes
    log "Created connection ${conn_name} (${ifname})"
  fi

  nmcli connection up "$existing" >/dev/null 2>&1 || nmcli connection up "$conn_name" >/dev/null 2>&1 || true
}

validate() {
  echo ""
  echo "${LOG_PREFIX} Validation:"
  nmcli dev status || true
  nmcli -t -f NAME,DEVICE,TYPE,STATE connection show --active || true
}

main() {
  require_root
  init_log_file
  trap 'err "Fatal error on line $LINENO"' ERR
  ensure_packages
  backup_ifupdown
  write_nm_config
  disable_networkd_if_active
  restart_nm

  ensure_conn enp1s0 enp1s0-dhcp auto auto

  for dev in enp2s0 enp3s0 enp4s0 enp5s0 enp7s0; do
    ensure_conn "$dev" "${dev}-noip" disabled ignore
  done

  validate
}

main "$@"
