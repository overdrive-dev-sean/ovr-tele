# Rolling updates (edge fleet)

Goal: upgrade many field nodes while minimizing risk.

## Rings
1. **Lab** (can be wiped)
2. **Pilot** (1–3 representative field sites)
3. **Stable** (the rest)

Maintain a simple mapping of node IDs to rings (example file):
- `ops/rings/lab.txt`
- `ops/rings/pilot.txt`
- `ops/rings/stable.txt`

## Standard upgrade command
On a node (assuming `/opt/stack`):
```bash
cd /opt/edge
EDGE_VERSION=1.2.3 GHCR_OWNER=<github_owner> docker compose -f compose.release.yml pull
EDGE_VERSION=1.2.3 GHCR_OWNER=<github_owner> docker compose -f compose.release.yml up -d
```

## Verification (per node)
- `docker ps` shows healthy containers
- `curl http://localhost:8088/health` returns `{status: ok}`
- telemetry ingest looks normal:
  - local VM: `curl -s http://localhost:8428/metrics | grep vm_rows_inserted_total`

## Rollback
Same process, but with previous `EDGE_VERSION`.
Keep at least 2 prior stable versions available.
