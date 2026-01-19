# GX D-Bus Exporter (optional)

This installs the `dbus2prom.py` exporter on a Victron GX device using `/data/overdrive` for persistence and a runit service at `/service/ovr-dbus2prom`.

## Files to copy to the GX

From the repo root on the N100:

```bash
# Copies the whole GX installer payload (dbus2prom.py + map TSVs + scripts)
scp -r edge/gx root@<GX_IP>:/data/overdrive/installer
```

## Install on GX

```bash
ssh root@<GX_IP> "sh /data/overdrive/installer/gx/install_dbus_exporter.sh"
```

## Verify

```bash
ssh root@<GX_IP> "busybox netstat -ltn | grep -E ':(9480|9481)\\b'"
ssh root@<GX_IP> "curl -fsS -m 2 http://127.0.0.1:9480/metrics | head -n 5"
ssh root@<GX_IP> "curl -fsS -m 2 http://127.0.0.1:9481/metrics | head -n 5"
```

## Notes

- The service writes logs to `/data/overdrive/dbus2prom/log/`.
- Map files live in:
  - `/data/overdrive/dbus2prom/map_fast.tsv`
  - `/data/overdrive/dbus2prom/map_slow.tsv`
