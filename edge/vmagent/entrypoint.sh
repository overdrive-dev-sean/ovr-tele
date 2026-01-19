#!/bin/sh
set -eu

SCRAPE_CONFIG="${VMAGENT_SCRAPE_CONFIG:-/etc/vmagent/scrape.yml}"
LOCAL_WRITE_URL="${VM_LOCAL_WRITE_URL:-http://victoria-metrics:8428/api/v1/write}"
REMOTE_WRITE_URL="${VM_REMOTE_WRITE_URL:-}"
REMOTE_WRITE_USERNAME="${VM_REMOTE_WRITE_USERNAME:-}"
REMOTE_WRITE_PASSWORD_FILE="${VM_REMOTE_WRITE_PASSWORD_FILE:-}"
REMOTE_WRITE_PASSWORD="${VM_REMOTE_WRITE_PASSWORD:-}"
TMP_DATA_PATH="${VM_REMOTE_WRITE_TMPDATA_PATH:-/tmp/vmagent}"
HTTP_LISTEN_ADDR="${VMAGENT_HTTP_LISTEN_ADDR:-:8429}"
STREAM_AGGR_CONFIG="${VM_REMOTE_WRITE_STREAM_AGGR_CONFIG:-/etc/overdrive/stream_aggr.yml}"
LOCAL_RELABEL_CONFIG="${VM_LOCAL_WRITE_RELABEL_CONFIG:-/etc/overdrive/remote_write_local_relabel.yml}"
REMOTE_RELABEL_CONFIG="${VM_REMOTE_WRITE_RELABEL_CONFIG:-/etc/overdrive/remote_write_cloud_relabel.yml}"

args="-promscrape.config=${SCRAPE_CONFIG} -remoteWrite.tmpDataPath=${TMP_DATA_PATH} -httpListenAddr=${HTTP_LISTEN_ADDR}"

# Cloud write (downsampled) when configured.
if [ -n "${REMOTE_WRITE_URL}" ]; then
  args="${args} -remoteWrite.url=${REMOTE_WRITE_URL}"
  if [ -f "${STREAM_AGGR_CONFIG}" ]; then
    args="${args} -remoteWrite.streamAggr.config=${STREAM_AGGR_CONFIG}"
  fi
  if [ -f "${REMOTE_RELABEL_CONFIG}" ]; then
    args="${args} -remoteWrite.urlRelabelConfig=${REMOTE_RELABEL_CONFIG}"
  fi
  if [ -n "${REMOTE_WRITE_USERNAME}" ]; then
    args="${args} -remoteWrite.basicAuth.username=${REMOTE_WRITE_USERNAME}"
  fi
  if [ -n "${REMOTE_WRITE_PASSWORD_FILE}" ]; then
    args="${args} -remoteWrite.basicAuth.passwordFile=${REMOTE_WRITE_PASSWORD_FILE}"
  elif [ -n "${REMOTE_WRITE_PASSWORD}" ]; then
    args="${args} -remoteWrite.basicAuth.password=${REMOTE_WRITE_PASSWORD}"
  fi
fi

# Local write (full resolution).
args="${args} -remoteWrite.url=${LOCAL_WRITE_URL}"
if [ -n "${REMOTE_WRITE_URL}" ] && [ -f "${LOCAL_RELABEL_CONFIG}" ]; then
  args="${args} -remoteWrite.urlRelabelConfig=${LOCAL_RELABEL_CONFIG}"
fi

exec /vmagent-prod ${args}
