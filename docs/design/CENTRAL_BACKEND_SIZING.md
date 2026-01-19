# Central Backend Sizing (Measured)

Inputs captured from a representative node with GX exporter, node-exporter, and Telegraf.

## Measurements

- Active series (VictoriaMetrics): 3,490 total series
- Ingest rates (60s window):
  - vmagent remote_write (promremotewrite): 31,434 rows / 60s ≈ **524 samples/sec**
  - Telegraf (influx): 8,895 rows / 60s ≈ **148 samples/sec**
  - Combined ≈ **672 samples/sec**
- Storage footprint:
  - `/storage/data` ≈ **343 MB**
  - Total rows inserted (lifetime): 44,201,218 promremotewrite + 12,651,426 influx ≈ **56.85M**
  - Observed storage ratio ≈ **6.3 bytes/sample** (includes index overhead)

## Derived projections (per node)

- Samples/day ≈ 672 * 86,400 ≈ **58.1M samples/day**
- Storage/day ≈ 58.1M * 6.3 bytes ≈ **0.35 GB/day**

## Downsampled remote-write scenario (10x)

If cloud remote_write is downsampled by ~10x, the cloud backend sees:

- Samples/day ≈ 5.81M samples/day
- Storage/day ≈ **0.035 GB/day** (based on observed 6.3 bytes/sample)

Approximate cloud retention impact:

- **10 nodes**
  - 30 days: ~10.5 GB
  - 180 days: ~63 GB
  - 365 days: ~128 GB
- **100 nodes**
  - 30 days: ~105 GB
  - 180 days: ~630 GB
  - 365 days: ~1.28 TB

## Fleet storage projections

Approximate disk usage assuming similar ingest:

- **10 nodes**
  - 30 days: ~105 GB
  - 180 days: ~630 GB
  - 365 days: ~1.28 TB
- **100 nodes**
  - 30 days: ~1.05 TB
  - 180 days: ~6.3 TB
  - 365 days: ~12.8 TB

## VPS sizing guidance (central backend)

These are conservative starting points. VictoriaMetrics scales well; adjust after observing real load.

- **10 nodes (~6.7k samples/sec)**:
  - CPU: 2–4 vCPU
  - RAM: 8–16 GB
  - Disk: 250–500 GB (30–60 day retention)

- **100 nodes (~67k samples/sec)**:
  - CPU: 8–16 vCPU
  - RAM: 32–64 GB
  - Disk: 2–6 TB (30–90 day retention)

## Mitigations / tuning

- Reduce retention period to control disk growth (`VM_RETENTION` in `cloud/.env`).
- Reduce scrape frequency for non-critical metrics.
- Consider downsampling (VictoriaMetrics downsampling or multiple retentions).
- Split Telegraf vs. Prometheus metrics if certain inputs are too chatty.
- Watch for rogue nodes with:
  ```
  topk(10, sum by (node_id) (rate({__name__=~".+"}[5m])))
  ```

## Notes

- Telegraf (Acuvim) input is intermittent; the 148 samples/sec rate reflects Logger0 being active.
- These projections assume the current workload and label cardinality (GX + node-exporter + telegraf).
- Re-run this sizing after adding new meters or changing scrape intervals.
