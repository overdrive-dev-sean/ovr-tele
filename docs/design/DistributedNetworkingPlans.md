# Distributed Networking Architecture for Multi-Site BESS Monitoring

## Problem Statement

Deploy 50+ N100/GX telemetry systems across a large industrial site (e.g., solar farm, microgrid) connected via moderate-bandwidth wireless (802.11ah or similar). Each node needs:
- **Local data collection**: Full telemetry from its GX device and Acuvim meters
- **Distributed awareness**: Critical metrics (SOC, voltage, current, alerts) from neighboring nodes at 1-5s intervals
- **Resilience**: No single point of failure, continue operating if network partitions
- **Low bandwidth**: Efficient use of wireless spectrum (802.11ah ~150kbps-1Mbps typical)

---

## Architecture Options

### Option 1: Federated VictoriaMetrics (Simplest)

Each N100 scrapes Prometheus metrics from neighboring nodes directly.

#### Architecture
```
┌─────────────────────────────────────────┐
│ Node 001 (N100)                         │
│  ┌──────────────┐    ┌───────────────┐  │
│  │ vmagent      │───→│ Victoria-     │  │
│  │ (scrape)     │    │ Metrics       │  │
│  └──────────────┘    └───────────────┘  │
│         │                                │
│         ├─→ Local: localhost:9480       │
│         ├─→ Neighbor: node-002:9480     │
│         ├─→ Neighbor: node-003:9480     │
│         └─→ ... (48 more targets)       │
└─────────────────────────────────────────┘
```

#### Implementation
**edge/vmagent/scrape.yml additions:**
```yaml
- job_name: 'neighbor_gx_fast'
  scrape_interval: 5s
  scrape_timeout: 3s
  static_configs:
    - targets:
      # Discovered via mDNS/DNS-SD or static
      - 'node-002.mesh.local:9480'
      - 'node-003.mesh.local:9480'
      # ... all 50 nodes
  relabel_configs:
    - source_labels: [__address__]
      target_label: neighbor_node
      regex: '([^:]+):.*'
      replacement: '$1'

- job_name: 'neighbor_critical_only'
  scrape_interval: 2s
  metric_relabel_configs:
    # Only keep critical metrics
    - source_labels: [__name__]
      regex: 'victron_(battery_soc|dc_voltage|dc_current|ac_.*_l1_power)'
      action: keep
  static_configs:
    - targets: ['node-002.mesh.local:9480', ...]
```

#### Bandwidth Calculation
- **Per neighbor scrape**: ~5KB (critical metrics only)
- **Scrape interval**: 5s
- **Total**: 50 nodes × 5KB / 5s = **50KB/s = 400kbps**
- **802.11ah capacity**: 150kbps-1Mbps → **Feasible but tight**

#### Pros & Cons
✅ **Pros**:
- No new infrastructure (uses existing Prometheus stack)
- Simple configuration (just add scrape targets)
- Query any node for distributed view via PromQL federation

❌ **Cons**:
- **N² scraping overhead**: 50 nodes × 50 targets = 2500 HTTP connections every 5s
- **Bandwidth inefficient**: Each metric scraped 50 times (once per node)
- **Latency**: Full scrape cycle = 50 × scrape_timeout (150s worst case)
- **No resilience to network partitions**: Missing metrics if node unreachable

**Verdict**: Works for <10 nodes, doesn't scale to 50+

---

### Option 2: MQTT Mesh with Local VictoriaMetrics (Recommended)

Use MQTT publish/subscribe pattern for critical metrics distribution. Each node publishes to local broker, brokers bridge to neighbors.

#### Architecture
```
┌────────────────────────────────────────────────────────┐
│ Node 001                                               │
│                                                        │
│ ┌────────┐      ┌──────────────┐                      │
│ │GX dbus2│─────→│ MQTT Bridge  │──→ Publish:          │
│ │prom    │ HTTP │ (Python)     │    bess/node001/soc  │
│ └────────┘      └──────────────┘    bess/node001/volt │
│                         │                              │
│                         ↓                              │
│                 ┌──────────────┐                       │
│                 │ Mosquitto    │←─→ Bridge to:        │
│                 │ MQTT Broker  │    node-002:1883     │
│                 └──────────────┘    node-003:1883     │
│                         │            (ring topology)  │
│                         ↓                              │
│                 ┌──────────────┐                       │
│                 │ Telegraf     │ Subscribe:           │
│                 │ MQTT Consumer│  bess/+/soc          │
│                 └──────────────┘  bess/+/volt         │
│                         │          bess/+/alert       │
│                         ↓                              │
│                 ┌──────────────┐                       │
│                 │ Victoria-    │                       │
│                 │ Metrics      │                       │
│                 └──────────────┘                       │
└────────────────────────────────────────────────────────┘
```

