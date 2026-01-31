# Cloud MQTT Broker

Mosquitto broker for fleet-wide realtime data distribution.

## Purpose

- Receives bridged summary data from edge nodes
- Serves WebSocket connections to fleet map UI
- Enables near-realtime fleet visibility

## Topics

| Topic Pattern | Publisher | Subscriber | Content |
|---------------|-----------|------------|---------|
| `ovr/<node_id>/realtime` | Edge bridge | Fleet map UI | System summary (SOC, voltage, power, mode) |
| `ovr/<node_id>/events` | Edge bridge | Fleet API | Event lifecycle (start, end, node join) |

## Authentication

### Edge bridges (MQTT)
- Connect to `mqtt://mqtt.overdrive.rocks:8883` (via Caddy TLS)
- Username: `ovr-bridge`
- Password: stored in `/etc/ovr/secrets/mqtt_bridge_password`

### Browser clients (WebSocket)
- Connect to `wss://mqtt.overdrive.rocks/mqtt`
- Anonymous (protected by Cloudflare Access)
- Read-only (ACL enforced)

## Setup

Generate password file:

```bash
# Generate password hash
sudo docker run --rm eclipse-mosquitto mosquitto_passwd -c -b /dev/stdout ovr-bridge 'your-password' > /tmp/passwd

# Move to secrets directory
sudo mv /tmp/passwd /etc/ovr/secrets/mqtt_passwd

# Also store plaintext for edge bridge config
echo 'your-password' | sudo tee /etc/ovr/secrets/mqtt_bridge_password
sudo chmod 600 /etc/ovr/secrets/mqtt_passwd /etc/ovr/secrets/mqtt_bridge_password
```

## Edge Bridge Config

Add to edge `/etc/ovr/mosquitto_bridge.conf`:

```
connection cloud-bridge
address mqtt.overdrive.rocks:8883
remote_username ovr-bridge
remote_password <password>
topic ovr/# out 1
bridge_protocol_version mqttv311
bridge_cafile /etc/ssl/certs/ca-certificates.crt
```
