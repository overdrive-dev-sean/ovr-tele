# Overdrive Telemetry (Monorepo)

This repo contains **two deployable products**:

- **Edge**: the on-site node stack (VictoriaMetrics + vmagent + Grafana + Telegraf + events + frontend)
- **Cloud**: the central fleet backend (VictoriaMetrics + Grafana + Fleet API + Fleet Map + Caddy)

It also contains **provisioning** scripts for preparing new edge nodes.

## Repository layout

```text
.
├── edge/                   # Everything that runs on the field node
│   ├── compose.dev.yml
│   ├── compose.release.yml
│   ├── services/
│   │   ├── events/
│   │   └── frontend/
│   ├── grafana/ telegraf/ vmagent/
│   ├── gx/                 # Victron GX exporter tooling + map files
│   └── networking/         # edge/networking/ovru-netkit + setup scripts
├── cloud/                  # Everything that runs on the central server
│   ├── compose.dev.yml
│   ├── compose.release.yml
│   ├── services/
│   │   ├── api/
│   │   └── map/
│   └── grafana/provisioning/
├── provisioning/
│   └── edge/               # Provisioning + firstboot assets for edge nodes
├── docs/                   # Product documentation
├── ops/                    # Runbooks + incident templates
└── scripts/release/        # Tag/release helper scripts
```

## Quick start (development)

### Edge (local builds)

```bash
cd edge
sudo docker compose -f compose.dev.yml up -d --build
```

### Cloud (local builds)

```bash
cd cloud
cp .env.example .env
# edit .env
sudo docker compose -f compose.dev.yml --env-file .env up -d --build
```

## Field deployments (reproducible)

For **identical field nodes**, use the release compose which pulls **pinned images** from GHCR.

```bash
cd edge
cp edge.env.example edge.env
# edit EDGE_VERSION, GHCR_OWNER, and pin third-party images if desired
sudo docker compose --env-file edge.env -f compose.release.yml pull
sudo docker compose --env-file edge.env -f compose.release.yml up -d
```

See:
- `VERSIONING.md`
- `RELEASE.md`
- `COMPATIBILITY.md`

## Provisioning (edge node)

Provisioning scripts live in `provisioning/edge/`.

Start here:
- `provisioning/edge/PROVISIONING_GUIDE.md`
- `provisioning/edge/bootstrap_n100.sh`

## Documentation

See `docs/README.md`.

## Contributing

See `CONTRIBUTING.md` and `AGENTS.md`.
