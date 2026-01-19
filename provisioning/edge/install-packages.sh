#!/usr/bin/env bash
set -euo pipefail
umask 027

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIST_FILE_DEFAULT="${SCRIPT_DIR}/firstboot-packages.txt"
WIFI_LIST_FILE_DEFAULT="${SCRIPT_DIR}/firstboot-packages-wifi.txt"
LIST_FILE="${LIST_FILE:-$LIST_FILE_DEFAULT}"
WIFI_LIST_FILE="${WIFI_LIST_FILE:-$WIFI_LIST_FILE_DEFAULT}"
FIRSTBOOT_ENV="/etc/ovr/firstboot.env"
if [ -f "$FIRSTBOOT_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$FIRSTBOOT_ENV"
  set +a
fi
APT_FORCE_IPV4="${APT_FORCE_IPV4:-0}"
PROMPT_ON_ERROR="${PROMPT_ON_ERROR:-1}"
FAILED_PACKAGES=()
INSTALL_TAILSCALE="${INSTALL_TAILSCALE:-1}"
INSTALL_CLOUDFLARED="${INSTALL_CLOUDFLARED:-1}"
INSTALL_DOCKER="${INSTALL_DOCKER:-1}"
DOCKER_GROUP_USER="${DOCKER_GROUP_USER:-${SUDO_USER:-}}"
WIFI_SSID="${WIFI_SSID:-}"
WIFI_PASS="${WIFI_PASS:-}"

usage() {
  cat <<'EOF'
Usage: sudo provisioning/edge/install-packages.sh [options]

Options:
  --list <file>         Override base package list
  --wifi-list <file>    Override WiFi package list
  -h, --help            Show this help

Environment:
  INSTALL_DOCKER=1|0    Install Docker Engine + Compose plugin from Docker repo
  DOCKER_GROUP_USER     Non-root user to add to docker group (default: sudo user)
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

collect_packages() {
  local file="$1"
  local -n out="$2"
  local line
  out=()
  if [ ! -f "$file" ]; then
    return 1
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    line="$(trim_line "$line")"
    if [ -z "$line" ] || [[ "$line" == \#* ]]; then
      continue
    fi
    out+=("$line")
  done < "$file"
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
    echo "Enabled non-free-firmware in APT sources."
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

prompt_ack() {
  if [ "$PROMPT_ON_ERROR" = "1" ]; then
    read -r -p "Press Enter to continue..." _reply
  fi
}

install_package_list() {
  local label="$1"
  local -n list="$2"
  local pkg

  if [ "${#list[@]}" -eq 0 ]; then
    return 0
  fi

  echo "Installing ${label} packages..."
  if apt_install "${list[@]}"; then
    return 0
  fi

  echo "Bulk install failed for ${label} packages. Retrying individually..."
  for pkg in "${list[@]}"; do
    if apt_install "$pkg"; then
      continue
    fi
    echo "ERROR: failed to install package: ${pkg}"
    FAILED_PACKAGES+=("$pkg")
    prompt_ack
  done
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --list) LIST_FILE="$2"; shift 2 ;;
      --wifi-list) WIFI_LIST_FILE="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "ERROR: Unknown argument: $1" >&2; usage; exit 1 ;;
    esac
  done
}

detect_os_id() {
  if [ ! -f /etc/os-release ]; then
    return 1
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  printf '%s' "${ID:-}"
}

detect_os_codename() {
  if [ ! -f /etc/os-release ]; then
    return 1
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  if [ -n "${VERSION_CODENAME:-}" ]; then
    printf '%s' "${VERSION_CODENAME}"
    return 0
  fi
  if [ -n "${UBUNTU_CODENAME:-}" ]; then
    printf '%s' "${UBUNTU_CODENAME}"
    return 0
  fi
  if command -v lsb_release >/dev/null 2>&1; then
    lsb_release -cs
    return 0
  fi
  return 1
}

ensure_docker_repo() {
  local os_id
  local codename
  os_id="$(detect_os_id)"
  codename="$(detect_os_codename)"
  if [ -z "$os_id" ] || [ -z "$codename" ]; then
    echo "ERROR: unable to detect OS ID/codename for Docker repo." >&2
    return 1
  fi
  case "$os_id" in
    debian|ubuntu) ;;
    *)
      echo "ERROR: unsupported OS for Docker repo: ${os_id}" >&2
      return 1
      ;;
  esac

  install -m 0755 -d /etc/apt/keyrings
  if ! curl -fsSL "https://download.docker.com/linux/${os_id}/gpg" \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg; then
    echo "ERROR: failed to import Docker GPG key." >&2
    return 1
  fi
  chmod a+r /etc/apt/keyrings/docker.gpg

  cat > /etc/apt/sources.list.d/docker.list <<EOF
deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${os_id} ${codename} stable
EOF
}

ensure_docker_group_user() {
  local user="${DOCKER_GROUP_USER}"
  if [ -z "$user" ] || [ "$user" = "root" ]; then
    echo "Skipping docker group add (no non-root user detected)."
    return 0
  fi
  if ! id "$user" >/dev/null 2>&1; then
    echo "Warning: user '${user}' not found; set DOCKER_GROUP_USER to override."
    return 0
  fi
  if ! getent group docker >/dev/null 2>&1; then
    groupadd docker >/dev/null 2>&1 || true
  fi
  usermod -aG docker "$user" >/dev/null 2>&1 || true
  echo "Added ${user} to docker group (log out/in to apply)."
}

install_docker_engine() {
  if [ "$INSTALL_DOCKER" != "1" ]; then
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    ensure_docker_group_user
    return 0
  fi

  echo "Installing Docker Engine (official repo)..."
  if ! apt_install ca-certificates curl gnupg; then
    echo "ERROR: failed to install Docker prerequisites."
    FAILED_PACKAGES+=("docker-prereqs")
    prompt_ack
    return 1
  fi

  apt-get remove -y docker.io docker-doc docker-compose podman-docker containerd runc >/dev/null 2>&1 || true

  if ! ensure_docker_repo; then
    FAILED_PACKAGES+=("docker-repo")
    prompt_ack
    return 1
  fi

  if ! apt_update; then
    echo "ERROR: failed to update APT after adding Docker repo."
    FAILED_PACKAGES+=("docker-apt-update")
    prompt_ack
    return 1
  fi
  if ! apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; then
    echo "ERROR: failed to install Docker Engine."
    FAILED_PACKAGES+=("docker")
    prompt_ack
    return 1
  fi

  systemctl enable --now docker >/dev/null 2>&1 || true
  ensure_docker_group_user
}

install_tailscale() {
  if [ "$INSTALL_TAILSCALE" != "1" ]; then
    return 0
  fi
  if command -v tailscale >/dev/null 2>&1; then
    return 0
  fi
  echo "Installing Tailscale..."
  if ! curl -fsSL https://tailscale.com/install.sh | sh; then
    echo "ERROR: failed to install tailscale."
    FAILED_PACKAGES+=("tailscale")
    prompt_ack
    return 1
  fi
}

install_cloudflared() {
  if [ "$INSTALL_CLOUDFLARED" != "1" ]; then
    return 0
  fi
  if command -v cloudflared >/dev/null 2>&1; then
    return 0
  fi
  echo "Installing snapd for cloudflared..."
  if ! apt_install snapd; then
    echo "ERROR: failed to install snapd."
    FAILED_PACKAGES+=("snapd")
    prompt_ack
    return 1
  fi
  systemctl enable --now snapd.socket >/dev/null 2>&1 || true
  for _i in 1 2 3 4 5 6 7 8 9 10; do
    if snap version >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  echo "Installing charmed-cloudflared snap..."
  if ! snap install charmed-cloudflared; then
    echo "ERROR: failed to install charmed-cloudflared."
    FAILED_PACKAGES+=("charmed-cloudflared")
    prompt_ack
    return 1
  fi
}

setup_wifi_post_install() {
  local script="${SCRIPT_DIR}/setup-wifi.sh"
  if [ ! -x "$script" ]; then
    echo "ERROR: WiFi setup script not found at ${script}"
    FAILED_PACKAGES+=("wifi-setup")
    prompt_ack
    return 1
  fi
  echo "Configuring WiFi..."
  if [ -z "$WIFI_SSID" ]; then
    echo "WiFi SSID not set (WIFI_SSID). Skipping WiFi setup." >&2
    return 0
  fi
  if ! "$script" --ssid "$WIFI_SSID" --pass "$WIFI_PASS"; then
    echo "ERROR: WiFi setup failed."
    FAILED_PACKAGES+=("wifi-setup")
    prompt_ack
    return 1
  fi
}

main() {
  local -a packages=()
  local -a wifi_packages=()

  require_root
  parse_args "$@"
  if [ ! -t 0 ]; then
    PROMPT_ON_ERROR=0
  fi

  if ! collect_packages "$LIST_FILE" packages; then
    echo "ERROR: package list not found: $LIST_FILE" >&2
    exit 1
  fi

  if [ "${#packages[@]}" -eq 0 ]; then
    echo "No base packages configured in $LIST_FILE."
  fi

  if ! collect_packages "$WIFI_LIST_FILE" wifi_packages; then
    echo "ERROR: WiFi package list not found: $WIFI_LIST_FILE" >&2
    exit 1
  fi
  ensure_nonfree_firmware_sources

  echo "Updating APT..."
  apt_update

  install_package_list "base" packages

  install_package_list "WiFi" wifi_packages

  install_docker_engine || true
  install_tailscale || true
  install_cloudflared || true
  setup_wifi_post_install || true

  if [ "${#FAILED_PACKAGES[@]}" -gt 0 ]; then
    echo "Install completed with errors. Failed packages:"
    printf '  - %s\n' "${FAILED_PACKAGES[@]}"
    exit 1
  fi
}

main "$@"
