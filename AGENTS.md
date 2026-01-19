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
- Update `CHANGELOG.md` for user-visible changes (or label PR `no-changelog`).

## Repo mental model

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

## Change checklists

### If you change **metrics**
- Update the producers (Telegraf config in `edge/telegraf/`, vmagent scrape targets in `edge/vmagent/`, and the events service)
- Update any consumers (api queries, Grafana dashboards)
- Add a transition plan in `CHANGELOG.md` and `COMPATIBILITY.md`

### If you change **provisioning**
- Keep it idempotent (safe to re-run)
- Never hardcode secrets
- Prefer writing config into `/etc/ovr/`

### If you change **compose files**
- Release compose files must remain reproducible.
- Avoid `:latest` in release compose; if you introduce it, document why.

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
