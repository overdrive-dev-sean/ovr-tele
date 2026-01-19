#!/usr/bin/env bash
set -euo pipefail

echo "== Mounts =="
mountpoint -q /mnt/esata && echo "OK: /mnt/esata mounted" || (echo "FAIL: /mnt/esata not mounted"; exit 1)

echo
echo "== Docker =="
sudo systemctl is-active --quiet docker && echo "OK: docker active" || (echo "FAIL: docker not active"; exit 1)

echo
echo "== Containers =="
sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | egrep 'victoria-metrics|vmagent|telegraf|modbus|grafana' || true

echo
echo "== VictoriaMetrics storage backing =="
sudo docker exec -it victoria-metrics sh -lc 'df -hT /storage; du -sh /storage/data'

echo
echo "== Quick scrape signals =="
VM="http://127.0.0.1:8428"
echo -n "promremotewrite rows total: "
curl -s "$VM/metrics" | awk '/^vm_rows_inserted_total\{type="promremotewrite"/{print $NF; exit}'
echo -n "influx rows total: "
curl -s "$VM/metrics" | awk '/^vm_rows_inserted_total\{type="influx"/{print $NF; exit}'
