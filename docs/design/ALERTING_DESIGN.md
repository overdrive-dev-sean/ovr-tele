# Alerting Design Notes

*Status: Design phase - no alerts implemented yet*

## Current Data Pipeline (2026-01)

```
GX device ──MQTT 1883──► Telegraf ──► victron_* metrics ──► VictoriaMetrics
                    └──► Events service ──► realtime cache (/api/realtime)

ACUVIM ──► vmagent federation ──► job="vm_federate_acuvim" ──► VM

Edge VM ──► remote_write ──► Cloud VM (if configured)
```

### Key differences from old pipeline
- GX data comes via Telegraf MQTT consumer, not vmagent scraping
- No `up` metric for GX targets (Telegraf doesn't produce Prometheus-style `up`)
- GX metrics have `db="telegraf"` label, not `job=gx_*`
- Metric names are `victron_*` (transformed by Starlark processor)

## Design Questions

### 1. Device offline vs pipeline broken

A GX being unreachable isn't necessarily an alert. Systems go offline for legitimate reasons:
- Maintenance
- Travel (mobile installations)
- Seasonal use
- Network issues at remote sites

**Question:** Do we want alerts for "device offline" or only "pipeline broken while device should be online"?

**Possible approach:**
- Don't alert on individual device offline
- Alert on "no data from ANY known system" (pipeline dead)
- Or: opt-in per-system "expected online" flag

### 2. Staleness detection

Could detect stale data with:
```promql
time() - max_over_time(victron_system_serial{system_id="X"}[5m]) > threshold
```

**Questions:**
- What threshold? 60s? 5m? Depends on expected gaps.
- How to distinguish "offline" from "broken pipeline"?
- Should staleness be per-system or aggregate?

### 3. Telegraf health

No `up` metric exists for Telegraf MQTT consumers. Options:
- Enable Telegraf internal metrics and monitor those
- Monitor presence of recent `victron_*` writes
- Check "expected systems not reporting"

### 4. What's actionable?

| Alert | Action |
|-------|--------|
| GX offline | Check physical device, network, power |
| Telegraf dead | `docker restart telegraf`, check logs |
| MQTT keepalive failing | Check `ovr-refresh-gx-mqtt-sources` timer |
| Cloud write failing | Check auth, network, Caddy config |
| All systems stale | Check Telegraf, MQTT broker, network |

### 5. Alert fatigue risk

Alerts that fire for expected conditions train operators to ignore them.
Better to have fewer, high-signal alerts than comprehensive low-signal ones.

## Candidate Alerts (not implemented)

### High-value, low-noise

1. **All known systems stale** - No victron_* data from any system in 10m
   - Indicates pipeline-level failure, not individual device
   - Actionable: check Telegraf, MQTT broker

2. **ACUVIM federation down** - `up{job="vm_federate_acuvim"} == 0` for 5m
   - Clear signal, has `up` metric
   - Actionable: check vmagent, network to ACUVIM

3. **Cloud remote_write failing** (if configured)
   - Need to verify current remote_write metrics availability

### Maybe later

- Per-system offline alerts (opt-in, for critical installations)
- Telegraf internal metric monitoring
- Disk space on VM storage

## Implementation Notes

- Grafana alerting requires datasource UID in provisioned rules
- Current Grafana runs on edge at :3000
- Cloud Grafana (if exists) would need separate alert rules

## References

- Old (deleted) branch `feat/3-grafana-alerts` had alerts for the pre-Telegraf pipeline
- Roadmap: `ovr-tele_feature_roadmap_v3.md`
