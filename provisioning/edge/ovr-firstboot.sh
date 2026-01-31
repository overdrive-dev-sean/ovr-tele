#!/usr/bin/env bash
set -euo pipefail
umask 027

MARKER="/etc/ovr/.firstboot_done"
ENV_FILE="/etc/ovr/firstboot.env"
BOOTSTRAP_ARGS_FILE="/etc/ovr/bootstrap.args"
BOOTSTRAP_SCRIPT="/opt/ovr/provisioning/edge/bootstrap_n100.sh"
NM_BOOTSTRAP_SCRIPT="/opt/ovr/provisioning/edge/bootstrap_networkmanager.sh"
PACKAGE_LIST_FILE_DEFAULT="/opt/ovr/provisioning/edge/firstboot-packages.txt"
PACKAGE_LIST_FILE="${PACKAGE_LIST_FILE:-$PACKAGE_LIST_FILE_DEFAULT}"
WIFI_PACKAGE_LIST_FILE_DEFAULT="/opt/ovr/provisioning/edge/firstboot-packages-wifi.txt"
WIFI_PACKAGE_LIST_FILE="${WIFI_PACKAGE_LIST_FILE:-$WIFI_PACKAGE_LIST_FILE_DEFAULT}"
RUN_NM_BOOTSTRAP="${RUN_NM_BOOTSTRAP:-1}"
RUN_PACKAGE_INSTALL="${RUN_PACKAGE_INSTALL:-1}"
RUN_WIFI_SETUP="${RUN_WIFI_SETUP:-1}"
APT_FORCE_IPV4="${APT_FORCE_IPV4:-0}"
LAST_NET_CONN=""
DEFAULT_DNS_FALLBACK="${DEFAULT_DNS_FALLBACK:-1.1.1.1,1.0.0.1,8.8.8.8,8.8.4.4}"
LOG_FILE="${LOG_FILE:-/var/log/ovr-firstboot.log}"
LAN_DHCP_RETRIES="${LAN_DHCP_RETRIES:-3}"
LAN_DHCP_WAIT_SECONDS="${LAN_DHCP_WAIT_SECONDS:-4}"
LAN_LINK_RETRIES="${LAN_LINK_RETRIES:-3}"
LAN_LINK_WAIT_SECONDS="${LAN_LINK_WAIT_SECONDS:-2}"
CONNECTIVITY_WAIT_SECONDS="${CONNECTIVITY_WAIT_SECONDS:-120}"
CONNECTIVITY_RETRY_SECONDS="${CONNECTIVITY_RETRY_SECONDS:-5}"
APT_UPDATE_RETRIES="${APT_UPDATE_RETRIES:-3}"
APT_UPDATE_WAIT_SECONDS="${APT_UPDATE_WAIT_SECONDS:-5}"
DNS_PROBE_HOST="${DNS_PROBE_HOST:-deb.debian.org}"
IP_PROBE_HOST="${IP_PROBE_HOST:-1.1.1.1}"
IP_PROBE_PORT="${IP_PROBE_PORT:-443}"
RESIZE_TTY="${RESIZE_TTY:-1}"
TTY_COLS="${TTY_COLS:-80}"
TTY_ROWS="${TTY_ROWS:-24}"
TTY_DEVICE="${TTY_DEVICE:-/dev/console}"

if [ -f "$MARKER" ]; then
  exit 0
fi

if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
fi

NONINTERACTIVE="${NONINTERACTIVE:-0}"
if [ ! -t 0 ]; then
  NONINTERACTIVE=1
fi

mkdir -p "$(dirname "$LOG_FILE")"
touch "$LOG_FILE"
chmod 0640 "$LOG_FILE" || true
exec > >(tee -a "$LOG_FILE") 2>&1

say() {
  printf '%s\n' "$*"
}

resize_tty() {
  local cols="$TTY_COLS"
  local rows="$TTY_ROWS"
  local dev="$TTY_DEVICE"

  if [ "$RESIZE_TTY" != "1" ]; then
    return 0
  fi

  if [ -z "$cols" ] || [ -z "$rows" ]; then
    return 0
  fi

  if [ -n "$dev" ] && [ -e "$dev" ]; then
    stty -F "$dev" cols "$cols" rows "$rows" >/dev/null 2>&1 || true
  elif [ -t 0 ]; then
    stty cols "$cols" rows "$rows" >/dev/null 2>&1 || true
  elif [ -t 1 ]; then
    stty cols "$cols" rows "$rows" >/dev/null 2>&1 || true
  fi

  export COLUMNS="$cols"
  export LINES="$rows"
}

