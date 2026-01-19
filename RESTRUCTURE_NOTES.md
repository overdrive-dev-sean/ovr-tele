# Restructure notes (old → new)

This repo was reorganized into a simple monorepo layout with **two deploy surfaces**:

- `edge/` (field node stack)
- `cloud/` (VPS backend)

The goal is to make:
- releases reproducible (build once, deploy many)
- CI/GitHub Actions more obvious
- provisioning scripts and docs discoverable

## Directory map

| Old path | New path |
|---|---|
| `docker-compose.yml` | `edge/compose.dev.yml` |
| `docker-compose.release.yml` | `edge/compose.release.yml` |
| `events/` | `edge/services/events/` |
| `web/` | `edge/services/frontend/` |
| `vmagent/` | `edge/vmagent/` |
| `telegraf/` | `edge/telegraf/` |
| `grafana/` | `edge/grafana/` |
| `gx/` | `edge/gx/` |
| `ovru-netkit/` | `edge/networking/ovru-netkit/` |
| `home/ovradmin/` | `edge/home/ovradmin/` |
| `scripts/` | `edge/scripts/` |
| `cloud/compose.yml` | `cloud/compose.dev.yml` |
| `cloud/api/` | `cloud/services/api/` |
| `cloud/map/` | `cloud/services/map/` |
| `cloud/provisioning/` | `cloud/grafana/provisioning/` |
| `provision/` | `provisioning/edge/` |

## Provisioning behavior changes

- Provisioning scripts now default to deploying **from `edge/`**.
- `bootstrap_n100.sh` supports:
  - **dev compose** if `/etc/overdrive/edge.env` is absent
  - **release compose** if `/etc/overdrive/edge.env` exists (it will be sourced)

## "Legacy" docs

The previous root README was preserved at:

- `docs/edge/LEGACY_STACK_README.md`

It may contain outdated paths; prefer the new docs in `docs/` and `edge/README.md`.
