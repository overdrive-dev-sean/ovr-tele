#!/usr/bin/env bash
# Example API calls for OVR Event Service
# Demonstrates complete event lifecycle

set -euo pipefail

# Configuration
EVENT_SERVICE_URL="${EVENT_SERVICE_URL:-http://localhost:8088}"
API_KEY="${API_KEY:-}"  # Set if authentication is enabled

# Helper function to make API calls
api_call() {
    local method="$1"
    local endpoint="$2"
    local data="${3:-}"
    
    local headers=("-H" "Content-Type: application/json")
    
    if [ -n "$API_KEY" ]; then
        headers+=("-H" "X-API-Key: $API_KEY")
    fi
    
    if [ "$method" = "GET" ]; then
        curl -s -X "$method" "$EVENT_SERVICE_URL$endpoint" "${headers[@]}"
    else
        curl -s -X "$method" "$EVENT_SERVICE_URL$endpoint" "${headers[@]}" -d "$data"
    fi
}

echo "=== OVR Event Service API Examples ==="
echo "Service URL: $EVENT_SERVICE_URL"
echo ""

# 1. Health Check
echo "1. Health Check"
api_call GET /health | jq .
echo ""

# 2. Start an event with location and note
echo "2. Starting event: startup at warehouse"
api_call POST /api/event/start '{
  "system_id": "rig_demo_01",
  "event_id": "startup_test",
  "location": "warehouse",
  "note": "Initial power-on test - all systems nominal"
}' | jq .
echo ""

sleep 2

# 3. Add a note during the event
echo "3. Adding note to active event"
api_call POST /api/note '{
  "system_id": "rig_demo_01",
  "msg": "Battery voltage holding steady at 51.2V"
}' | jq .
echo ""

sleep 1

# 4. Change location while event is active
echo "4. Moving to field site"
api_call POST /api/location/set '{
  "system_id": "rig_demo_01",
  "location": "field_site_a"
}' | jq .
echo ""

sleep 2

# 5. Add another note
echo "5. Adding field observation"
api_call POST /api/note '{
  "system_id": "rig_demo_01",
  "msg": "Ambient temp: 28C, all breakers closed"
}' | jq .
echo ""

sleep 1

# 6. Check status
echo "6. Checking current status"
api_call GET "/api/status?system_id=rig_demo_01" | jq .
echo ""

# 7. Start a second system's event
echo "7. Starting event on second system"
api_call POST /api/event/start '{
  "system_id": "rig_demo_02",
  "event_id": "load_test",
  "location": "workshop"
}' | jq .
echo ""

sleep 1

# 8. Check status for all systems
echo "8. Checking all systems status"
api_call GET /api/status | jq '.active_events, .locations | length'
echo ""

# 9. End first system's event
echo "9. Ending startup test on rig_demo_01"
api_call POST /api/event/end '{
  "system_id": "rig_demo_01",
  "event_id": "startup_test"
}' | jq .
echo ""

sleep 1

# 10. End second system's event (without specifying event_id)
echo "10. Ending active event on rig_demo_02 (auto-detect)"
api_call POST /api/event/end '{
  "system_id": "rig_demo_02"
}' | jq .
echo ""

# 11. Final status check
echo "11. Final status check (should show no active events)"
api_call GET /api/status | jq '.active_events | length'
echo ""

echo "=== Complete! ==="
echo ""
echo "To verify data in VictoriaMetrics:"
echo "  curl -s 'http://localhost:8428/api/v1/query?query=ovr_event_active' | jq ."
echo "  curl -s 'http://localhost:8428/api/v1/query?query=ovr_event_active{system_id=\"rig_demo_01\"}' | jq ."
echo ""
echo "To view notes (stored in SQLite):"
echo "  curl -s \"$EVENT_SERVICE_URL/api/notes?event_id=startup_test\" | jq ."
echo ""
echo "To visualize in Grafana:"
echo "  1. Add panel with query: ovr_event_active{system_id=\"rig_demo_01\"}"
echo "  2. Panel type: State timeline"
echo "  3. Value mappings: 1 = Active, 0 = Ended"
