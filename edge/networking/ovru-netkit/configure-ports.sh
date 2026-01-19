#!/usr/bin/env bash
set -euo pipefail
umask 027

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VARS_FILE="${VARS_FILE:-$SCRIPT_DIR/vars.env}"

if [ -f "$VARS_FILE" ]; then
  # shellcheck disable=SC1090
  . "$VARS_FILE"
else
  echo "ERROR: vars file not found: $VARS_FILE" >&2
  exit 1
fi

GX_ENABLE="${GX_ENABLE:-1}"
WAN_ENABLE="${WAN_ENABLE:-1}"
MODBUS_ENABLE="${MODBUS_ENABLE:-0}"
DISABLE_NETWORKD="${DISABLE_NETWORKD:-0}"
PROMPT_ENABLE="${PROMPT_ENABLE:-1}"
PROMPT_GUIDANCE="${PROMPT_GUIDANCE:-1}"
IFACE_LABELS="${IFACE_LABELS:-}"
IFACE_LABELS_FILE="${IFACE_LABELS_FILE:-}"

GX_IP_CIDR_SET=0
if [ -n "${GX_IP_CIDR+x}" ] && [ -n "${GX_IP_CIDR:-}" ]; then
  GX_IP_CIDR_SET=1
fi

GX_CLIENT_IP_SET=0
if [ -n "${GX_CLIENT_IP+x}" ] && [ -n "${GX_CLIENT_IP:-}" ]; then
  GX_CLIENT_IP_SET=1
fi

GX_IF="${GX_IF:-${LAN_IF:-}}"
GX_CON="${GX_CON:-oneclient}"
GX_IP_CIDR="${GX_IP_CIDR:-${LAN_IP_CIDR:-}}"
GX_CLIENT_IP="${GX_CLIENT_IP:-${LAN_CLIENT_IP:-}}"
GX_DNS="${GX_DNS:-${LAN_DNS:-1.1.1.1,8.8.8.8}}"
GX_DHCP_LEASE="${GX_DHCP_LEASE:-${LAN_DHCP_LEASE:-5m}}"
GX_DHCP_NETMASK="${GX_DHCP_NETMASK:-${LAN_DHCP_NETMASK:-auto}}"
GX_ENABLE_DNSMASQ="${GX_ENABLE_DNSMASQ:-1}"
GX_ENABLE_NAT="${GX_ENABLE_NAT:-1}"
GX_ENABLE_FORWARDING="${GX_ENABLE_FORWARDING:-1}"

WAN_IF="${WAN_IF:-${WAN_ETH_IF:-}}"
WAN_CON="${WAN_CON:-}"
WAN_MODE="${WAN_MODE:-dhcp}"
WAN_IP_CIDR="${WAN_IP_CIDR:-}"
WAN_GW="${WAN_GW:-}"
WAN_DNS="${WAN_DNS:-}"
WAN_METRIC="${WAN_METRIC:-100}"
WAN_PRIORITY="${WAN_PRIORITY:-10}"

WIFI_CON="${WIFI_CON:-wan-wifi}"
WIFI_METRIC="${WIFI_METRIC:-600}"
WIFI_PRIORITY="${WIFI_PRIORITY:--10}"

MODBUS_IF="${MODBUS_IF:-}"
MODBUS_CON="${MODBUS_CON:-}"
MODBUS_MODE="${MODBUS_MODE:-static}"
MODBUS_IP_CIDR="${MODBUS_IP_CIDR:-}"
MODBUS_GW="${MODBUS_GW:-}"
MODBUS_DNS="${MODBUS_DNS:-}"
MODBUS_NEVER_DEFAULT="${MODBUS_NEVER_DEFAULT:-1}"
MODBUS_IGNORE_DNS="${MODBUS_IGNORE_DNS:-1}"
MODBUS_METRIC="${MODBUS_METRIC:-300}"

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root (sudo)." >&2
    exit 1
  fi
}

