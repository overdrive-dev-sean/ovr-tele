#!/bin/sh
set -e

if [ -n "${BASE_DOMAIN:-}" ] && [ -n "${NODE_ID:-}" ]; then
  export GF_SERVER_DOMAIN="${NODE_ID}-grafana.${BASE_DOMAIN}"
  export GF_SERVER_ROOT_URL="https://${NODE_ID}-grafana.${BASE_DOMAIN}/"
fi

exec /run.sh