have_nmcli() {
  command -v nmcli >/dev/null 2>&1
}

probe_dns() {
  local host="${DNS_PROBE_HOST}"
  if command -v getent >/dev/null 2>&1; then
    if [ "$APT_FORCE_IPV4" = "1" ]; then
      if getent ahostsv4 "$host" >/dev/null 2>&1; then
        return 0
      fi
    else
      if getent hosts "$host" >/dev/null 2>&1; then
        return 0
      fi
    fi
  fi
  return 1
}

probe_tcp() {
  local host="$1"
  local port="$2"
  if ! command -v timeout >/dev/null 2>&1; then
    return 1
  fi
  if timeout 2 bash -c 'cat < /dev/null > /dev/tcp/$1/$2' _ "$host" "$port" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

probe_ip() {
  local host="${IP_PROBE_HOST}"
  local port="${IP_PROBE_PORT}"
  if probe_tcp "$host" "$port"; then
    return 0
  fi
  if command -v ping >/dev/null 2>&1; then
    if ping -c 1 -W 2 "$host" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

check_connectivity() {
  probe_dns && probe_ip
}

wait_for_connectivity() {
  local label="$1"
  local max_wait="$CONNECTIVITY_WAIT_SECONDS"
  local interval="$CONNECTIVITY_RETRY_SECONDS"
  local elapsed=0

  if [ "$max_wait" -le 0 ]; then
    check_connectivity
    return $?
  fi

  say "Waiting for internet connectivity before ${label} package install (up to ${max_wait}s)..."
  while [ "$elapsed" -lt "$max_wait" ]; do
    if check_connectivity; then
      return 0
    fi
    fix_dns_if_needed
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done

  return 1
}

ensure_nonfree_firmware_sources() {
  local changed=0

  if [ -f /etc/apt/sources.list.d/debian.sources ]; then
    if ! grep -qE '^Components:.*non-free-firmware' /etc/apt/sources.list.d/debian.sources; then
      sed -i '/^Components:/ s/$/ non-free-firmware/' /etc/apt/sources.list.d/debian.sources
      changed=1
    fi
  fi

  if [ -f /etc/apt/sources.list ]; then
    if grep -qE '^deb ' /etc/apt/sources.list; then
      sed -i '/^deb /{/non-free-firmware/! s/$/ non-free-firmware/}' /etc/apt/sources.list
      changed=1
    fi
  fi

  if [ "$changed" -eq 1 ]; then
    say "Enabled non-free-firmware in APT sources."
  fi
}

apt_update() {
  if [ "$APT_FORCE_IPV4" = "1" ]; then
    apt-get -o Acquire::ForceIPv4=true update
  else
    apt-get update
  fi
}

apt_install() {
  if [ "$APT_FORCE_IPV4" = "1" ]; then
    DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::ForceIPv4=true install -y --no-install-recommends "$@"
  else
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
  fi
}

apt_update_with_retries() {
  local attempt=1
  while [ "$attempt" -le "$APT_UPDATE_RETRIES" ]; do
    if apt_update; then
      return 0
    fi
    say "apt-get update failed (attempt ${attempt}/${APT_UPDATE_RETRIES})."
    fix_dns_if_needed
    sleep "$APT_UPDATE_WAIT_SECONDS"
    attempt=$((attempt + 1))
  done
  return 1
}

iface_type() {
  local iface="$1"
  if [ -d "/sys/class/net/${iface}/wireless" ]; then
    printf 'wifi'
  else
    printf 'ethernet'
  fi
}

iface_state() {
  local iface="$1"
  local oper="unknown"
  local link="unknown"
  if [ -f "/sys/class/net/${iface}/operstate" ]; then
    oper="$(cat "/sys/class/net/${iface}/operstate" 2>/dev/null || true)"
  fi
  if [ -f "/sys/class/net/${iface}/carrier" ]; then
    if [ "$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null || echo 0)" = "1" ]; then
      link="up"
    else
      link="down"
    fi
  fi
  printf '%s/%s' "$oper" "$link"
}

iface_carrier_up() {
  local iface="$1"
  if [ -f "/sys/class/net/${iface}/carrier" ]; then
    [ "$(cat "/sys/class/net/${iface}/carrier" 2>/dev/null || echo 0)" = "1" ]
    return
  fi
  return 1
}

iface_ipv4() {
  local iface="$1"
  if command -v ip >/dev/null 2>&1; then
    ip -4 -o addr show dev "$iface" 2>/dev/null | awk '{print $4}' | paste -sd ',' - || true
  fi
}

iface_has_ipv4() {
  local iface="$1"
  if [ -z "$iface" ]; then
    return 1
  fi
  if command -v ip >/dev/null 2>&1; then
    ip -4 -o addr show dev "$iface" 2>/dev/null | awk '{print $4}' | grep -q .
  else
    return 1
  fi
}

list_sys_ifaces() {
  local path
  local iface
  for path in /sys/class/net/*; do
    iface="${path##*/}"
    if [ "$iface" = "lo" ]; then
      continue
    fi
    printf '%s\n' "$iface"
  done
}

list_sys_ifaces_by_type() {
  local kind="$1"
  local iface
  while IFS= read -r iface; do
    if [ "$(iface_type "$iface")" = "$kind" ]; then
      printf '%s\n' "$iface"
    fi
  done < <(list_sys_ifaces)
}

find_iface_with_ipv4() {
  local iface
  while IFS= read -r iface; do
    if iface_has_ipv4 "$iface"; then
      printf '%s' "$iface"
      return 0
    fi
  done < <(list_sys_ifaces_by_type ethernet)
  return 1
}

bring_iface_up() {
  local iface="$1"
  if [ -z "$iface" ]; then
    return 1
  fi
  if command -v ip >/dev/null 2>&1; then
    ip link set dev "$iface" up >/dev/null 2>&1 || true
  fi
  return 0
}

wait_for_carrier() {
  local iface="$1"
  local attempt=1
  if [ -z "$iface" ]; then
    return 1
  fi
  while [ "$attempt" -le "$LAN_LINK_RETRIES" ]; do
    if iface_carrier_up "$iface"; then
      return 0
    fi
    say "Waiting for link on ${iface} (attempt ${attempt}/${LAN_LINK_RETRIES})..."
    sleep "$LAN_LINK_WAIT_SECONDS"
    attempt=$((attempt + 1))
  done
  say "No link detected on ${iface}; check cable or switch."
  return 1
}

show_interface_status() {
  local iface
  local ip
  say "Adapters:"
  printf '  %-10s %-9s %-14s %-18s\n' "IFACE" "TYPE" "STATE" "IPV4"
  while IFS= read -r iface; do
    ip="$(iface_ipv4 "$iface")"
    if [ -z "$ip" ]; then
      ip="-"
    fi
    printf '  %-10s %-9s %-14s %-18s\n' "$iface" "$(iface_type "$iface")" "$(iface_state "$iface")" "$ip"
  done < <(list_sys_ifaces)
}

configure_ethernet_networkd() {
  local lan_mode="${LAN_MODE:-}"
  local lan_if="${LAN_IF:-}"
  local lan_ip="${LAN_IP:-}"
  local lan_gw="${LAN_GW:-}"
  local lan_dns="${LAN_DNS:-}"
  local lan_dns_fallback="${LAN_DNS_FALLBACK:-$DEFAULT_DNS_FALLBACK}"

  if [ "$NONINTERACTIVE" -eq 1 ] && [ -z "$lan_if" ]; then
    mapfile -t ifaces < <(list_sys_ifaces_by_type ethernet)
    if [ "${#ifaces[@]}" -gt 0 ]; then
      lan_if="${ifaces[0]}"
    fi
  fi

  if [ "$NONINTERACTIVE" -ne 1 ]; then
    if [ -z "$lan_if" ]; then
      mapfile -t ifaces < <(list_sys_ifaces_by_type ethernet)
      if [ "${#ifaces[@]}" -gt 0 ]; then
        lan_if="$(choose_iface 'Select LAN interface:' "${ifaces[@]}" || true)"
      fi
    fi

    if [ -n "$lan_if" ] && [ -z "$lan_mode" ]; then
      read -r -p "LAN mode [dhcp/static] (default: dhcp): " lan_mode
    fi

    if [ -n "$lan_if" ] && [ "${lan_mode:-dhcp}" = "static" ]; then
      if [ -z "$lan_ip" ]; then
        read -r -p "LAN IP CIDR (e.g. 192.168.1.10/24): " lan_ip
      fi
      if [ -z "$lan_gw" ]; then
        read -r -p "LAN gateway (e.g. 192.168.1.1): " lan_gw
      fi
      if [ -z "$lan_dns" ]; then
        read -r -p "LAN DNS (comma-separated, blank for none): " lan_dns
      fi
    elif [ -n "$lan_if" ] && [ "${lan_mode:-dhcp}" = "dhcp" ]; then
      if [ -z "$lan_dns" ]; then
        read -r -p "LAN DNS fallback (comma-separated, blank for default ${lan_dns_fallback}): " lan_dns
      fi
    fi
  fi

  if [ -z "$lan_if" ]; then
    LAN_IF=""
    return 1
  fi

  lan_mode="${lan_mode:-dhcp}"
  LAN_IF="$lan_if"
  LAN_MODE="$lan_mode"
  bring_iface_up "$lan_if"
  wait_for_carrier "$lan_if" || true
  if [ "$lan_mode" = "dhcp" ]; then
    if [ -z "$lan_dns" ]; then
      lan_dns="$lan_dns_fallback"
    fi
  fi

  mkdir -p /etc/systemd/network
  local cfg="/etc/systemd/network/10-ovr-lan.network"
  if [ "$lan_mode" = "dhcp" ]; then
    cat > "$cfg" <<EOF
[Match]
Name=${lan_if}

[Network]
DHCP=yes
EOF
    if [ -n "$lan_dns" ]; then
      printf '%s\n' "$lan_dns" | tr ',' '\n' | while IFS= read -r dns; do
        dns="$(trim_line "$dns")"
        [ -z "$dns" ] && continue
        printf 'DNS=%s\n' "$dns"
      done >> "$cfg"
    fi
  else
    if [ -z "$lan_ip" ] || [ -z "$lan_gw" ]; then
      say "Skipping LAN config: static settings are incomplete."
      return 1
    fi
    cat > "$cfg" <<EOF
[Match]
Name=${lan_if}

[Network]
Address=${lan_ip}
Gateway=${lan_gw}
EOF
    if [ -n "$lan_dns" ]; then
      printf '%s\n' "$lan_dns" | tr ',' '\n' | while IFS= read -r dns; do
        dns="$(trim_line "$dns")"
        [ -z "$dns" ] && continue
        printf 'DNS=%s\n' "$dns"
      done >> "$cfg"
    fi
  fi

  systemctl enable systemd-networkd >/dev/null 2>&1 || true
  systemctl restart systemd-networkd || true
  if [ "$lan_mode" = "dhcp" ]; then
    local attempt=1
    while [ "$attempt" -le "$LAN_DHCP_RETRIES" ]; do
      if iface_has_ipv4 "$lan_if"; then
        break
      fi
      say "Waiting for DHCP on ${lan_if} (attempt ${attempt}/${LAN_DHCP_RETRIES})..."
      sleep "$LAN_DHCP_WAIT_SECONDS"
      if iface_has_ipv4 "$lan_if"; then
        break
      fi
      systemctl restart systemd-networkd || true
      attempt=$((attempt + 1))
    done
  fi
  LAST_NET_CONN="systemd-networkd"
  return 0
}

configure_nm_unmanaged_lan() {
  if [ -z "${LAN_IF:-}" ]; then
    return 0
  fi

  mkdir -p /etc/NetworkManager/conf.d
  cat > /etc/NetworkManager/conf.d/10-ovr-unmanaged.conf <<EOF
[keyfile]
unmanaged-devices=interface-name:${LAN_IF}
EOF
}

prompt_dns_fix() {
  local dns=""
  if [ "$NONINTERACTIVE" -eq 1 ]; then
    dns="${LAN_DNS_FALLBACK:-$DEFAULT_DNS_FALLBACK}"
  else
    read -r -p "DNS lookup failed. Enter DNS server(s) (comma-separated) or blank to skip: " dns
  fi
  if [ -z "$dns" ]; then
    return 0
  fi

  if have_nmcli && [ -n "$LAST_NET_CONN" ] && nmcli -t -f NAME con show | grep -qx "$LAST_NET_CONN"; then
    nmcli con mod "$LAST_NET_CONN" ipv4.dns "$dns"
    nmcli con up "$LAST_NET_CONN" >/dev/null 2>&1 || true
    return 0
  fi

  if [ "$LAST_NET_CONN" = "systemd-networkd" ] && [ -f /etc/systemd/network/10-ovr-lan.network ]; then
    sed -i '/^DNS=/d' /etc/systemd/network/10-ovr-lan.network
    printf '%s\n' "$dns" | tr ',' '\n' | while IFS= read -r server; do
      server="$(trim_line "$server")"
      [ -z "$server" ] && continue
      printf 'DNS=%s\n' "$server"
    done >> /etc/systemd/network/10-ovr-lan.network
    systemctl restart systemd-networkd || true
  fi

  {
    printf '%s\n' "$dns" | tr ',' '\n' | while IFS= read -r server; do
      server="$(trim_line "$server")"
      [ -z "$server" ] && continue
      printf 'nameserver %s\n' "$server"
    done
  } > /etc/resolv.conf
}

fix_dns_if_needed() {
  local dns_ok=0
  local ip_ok=0

  if probe_dns; then
    dns_ok=1
  fi
  if probe_ip; then
    ip_ok=1
  fi

  if [ "$ip_ok" -eq 1 ] && [ "$dns_ok" -eq 0 ]; then
    say "DNS looks down while IP is up."
    prompt_dns_fix
  fi
}

preflight_network() {
  local dns_ok=0
  local ip_ok=0
  local choice=""
  local have_ethernet=0
  local active_lan=""
  local lan_has_ipv4=0

  if list_sys_ifaces_by_type ethernet | grep -q .; then
    have_ethernet=1
  fi

  say "Network pre-flight check..."
  show_interface_status

  if [ -n "${LAN_IF:-}" ] && iface_has_ipv4 "$LAN_IF"; then
    lan_has_ipv4=1
  elif [ -z "${LAN_IF:-}" ]; then
    active_lan="$(find_iface_with_ipv4 || true)"
    if [ -n "$active_lan" ]; then
      LAN_IF="$active_lan"
      lan_has_ipv4=1
    fi
  fi

  if [ "$have_ethernet" -eq 1 ] && [ ! -f /etc/systemd/network/10-ovr-lan.network ] && [ "$lan_has_ipv4" -eq 0 ]; then
    say "Configuring LAN via systemd-networkd (DHCP + DNS fallback)..."
    configure_ethernet_networkd || true
  fi

  if probe_dns; then
    dns_ok=1
  fi
  if probe_ip; then
    ip_ok=1
  fi

  if [ "$ip_ok" -eq 0 ] || [ "$dns_ok" -eq 0 ]; then
    if [ "$NONINTERACTIVE" -eq 0 ]; then
      read -r -p "Configure ethernet with systemd-networkd now? [Y/n] " choice
      case "$choice" in
        n|N|no|NO) ;;
        *) configure_ethernet_networkd || true ;;
      esac
    else
      if [ -n "${LAN_IF:-}" ] && iface_has_ipv4 "$LAN_IF"; then
        :
      else
        configure_ethernet_networkd || true
      fi
    fi
  fi

  dns_ok=0
  ip_ok=0
  if probe_dns; then
    dns_ok=1
  fi
  if probe_ip; then
    ip_ok=1
  fi

  say "Connectivity: DNS $( [ "$dns_ok" -eq 1 ] && printf 'OK' || printf 'FAIL' ), IP $( [ "$ip_ok" -eq 1 ] && printf 'OK' || printf 'FAIL' )."

  if [ "$ip_ok" -eq 1 ] && [ "$dns_ok" -eq 0 ]; then
    prompt_dns_fix
  fi

  show_interface_status
  return 0
}

