# IDE / AI Agent Instructions

It describes *how to make changes* in this repo without breaking field deployments.

## Golden rules

1. **Do not break field nodes.** Edge deployments are distributed and expensive to fix.
2. **No silent schema changes.** Metric schema and HTTP APIs are contracts (see `COMPATIBILITY.md`).
3. **Reproducibility first.** Avoid `:latest` and unpinned deps in production artifacts.
4. **Small PRs.** Prefer reviewable, incremental changes.
5. **Networking/firewall/routing changes are high-risk.** Do not change nftables/iptables, routing tables, policy routing, Docker networking, or DNS behavior without an explicit plan, rollback steps, and a verification checklist (host + container level).
6. **Config location is non-negotiable.** Overdrive config lives under `/etc/ovr/`. Do not create or reference `/etc/overdrive/`. No symlinks except the allowed `.env -> /etc/ovr/*.env` pattern.
7. **Never ship a default-drop FORWARD policy without Docker allowances.** Containers egress via host **FORWARD** (veth → `br-*` → FORWARD → uplink). The host can look “online” while containers are completely offline.

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
- Do not create or reference `/etc/overdrive/`
- If you introduce a compose `.env`, it may be a symlink to `/etc/ovr/*.env` (allowed). Keep secrets under `/etc/ovr/secrets/*` with restrictive perms (e.g. directory `0700`, files `0600`).

### If you change **compose files**
- Release compose files must remain reproducible.
- Avoid `:latest` in release compose; if you introduce it, document why.
- Be explicit about compose filenames in docs and commands (this repo uses `compose.release.yml` / `compose.dev.yml`; do not assume a default `compose.yml` exists).

### If you change **networking / firewall / routing**

Treat this like production networking work. Any proposal MUST include the items below.

#### 1) Mental model (required in PR/issue description)
- Host connectivity uses **OUTPUT**.
- Container connectivity uses **FORWARD** (veth → `br-*` → FORWARD → uplink).
- Docker user-defined bridge networks appear as `br-<id>` interfaces.
- It is possible for the host to have working internet/DNS while containers are fully offline if the FORWARD path is blocked.

#### 2) Pre-change snapshot + rollback
Before changing anything on a node, take a snapshot into `/etc/ovr/debug/netfilter/`:

```bash
sudo install -d -m 0755 /etc/ovr/debug/netfilter
sudo nft -a list ruleset > /etc/ovr/debug/netfilter/nft.before.$(date -Is | tr ':' '-').txt
sudo iptables-save > /etc/ovr/debug/netfilter/iptables-nft.before.$(date -Is | tr ':' '-').v4 || true
sudo iptables-legacy-save > /etc/ovr/debug/netfilter/iptables-legacy.before.$(date -Is | tr ':' '-').v4 2>/dev/null || true
```

Rollback steps MUST be stated (e.g., restore previous `/etc/nftables.conf` from backup and restart `nftables` + `docker`).

#### 3) Safety constraints
- Do not introduce `policy drop` on a forward hook unless Docker egress is explicitly permitted.
- Avoid layering UFW/firewalld on top of custom nftables without an integration plan.
- Prefer a dedicated nftables table for Overdrive (e.g. `table inet ovr`) and avoid manually modifying Docker-managed tables (iptables-nft warns when you do).
- Avoid “bandaids” for debugging (no host networking; no hardcoded host entries; no `extra_hosts`).

#### 4) Minimum allow rules if FORWARD is default-drop
If a forward hook has `policy drop`, the ruleset MUST include:
- Return traffic: `ct state established,related accept`
- Docker forwarding: `iifname "br-*" accept` (and `iifname "docker0" accept` if docker0 is used)
- If ports are published to containers: allow DNATed flows (e.g. `ct status dnat accept`)

#### 5) Validation before applying
Always syntax-check nft config before restart:

```bash
sudo nft -c -f /etc/nftables.conf
```

#### 6) Post-change verification (must be documented + run on a node)
Containers must be able to:
- ping the upstream gateway
- resolve DNS via upstream resolver and via Docker embedded DNS (`127.0.0.11`)
- reach the cloud endpoint over HTTPS

Example ephemeral test (do not leave debug containers behind):

```bash
sudo docker run --rm --network edge_default nicolaka/netshoot   sh -lc 'ping -c1 10.10.4.1 && dig +time=2 +tries=1 metrics.overdrive.rocks @10.10.4.1 && dig +time=2 +tries=1 metrics.overdrive.rocks @127.0.0.11'
```

#### 7) Command hygiene (prevents “it ran but didn’t actually apply”)
- Prefer one command per line; avoid `sudo -i` blocks in docs (they can hide failures mid-block).
- For nftables strings/wildcards/comments, prefer `nft -f` heredocs so quoting is unambiguous (avoid silent no-ops).

## Known issues / workarounds

### Docker bridge networking broken on some hosts

On hosts where Tailscale or other VPN tools manage `/etc/resolv.conf`, Docker's default bridge networking may fail to route outbound traffic (containers can't reach the internet even though the host can).

**Symptoms:**
- `docker run --rm alpine ping 1.1.1.1` → 100% packet loss
- `docker build` fails with `getaddrinfo EAI_AGAIN` during `npm install`
- Host networking works fine: `docker run --rm --network=host alpine nslookup google.com`

**Workaround for builds:** Use `network: host` in compose build config:
```yaml
services:
  frontend:
    build:
      context: ./services/frontend
      network: host
```

**Workaround for runtime:** Add explicit DNS to services:
```yaml
services:
  myservice:
    dns:
      - 1.1.1.1
      - 8.8.8.8
```

The root cause is likely iptables/nftables FORWARD chain rules or NAT not applying correctly to Docker bridge traffic. A proper fix would require debugging the firewall ruleset.

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
