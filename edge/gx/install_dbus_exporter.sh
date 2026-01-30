#!/bin/sh
set -eu

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
PAYLOAD_DIR="${SRC_DIR}"
if [ ! -f "${PAYLOAD_DIR}/dbus2prom.py" ] && [ -f "${SRC_DIR}/../dbus2prom.py" ]; then
  PAYLOAD_DIR="$(cd "${SRC_DIR}/.." && pwd)"
fi
INSTALL_DIR="/data/ovr/dbus2prom"
SERVICE_DIR="/service/ovr-dbus2prom"

need_file() {
  if [ ! -f "$1" ]; then
    echo "ERROR: Missing required file: $1" >&2
    exit 1
  fi
}

need_file "${PAYLOAD_DIR}/dbus2prom.py"
need_file "${PAYLOAD_DIR}/map_fast.tsv"
need_file "${PAYLOAD_DIR}/map_slow.tsv"
need_file "${PAYLOAD_DIR}/run_exporters.sh"

mkdir -p "${INSTALL_DIR}"
cp "${PAYLOAD_DIR}/dbus2prom.py" "${INSTALL_DIR}/"
cp "${PAYLOAD_DIR}/map_fast.tsv" "${INSTALL_DIR}/"
cp "${PAYLOAD_DIR}/map_slow.tsv" "${INSTALL_DIR}/"
cp "${PAYLOAD_DIR}/run_exporters.sh" "${INSTALL_DIR}/"
chmod 0755 "${INSTALL_DIR}/run_exporters.sh"

if [ ! -e "/data/dbus2prom" ]; then
  ln -s "${INSTALL_DIR}" "/data/dbus2prom"
fi

mkdir -p "${SERVICE_DIR}"
cp -r "${SRC_DIR}/service/ovr-dbus2prom/"* "${SERVICE_DIR}/"
chmod 0755 "${SERVICE_DIR}/run" "${SERVICE_DIR}/log/run"

echo "Installed dbus2prom to ${INSTALL_DIR}"
echo "Service installed at ${SERVICE_DIR} (runit)"