collect_packages() {
  local file="$1"
  local -n out="$2"
  local line
  out=()
  if [ ! -f "$file" ]; then
    return 0
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    line="$(trim_line "$line")"
    if [ -z "$line" ] || [[ "$line" == \#* ]]; then
      continue
    fi
    out+=("$line")
  done < "$file"
}

install_packages_from_list() {
  local list_file="$1"
  local label="$2"
  local -a packages=()

  if [ ! -f "$list_file" ]; then
    say "Package list not found: $list_file"
    return 0
  fi

  collect_packages "$list_file" packages
  if [ "${#packages[@]}" -eq 0 ]; then
    say "No ${label} packages configured."
    return 0
  fi

  say "Checking network connectivity before ${label} package install..."
  if ! wait_for_connectivity "$label"; then
    say "No internet connectivity detected. Skipping ${label} package installation."
    return 1
  fi

  say "Installing ${label} packages..."
  if ! apt_update_with_retries; then
    say "apt-get update failed after ${APT_UPDATE_RETRIES} attempts."
    return 1
  fi
  if ! apt_install "${packages[@]}"; then
    say "apt-get install failed for ${label} packages."
    return 1
  fi

  return 0
}

list_ifaces() {
  nmcli -t -f DEVICE,TYPE,STATE dev status | awk -F: -v t="$1" '$2==t {print $1}'
}

choose_iface() {
  local prompt="$1"
  shift
  local -a options=("$@")
  local choice

  if [ "${#options[@]}" -eq 0 ]; then
    return 1
  fi

  say "$prompt"
  for i in "${!options[@]}"; do
    printf '  %s) %s\n' "$((i + 1))" "${options[$i]}"
  done

  while true; do
    read -r -p "Select (1-${#options[@]}) or blank to skip: " choice
    if [ -z "$choice" ]; then
      return 1
    fi
    if [[ "$choice" =~ ^[0-9]+$ ]] && ((choice >= 1 && choice <= ${#options[@]})); then
      printf '%s' "${options[$((choice - 1))]}"
      return 0
    fi
  done
}

configure_lan() {
  local lan_mode="${LAN_MODE:-}"
  local lan_if="${LAN_IF:-}"
  local lan_ip="${LAN_IP:-}"
  local lan_gw="${LAN_GW:-}"
  local lan_dns="${LAN_DNS:-}"

  if [ "$NONINTERACTIVE" -ne 1 ]; then
    if [ -z "$lan_if" ]; then
      mapfile -t ifaces < <(list_ifaces ethernet)
      if [ "${#ifaces[@]}" -gt 0 ]; then
        lan_if="$(choose_iface 'Select LAN interface:' "${ifaces[@]}" || true)"
      fi
    fi

    if [ -n "$lan_if" ] && [ -z "$lan_mode" ]; then
      read -r -p "LAN mode [dhcp/static] (default: dhcp): " lan_mode
    fi

    if [ -n "$lan_if" ] && [ "${lan_mode:-dhcp}" = "static" ]; then
      if [ -z "$lan_ip" ]; then
        read -r -p "LAN IP CIDR (e.g. 192.168.1.10/24): " lan_ip
      fi
      if [ -z "$lan_gw" ]; then
        read -r -p "LAN gateway (e.g. 192.168.1.1): " lan_gw
      fi
      if [ -z "$lan_dns" ]; then
        read -r -p "LAN DNS (comma-separated, blank for none): " lan_dns
      fi
    fi
  fi

  if [ -z "$lan_if" ]; then
    LAN_IF=""
    return 0
  fi

  lan_mode="${lan_mode:-dhcp}"
  LAN_IF="$lan_if"
  LAN_MODE="$lan_mode"

  if nmcli -t -f NAME con show | grep -qx ovr-lan; then
    nmcli con delete ovr-lan >/dev/null 2>&1 || true
  fi

  if [ "$lan_mode" = "dhcp" ]; then
    nmcli con add type ethernet ifname "$lan_if" con-name ovr-lan \
      ipv4.method auto ipv6.method ignore
  else
    if [ -z "$lan_ip" ] || [ -z "$lan_gw" ]; then
      say "Skipping LAN config: static settings are incomplete."
      return 0
    fi
    nmcli con add type ethernet ifname "$lan_if" con-name ovr-lan \
      ipv4.addresses "$lan_ip" ipv4.gateway "$lan_gw" ipv4.method manual ipv6.method ignore
    if [ -n "$lan_dns" ]; then
      nmcli con mod ovr-lan ipv4.dns "$lan_dns"
    fi
  fi

  nmcli con up ovr-lan || true
  LAST_NET_CONN="ovr-lan"
}

configure_wifi() {
  local wifi_ssid="${WIFI_SSID:-}"
  local wifi_pass="${WIFI_PASS:-}"
  local wifi_pass_file="${WIFI_PASS_FILE:-}"
  local wifi_if="${WIFI_IF:-}"

  mapfile -t ifaces < <(list_ifaces wifi)
  if [ "${#ifaces[@]}" -eq 0 ]; then
    return 0
  fi

  if [ -z "$wifi_if" ]; then
    if [ "${#ifaces[@]}" -eq 1 ]; then
      wifi_if="${ifaces[0]}"
    elif [ "$NONINTERACTIVE" -ne 1 ]; then
      wifi_if="$(choose_iface 'Select WiFi interface:' "${ifaces[@]}" || true)"
    fi
  fi

  if [ "$NONINTERACTIVE" -ne 1 ] && [ -z "$wifi_ssid" ]; then
    read -r -p "WiFi SSID (blank to skip): " wifi_ssid
  fi

  if [ -z "$wifi_ssid" ]; then
    return 0
  fi

  if [ -z "$wifi_pass" ] && [ -n "$wifi_pass_file" ] && [ -f "$wifi_pass_file" ]; then
    wifi_pass="$(<"$wifi_pass_file")"
    wifi_pass="$(trim_line "$wifi_pass")"
  fi

  if [ "$NONINTERACTIVE" -ne 1 ] && [ -z "$wifi_pass" ]; then
    read -r -s -p "WiFi password (blank for open): " wifi_pass
    echo ""
  fi

  if nmcli -t -f NAME con show | grep -qx ovr-wifi; then
    nmcli con delete ovr-wifi >/dev/null 2>&1 || true
  fi

  if [ -n "$wifi_pass" ]; then
    wifi_cmd=(nmcli dev wifi connect "$wifi_ssid" password "$wifi_pass" name ovr-wifi)
  else
    wifi_cmd=(nmcli dev wifi connect "$wifi_ssid" name ovr-wifi)
  fi
  if [ -n "$wifi_if" ]; then
    wifi_cmd+=(ifname "$wifi_if")
  fi
  "${wifi_cmd[@]}"
  LAST_NET_CONN="ovr-wifi"
}

write_firewall() {
  local lan_if="$1"
  local ports_csv="$2"
  local ports_list
  local ports_nft
  local p

  ports_csv="${ports_csv// /}"
  ports_list="$(printf '%s' "$ports_csv" | tr ',' ' ')"
  for p in $ports_list; do
    if ! [[ "$p" =~ ^[0-9]+$ ]]; then
      say "Invalid port: $p"
      return 1
    fi
  done

  ports_nft="$(printf '%s, ' $ports_list)"
  ports_nft="${ports_nft%, }"

  cat > /etc/nftables.conf <<EOF
#!/usr/sbin/nft -f
flush ruleset
table inet filter {
  chain input {
    type filter hook input priority 0;
    policy drop;

    iif "lo" accept
    ct state established,related accept
    ip protocol icmp accept
    ip6 nexthdr ipv6-icmp accept

    iifname "tailscale0" accept
    iifname "docker0" accept
    iifname "br-*" accept

    iifname "${lan_if}" tcp dport { ${ports_nft} } accept
    iifname "${lan_if}" udp dport { 67, 68 } accept
  }

  chain forward {
    type filter hook forward priority 0;
    policy accept;
  }

  chain output {
    type filter hook output priority 0;
    policy accept;
  }
}
EOF

  systemctl enable nftables >/dev/null 2>&1 || true
  systemctl restart nftables || true
}

configure_firewall() {
  local ports_csv="${ALLOWED_TCP_PORTS:-22,3000,8428,8429,9100,8080,8088}"

  if [ -z "${LAN_IF:-}" ]; then
    say "Skipping firewall: LAN interface not set."
    return 0
  fi

  if [ "$NONINTERACTIVE" -ne 1 ]; then
    read -r -p "TCP ports to allow on LAN [${ports_csv}]: " input_ports
    if [ -n "${input_ports:-}" ]; then
      ports_csv="$input_ports"
    fi
  fi

  write_firewall "$LAN_IF" "$ports_csv"
}

trim_line() {
  local s="$1"
  s="${s%$'\r'}"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

read_bootstrap_args_file() {
  local file="$1"
  local -n out="$2"
  local line
  local pending=""
  local prompt
  local value

  while IFS= read -r line || [ -n "$line" ]; do
    line="$(trim_line "$line")"
    if [ -z "$line" ] || [[ "$line" == \#* ]]; then
      continue
    fi
    if [[ "$line" == \?* ]]; then
      if [ -z "$pending" ]; then
        continue
      fi
      if [ "$NONINTERACTIVE" -eq 1 ]; then
        pending=""
        continue
      fi
      prompt="${line#\?}"
      prompt="${prompt# }"
      read -r -p "${prompt}: " value
      if [ -n "$value" ]; then
        out+=("$pending" "$value")
      fi
      pending=""
      continue
    fi
    if [[ "$line" == --* ]]; then
      if [[ "$line" == *=* ]]; then
        if [ -n "$pending" ]; then
          out+=("$pending")
          pending=""
        fi
        out+=("$line")
      else
        if [ -n "$pending" ]; then
          out+=("$pending")
        fi
        pending="$line"
      fi
      continue
    fi
    if [ -n "$pending" ]; then
      out+=("$pending" "$line")
      pending=""
    else
      out+=("$line")
    fi
  done < "$file"

  if [ -n "$pending" ]; then
    out+=("$pending")
  fi
}

run_bootstrap() {
  local -a args=()

  if [ -f "$BOOTSTRAP_ARGS_FILE" ]; then
    read_bootstrap_args_file "$BOOTSTRAP_ARGS_FILE" args
  else
    if [ -z "${DEPLOYMENT_ID:-}" ] && [ "$NONINTERACTIVE" -ne 1 ]; then
      read -r -p "Deployment ID: " DEPLOYMENT_ID
    fi
    if [ -z "${NODE_ID:-}" ] && [ "$NONINTERACTIVE" -ne 1 ]; then
      read -r -p "Node ID: " NODE_ID
    fi
    if [ -n "${DEPLOYMENT_ID:-}" ]; then
      args+=(--deployment-id "$DEPLOYMENT_ID")
    fi
    if [ -n "${NODE_ID:-}" ]; then
      args+=(--node-id "$NODE_ID")
    fi
    if [ -n "${HOSTNAME_SET:-}" ]; then
      args+=(--hostname "$HOSTNAME_SET")
    fi
    if [ -n "${REMOTE_WRITE_URL:-}" ]; then
      args+=(--remote-write-url "$REMOTE_WRITE_URL")
    fi
    if [ -n "${REMOTE_WRITE_USER:-}" ]; then
      args+=(--remote-write-user "$REMOTE_WRITE_USER")
    fi
    if [ -n "${REMOTE_WRITE_PASSWORD_FILE:-}" ]; then
      args+=(--remote-write-password-file "$REMOTE_WRITE_PASSWORD_FILE")
    fi
    if [ -n "${VM_WRITE_URL:-}" ]; then
      args+=(--vm-write-url "$VM_WRITE_URL")
    fi
    if [ -n "${VM_QUERY_URL:-}" ]; then
      args+=(--vm-query-url "$VM_QUERY_URL")
    fi
    if [ -n "${VM_WRITE_URL_SECONDARY:-}" ]; then
      args+=(--vm-write-url-secondary "$VM_WRITE_URL_SECONDARY")
    fi
    if [ -n "${VM_WRITE_USERNAME:-}" ]; then
      args+=(--vm-write-username "$VM_WRITE_USERNAME")
    fi
    if [ -n "${VM_WRITE_PASSWORD:-}" ]; then
      args+=(--vm-write-password "$VM_WRITE_PASSWORD")
    fi
    if [ -n "${VM_WRITE_PASSWORD_FILE:-}" ]; then
      args+=(--vm-write-password-file "$VM_WRITE_PASSWORD_FILE")
    fi
    if [ -n "${EVENT_API_KEY:-}" ]; then
      args+=(--event-api-key "$EVENT_API_KEY")
    fi
    if [ -n "${EVENT_API_KEY_FILE:-}" ]; then
      args+=(--event-api-key-file "$EVENT_API_KEY_FILE")
    fi
  fi

  if [ ! -x "$BOOTSTRAP_SCRIPT" ]; then
    say "Bootstrap script not found: $BOOTSTRAP_SCRIPT"
    return 0
  fi

  if [ "${#args[@]}" -eq 0 ]; then
    say "Bootstrap skipped (missing args)."
    return 0
  fi

  /usr/bin/bash "$BOOTSTRAP_SCRIPT" "${args[@]}"
}

main() {
  resize_tty
  preflight_network

  if [ "${RUN_NM_BOOTSTRAP:-1}" = "1" ]; then
    if [ -x "$NM_BOOTSTRAP_SCRIPT" ]; then
      /usr/bin/bash "$NM_BOOTSTRAP_SCRIPT"
    else
      say "NetworkManager bootstrap script not found: $NM_BOOTSTRAP_SCRIPT"
    fi
  fi

  if [ "${RUN_WIFI_SETUP:-1}" = "1" ]; then
    ensure_nonfree_firmware_sources
    if ! install_packages_from_list "$WIFI_PACKAGE_LIST_FILE" "wifi"; then
      say "WiFi package install incomplete. Fix network and rerun /usr/local/sbin/ovr-firstboot."
      exit 1
    fi

    if have_nmcli; then
      configure_nm_unmanaged_lan
      systemctl enable --now NetworkManager >/dev/null 2>&1 || true
      configure_wifi
      fix_dns_if_needed
    else
      say "NetworkManager not installed; skipping WiFi configuration."
    fi
  fi

  if [ "${RUN_PACKAGE_INSTALL:-1}" = "1" ]; then
    if ! install_packages_from_list "$PACKAGE_LIST_FILE" "base"; then
      say "Base package install incomplete. Fix network and rerun /usr/local/sbin/ovr-firstboot."
      exit 1
    fi
  fi

  if [ "${SETUP_FIREWALL:-1}" = "1" ]; then
    configure_firewall
  fi

  if [ "${RUN_BOOTSTRAP:-1}" = "1" ]; then
    if [ "$NONINTERACTIVE" -ne 1 ]; then
      read -r -p "Run bootstrap_n100.sh now? [Y/n] " reply
      case "$reply" in
        n|N|no|NO) RUN_BOOTSTRAP=0 ;;
      esac
    fi
  fi

  if [ "${RUN_BOOTSTRAP:-1}" = "1" ]; then
    if ! check_connectivity; then
      say "No internet connectivity detected. Skipping bootstrap."
      exit 1
    fi
    run_bootstrap
  fi

  mkdir -p /etc/ovr
  touch "$MARKER"
}

main "$@"
