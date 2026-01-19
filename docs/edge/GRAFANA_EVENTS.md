# OVR Events + Metrics - Example Grafana Dashboard

This is an example Grafana dashboard configuration showing how to visualize events, locations, and correlated metrics.

## Dashboard Overview

The example dashboard includes:
1. **Event Timeline** - Shows active events as colored regions
2. **Location Changes** - Displays location transitions over time
3. **Event Notes** - Table of annotations and observations
4. **Battery Voltage with Events** - Victron DC voltage overlaid with event markers
5. **Acuvim Power with Events** - Power consumption during events

## Creating Panels

### 1. Event State Timeline

**Panel Type**: State timeline

**Query A** (VictoriaMetrics):
```promql
ovr_event_active{system_id="$system_id"}
```

**Transform**:
- Use labels as fields: `event_id`

**Display**:
- Value mappings:
  - 1 → "Active" (color: green)
  - 0 → "Ended" (color: red/transparent)

### 2. Location Timeline

**Panel Type**: State timeline

**Query A**:
```promql
ovr_event_active{system_id="$system_id"}
```

**Transform**:
- Use labels as fields: `location`

### 3. Event Notes Table

Notes are stored in SQLite and shown in the web UI only (not in VictoriaMetrics, so no PromQL query).

### 4. Battery Voltage with Event Overlay

**Panel Type**: Time series

**Query A** - Voltage:
```promql
victron_dc_voltage_v{system_id="$system_id"}
```

**Query B** - Events (as annotation overlay):
```promql
ovr_event_active{system_id="$system_id"} * 60
```
(Multiply by 60 to scale event regions to voltage range)

**Display**:
- Query A: Line graph
- Query B: Bars (fill opacity 0.2)

**Alternative**: Use Grafana Annotations

### 5. Power During Specific Event

**Panel Type**: Time series

**Query**:
```promql
acuvim_power_total_kw{meter_id=~"$meter"} 
  and on(system_id) 
  ovr_event_active{event_id="$event"} == 1
```

This shows power ONLY during the selected event.

## Dashboard Variables

Create template variables for filtering:

### Variable: system_id
**Type**: Query  
**Query**: 
```promql
label_values(ovr_event_active, system_id)
```
**Multi-value**: false

### Variable: event
**Type**: Query  
**Query**:
```promql
label_values(ovr_event_active{system_id="$system_id"}, event_id)
```
**Multi-value**: true  
**Include All option**: true

### Variable: meter
**Type**: Query  
**Query**:
```promql
label_values(acuvim_power_total_kw, meter_id)
```
**Multi-value**: true

## Annotations

Add automatic event annotations to all panels:

**Name**: Event Changes  
**Datasource**: VictoriaMetrics  
**Query**:
```promql
changes(ovr_event_active{system_id="$system_id"}[1s]) != 0
```

**Mapping**:
- Time field: Time
- Text field: event_id (label)
- Tags field: system_id (label)

**Style**:
- Color: Auto
- Line: Dashed

## Example: Complete Event Analysis Dashboard

### Row 1: Event Overview
- Panel: Event timeline (state timeline)
- Panel: Location timeline (state timeline)

### Row 2: Event Notes & Status
- Panel: Notes table (table)
- Panel: Current status (stat/gauge)

### Row 3: Victron Metrics During Events
- Panel: DC Voltage (time series with event overlay)
- Panel: Battery Current (time series with event overlay)
- Panel: AC Power Out (time series with event overlay)

### Row 4: Acuvim Metrics During Events
- Panel: Total Power (time series)
- Panel: Voltage L1-L3 (time series)
- Panel: Current L1-L3 (time series)

### Row 5: Correlation
- Panel: Voltage Drop vs Event Start (bar chart, aggregated)
- Panel: Event Duration (stat)

## Prometheus/PromQL Examples

### Find all events in last 7 days
```promql
count by (event_id, system_id) (
  changes(ovr_event_active[7d])
)
```

### Average event duration
```promql
avg(
  timestamp(ovr_event_active == 0) - 
  timestamp(ovr_event_active == 1)
) by (event_id, system_id)
```

### Count location changes per system
```promql
count(changes(ovr_event_active{system_id="rig_01"}[30d])) by (system_id, location)
```

### Voltage during events vs baseline
```promql
# During events
avg(victron_dc_voltage_v and on(system_id) ovr_event_active == 1)

# Baseline (no events)
avg(victron_dc_voltage_v unless on(system_id) ovr_event_active == 1)
```

## Alerting Examples

### Alert: Long-running event (>4 hours)
```promql
time() - ovr_event_active * time() > 14400
```

### Alert: Location not set
```promql
ovr_event_active{system_id="rig_01",location="-"} == 1
```

## Import Dashboard JSON

To import a complete dashboard:

1. Create a JSON file with the panels above
2. In Grafana: **Dashboards** → **New** → **Import**
3. Paste JSON or upload file
4. Select VictoriaMetrics datasource
5. Click **Import**

## Tips

1. **Time Range**: Use relative time ranges (e.g., "Last 24 hours") for live monitoring
2. **Auto-refresh**: Set to 10s-30s for real-time event tracking
3. **Shared Crosshair**: Enable to correlate across all panels
4. **Repeated Panels**: Use `system_id` variable to create per-system views
5. **Export Data**: Use table panels with CSV export for event reports

## Further Customization

- Add thresholds for critical voltage/power levels
- Create separate dashboards for event analysis vs live monitoring
- Use panel links to jump between event detail and metric drilldowns
- Configure alerts based on event patterns