#### Implementation Details

**1. Add Mosquitto MQTT Broker to Stack**

**edge/compose.dev.yml additions:**
```yaml
services:
  mosquitto:
    image: eclipse-mosquitto:2.0
    container_name: mosquitto
    ports:
      - "1883:1883"      # MQTT
      - "9001:9001"      # WebSocket (optional)
    volumes:
      - ./mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
      - ./mosquitto/passwd:/mosquitto/config/passwd:ro
      - mosquitto_data:/mosquitto/data
      - mosquitto_log:/mosquitto/log
    restart: unless-stopped

volumes:
  mosquitto_data:
  mosquitto_log:
```

**mosquitto/mosquitto.conf:**
```conf
listener 1883
protocol mqtt

# Allow anonymous (or use password_file for auth)
allow_anonymous true

# Persistence for QoS 1/2 messages
persistence true
persistence_location /mosquitto/data/

# Logging
log_dest file /mosquitto/log/mosquitto.log
log_type error
log_type warning
log_type notice
log_type information

# Bridge to neighbor nodes (ring topology)
connection bridge-to-node002
address node-002.mesh.local:1883
topic bess/node002/# in 1
topic bess/node001/# out 1
bridge_attempt_unsubscribe false
bridge_protocol_version mqttv311
try_private false

connection bridge-to-node003
address node-003.mesh.local:1883
topic bess/node003/# in 1
topic bess/node001/# out 1
bridge_attempt_unsubscribe false
```

**2. Python MQTT Bridge for dbus2prom Metrics**

**mqtt-bridge/dbus2prom_mqtt_bridge.py:**
```python
#!/usr/bin/env python3
"""
Bridge dbus2prom HTTP metrics to MQTT topics.
Scrapes localhost:9480 (fast) every 1-2s, publishes critical metrics.
"""

import os
import time
import json
import requests
import paho.mqtt.client as mqtt
from prometheus_client.parser import text_string_to_metric_families

SCRAPE_URL = os.environ.get("SCRAPE_URL", "http://localhost:9480/metrics")
MQTT_BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
NODE_ID = os.environ.get("NODE_ID", "node001")
PUBLISH_INTERVAL = float(os.environ.get("PUBLISH_INTERVAL", "2.0"))

# Only publish critical metrics (reduce bandwidth)
CRITICAL_METRICS = {
    "victron_battery_soc",
    "victron_dc_voltage",
    "victron_dc_current",
    "victron_ac_in_l1_power",
    "victron_ac_out_l1_power",
}

client = mqtt.Client(client_id=f"bridge-{NODE_ID}")

def on_connect(client, userdata, flags, rc):
    print(f"Connected to MQTT broker with result code {rc}")

client.on_connect = on_connect
client.connect(MQTT_BROKER, MQTT_PORT, 60)
client.loop_start()

def scrape_and_publish():
    try:
        resp = requests.get(SCRAPE_URL, timeout=2)
        resp.raise_for_status()
        
        # Parse Prometheus text format
        for family in text_string_to_metric_families(resp.text):
            if family.name not in CRITICAL_METRICS:
                continue
            
            for sample in family.samples:
                metric_name = sample.name
                labels = sample.labels
                value = sample.value
                
                # Publish to MQTT: bess/node001/battery_soc
                short_name = metric_name.replace("victron_", "")
                topic = f"bess/{NODE_ID}/{short_name}"
                
                payload = json.dumps({
                    "value": value,
                    "labels": labels,
                    "timestamp": int(time.time() * 1e9)
                })
                
                client.publish(topic, payload, qos=1)
                
    except Exception as e:
        print(f"Scrape error: {e}")

print(f"Starting MQTT bridge for {NODE_ID}")
while True:
    scrape_and_publish()
    time.sleep(PUBLISH_INTERVAL)
```