ensure_hosts_entry() {
  local full short
  full="$(hostname 2>/dev/null | tr -d '\r\n' || true)"
  if [ -z "$full" ] || [ "$full" = "localhost" ]; then
    return 0
  fi
  short="${full%%.*}"

  if ! awk -v full="$full" -v short="$short" '
      $1 !~ /^#/ {
        for (i = 2; i <= NF; i++) {
          if ($i == full || $i == short) {
            found = 1
          }
        }
      }
      END { exit found ? 0 : 1 }
    ' /etc/hosts; then
    local line="127.0.1.1 ${full}"
    if [ "$short" != "$full" ]; then
      line="${line} ${short}"
    fi
    echo "Adding hostname to /etc/hosts: ${line}"
    echo "$line" >> /etc/hosts
  fi
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $1" >&2
    exit 1
  }
}

need_var() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "ERROR: required variable not set: $name" >&2
    exit 1
  fi
}

detect_prompt_mode() {
  if [ ! -t 0 ]; then
    PROMPT_ENABLE=0
  fi
}

cidr_to_netmask() {
  local prefix="$1"
  if ! [[ "$prefix" =~ ^[0-9]+$ ]] || [ "$prefix" -lt 0 ] || [ "$prefix" -gt 32 ]; then
    return 1
  fi
  local bits="$prefix"
  local o1 o2 o3 o4
  for o in 1 2 3 4; do
    if [ "$bits" -ge 8 ]; then
      eval "o${o}=255"
      bits=$((bits - 8))
    elif [ "$bits" -gt 0 ]; then
      eval "o${o}=$((256 - (1 << (8 - bits))))"
      bits=0
    else
      eval "o${o}=0"
    fi
  done
  printf '%s.%s.%s.%s' "$o1" "$o2" "$o3" "$o4"
}

