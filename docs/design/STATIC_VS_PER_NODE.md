| Item | Repo path(s) | Static/Per-node | Proposed mechanism |
| --- | --- | --- | --- |
| Docker compose topology | `edge/compose.dev.yml` | Static | Keep identical across nodes; inject values via `/etc/overdrive/site.env`. |
| vmagent scrape template | `edge/vmagent/scrape.yml` | Static template | Bootstrap renders `/etc/overdrive/vmagent.scrape.yml` with `deployment_id` + `node_id`. |
| vmagent targets | (new) `/etc/overdrive/targets.yml` | Per-node | File SD list of `gx_fast`, `gx_slow`, `node_exporter` targets. |
| vmagent stream aggregation | (new) `/etc/overdrive/stream_aggr.yml` | Per-node | Downsample config for cloud remote_write (default 10s, excludes node_exporter). |
| vmagent remote write relabel | (new) `/etc/overdrive/remote_write_local_relabel.yml`, `/etc/overdrive/remote_write_cloud_relabel.yml` | Per-node | Controls which series go to local vs cloud remote_write. |
| Node identity | (new) `/etc/overdrive/site.env` | Per-node | `DEPLOYMENT_ID`, `NODE_ID`, `SYSTEM_ID`, remote write creds. |
| Remote write secret | (new) `/etc/overdrive/secrets/remote_write_password` | Per-node secret | Referenced by `VM_REMOTE_WRITE_PASSWORD_FILE`. |
| GX IP + SSH creds | `edge/services/events/app.py` env | Per-node | `GX_HOST`, `GX_USER`, `GX_PASSWORD` in `/etc/overdrive/site.env`. |
| Telegraf global tags | `edge/telegraf/telegraf.conf` | Per-node values | Uses env vars from `/etc/overdrive/site.env`. |
| Acuvim IP list | `edge/telegraf/targets_acuvim.txt` | Per-node | Move to `/etc/overdrive/targets_acuvim.txt` (symlink). |
| Generated meter configs | `edge/telegraf/telegraf.d/*.conf` | Per-node | Created by `edge/scripts/telegraf_discover_acuvim.sh`. |
| GX dbus2prom maps | `edge/gx/map_fast.tsv`, `edge/gx/map_slow.tsv` | Per-node (GX) | Copied to `/data/overdrive/dbus2prom/` on GX. |
| GX exporter script | `dbus2prom.py`, `gx/*` | Static | Deployed to GX via `/data/overdrive` + `/service`. |
| Network NAT config | `edge/networking/ovru-netkit/vars.env` | Per-node | Edit per host before running `edge/networking/ovru-netkit/install.sh`. |