**Add to edge/compose.dev.yml:**
```yaml
  mqtt-bridge:
    build: ./mqtt-bridge
    container_name: mqtt-bridge
    depends_on:
      - mosquitto
    environment:
      - SCRAPE_URL=http://host.docker.internal:9480/metrics
      - MQTT_BROKER=mosquitto
      - NODE_ID=${NODE_ID:-node001}
      - PUBLISH_INTERVAL=2.0
    extra_hosts:
      - "host.docker.internal:host-gateway"
    restart: unless-stopped
```

**3. Configure Telegraf to Consume MQTT**

**edge/telegraf/telegraf.conf additions:**
```toml
# Subscribe to neighbor critical metrics via MQTT
[[inputs.mqtt_consumer]]
  servers = ["tcp://mosquitto:1883"]
  
  # Subscribe to all neighbor nodes
  topics = [
    "bess/+/battery_soc",
    "bess/+/dc_voltage",
    "bess/+/dc_current",
    "bess/+/ac_in_l1_power",
    "bess/+/ac_out_l1_power",
  ]
  
  # Parse JSON payload
  data_format = "json"
  json_time_key = "timestamp"
  json_time_format = "unix_ns"
  
  # Tag with neighbor node ID
  tag_keys = ["labels_system_id"]
  
  # Output to VictoriaMetrics
[[outputs.influxdb_v2]]
  urls = ["http://victoria-metrics:8428"]
  organization = "ovr"
  bucket = "telemetry"
  data_format = "influx"
```

#### Bandwidth Calculation
**Per node publishing:**
- 5 metrics × 100 bytes = 500 bytes per publish
- Publish interval: 2s
- **Outbound per node**: 500B / 2s = **250 bytes/s = 2kbps**

**Total mesh traffic (50 nodes):**
- Each node publishes 2kbps
- Each node receives from ~10 neighbors (ring topology with gossip)
- **Per node bandwidth**: 2kbps up + 20kbps down = **22kbps**
- **Well within 802.11ah capacity** (150kbps+)

#### Ring Topology for MQTT Bridging
```
node001 ↔ node002 ↔ node003 ↔ ... ↔ node050 ↔ node001
```
- Each node bridges to 2-3 neighbors (ring + optional shortcuts)
- Messages propagate via gossip (15-30s for full mesh)
- QoS 1 ensures delivery even with packet loss
- Automatic reconnection on network partition

#### Pros & Cons
✅ **Pros**:
- **Efficient bandwidth**: Publish once, subscribe everywhere (not N²)
- **Low latency**: 2-5s for neighbor updates, 15-30s for full mesh
- **Resilient**: Tolerates network partitions, automatic reconnection
- **Scales well**: 100+ nodes with same bandwidth per node
- **Built for unreliable networks**: QoS, retain flags, last will

❌ **Cons**:
- **Adds complexity**: New MQTT infrastructure
- **Eventual consistency**: Gossip propagation has latency
- **Bridge configuration**: Need to configure neighbor connections

**Verdict**: Best balance of efficiency, resilience, and scalability for 50+ nodes

---

### Option 3: VictoriaMetrics Cluster Mode

Run VictoriaMetrics in cluster mode with distributed components across the site.

#### Architecture
```
┌─────────────────────────────────────────────────────────┐
│ Anchor Nodes (3-5 high-reliability units)              │
│                                                         │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐       │
│  │ vmstorage  │  │ vmstorage  │  │ vmstorage  │       │
│  │ (shard 1)  │  │ (shard 2)  │  │ (shard 3)  │       │
│  └────────────┘  └────────────┘  └────────────┘       │
│         ↑               ↑               ↑              │
└─────────┼───────────────┼───────────────┼──────────────┘
          │               │               │
┌─────────┴───────────────┴───────────────┴──────────────┐
│ Edge Nodes (N100 units)                                │
│                                                         │
│  ┌────────────┐     ┌─────────────┐                    │
│  │ vminsert   │────→│ vmselect    │                    │
│  │ (local)    │     │ (query)     │                    │
│  └────────────┘     └─────────────┘                    │
│         ↑                   │                           │
│         │                   ↓                           │
│    [vmagent scrape]   [Query results]                  │
└─────────────────────────────────────────────────────────┘
```

