# Edge third-party image pins

Field deployments should be **identical**.

To achieve that, we pin **third-party** container images (VictoriaMetrics, vmagent, Grafana, Telegraf, node-exporter) by **digest**.

This directory is the recommended place to store those pins *per edge release*.

## Pattern

- Create a file: `edge/pins/vX.Y.Z.env`
- Commit it in the repo
- The edge node copies it to `edge/edge.env` (or uses it directly with `--env-file`)

## Why keep pins in git?

- The pins become part of the tagged `edge/vX.Y.Z` release.
- You can roll back a fleet *without guessing* which third-party versions you were on.

## Generate pins from a known-good node

On a node where the stack is running and known-good:

```bash
cd /opt/edge
./scripts/pin_third_party_images.sh edge edge/compose.dev.yml
```

Paste the output into `edge/pins/vX.Y.Z.env`.