ensure_gx_netmask() {
  if [ -n "$GX_DHCP_NETMASK" ] && [ "$GX_DHCP_NETMASK" != "auto" ]; then
    return 0
  fi

  local cidr="${GX_IP_CIDR:-${LAN_IP_CIDR:-}}"
  if [ -z "$cidr" ] || ! [[ "$cidr" == */* ]]; then
    return 0
  fi
  local prefix="${cidr##*/}"
  local netmask
  netmask="$(cidr_to_netmask "$prefix" 2>/dev/null || true)"
  if [ -n "$netmask" ]; then
    GX_DHCP_NETMASK="$netmask"
  fi
}

show_prompt_guidance() {
  if [ "$PROMPT_GUIDANCE" != "1" ] || [ "${PROMPT_GUIDANCE_SHOWN:-0}" = "1" ]; then
    return 0
  fi
  echo "Tip: enter the number or interface name; Enter accepts the default."
  echo "LAN = GX/LAN device, WAN = uplink, Modbus = isolated device net."
  echo "Wi-Fi fallback uses an existing NM profile (WIFI_CON); not set here."
  PROMPT_GUIDANCE_SHOWN=1
}

list_ifaces() {
  if command -v nmcli >/dev/null 2>&1; then
    nmcli -t -f DEVICE,TYPE dev status | awk -F: '$1 != "lo" {print $1 ":" $2}'
    return 0
  fi
  if command -v ip >/dev/null 2>&1; then
    ip -br link | awk '$1 != "lo" {print $1}'
    return 0
  fi
  if [ -d /sys/class/net ]; then
    ls /sys/class/net | awk '$1 != "lo" {print $1}'
  fi
}

list_ifaces_by_type() {
  local type="$1"
  if command -v nmcli >/dev/null 2>&1; then
    nmcli -t -f DEVICE,TYPE dev status | awk -F: -v t="$type" '$2==t {print $1}'
    return 0
  fi
  if [ "$type" = "ethernet" ]; then
    list_ifaces | awk -F: '{print $1}'
  fi
}

lookup_iface_label() {
  local dev="$1"
  local label=""

  if [ -n "$IFACE_LABELS" ]; then
    label="$(printf '%s\n' "$IFACE_LABELS" | tr ',' '\n' | awk -F= -v d="$dev" '$1==d {print $2; exit}')"
  fi
  if [ -z "$label" ] && [ -n "$IFACE_LABELS_FILE" ] && [ -f "$IFACE_LABELS_FILE" ]; then
    label="$(awk -F'[= ]+' -v d="$dev" '$1==d {print $2; exit}' "$IFACE_LABELS_FILE")"
  fi

  printf '%s' "$label"
}

get_iface_hint() {
  local dev="$1"
  local hint=""
  local label driver path mac props

  label="$(lookup_iface_label "$dev")"
  if [ -n "$label" ]; then
    hint="label=${label}"
  fi

  if command -v udevadm >/dev/null 2>&1; then
    props="$(udevadm info -q property -n "$dev" 2>/dev/null || true)"
    driver="$(printf '%s\n' "$props" | awk -F= '$1=="ID_NET_DRIVER"{print $2; exit}')"
    path="$(printf '%s\n' "$props" | awk -F= '$1=="ID_NET_NAME_ONBOARD"{print $2; exit}')"
    if [ -z "$path" ]; then
      path="$(printf '%s\n' "$props" | awk -F= '$1=="ID_NET_NAME_SLOT"{print $2; exit}')"
    fi
    if [ -z "$path" ]; then
      path="$(printf '%s\n' "$props" | awk -F= '$1=="ID_NET_NAME_PATH"{print $2; exit}')"
    fi
  fi

  if [ -n "$driver" ]; then
    hint="${hint}${hint:+, }driver=${driver}"
  fi
  if [ -n "$path" ]; then
    hint="${hint}${hint:+, }path=${path}"
  fi

  if [ -r "/sys/class/net/${dev}/address" ]; then
    mac="$(cat "/sys/class/net/${dev}/address" 2>/dev/null || true)"
    if [ -n "$mac" ]; then
      mac="${mac//:/-}"
      hint="${hint}${hint:+, }mac=${mac}"
    fi
  fi

  printf '%s' "$hint"
}

list_ifaces_detail() {
  if command -v nmcli >/dev/null 2>&1; then
    nmcli -t -f DEVICE,TYPE,STATE,CONNECTION dev status | awk -F: '$1 != "lo" {print}'
    return 0
  fi
  if command -v ip >/dev/null 2>&1; then
    ip -br link | awk '$1 != "lo" {print $1 ":unknown:" $2 ":"}'
    return 0
  fi
  if [ -d /sys/class/net ]; then
    ls /sys/class/net | awk '$1 != "lo" {print $1 ":unknown:unknown:"}'
  fi
}

show_ifaces() {
  local line
  if command -v nmcli >/dev/null 2>&1; then
    local summary=""
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      summary="${summary}${summary:+ }${line//:/(}"
      summary="${summary})"
    done < <(list_ifaces)
    if [ -n "$summary" ]; then
      echo "Detected interfaces: ${summary}"
    fi
    return 0
  fi
  if command -v ip >/dev/null 2>&1; then
    echo "Detected interfaces:"
    ip -br link | awk '$1 != "lo" {print "  " $1}'
  fi
}

build_iface_candidates() {
  local type="$1"
  shift
  local -a avoid=("$@")
  local line dev itype state conn hint

  while IFS=: read -r dev itype state conn; do
    [ -z "$dev" ] && continue
    if [ -n "$type" ] && [ "$type" != "any" ] && [ "$itype" != "$type" ]; then
      continue
    fi
    local skip=0
    for avoid_iface in "${avoid[@]}"; do
      if [ -n "$avoid_iface" ] && [ "$dev" = "$avoid_iface" ]; then
        skip=1
        break
      fi
    done
    if [ "$skip" -eq 0 ]; then
      hint="$(get_iface_hint "$dev")"
      if [ -n "$hint" ]; then
        hint="${hint//:/-}"
      fi
      echo "${dev}:${itype}:${state}:${conn}:${hint}"
    fi
  done < <(list_ifaces_detail)
}

suggest_iface() {
  local type="$1"
  shift
  local -a avoid=("$@")
  local -a ifaces=()
  local iface

  if [ "$type" = "any" ]; then
    mapfile -t ifaces < <(list_ifaces | awk -F: '{print $1}')
  else
    mapfile -t ifaces < <(list_ifaces_by_type "$type")
  fi

  for iface in "${ifaces[@]}"; do
    [ -z "$iface" ] && continue
    local skip=0
    for avoid_iface in "${avoid[@]}"; do
      if [ -n "$avoid_iface" ] && [ "$iface" = "$avoid_iface" ]; then
        skip=1
        break
      fi
    done
    if [ "$skip" -eq 0 ]; then
      echo "$iface"
      return 0
    fi
  done
}

prompt_iface() {
  local var="$1"
  local label="$2"
  local type="$3"
  shift 3
  local -a avoid=("$@")
  local current="${!var}"
  local default="$current"

  if [ -z "$default" ]; then
    default="$(suggest_iface "$type" "${avoid[@]}")"
  fi
  if [ -z "$default" ] && [ "$type" != "any" ]; then
    default="$(suggest_iface any "${avoid[@]}")"
  fi

  if [ "$PROMPT_ENABLE" != "1" ]; then
    if [ -z "$current" ] && [ -n "$default" ]; then
      printf -v "$var" '%s' "$default"
    fi
    return 0
  fi

  local -a candidates=()
  mapfile -t candidates < <(build_iface_candidates "$type" "${avoid[@]}")
  if [ "${#candidates[@]}" -eq 0 ] && [ "$type" != "any" ]; then
    mapfile -t candidates < <(build_iface_candidates any "${avoid[@]}")
  fi
  if [ "${#candidates[@]}" -gt 0 ]; then
    echo "Available interfaces:"
    local idx=1
    local entry dev itype state conn hint desc
    for entry in "${candidates[@]}"; do
      IFS=: read -r dev itype state conn hint <<< "$entry"
      desc="$dev"
      if [ -n "$itype" ] && [ "$itype" != "unknown" ]; then
        desc="${desc} (${itype}"
        if [ -n "$state" ] && [ "$state" != "--" ] && [ "$state" != "unknown" ]; then
          desc="${desc}, ${state}"
        fi
        if [ -n "$conn" ] && [ "$conn" != "--" ]; then
          desc="${desc}, ${conn}"
        fi
        if [ -n "$hint" ]; then
          desc="${desc}, ${hint}"
        fi
        desc="${desc})"
      fi
      printf "  %s) %s\n" "$idx" "$desc"
      idx=$((idx + 1))
    done
  fi

  local prompt_default="(blank)"
  if [ -n "$default" ]; then
    prompt_default="$default"
  fi

  local reply=""
  read -r -p "$label [$prompt_default]: " reply
  if [ -n "$reply" ]; then
    if [[ "$reply" =~ ^[0-9]+$ ]]; then
      local sel=$((reply - 1))
      if [ "$sel" -ge 0 ] && [ "$sel" -lt "${#candidates[@]}" ]; then
        IFS=: read -r dev itype state conn hint <<< "${candidates[$sel]}"
        printf -v "$var" '%s' "$dev"
        return 0
      fi
      echo "Warning: invalid selection '$reply'."
    else
      printf -v "$var" '%s' "$reply"
      return 0
    fi
  fi

  if [ -n "$default" ]; then
    printf -v "$var" '%s' "$default"
  fi
}

prompt_value() {
  local var="$1"
  local label="$2"
  local default="$3"
  local allow_blank="${4:-0}"
  local current="${!var}"
  local reply=""

  if [ -n "$current" ]; then
    default="$current"
  fi

  if [ "$PROMPT_ENABLE" != "1" ]; then
    if [ -z "$current" ] && [ -n "$default" ]; then
      printf -v "$var" '%s' "$default"
    fi
    return 0
  fi

  local prompt_default="(blank)"
  if [ -n "$default" ]; then
    prompt_default="$default"
  fi
  read -r -p "$label [$prompt_default]: " reply
  if [ -n "$reply" ]; then
    printf -v "$var" '%s' "$reply"
  elif [ -n "$default" ]; then
    printf -v "$var" '%s' "$default"
  elif [ "$allow_blank" = "1" ]; then
    :
  fi
}

prompt_bool() {
  local var="$1"
  local label="$2"
  local default="$3"
  local explain="${4:-}"
  local current="${!var}"
  local reply=""

  if [ -n "$current" ]; then
    default="$current"
  fi
  if [ -z "$default" ]; then
    default="0"
  fi

  if [ "$PROMPT_ENABLE" != "1" ]; then
    if [ -z "$current" ] && [ -n "$default" ]; then
      printf -v "$var" '%s' "$default"
    fi
    return 0
  fi

  if [ -n "$explain" ]; then
    echo "$explain"
  fi

  local prompt_default="n"
  if [ "$default" = "1" ]; then
    prompt_default="y"
  fi

  read -r -p "$label [${prompt_default}]: " reply
  if [ -z "$reply" ]; then
    reply="$prompt_default"
  fi

  case "$reply" in
    y|Y|yes|YES|1|true|TRUE)
      printf -v "$var" '%s' "1"
      ;;
    n|N|no|NO|0|false|FALSE)
      printf -v "$var" '%s' "0"
      ;;
    *)
      echo "Warning: invalid choice '$reply'. Using default."
      printf -v "$var" '%s' "$default"
      ;;
  esac
}