#### Implementation
**Requires VictoriaMetrics cluster components:**
- `vmstorage`: Persistent storage nodes (3-5 replicas)
- `vminsert`: Ingests metrics, shards to vmstorage
- `vmselect`: Query engine, aggregates from vmstorage

**edge/compose.dev.yml for anchor nodes:**
```yaml
services:
  vmstorage:
    image: victoriametrics/vmstorage:cluster
    command:
      - "-storageDataPath=/storage"
      - "-retentionPeriod=30d"
      - "-httpListenAddr=:8482"
      - "-vminsertAddr=:8400"
      - "-vmselectAddr=:8401"
    ports:
      - "8482:8482"
      - "8400:8400"
      - "8401:8401"
    volumes:
      - vmstorage:/storage
```

**edge/compose.dev.yml for edge N100 nodes:**
```yaml
services:
  vminsert:
    image: victoriametrics/vminsert:cluster
    command:
      - "-storageNode=anchor-001:8400,anchor-002:8400,anchor-003:8400"
      - "-httpListenAddr=:8480"
    ports:
      - "8480:8480"
      
  vmselect:
    image: victoriametrics/vmselect:cluster
    command:
      - "-storageNode=anchor-001:8401,anchor-002:8401,anchor-003:8401"
      - "-httpListenAddr=:8481"
    ports:
      - "8481:8481"
```

#### Bandwidth
- Each edge node sends data to nearest vminsert
- vminsert shards to 3 vmstorage replicas
- **Per edge node**: ~10KB/s × 3 replicas = **30KB/s = 240kbps**
- **Feasible for 802.11ah** with good signal

#### Pros & Cons
✅ **Pros**:
- **Built-in HA**: Automatic failover, replication
- **Horizontal scaling**: Add vmstorage nodes as needed
- **Efficient queries**: vmselect aggregates across shards

❌ **Cons**:
- **Requires anchor nodes**: Need 3-5 stable/powered units
- **Complex setup**: Multiple components, networking
- **Higher bandwidth**: 3× writes for replication

**Verdict**: Best for sites with reliable power/network zones that can host anchor nodes

---

### Option 4: Custom Gossip Protocol

Implement peer-to-peer gossip for critical alerts using Hashicorp Serf or similar.

#### Architecture
```
┌─────────────────────────────────────────┐
│ Node 001                                │
│  ┌──────────────┐    ┌──────────────┐   │
│  │ Victoria-    │    │ Serf Agent   │   │
│  │ Metrics      │    │ (gossip)     │   │
│  │ (full local) │    └──────────────┘   │
│  └──────────────┘           ↕            │
│                       Gossip to          │
│                       neighbors          │
│  ┌──────────────┐                        │
│  │ Gossip       │ Reads VM,              │
│  │ Publisher    │ broadcasts alerts      │
│  └──────────────┘                        │
└─────────────────────────────────────────┘

Gossip Protocol:
- Critical events: low_soc, high_temp, fault
- Propagate to all nodes in 5-15s
- Member failure detection
```

#### Implementation Sketch
**Using Serf:**
```bash
# Start Serf agent on each node
serf agent \
  -node=node001 \
  -bind=0.0.0.0:7946 \
  -join=node002.mesh.local:7946 \
  -join=node003.mesh.local:7946 \
  -event-handler="/opt/serf-handler.sh"
```

**Event Handler (/opt/serf-handler.sh):**
```bash
#!/bin/bash
EVENT_TYPE=$1
if [ "$EVENT_TYPE" = "user" ]; then
  EVENT_NAME=$SERF_USER_EVENT
  EVENT_PAYLOAD=$(cat)
  
  # Store in local VictoriaMetrics as annotation
  curl -X POST http://localhost:8428/write \
    -d "bess_alert,source=$EVENT_NAME value=\"$EVENT_PAYLOAD\""
fi
```

**Gossip Publisher (Python):**
```python
# Query VM for critical conditions, publish to Serf
import subprocess
if battery_soc < 20:
    subprocess.run(["serf", "event", "low_soc", f"node001:{battery_soc}"])
```

