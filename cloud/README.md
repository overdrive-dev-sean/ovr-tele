# Cloud backend

This directory contains the central fleet backend (VPS): VictoriaMetrics + Grafana + Fleet API + Fleet Map behind a TLS proxy (Caddy).

## Compose files

- `compose.dev.yml`
  - Development / local builds.
  - Uses `build:` for `api` and `map`.
- `compose.release.yml`
  - Production/release deployment.
  - Pulls `api` and `map` images from GHCR pinned by `CLOUD_TAG`.

- `compose.sandbox.yml`
  - Parallel testing deployment on the **same VPS**.
  - No TLS/Caddy; binds high ports (so it can coexist with the production stack).

## DNS

Create DNS records pointing to the VPS public IP:

- `metrics.<domain>`
- `grafana.<domain>`
- `map.<domain>`

## Environment

Copy and edit the example (canonical location is `/etc/ovr/cloud.env`):

```bash
cd cloud
sudo mkdir -p /etc/ovr
sudo cp .env.example /etc/ovr/cloud.env
sudo ln -sf /etc/ovr/cloud.env .env
```

Set at minimum:
- `BASE_DOMAIN`
- `ACME_EMAIL`
- `GRAFANA_ADMIN_USER` (optional)
- `VM_WRITE_USER`
- `VM_WRITE_PASS_HASH` (or `VM_WRITE_PASS_HASH_FILE`)

## Start (dev/local)

```bash
cd cloud
sudo docker compose -f compose.dev.yml up -d --build
```

## Start (production/release)

```bash
cd cloud
# set GHCR_OWNER + CLOUD_TAG in .env
# pin VM_IMAGE and GRAFANA_IMAGE (no :latest)
sudo docker compose -f compose.release.yml pull
sudo docker compose -f compose.release.yml up -d

```

## Start (sandbox / parallel on same VPS)

This is the safest way to test a new repo on the same VPS **without** touching the running production stack.

```bash
cd cloud
# .env should already be linked to /etc/ovr/cloud.env
# set SANDBOX_BIND=0.0.0.0 if you want a remote edge node to reach the sandbox ports.
COMPOSE_PROJECT_NAME=cloud_sandbox \
  sudo docker compose -f compose.sandbox.yml up -d
```

Exposed ports (host):
- Map UI + API (same origin): `18080`
- VictoriaMetrics write endpoint (basic_auth): `18428`
- Grafana (direct): `13001`

Tip: bind to `127.0.0.1` (default) and use SSH tunnels whenever possible.

Remote write example for edge nodes targeting the sandbox:
- URL: `http://<vps-ip>:18428/api/v1/write`
- Auth: basic auth (`VM_WRITE_USER`, password that produced `VM_WRITE_PASS_HASH`)

## Docs

- Remote write wiring: `../docs/cloud/REMOTE_WRITE_WIRING.md`