sync_gx_from_lan() {
  if [ "$GX_IP_CIDR_SET" = "0" ] && [ -n "${LAN_IP_CIDR:-}" ]; then
    GX_IP_CIDR="$LAN_IP_CIDR"
  fi
  if [ "$GX_CLIENT_IP_SET" = "0" ] && [ -n "${LAN_CLIENT_IP:-}" ]; then
    GX_CLIENT_IP="$LAN_CLIENT_IP"
  fi
}

maybe_prompt_labels() {
  if [ "$PROMPT_ENABLE" != "1" ]; then
    return 0
  fi

  prompt_bool RELABEL_IFACES "Add/update interface labels?" "0" \
    "Labels appear in the interface picker to clarify which port is which."
  if [ "${RELABEL_IFACES:-0}" != "1" ]; then
    return 0
  fi

  local labels_file="${IFACE_LABELS_FILE:-/etc/ovr/iface-labels.conf}"
  local labels_dir
  labels_dir="$(dirname "$labels_file")"
  if [ -n "$labels_dir" ]; then
    mkdir -p "$labels_dir"
  fi

  local -a devs=()
  if command -v nmcli >/dev/null 2>&1; then
    mapfile -t devs < <(nmcli -t -f DEVICE,TYPE dev status | awk -F: '$1 != "lo" && ($2=="ethernet" || $2=="wifi") {print $1}')
  else
    mapfile -t devs < <(list_ifaces | awk -F: '{print $1}')
  fi

  if [ "${#devs[@]}" -eq 0 ]; then
    echo "No interfaces found to label."
    return 0
  fi

  local tmp="/tmp/iface-labels.$$"
  : > "$tmp"

  echo "Enter labels (blank keeps current, '-' clears)."
  local dev current reply label
  for dev in "${devs[@]}"; do
    current="$(lookup_iface_label "$dev")"
    local prompt_default="(blank)"
    if [ -n "$current" ]; then
      prompt_default="$current"
    fi
    read -r -p "Label for ${dev} [$prompt_default]: " reply
    if [ -z "$reply" ]; then
      label="$current"
    elif [ "$reply" = "-" ]; then
      label=""
    else
      label="$reply"
    fi
    if [ -n "$label" ]; then
      printf '%s %s\n' "$dev" "$label" >> "$tmp"
    fi
  done

  mv "$tmp" "$labels_file"
  IFACE_LABELS_FILE="$labels_file"
  echo "Saved interface labels to ${labels_file}"
}

