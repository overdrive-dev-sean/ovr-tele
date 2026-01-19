# Versioning

This repo has **two deploy surfaces** with different operational constraints:

- **Edge (field nodes)**: many distributed deployments. Must be **identical** across nodes within a ring and upgrade safely.
- **Cloud (VPS backend)**: few deployments. Can ship more frequently, but must remain compatible with the supported edge versions.

To make this practical, we use **SemVer for edge**, and **build IDs (git SHA) for cloud** (with optional cloud SemVer tags when desired).

---

## Edge versioning (SemVer)

### Git tag
- Release tags: `edge/vMAJOR.MINOR.PATCH`
- RC tags: `edge/vMAJOR.MINOR.PATCH-rc.N`

Examples:
- `edge/v1.4.2`
- `edge/v1.5.0-rc.1`

### What an "edge release" contains
An edge release is a fully reproducible bundle:
- pinned container images for **internal services** (events + edge UI)
- pinned container image versions for **third-party dependencies** (Grafana, VictoriaMetrics, vmagent, Telegraf, node-exporter)
- the exact Compose files and config templates used by field nodes

Recommended: store third-party pins in `edge/pins/vX.Y.Z.env` so the pins travel with the git tag.

Field nodes should deploy using:
- `edge/compose.release.yml` (production)
- `edge/compose.dev.yml` is allowed for development/local builds

### SemVer rules (edge)
- **PATCH**: fixes only; no intentional behavior change
- **MINOR**: backward-compatible features
- **MAJOR**: breaking changes to any of:
  - remote write / metric names relied on by cloud UIs
  - API contracts (events endpoints if used remotely)
  - required config schema

---

## Cloud versioning (build ID + optional SemVer)

### Default
- Cloud deploys from `main` using an immutable build identifier (git SHA or image digest).
- We *optionally* tag cloud releases as `cloud/vX.Y.Z` when:
  - the cloud UI/API contract changes
  - we want a named rollback point

### Git tag (optional)
- `cloud/vMAJOR.MINOR.PATCH`

---

## Container image tags

We publish container images to **GitHub Container Registry (GHCR)**.

### What "GHCR images" means (plain-English)

- A **container image** is the packaged filesystem + runtime for a service (like a sealed, versioned "app bundle" for Docker).
- **GHCR** is GitHub's built-in registry for storing those images (similar to Docker Hub, but tied to your GitHub org/repo).
- The point of publishing images is: **build once in CI** → every edge node pulls the **exact same** bytes for a given version.

### Edge images
- `ghcr.io/<owner>/edge-events:<EDGE_VERSION>`
- `ghcr.io/<owner>/edge-frontend:<EDGE_VERSION>`

Where `<EDGE_VERSION>` is the SemVer string (e.g. `1.5.0`, `1.5.0-rc.1`).

### Cloud images
- `ghcr.io/<owner>/cloud-api:<CLOUD_TAG>`
- `ghcr.io/<owner>/cloud-map:<CLOUD_TAG>`

Where `<CLOUD_TAG>` can be:
- a git SHA (recommended for continuous deploy)
- or a SemVer string from `cloud/vX.Y.Z` tags

---

## Source-of-truth for "what is running"

- **Edge**: `EDGE_VERSION` (SemVer) + container digests
- **Cloud**: container digest (or git SHA)

Operationally, every node and the cloud backend should expose its running version in:
- `/health` endpoints (JSON contains version + git SHA)
- logs on boot
- (future) a metrics gauge like `ovr_build_info{version="...",git_sha="..."} 1`
