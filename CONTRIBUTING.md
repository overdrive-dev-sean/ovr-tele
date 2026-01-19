# Contributing

This repo deploys to:
- **Edge nodes** (N100 field deployments)
- **Cloud backend** (VPS)

Because edge deployments are distributed and costly to fix, we optimize for:
- small PRs
- reproducible builds
- conservative changes to metrics/remote-write schema

## Workflow

### 1) Create an issue first
Even small changes should have an issue so we can:
- track intent
- write acceptance criteria
- link PRs and release notes

Recommended labels:
- `type: feature` / `type: bug` / `type: docs` / `type: chore`
- `priority: p0`–`p3`
- `severity: s0`–`s3` (bugs)

### 2) Branch naming
- `feat/<issue>-short-desc`
- `fix/<issue>-short-desc`
- `docs/<issue>-short-desc`
- `chore/<issue>-short-desc`

### 3) Commit messages (Conventional Commits)
We use Conventional Commits to make release notes and versioning easier:
- `feat: ...`
- `fix: ...`
- `docs: ...`
- `chore: ...`

Breaking changes:
- `feat!: ...` (or `BREAKING CHANGE:` in the body)

### 4) Pull requests
PRs should include:
- link to the issue
- how to test
- risk level + rollback note
- changelog entry for user-visible changes (or label `no-changelog`)

### 5) Keep PRs small
Aim for a PR that can be reviewed in **10–20 minutes**.
If it’s larger, split it by vertical slices or use a feature flag.

---

## Local development

### Edge stack (local)
Dev compose file: `edge/compose.dev.yml`

```bash
cd edge
sudo docker compose -f compose.dev.yml up -d --build
```

### Cloud stack (local)
Dev compose file: `cloud/compose.dev.yml`

```bash
cd cloud
cp .env.example .env
# edit .env
sudo docker compose -f compose.dev.yml --env-file .env up -d --build
```

---

## Tests

CI runs:
- Python unit tests for:
  - `edge/services/events/`
  - `cloud/services/api/`
- JS build for:
  - `edge/services/frontend/`
  - `cloud/services/map/`

It also runs a **naming guard** to prevent reintroducing legacy names after the restructure.

Run locally:
```bash
# Python (edge events)
python -m unittest discover -s edge/services/events -p 'test_*.py'
python edge/services/events/test_influx_escaping.py

# Python (cloud api)
python -m unittest discover -s cloud/services/api -p 'test_*.py'

# JS builds
cd edge/services/frontend && npm install && npm run build
cd ../../../cloud/services/map && npm install && npm run build

# Naming guardrails
bash scripts/ci/naming_guard.sh
```

---

## Compatibility rules (read before changing metrics)

Before changing metric names or labels, read:
- `COMPATIBILITY.md`

Rule of thumb:
- **add** new metrics first
- keep old metrics for at least **2 edge MINOR releases**
- document deprecations in `CHANGELOG.md`

---

## Releases

Release process is documented in:
- `RELEASE.md`
- `VERSIONING.md`
