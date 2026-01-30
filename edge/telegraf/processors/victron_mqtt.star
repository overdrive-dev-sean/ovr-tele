# Starlark processor to transform Victron MQTT topics into semantic metric names
# Topic format: N/<portal_id>/<service>/<instance>/<path...>
# Example: N/c0619ab68e54/system/0/Dc/InverterCharger/Power
# Output: victron_system_dc_invertercharger_power {instance="0", service="system"}

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
