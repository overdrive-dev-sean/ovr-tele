# Edge stack

This directory contains the full on-site node stack ("edge").

## Compose files

- `compose.dev.yml`
  - For development / local builds.
  - Uses `build:` for internal services.
- `compose.release.yml`
  - For field deployments / reproducible releases.
  - Pulls internal images from GHCR pinned by `EDGE_VERSION`.

## Run (development)

```bash
cd edge
sudo docker compose -f compose.dev.yml up -d --build
```

## Run (field/release)

```bash
cd edge
cp edge.env.example edge.env
# edit GHCR_OWNER (required unless hardcoded), EDGE_VERSION, and pin third-party images (no :latest)
# Helper: ./scripts/pin_third_party_images.sh edge edge/compose.dev.yml
sudo docker compose --env-file edge.env -f compose.release.yml pull
sudo docker compose --env-file edge.env -f compose.release.yml up -d
```

### Recommended: store pins per release

For true reproducibility across a fleet, store pinned third-party images in git:

- `edge/pins/vX.Y.Z.env`

Then on nodes, you can do:

```bash
cd /opt/edge/edge
sudo docker compose --env-file edge/pins/vX.Y.Z.env -f compose.release.yml up -d
```

## Node configuration

Most node-specific configuration is externalized to `/etc/overdrive/` on the host:

- `/etc/overdrive/site.env` (deployment_id, node_id, remote write URLs, etc)
- `/etc/overdrive/targets.yml` (optional)
- `/etc/overdrive/secrets/` (API keys, passwords)

Provisioning scripts for creating these live in `../provisioning/edge/`.

## Key services

- `victoria-metrics` (local storage)
- `vmagent` (scrapes + remote write)
- `grafana` (local dashboards)
- `telegraf` (Modbus + host metrics)
- `events` (Python API that also generates event reports)
- `frontend` (web UI)

## Health checks

- Event service: `http://127.0.0.1:8088/health`
- Frontend: `http://<node-ip>:8080/`
- Grafana: `http://<node-ip>:3000/`

