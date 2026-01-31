#!/bin/sh
set -eu

# Load edge identity labels (deployment/node/system ids, etc)
set -a
[ -f /etc/ovr/edge.env ] && . /etc/ovr/edge.env
[ -f /etc/ovr/edge.env ] && . /etc/ovr/edge.env
set +a

SCRAPE_CONFIG="${VMAGENT_SCRAPE_CONFIG:-/etc/vmagent/scrape.yml}"
# Use localhost since vmagent runs on host network (Docker bridge broken on this host)
LOCAL_WRITE_URL="${VM_LOCAL_WRITE_URL:-http://localhost:8428/api/v1/write}"
REMOTE_WRITE_URL="${VM_REMOTE_WRITE_URL:-}"
REMOTE_WRITE_USERNAME="${VM_REMOTE_WRITE_USERNAME:-}"
REMOTE_WRITE_PASSWORD_FILE="${VM_REMOTE_WRITE_PASSWORD_FILE:-}"
REMOTE_WRITE_PASSWORD="${VM_REMOTE_WRITE_PASSWORD:-}"
TMP_DATA_PATH="${VM_REMOTE_WRITE_TMPDATA_PATH:-/tmp/vmagent}"
HTTP_LISTEN_ADDR="${VMAGENT_HTTP_LISTEN_ADDR:-:8429}"

STREAM_AGGR_CONFIG="${VM_REMOTE_WRITE_STREAM_AGGR_CONFIG:-/etc/ovr/stream_aggr.yml}"
LOCAL_RELABEL_CONFIG="${VM_LOCAL_WRITE_RELABEL_CONFIG:-/etc/ovr/remote_write_local_relabel.yml}"
REMOTE_RELABEL_CONFIG="${VM_REMOTE_WRITE_RELABEL_CONFIG:-/etc/ovr/remote_write_cloud_relabel.yml}"

# Build argv safely (no string concat bugs)
set -- /vmagent-prod \
  -promscrape.config="$SCRAPE_CONFIG" \
  -remoteWrite.tmpDataPath="$TMP_DATA_PATH" \
  -httpListenAddr="$HTTP_LISTEN_ADDR"

# Apply identity labels to ALL outgoing series (cloud + local)
[ -n "${DEPLOYMENT_ID:-}" ] && set -- "$@" -remoteWrite.label="deployment_id=$DEPLOYMENT_ID"
[ -n "${NODE_ID:-}" ]       && set -- "$@" -remoteWrite.label="node_id=$NODE_ID"
[ -n "${SYSTEM_ID:-}" ]     && set -- "$@" -remoteWrite.label="system_id=$SYSTEM_ID"
[ -n "${STACK_NAME:-}" ]    && set -- "$@" -remoteWrite.label="stack_name=$STACK_NAME"

# Enable GLOBAL stream aggregation (creates :10s_avg series)
# keepInput=true so local can still store raw, while cloud keeps only aggregates via relabel.
if [ -f "$STREAM_AGGR_CONFIG" ]; then
  set -- "$@" \
    -streamAggr.config="$STREAM_AGGR_CONFIG" \
    -streamAggr.keepInput=true
fi


# Cloud remote write (optional)
if [ -n "$REMOTE_WRITE_URL" ]; then
  set -- "$@" -remoteWrite.url="$REMOTE_WRITE_URL"
  [ -f "$REMOTE_RELABEL_CONFIG" ] && set -- "$@" -remoteWrite.urlRelabelConfig="$REMOTE_RELABEL_CONFIG"

  [ -n "$REMOTE_WRITE_USERNAME" ] && set -- "$@" -remoteWrite.basicAuth.username="$REMOTE_WRITE_USERNAME"

  if [ -n "$REMOTE_WRITE_PASSWORD_FILE" ]; then
    set -- "$@" -remoteWrite.basicAuth.passwordFile="$REMOTE_WRITE_PASSWORD_FILE"
  elif [ -n "$REMOTE_WRITE_PASSWORD" ]; then
    set -- "$@" -remoteWrite.basicAuth.password="$REMOTE_WRITE_PASSWORD"
  fi
fi

# Local write (always)
set -- "$@" -remoteWrite.url="$LOCAL_WRITE_URL"
if [ -n "$REMOTE_WRITE_URL" ] && [ -f "$LOCAL_RELABEL_CONFIG" ]; then
  set -- "$@" -remoteWrite.urlRelabelConfig="$LOCAL_RELABEL_CONFIG"
fi

exec "$@"
