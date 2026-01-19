# Roadmap

This repo is a **monorepo** for:
- **Edge stack (field node)**: N100 host + Docker Compose stack (VictoriaMetrics, vmagent, Grafana, Telegraf, events, UI)
- **Cloud backend (VPS)**: central VictoriaMetrics + Grafana + Fleet API + Fleet Map UI behind Caddy
- **Provisioning**: OS/bootstrap scripts for field nodes and Victron GX exporter deployment

## How to use this roadmap

We run a simple **Now / Next / Later** roadmap:
- **Now (0–4 weeks):** actively being built and expected to ship
- **Next (1–3 months):** planned and sized
- **Later (3+ months):** ideas / research / not committed

Each roadmap item should link to a GitHub Issue (or a Project card) and have:
- acceptance criteria
- owner
- rough size (S/M/L)
- risks/unknowns

## Now

- [ ] (Add items here)

## Next

- [ ] (Add items here)

## Later

- [ ] (Add items here)

## Backlog hygiene

**Weekly**:
- Triage new issues
- Assign `type:*`, `priority:*`, and (for bugs) `severity:*`
- Ensure anything in **Now** has acceptance criteria and a clear ship target

**Monthly**:
- Re-evaluate Next/Later, archive stale items