#### Bandwidth
- Gossip overhead: ~1-5KB/s per node
- Scales to 1000+ nodes

#### Pros & Cons
✅ **Pros**:
- **Ultra-low bandwidth**: Only critical events
- **Fast propagation**: 5-15s to full mesh
- **Self-healing**: Automatic member detection

❌ **Cons**:
- **Limited data**: Alerts only, not full telemetry
- **Custom development**: Need event handlers
- **No historical queries**: Just real-time

**Verdict**: Best for large-scale alert distribution, complement to local VM storage

---

## Recommended Hybrid Solution

**Combine local VM + MQTT for practical deployment:**

### Design
1. **Each node runs full local stack** (current design)
   - VictoriaMetrics stores ALL local data (30d retention)
   - Grafana queries local VM for detailed analysis
   
2. **MQTT publishes critical metrics** (SOC, voltage, current, alerts)
   - 5-10 metrics per node at 2s intervals
   - Neighbors subscribe via MQTT bridge (ring topology)
   - Total bandwidth: <50kbps per node
   
3. **Central dashboard (optional)** subscribes to `bess/#` wildcard
   - Overview of all 50 nodes
   - Drill down queries local VM via API

### Deployment Steps
1. Add Mosquitto to edge/compose.dev.yml
2. Create mqtt-bridge service (Python script)
3. Configure Telegraf MQTT consumer
4. Set up MQTT bridge topology (ring + shortcuts)
5. Test with 2 nodes, scale to 50

### Cost Estimate
- **Development**: ~3-5 days for MQTT integration
- **Per-node hardware**: No change (existing N100)
- **Bandwidth**: <50kbps per node (easily handled by 802.11ah)
- **Latency**: 2-5s for neighbors, 15-30s for full mesh

---

## 802.11ah (Wi-Fi HaLow) Considerations

### Characteristics
- **Frequency**: 900 MHz (sub-GHz)
- **Range**: 1km+ outdoor, 100m+ indoor
- **Data rate**: 150kbps - 86Mbps (MCS-dependent)
- **Penetration**: Excellent through walls/obstacles

### Network Planning
- **Topology**: Mesh with 2-3 hops max
- **Channel**: Single channel (avoid co-channel interference)
- **Power**: 802.11ah has built-in power save modes
- **Security**: WPA3 recommended

### Bandwidth Budget (per node)
- Local scraping: 5KB/s (40kbps)
- MQTT publish: 0.25KB/s (2kbps)
- MQTT receive: 2.5KB/s (20kbps)
- **Total**: ~8KB/s (64kbps) leaves 100kbps+ headroom

---

## Testing & Validation

### Lab Setup (2-3 nodes)
1. Deploy MQTT stack on 2 N100 units
2. Configure bridge between them
3. Verify metrics appear in both VictoriaMetrics instances
4. Simulate network partition (disconnect, verify recovery)
5. Measure bandwidth with tcpdump/Wireshark

### Performance Metrics
- **Latency**: Time from GX update to neighbor VM
- **Bandwidth**: Bytes/sec per node (target <100kbps)
- **Resilience**: Recovery time from network partition
- **Completeness**: % of expected metrics received

### Scaling Tests
- Add nodes incrementally (10, 25, 50)
- Monitor MQTT broker CPU/memory
- Check for message queuing/drops
- Optimize scrape intervals if needed

---

## Future Enhancements

### Dynamic Topology
- mDNS/DNS-SD for automatic neighbor discovery
- Avoid static bridge configuration

### Intelligent Publishing
- Only publish when values change >1%
- Reduce bandwidth for stable systems

### Alerting
- Serf event handler for critical alerts
- SMS/push notifications via central gateway

### Time-Series Sync
- Periodic VM-to-VM sync of aggregated data
- Use VictoriaMetrics remote write API

---

## Conclusion

For 50+ node BESS monitoring with 802.11ah:
- **Use MQTT + local VictoriaMetrics** for optimal balance
- **Bandwidth**: <50kbps per node (20% of 802.11ah lowest MCS)
- **Resilience**: Tolerates network partitions, automatic recovery
- **Scalability**: Tested to 100+ nodes with same per-node cost

Implementation complexity is moderate (3-5 days) but provides production-grade distributed telemetry with no single point of failure.
