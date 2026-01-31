# Starlark processor to transform Victron MQTT topics into semantic metric names
# Topic format: N/<portal_id>/<service>/<instance>/<path...>
# Example: N/c0619ab68e54/system/0/Dc/InverterCharger/Power
# Output: victron_system_dc_invertercharger_power {instance="0", service="system"}

# ALLOWLIST: Only these path prefixes are kept (derived from map_fast.tsv + map_slow.tsv)
# Paths not matching any prefix are dropped
ALLOWED_PATH_PREFIXES = [
    # === VEBUS SERVICE ===
    # Diagnostics (per device)
    "Devices/",
    # LEDs
    "Leds/",
    # Alarms
    "Alarms/",
    "VebusError",
    # AC In
    "Ac/ActiveIn/",
    # AC Out
    "Ac/Out/",
    # AC State/Config
    "Ac/NumberOfPhases",
    "Ac/PowerMeasurementType",
    "Ac/State/",
    "Ac/Control/",
    # DC
    "Dc/0/",
    # State
    "VebusMainState",
    "VebusChargeState",
    "State",
    "Mode",
    "ModeIsAdjustable",
    # Energy counters
    "Energy/",

    # === SYSTEM SERVICE ===
    # DC Battery/System
    "Dc/Battery/",
    "Dc/InverterCharger/",
    "Dc/System/",
    "Dc/Vebus/",
    # AC from system
    "Ac/Consumption/",
    "Ac/ConsumptionOnOutput/",
    "Ac/Grid/",
    # Relays
    "Relay/",
    # GPS speed from system
    "GpsSpeed",

    # === GPS SERVICE ===
    "Position/",
    "Speed",
    "Altitude",
    "Course",
    "NrOfSatellites",

    # === BATTERY SERVICE ===
    "Soc",
    "Voltage",
    "Current",
    "Power",
    "Temperature",
]

# Service-specific allowlist (only allowed for specific services)
# Format: { "service_name": ["prefix1", "prefix2", ...] }
SERVICE_SPECIFIC_PREFIXES = {
    "vebus": ["Settings/"],
}

def apply(metric):
    # Only process mqtt_consumer metrics
    if not metric.name.startswith("mqtt_consumer"):
        return metric

    topic = metric.tags.get("topic", "")
    if not topic:
        return metric

    # Parse topic: N/<portal_id>/<service>/<instance>/<path...>
    parts = topic.split("/")
    if len(parts) < 5 or parts[0] != "N":
        return metric

    portal_id = parts[1]
    service = parts[2]
    instance = parts[3]
    path_parts = parts[4:]
    path = "/".join(path_parts)

    # Check if path matches any allowed prefix
    allowed = False
    for prefix in ALLOWED_PATH_PREFIXES:
        if path.startswith(prefix):
            allowed = True
            break

    # Check service-specific prefixes if not already allowed
    if not allowed and service in SERVICE_SPECIFIC_PREFIXES:
        for prefix in SERVICE_SPECIFIC_PREFIXES[service]:
            if path.startswith(prefix):
                allowed = True
                break

    if not allowed:
        return None

    # Build metric name: victron_<service>_<path_parts_joined>
    # Convert to lowercase and replace special chars
    path_clean = []
    for p in path_parts:
        # Keep L1, L2, L3 etc as tags instead of in metric name
        if p in ["L1", "L2", "L3"]:
            metric.tags["phase"] = p
        else:
            path_clean.append(p.lower())

    if len(path_clean) == 0:
        return metric

    # Create semantic metric name
    metric_name = "victron_" + service.lower() + "_" + "_".join(path_clean)

    # Clean up metric name (remove consecutive underscores, etc)
    metric_name = metric_name.replace("__", "_").strip("_")

    # Set the new metric name
    metric.name = metric_name

    # Add useful tags
    metric.tags["service"] = service
    metric.tags["instance"] = instance

    # Remove the raw topic tag to reduce cardinality (it's now encoded in metric name)
    metric.tags.pop("topic", None)

    return metric
