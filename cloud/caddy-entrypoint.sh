#!/bin/sh
set -eu
if [ -n "${VM_WRITE_PASS_HASH_FILE:-}" ] && [ -f "${VM_WRITE_PASS_HASH_FILE}" ]; then
  VM_WRITE_PASS_HASH="$(cat "${VM_WRITE_PASS_HASH_FILE}")"
  export VM_WRITE_PASS_HASH
fi
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