maybe_prompt_lan_settings() {
  if [ "$PROMPT_ENABLE" != "1" ]; then
    return 0
  fi
  if [ "$GX_ENABLE" != "1" ]; then
    return 0
  fi

  echo "CIDR tips: /24 normal, /30 GX point-to-point, /29 = 6 usable."
  prompt_value LAN_IP_CIDR "LAN IP/CIDR (node)" "${LAN_IP_CIDR:-}"
  prompt_value LAN_CLIENT_IP "GX/LAN client IP (DHCP lease)" "${LAN_CLIENT_IP:-}"
  sync_gx_from_lan
}

maybe_prompt_wan_settings() {
  if [ "$PROMPT_ENABLE" != "1" ]; then
    return 0
  fi
  if [ "$WAN_ENABLE" != "1" ]; then
    return 0
  fi

  echo "WAN mode: dhcp uses uplink settings; static uses a fixed IP (e.g. 10.10.4.2/24)."
  prompt_value WAN_MODE "WAN mode (dhcp/static)" "${WAN_MODE:-dhcp}"
  case "$WAN_MODE" in
    dhcp|DHCP)
      WAN_MODE="dhcp"
      ;;
    static|STATIC)
      WAN_MODE="static"
      ;;
    *)
      echo "Warning: invalid WAN mode '$WAN_MODE'; defaulting to dhcp."
      WAN_MODE="dhcp"
      ;;
  esac

  if [ "$WAN_MODE" = "static" ]; then
    prompt_value WAN_IP_CIDR "WAN IP/CIDR (static)" "${WAN_IP_CIDR:-10.10.4.2/24}"
    prompt_value WAN_GW "WAN gateway (router IP)" "${WAN_GW:-10.10.4.1}" 1
    prompt_value WAN_DNS "WAN DNS (comma-separated, optional)" "${WAN_DNS:-10.10.4.1}" 1
  fi
}

