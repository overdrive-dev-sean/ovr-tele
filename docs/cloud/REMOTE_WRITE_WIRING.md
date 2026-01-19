# Remote Write Wiring

This stack expects vmagent on each N100 to remote_write to the cloud endpoint:

```
https://metrics.<domain>/api/v1/write
```

Auth is enforced by Caddy using HTTP basic auth on `/api/v1/write`.
Credentials are per-deployment; all nodes in a deployment share a single write credential.
Do not reuse this credential for Grafana or any read access.

## Generate the password hash

```bash
cd cloud

docker compose -f compose.dev.yml --env-file .env run --rm caddy \
  caddy hash-password --plaintext 'your_password'
```

Put the hash into `cloud/.env` as `VM_WRITE_PASS_HASH`.

## N100 config (site.env)

Set these in `/etc/ovr/site.env`:

```
VM_REMOTE_WRITE_URL=https://metrics.<domain>/api/v1/write
VM_REMOTE_WRITE_USERNAME=ovr
VM_REMOTE_WRITE_PASSWORD_FILE=/etc/ovr/secrets/remote_write_password
```

Store the password in `/etc/ovr/secrets/remote_write_password` (0600).

## Downsampling (cloud only)

When `VM_REMOTE_WRITE_URL` is set, vmagent will downsample to 10s intervals using:

- `/etc/ovr/stream_aggr.yml` (default: 10s avg, excludes port `9100`)
- `/etc/ovr/remote_write_cloud_relabel.yml` (send only `:avg` series)
- `/etc/ovr/remote_write_local_relabel.yml` (drop `:avg` locally)

To change the interval or exclusions, edit `/etc/ovr/stream_aggr.yml` and restart vmagent.

## Labels

vmagent injects:

- `deployment_id` and `node_id` from `/etc/ovr/site.env` (external labels)
- `system_id` from `/etc/ovr/targets.yml`

Use these for filtering in Grafana and alerts.

## Rogue writer detection (snippet)

Use this in Grafana/Explore to find top writers by node:

```
topk(10, sum by (node_id) (rate({__name__=~".+"}[5m])))
```

## Credential rotation

1. Create a new password hash and update `cloud/.env`, then restart Caddy.
2. Update `/etc/ovr/secrets/remote_write_password` on each node.
3. Restart vmagent (or `docker compose up -d`).
