#!/usr/bin/env bash
# Limit disk growth for systemd journals and Docker container logs
set -euo pipefail

echo "== Configuring systemd journal size limits =="

# Create journald config directory if it doesn't exist
sudo mkdir -p /etc/systemd/journald.conf.d

# Create journald size configuration
sudo tee /etc/systemd/journald.conf.d/99-size.conf >/dev/null <<'JOURNAL'
[Journal]
SystemMaxUse=200M
SystemKeepFree=1G
MaxRetentionSec=7day
JOURNAL

echo "✓ Created /etc/systemd/journald.conf.d/99-size.conf"

# Restart systemd-journald to apply configuration
echo "Restarting systemd-journald..."
sudo systemctl restart systemd-journald
echo "✓ systemd-journald restarted"

# Vacuum existing journals to 200M limit
echo "Vacuuming existing journals to 200M..."
sudo journalctl --vacuum-size=200M
echo "✓ Journal vacuum complete"

echo ""
echo "== Configuring Docker container log limits =="

# Create docker config directory if it doesn't exist
sudo mkdir -p /etc/docker

# Check if daemon.json exists and has content
if [ -f /etc/docker/daemon.json ] && [ -s /etc/docker/daemon.json ]; then
  # Backup existing config
  sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.bak
  echo "✓ Backed up existing daemon.json to daemon.json.bak"
  
  # Merge log configuration with existing config using jq
  if command -v jq &> /dev/null; then
    sudo jq '. + {"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}' \
      /etc/docker/daemon.json > /tmp/daemon.json.tmp
    sudo mv /tmp/daemon.json.tmp /etc/docker/daemon.json
    echo "✓ Merged log configuration with existing daemon.json"
  else
    echo "WARNING: jq not installed, cannot merge with existing daemon.json"
    echo "         Manual merge required or install jq and re-run"
  fi
else
  # Create new daemon.json with log configuration
  sudo tee /etc/docker/daemon.json >/dev/null <<'DOCKER'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
DOCKER
  echo "✓ Created /etc/docker/daemon.json"
fi

# Restart Docker to apply configuration (only affects new containers)
echo "Restarting Docker daemon..."
if systemctl is-active --quiet docker; then
  sudo systemctl restart docker
  echo "✓ Docker restarted (note: only new containers use new log limits)"
else
  echo "! Docker is not running, skipping restart"
fi

echo ""
echo "== Status Summary =="
echo ""

# Show journal disk usage
echo "Journal disk usage:"
journalctl --disk-usage
echo ""

# Show Docker logging driver
if systemctl is-active --quiet docker; then
  echo "Docker logging driver:"
  sudo docker info --format '{{.LoggingDriver}}' 2>/dev/null || echo "  (Could not retrieve - Docker may still be starting)"
  echo ""
  
  # Show Docker log options
  echo "Docker log options:"
  sudo docker info 2>/dev/null | grep -A 3 "Default Logging Driver:" || true
else
  echo "Docker is not running"
fi

echo ""
echo "== Hardening Complete =="
echo "Note: Existing containers continue using old log settings."
echo "      Recreate containers (docker-compose up -d --force-recreate) to apply new limits."