maybe_prompt_interfaces() {
  if [ "$PROMPT_ENABLE" != "1" ]; then
    return 0
  fi

  show_ifaces
  show_prompt_guidance
  maybe_prompt_labels

  prompt_iface LAN_IF "LAN interface (GX LAN)" ethernet
  if [ -n "$LAN_IF" ] && [ -z "$GX_IF" ]; then
    GX_IF="$LAN_IF"
  fi

  if [ "$WAN_ENABLE" = "1" ]; then
    prompt_iface WAN_IF "WAN interface (uplink)" ethernet "$LAN_IF"
  fi

  if [ "$MODBUS_ENABLE" = "1" ]; then
    prompt_iface MODBUS_IF "Modbus interface" ethernet "$LAN_IF" "$WAN_IF"
  fi
}

maybe_prompt_gx_features() {
  if [ "$GX_ENABLE" != "1" ]; then
    return 0
  fi

  if [ "$PROMPT_ENABLE" = "1" ]; then
    prompt_bool GX_ENABLE_DNSMASQ "Enable DHCP (dnsmasq) on LAN?" "$GX_ENABLE_DNSMASQ" \
      "DHCP gives the GX/LAN device its IP automatically; disable if you set static IPs."

    prompt_bool GX_ENABLE_NAT "Enable NAT for LAN/GX traffic?" "$GX_ENABLE_NAT" \
      "NAT (masquerade) lets GX/LAN devices reach the uplink through this node."
  fi

  GX_ENABLE_FORWARDING="$GX_ENABLE_NAT"
}

ensure_nm_running() {
  systemctl enable --now NetworkManager >/dev/null 2>&1 || true
}

