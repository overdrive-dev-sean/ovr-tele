# IDE / AI Agent Instructions

This file is written for IDE agents (Copilot, Cursor, etc.).
It describes *how to make changes* in this repo without breaking field deployments.

## Golden rules

1. **Do not break field nodes.** Edge deployments are distributed and expensive to fix.
2. **No silent schema changes.** Metric schema and HTTP APIs are contracts (see `COMPATIBILITY.md`).
3. **Reproducibility first.** Avoid `:latest` and unpinned deps in production artifacts.
4. **Small PRs.** Prefer reviewable, incremental changes.

## Required workflow

- Create or reference a GitHub Issue for every change.
- Use a short-lived branch named `feat/<issue>-...` or `fix/<issue>-...`.
- Use **Conventional Commits** (`feat:`, `fix:`, `docs:`, `chore:`).
- Mark breaking changes with `feat!:` and document them.
- Update `CHANGELOG.md` for user-visible changes (or label PR `no-changelog`).
- Include tests or a short manual test plan.

## Repo mental model

- Respect repo layout: `edge/`, `cloud/`, `provisioning/edge/`. Don’t invent new top-level folders without justification.

### Edge (field nodes)
- Dev compose: `edge/compose.dev.yml`
- Release compose: `edge/compose.release.yml`
- Services:
  - `edge/services/events/`
  - `edge/services/frontend/`
- Provisioning: `provisioning/edge/`
- Networking kit: `edge/networking/ovru-netkit/`

### Cloud (VPS)
- Dev compose: `cloud/compose.dev.yml`
- Release compose: `cloud/compose.release.yml`
- Services:
  - `cloud/services/api/`
  - `cloud/services/map/`

## Contracts and compatibility

- Treat these as contracts: metrics names/labels, report payload schema, `/api/*` endpoints, and node URL conventions.
- If any contract changes, update `COMPATIBILITY.md`, add a transition plan in `CHANGELOG.md`, and bump the edge version appropriately.

## Config and secrets

- Never commit secrets. `.env` and `/etc/ovr/*` values must stay out of git.
- Only update `*.env.example` and docs.
- When adding config/env vars, add them to `*.env.example`, document in `/docs`, and provide safe defaults.

## Change checklists

### If you change **metrics**
- Update the producers (Telegraf config in `edge/telegraf/`, vmagent scrape targets in `edge/vmagent/`, and the events service)
- Update any consumers (api queries, Grafana dashboards)
- Add a transition plan in `CHANGELOG.md` and `COMPATIBILITY.md`

### If you change **provisioning**
- Keep it idempotent (safe to re-run)
- Never hardcode secrets
- Prefer writing config into `/etc/ovr/`
- Maintain backwards compatibility for existing nodes or provide a clear migration step

### If you change **compose files**
- Use `compose.dev.yml` for local development; do not require GHCR to run locally.
- Release compose files must remain reproducible.
- Avoid `:latest` in release or non-dev compose; if you introduce it, document why.
- Validate with `docker compose config`.
- Ensure volumes (especially VM storage) are preserved intentionally.

## Testing expectations

Before opening a PR, run:

```bash
python -m unittest discover -s edge/services/events -p 'test_*.py'
python edge/services/events/test_influx_escaping.py
python -m unittest discover -s cloud/services/api -p 'test_*.py'

cd edge/services/frontend && npm install && npm run build
cd ../../../cloud/services/map && npm install && npm run build
```

## Release awareness

- Edge releases are tagged `edge/vX.Y.Z`.
- Cloud builds can ship from `main`, but must support the edge compatibility window.

See `RELEASE.md` and `VERSIONING.md`.

## Extra high-leverage behaviors

Do these automatically whenever relevant:

- Communication and guidance:
  - Assume the user is still learning the stack; be patient, explicit, and avoid jargon.
  - When Docker rebuilds or restarts are needed, give exact commands but skip redundant “cd to X” prefaces unless required.
- If it touches **cloud ingest logic** (report collection):
  - Add logging for why a report was rejected.
  - Keep backward compatibility for older payload versions if possible.
- If it touches **edge reporting code**:
  - Include the edge version in headers/payload.
  - Keep retries/backoff safe (don’t spam the server).