check_networkd() {
  if systemctl is-active --quiet systemd-networkd 2>/dev/null; then
    if [ "$DISABLE_NETWORKD" = "1" ]; then
      systemctl disable --now systemd-networkd >/dev/null 2>&1 || true
    else
      echo "Warning: systemd-networkd is active and may conflict with NetworkManager."
    fi
  fi
}

nm_delete_if_exists() {
  local name="$1"
  if nmcli -t -f NAME con show | grep -qx "$name"; then
    nmcli con delete "$name" >/dev/null 2>&1 || true
  fi
}

configure_gx() {
  need_var GX_IF
  need_var GX_IP_CIDR
  nm_delete_if_exists "$GX_CON"

  nmcli con add type ethernet ifname "$GX_IF" con-name "$GX_CON" \
    ipv4.addresses "$GX_IP_CIDR" ipv4.method manual ipv6.method ignore
  nmcli con mod "$GX_CON" ipv4.never-default yes ipv6.never-default yes connection.autoconnect yes
  nmcli con up "$GX_CON" >/dev/null 2>&1 || true
}

configure_wan() {
  need_var WAN_IF
  if [ -z "$WAN_CON" ]; then
    WAN_CON="wan-${WAN_IF}"
  fi
  nm_delete_if_exists "$WAN_CON"

  if [ "$WAN_MODE" = "dhcp" ]; then
    nmcli con add type ethernet ifname "$WAN_IF" con-name "$WAN_CON" \
      ipv4.method auto ipv6.method ignore
  elif [ "$WAN_MODE" = "static" ]; then
    need_var WAN_IP_CIDR
    nmcli con add type ethernet ifname "$WAN_IF" con-name "$WAN_CON" \
      ipv4.addresses "$WAN_IP_CIDR" ipv4.method manual ipv6.method ignore
    if [ -n "$WAN_GW" ]; then
      nmcli con mod "$WAN_CON" ipv4.gateway "$WAN_GW"
    fi
    if [ -n "$WAN_DNS" ]; then
      nmcli con mod "$WAN_CON" ipv4.dns "$WAN_DNS"
    fi
  else
    echo "ERROR: invalid WAN_MODE: $WAN_MODE" >&2
    exit 1
  fi

  nmcli con mod "$WAN_CON" connection.autoconnect yes \
    ipv4.route-metric "$WAN_METRIC" connection.autoconnect-priority "$WAN_PRIORITY"
  nmcli con up "$WAN_CON" >/dev/null 2>&1 || true
}

configure_wifi_fallback() {
  if [ -z "$WIFI_CON" ]; then
    return 0
  fi
  if nmcli -t -f NAME con show | grep -qx "$WIFI_CON"; then
    nmcli con mod "$WIFI_CON" connection.autoconnect yes \
      ipv4.route-metric "$WIFI_METRIC" connection.autoconnect-priority "$WIFI_PRIORITY" \
      ipv4.never-default no ipv6.never-default yes
    nmcli con up "$WIFI_CON" >/dev/null 2>&1 || true
  else
    echo "Warning: Wi-Fi profile '$WIFI_CON' not found."
  fi
}

configure_modbus() {
  need_var MODBUS_IF
  if [ -z "$MODBUS_CON" ]; then
    MODBUS_CON="modbus-${MODBUS_IF}"
  fi
  nm_delete_if_exists "$MODBUS_CON"

  if [ "$MODBUS_MODE" = "dhcp" ]; then
    nmcli con add type ethernet ifname "$MODBUS_IF" con-name "$MODBUS_CON" \
      ipv4.method auto ipv6.method ignore
  elif [ "$MODBUS_MODE" = "static" ]; then
    need_var MODBUS_IP_CIDR
    nmcli con add type ethernet ifname "$MODBUS_IF" con-name "$MODBUS_CON" \
      ipv4.addresses "$MODBUS_IP_CIDR" ipv4.method manual ipv6.method ignore
    if [ -n "$MODBUS_GW" ]; then
      nmcli con mod "$MODBUS_CON" ipv4.gateway "$MODBUS_GW"
    fi
    if [ -n "$MODBUS_DNS" ]; then
      nmcli con mod "$MODBUS_CON" ipv4.dns "$MODBUS_DNS"
    fi
  else
    echo "ERROR: invalid MODBUS_MODE: $MODBUS_MODE" >&2
    exit 1
  fi

  nmcli con mod "$MODBUS_CON" connection.autoconnect yes ipv4.route-metric "$MODBUS_METRIC"
  if [ "$MODBUS_NEVER_DEFAULT" = "1" ]; then
    nmcli con mod "$MODBUS_CON" ipv4.never-default yes ipv6.never-default yes
  fi
  if [ "$MODBUS_IGNORE_DNS" = "1" ]; then
    nmcli con mod "$MODBUS_CON" ipv4.ignore-auto-dns yes
  fi

  nmcli con up "$MODBUS_CON" >/dev/null 2>&1 || true
}

configure_dnsmasq() {
  need_var GX_IF
  need_var GX_CLIENT_IP
  need_cmd dnsmasq

  cat > /etc/dnsmasq.d/ovr-gx-dhcp.conf <<EOF
port=0
interface=${GX_IF}
bind-dynamic
dhcp-authoritative
dhcp-range=${GX_CLIENT_IP},${GX_CLIENT_IP},${GX_DHCP_NETMASK},${GX_DHCP_LEASE}
dhcp-option=option:router,${GX_IP_CIDR%/*}
dhcp-option=option:dns-server,${GX_DNS}
EOF

  systemctl enable --now dnsmasq >/dev/null 2>&1 || true
  systemctl restart dnsmasq >/dev/null 2>&1 || true
}

configure_nat() {
  need_var GX_IF
  need_var GX_IP_CIDR
  need_cmd nft

  mkdir -p /etc/nftables.d
  cat > /etc/nftables.d/ovr-gx-nat.nft <<EOF
table ip ovr_gx_nat {
  chain postrouting {
    type nat hook postrouting priority srcnat;
    ip saddr ${GX_IP_CIDR} oifname != "${GX_IF}" masquerade
  }
}
EOF

  grep -q 'include "/etc/nftables.d/\*\.nft"' /etc/nftables.conf || \
    echo 'include "/etc/nftables.d/*.nft"' >> /etc/nftables.conf

  systemctl enable --now nftables >/dev/null 2>&1 || true
  systemctl restart nftables >/dev/null 2>&1 || true
}

enable_forwarding() {
  echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-ipforward.conf
  sysctl --system >/dev/null 2>&1 || true
}

main() {
  require_root
  ensure_hosts_entry
  detect_prompt_mode
  need_cmd nmcli
  need_cmd ip

  check_networkd
  ensure_nm_running
  maybe_prompt_interfaces
  maybe_prompt_lan_settings
  maybe_prompt_wan_settings
  maybe_prompt_gx_features
  ensure_gx_netmask

  if [ "$GX_ENABLE" = "1" ]; then
    configure_gx
    if [ "$GX_ENABLE_DNSMASQ" = "1" ]; then
      configure_dnsmasq
    fi
    if [ "$GX_ENABLE_FORWARDING" = "1" ] || [ "$GX_ENABLE_NAT" = "1" ]; then
      enable_forwarding
    fi
    if [ "$GX_ENABLE_NAT" = "1" ]; then
      configure_nat
    fi
  fi

  if [ "$WAN_ENABLE" = "1" ]; then
    configure_wan
  fi

  configure_wifi_fallback

  if [ "$MODBUS_ENABLE" = "1" ]; then
    configure_modbus
  fi

  echo "== Ports configured =="
  nmcli -t -f DEVICE,STATE,CONNECTION dev status || true
}

main "$@"
