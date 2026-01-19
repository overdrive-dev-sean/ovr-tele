import { useEffect, useMemo, useRef, useState } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';

const API_BASE = '/api';
const POLL_INTERVALS = [15, 30, 60, 120, 300, 600, 1200, 1800];
const MOVE_THRESHOLD_METERS = 20;
const EVENT_STALE_SECONDS = 300;
const MAP_USAGE_STORAGE_KEY = 'ovr_fleet_map_usage_v1';
const MAP_PROVIDER_STORAGE_KEY = 'ovr_fleet_map_provider_v1';
const MAP_STATUS_POLL_MS = 15000;
const PLACEHOLDER_DEPLOYMENT_IDS = new Set(['__DEPLOYMENT_ID__', '__DEPLOYEMENT_ID__']);

const isPlaceholderDeployment = (value) => {
  if (!value) return true;
  const trimmed = String(value).trim();
  if (!trimmed) return true;
  if (PLACEHOLDER_DEPLOYMENT_IDS.has(trimmed)) return true;
  return trimmed.startsWith('__DEPLOYMENT') || trimmed.startsWith('__DEPLOYEMENT');
};

const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

const toRad = (deg) => (deg * Math.PI) / 180;

const haversineMeters = (a, b) => {
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lon - a.lon);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const sinLat = Math.sin(dLat / 2);
  const sinLon = Math.sin(dLon / 2);
  const h = sinLat * sinLat + Math.cos(lat1) * Math.cos(lat2) * sinLon * sinLon;
  return 2 * 6371000 * Math.asin(Math.sqrt(h));
};

const escapeHtml = (value) =>
  String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/\"/g, '&quot;')
    .replace(/'/g, '&#39;');

const formatPercent = (value) => {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return `${Number(value).toFixed(1)}%`;
};

const formatPower = (value) => {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  const num = Number(value);
  const abs = Math.abs(num);
  if (abs >= 1000) {
    return `${(num / 1000).toFixed(2)} kW`;
  }
  return `${num.toFixed(0)} W`;
};

const formatVoltage = (value) => {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return `${Number(value).toFixed(1)} V`;
};

const formatCurrent = (value) => {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return `${Number(value).toFixed(1)} A`;
};

const formatAge = (seconds) => {
  if (!seconds && seconds !== 0) return '--';
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return `${hours}h ago`;
};

const formatLoggerListText = (loggers, limit) => {
  if (!loggers || loggers.length === 0) return '--';
  const items = loggers.map((item) => String(item));
  if (!limit || items.length <= limit) return items.join(', ');
  const trimmed = items.slice(0, limit);
  const remaining = items.length - trimmed.length;
  return `${trimmed.join(', ')} +${remaining} more`;
};

const formatLoggerListHtml = (loggers, limit) => {
  if (!loggers || loggers.length === 0) return '--';
  const safe = loggers.map((item) => escapeHtml(item));
  return formatLoggerListText(safe, limit);
};

const monthKeyUtc = () => new Date().toISOString().slice(0, 7);

const buildEmptyUsage = (monthKey) => ({
  monthKey,
  counts: { mapbox: 0, esri: 0 }
});

const loadMapUsage = () => {
  const monthKey = monthKeyUtc();
  try {
    const raw = window.localStorage.getItem(MAP_USAGE_STORAGE_KEY);
    if (!raw) return buildEmptyUsage(monthKey);
    const parsed = JSON.parse(raw);
    if (!parsed || parsed.monthKey !== monthKey) return buildEmptyUsage(monthKey);
    return {
      monthKey,
      counts: {
        mapbox: Number(parsed.counts?.mapbox || 0),
        esri: Number(parsed.counts?.esri || 0)
      }
    };
  } catch (err) {
    return buildEmptyUsage(monthKey);
  }
};

const persistMapUsage = (usage) => {
  try {
    window.localStorage.setItem(MAP_USAGE_STORAGE_KEY, JSON.stringify(usage));
  } catch (err) {
    // Ignore localStorage failures.
  }
};

const formatCount = (value) => {
  const num = Number(value || 0);
  if (!Number.isFinite(num)) return '--';
  return num.toLocaleString();
};

const formatMonthKey = (value) => {
  if (!value) return 'this month';
  const [year, month] = String(value).split('-').map((part) => Number(part));
  if (!year || !month) return value;
  const date = new Date(Date.UTC(year, month - 1, 1));
  return date.toLocaleString(undefined, { month: 'long', year: 'numeric' });
};

const formatReportTimestamp = (value) => {
  const ms = Number(value) / 1e6;
  if (!Number.isFinite(ms) || ms <= 0) return '--';
  return new Date(ms).toLocaleString();
};

const buildNodesUrl = (deploymentIds, eventId) => {
  const params = new URLSearchParams();
  if (deploymentIds && deploymentIds.length) {
    deploymentIds.forEach((id) => params.append('deployment_id', id));
  }
  if (eventId) {
    params.append('event_id', eventId);
  }
  if ([...params.keys()].length === 0) {
    return `${API_BASE}/nodes`;
  }
  return `${API_BASE}/nodes?${params.toString()}`;
};

const buildEventsUrl = (deploymentIds) => {
  const params = new URLSearchParams();
  if (deploymentIds && deploymentIds.length) {
    deploymentIds.forEach((id) => params.append('deployment_id', id));
  }
  if ([...params.keys()].length === 0) {
    return `${API_BASE}/events`;
  }
  return `${API_BASE}/events?${params.toString()}`;
};

const getStatusClass = (node) => {
  if (node.alerts_count > 0) return 'alert';
  if (node.soc === null || node.soc === undefined) return 'neutral';
  if (node.soc < 25) return 'bad';
  if (node.soc < 40) return 'warn';
  return 'ok';
};

const STATUS_RANK = {
  neutral: 0,
  ok: 1,
  warn: 2,
  bad: 3,
  alert: 4
};

const getGroupStatus = (nodes) => {
  if (!nodes || nodes.length === 0) return 'neutral';
  let best = 'neutral';
  nodes.forEach((node) => {
    const status = getStatusClass(node);
    if (STATUS_RANK[status] > STATUS_RANK[best]) {
      best = status;
    }
  });
  return best;
};

const getGroupNodes = (group) => {
  if (!group) return [];
  const ordered = [];
  const seen = new Set();
  const pushNode = (node) => {
    if (!node || seen.has(node.system_id)) return;
    seen.add(node.system_id);
    ordered.push(node);
  };
  if (group.host) {
    pushNode(group.host);
  }
  if (Array.isArray(group.loggers)) {
    group.loggers.forEach(pushNode);
  }
  if (ordered.length === 0 && Array.isArray(group.nodes)) {
    group.nodes.forEach(pushNode);
  }
  if (ordered.length === 0 && group.primary) {
    pushNode(group.primary);
  }
  return ordered;
};

const getGroupEventNode = (group) => {
  if (!group || !Array.isArray(group.nodes)) return null;
  let best = null;
  let bestTs = -1;
  group.nodes.forEach((node) => {
    if (!node?.event_id) return;
    const ts = Number(node.event_updated_at || 0);
    if (ts > bestTs) {
      best = node;
      bestTs = ts;
    }
  });
  return best;
};

const buildGroupMarkerSize = (rows) => {
  const rowCount = Math.max(1, rows);
  const width = 190;
  const height = 46 + rowCount * 18;
  return { width, height };
};

const buildGroupMarkerHtml = (group, groupNodes, eventGroup, eventIdOverride) => {
  const status = getGroupStatus(groupNodes);
  const labelSource = group.primary || group.mapNode || groupNodes[0];
  const label = escapeHtml(labelSource?.system_id || group.key);
  const alertCount = groupNodes.reduce(
    (total, node) => total + (node.alerts_count || 0),
    0
  );
  const alerts = alertCount > 0 ? `!${alertCount}` : '';
  const eventIdRaw = eventIdOverride || labelSource?.event_id;
  const eventId = eventIdRaw ? escapeHtml(eventIdRaw) : '';
  const eventCount = eventGroup?.count ? ` (${eventGroup.count})` : '';
  const eventTag = eventId
    ? `<div class="marker-event" title="${eventId}${eventCount}">${eventId}${eventCount}</div>`
    : '';
  const eventClass = eventId ? 'marker-has-event' : '';
  const source = labelSource?.gps_source === 'manual' ? 'Manual' : 'GPS';
  const rows = groupNodes
    .map((node) => {
      const soc = formatPercent(node.soc);
      const pout = formatPower(node.pout);
      return `
        <div class="marker-group-row">
          <span class="marker-group-id">${escapeHtml(node.system_id)}</span>
          <span class="marker-group-metric">SOC ${soc}</span>
          <span class="marker-group-metric">P<sub>out</sub> ${pout}</span>
        </div>
      `;
    })
    .join('');

  return `
    <div class="marker marker-group marker-${status} ${labelSource?.gps_source === 'manual' ? 'marker-manual' : ''} ${eventClass}">
      <div class="marker-header">
        <span class="marker-id">${label}</span>
        <span class="marker-alerts">${alerts}</span>
      </div>
      ${eventTag}
      <div class="marker-group-list">
        ${rows}
      </div>
      <div class="marker-source">${source}</div>
    </div>
  `;
};

const offsetLatLng = (lat, lon, dxMeters, dyMeters) => {
  const metersPerDegree = 111320;
  const deltaLat = dyMeters / metersPerDegree;
  const deltaLon =
    dxMeters / (metersPerDegree * Math.cos(toRad(lat)) || metersPerDegree);
  return { lat: lat + deltaLat, lon: lon + deltaLon };
};

const buildSpiderfyPositions = (center, count) => {
  if (!center || count <= 1) return [center];
  const radius = 24 + Math.min(count, 6) * 6;
  const positions = [];
  for (let i = 0; i < count; i += 1) {
    const angle = (Math.PI * 2 * i) / count;
    const dx = radius * Math.cos(angle);
    const dy = radius * Math.sin(angle);
    positions.push(offsetLatLng(center.lat, center.lon, dx, dy));
  }
  return positions;
};

const buildMarkerHtml = (node, eventLoggers) => {
  const status = getStatusClass(node);
  const label = escapeHtml(node.system_id);
  const isLogger = Boolean(node.is_logger);
  const hasAcuvim = isLogger && node.acuvim_updated_at !== null && node.acuvim_updated_at !== undefined;
  const leftMetric = isLogger
    ? hasAcuvim
      ? formatVoltage(node.acuvim_vavg)
      : '--'
    : formatPercent(node.soc);
  const rightMetric = isLogger
    ? hasAcuvim
      ? formatPower(node.acuvim_p)
      : '--'
    : formatPower(node.pout);
  const alerts = node.alerts_count > 0 ? `!${node.alerts_count}` : '';
  const eventId = node.event_id ? escapeHtml(node.event_id) : '';
  const eventCount = eventLoggers && eventLoggers.length ? ` (${eventLoggers.length})` : '';
  const eventTag = eventId
    ? `<div class="marker-event" title="${eventId}${eventCount}">${eventId}${eventCount}</div>`
    : '';
  const eventClass = eventId ? 'marker-has-event' : '';
  const source = node.gps_source === 'manual' ? 'Manual' : 'GPS';

  return `
    <div class="marker marker-${status} ${node.gps_source === 'manual' ? 'marker-manual' : ''} ${eventClass}">
      <div class="marker-header">
        <span class="marker-id">${label}</span>
        <span class="marker-alerts">${alerts}</span>
      </div>
      ${eventTag}
      <div class="marker-metrics">
        <span>${leftMetric}</span>
        <span>${rightMetric}</span>
      </div>
      <div class="marker-source">${source}</div>
    </div>
  `;
};

const buildPopupHtml = (node, eventDetails, eventIdOverride) => {
  const location = node.location ? escapeHtml(node.location) : '--';
  const alerts = node.alerts && node.alerts.length ? escapeHtml(node.alerts.join(', ')) : 'None';
  const rawEventId = eventIdOverride || node.event_id;
  const eventId = rawEventId ? escapeHtml(rawEventId) : '--';
  const isLogger = Boolean(node.is_logger);
  const hasAcuvim = isLogger && node.acuvim_updated_at !== null && node.acuvim_updated_at !== undefined;
  const nodeUrl = node.node_url
    ? `<a href="${escapeHtml(node.node_url)}" target="_blank" rel="noreferrer">Open node UI</a>`
    : '';
  return `
    <div class="popup">
      <div class="popup-title">${escapeHtml(node.system_id)}</div>
      <div class="popup-row"><strong>Location:</strong> ${location}</div>
      <div class="popup-row"><strong>Event:</strong> ${eventId}</div>
      ${
        rawEventId
          ? `<div class="popup-row"><strong>Event Loggers:</strong> ${eventDetails || '--'}</div>`
          : ''
      }
      ${
        isLogger
          ? `<div class="popup-row"><strong>Vavg:</strong> ${hasAcuvim ? formatVoltage(node.acuvim_vavg) : '--'}</div>
             <div class="popup-row"><strong>Iavg:</strong> ${hasAcuvim ? formatCurrent(node.acuvim_iavg) : '--'}</div>
             <div class="popup-row"><strong>P:</strong> ${hasAcuvim ? formatPower(node.acuvim_p) : '--'}</div>`
          : `<div class="popup-row"><strong>SOC:</strong> ${formatPercent(node.soc)}</div>
             <div class="popup-row"><strong>Pout:</strong> ${formatPower(node.pout)}</div>`
      }
      <div class="popup-row"><strong>Alerts:</strong> ${alerts}</div>
      <div class="popup-row"><strong>GPS:</strong> ${node.gps_source === 'manual' ? 'manual' : 'gps'} (${formatAge(node.gps_age_sec)})</div>
      ${nodeUrl ? `<div class="popup-row">${nodeUrl}</div>` : ''}
    </div>
  `;
};

export default function App() {
  const [nodes, setNodes] = useState([]);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [pollIndex, setPollIndex] = useState(0);
  const [statusMessage, setStatusMessage] = useState('');
  const [placementTarget, setPlacementTarget] = useState(null);
  const [deployments, setDeployments] = useState([]);
  const [selectedDeployments, setSelectedDeployments] = useState([]);
  const [events, setEvents] = useState([]);
  const [selectedEvent, setSelectedEvent] = useState('');
  const [reportMonths, setReportMonths] = useState([]);
  const [reportEvents, setReportEvents] = useState([]);
  const [selectedReportMonth, setSelectedReportMonth] = useState('');
  const [selectedReportEvent, setSelectedReportEvent] = useState('');
  const [reportItems, setReportItems] = useState([]);
  const [reportMessage, setReportMessage] = useState('');
  const [mapStatus, setMapStatus] = useState(null);
  const [mapProviders, setMapProviders] = useState({
    esri: {
      id: 'esri',
      label: 'Esri World Imagery',
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      attribution: 'Tiles (c) Esri',
      maxZoom: 19,
      tileSize: 256,
      zoomOffset: 0
    }
  });
  const [activeProvider, setActiveProvider] = useState(() => {
    return window.localStorage.getItem(MAP_PROVIDER_STORAGE_KEY) || 'esri';
  });
  const [mapUsage, setMapUsage] = useState(() => loadMapUsage());
  const [expandedCards, setExpandedCards] = useState(() => ({}));
  const [expandedGroups, setExpandedGroups] = useState(() => ({}));

  const mapRef = useRef(null);
  const baseLayerRef = useRef(null);
  const markersRef = useRef(new Map());
  const childMarkersRef = useRef(new Map());
  const hasFitRef = useRef(false);
  const prevPositionsRef = useRef(new Map());
  const pollTimeoutRef = useRef(null);
  const mapUsageRef = useRef(mapUsage);
  const mapStatusRef = useRef(null);
  const activeProviderRef = useRef(activeProvider);
  const lastRecommendedRef = useRef(null);

  const pollSeconds = POLL_INTERVALS[pollIndex] || POLL_INTERVALS[POLL_INTERVALS.length - 1];
  const mapBlocked = mapStatus?.blocked || {};
  const mapFleet = mapStatus?.fleet || {};
  const mapPct = mapStatus?.pct || {};
  const mapMonthLabel = formatMonthKey(mapStatus?.month_key);
  const guardrailPct = mapStatus?.thresholds?.guardrailPct ?? 0.95;
  const guardrailPctLabel = Math.round(guardrailPct * 100);
  const mapBothBlocked = Boolean(mapBlocked?.mapbox && mapBlocked?.esri);
  const hudLabel = (provider) => (provider === 'mapbox' ? 'Mapbox' : 'Esri World');

  const toggleCardExpanded = (key) => {
    setExpandedCards((prev) => {
      const next = { ...prev };
      if (next[key]) {
        delete next[key];
      } else {
        next[key] = true;
      }
      return next;
    });
  };

  const toggleGroupExpanded = (key) => {
    setExpandedGroups((prev) => {
      const next = { ...prev };
      if (next[key]) {
        delete next[key];
      } else {
        next[key] = true;
      }
      return next;
    });
  };

  useEffect(() => {
    mapUsageRef.current = mapUsage;
    persistMapUsage(mapUsage);
  }, [mapUsage]);

  useEffect(() => {
    activeProviderRef.current = activeProvider;
    try {
      window.localStorage.setItem(MAP_PROVIDER_STORAGE_KEY, activeProvider);
    } catch (err) {
      // Ignore localStorage failures.
    }
  }, [activeProvider]);

  const isLoggerNode = (node) =>
    Boolean(node.is_logger || (node.system_id && node.system_id.startsWith('Logger')));

  const hasFreshSignal = (node) => {
    if (!node) return false;
    if (node.manual) return true;
    if (node.event_id) return true;
    if (node.alerts_count && node.alerts_count > 0) return true;
    if (node.soc !== null && node.soc !== undefined) return true;
    if (node.pout !== null && node.pout !== undefined) return true;
    if (node.acuvim_updated_at !== null && node.acuvim_updated_at !== undefined) return true;
    if (node.acuvim_vavg !== null && node.acuvim_vavg !== undefined) return true;
    if (node.acuvim_iavg !== null && node.acuvim_iavg !== undefined) return true;
    if (node.acuvim_p !== null && node.acuvim_p !== undefined) return true;
    if (node.gps_updated_at !== null && node.gps_updated_at !== undefined) return true;
    return false;
  };

  const visibleNodes = useMemo(() => nodes.filter(hasFreshSignal), [nodes]);

  const getGroupKey = (node) =>
    node.node_id || node.host_system_id || node.system_id || 'unknown';

  const groupLabels = useMemo(() => {
    const labels = new Map();
    visibleNodes.forEach((node) => {
      if (!node.node_id) return;
      if (!isLoggerNode(node)) {
        labels.set(node.node_id, node.system_id);
      }
    });
    visibleNodes.forEach((node) => {
      if (!node.node_id || labels.has(node.node_id)) return;
      labels.set(node.node_id, node.host_system_id || node.system_id);
    });
    return labels;
  }, [visibleNodes]);

  const nodeGroups = useMemo(() => {
    const groups = new Map();
    visibleNodes.forEach((node) => {
      const groupKey = getGroupKey(node);
      let entry = groups.get(groupKey);
      if (!entry) {
        entry = { key: groupKey, host: null, loggers: [], nodes: [] };
        groups.set(groupKey, entry);
      }
      entry.nodes.push(node);
      if (isLoggerNode(node)) {
        entry.loggers.push(node);
      } else if (!entry.host) {
        entry.host = node;
      } else {
        entry.loggers.push(node);
      }
    });

    const result = [];
    groups.forEach((entry) => {
      const loggers = [...entry.loggers].sort((a, b) => a.system_id.localeCompare(b.system_id));
      const primary = entry.host || loggers[0] || entry.nodes[0];
      const loggerList = entry.host
        ? loggers
        : loggers.filter((logger) => logger.system_id !== primary?.system_id);
      const mapNode =
        entry.host && entry.host.latitude !== null && entry.host.longitude !== null
          ? entry.host
          : entry.nodes.find((node) => node.latitude !== null && node.longitude !== null) || primary;
      result.push({
        key: entry.key,
        host: entry.host,
        primary,
        loggers: loggerList,
        nodes: entry.nodes,
        mapNode,
      });
    });

    result.sort((a, b) => a.key.localeCompare(b.key));
    return result;
  }, [visibleNodes]);

  const gpsGroups = useMemo(
    () =>
      nodeGroups.filter(
        (group) =>
          group.mapNode &&
          group.mapNode.latitude !== null &&
          group.mapNode.longitude !== null
      ),
    [nodeGroups]
  );

  const noGpsGroups = useMemo(
    () =>
      nodeGroups.filter(
        (group) =>
          !group.mapNode ||
          group.mapNode.latitude === null ||
          group.mapNode.longitude === null
      ),
    [nodeGroups]
  );

  const eventGroups = useMemo(() => {
    const groups = new Map();
    const nowSec = Date.now() / 1000;
    visibleNodes.forEach((node) => {
      if (!node.event_id) return;
      if (!isLoggerNode(node)) return;
      if (
        !node.event_updated_at ||
        nowSec - node.event_updated_at > EVENT_STALE_SECONDS
      ) {
        return;
      }
      const eventId = node.event_id;
      const nodeKey = getGroupKey(node);
      let byNode = groups.get(eventId);
      if (!byNode) {
        byNode = new Map();
        groups.set(eventId, byNode);
      }
      const list = byNode.get(nodeKey) || [];
      list.push(node.system_id);
      byNode.set(nodeKey, list);
    });
    groups.forEach((byNode) => {
      byNode.forEach((list) => list.sort());
    });
    return groups;
  }, [visibleNodes]);

  const getEventGroup = (eventId) => {
    if (!eventId) return null;
    const byNode = eventGroups.get(eventId);
    if (!byNode) return null;
    let count = 0;
    byNode.forEach((list) => {
      count += list.length;
    });
    return { count, byNode };
  };

  const getNodeEventLoggers = (eventGroup, nodeOrKey) => {
    if (!eventGroup) return [];
    const nodeKey =
      typeof nodeOrKey === 'string' ? nodeOrKey : getGroupKey(nodeOrKey);
    return eventGroup.byNode.get(nodeKey) || [];
  };

  const formatEventGroupHtml = (eventGroup) => {
    if (!eventGroup || eventGroup.byNode.size === 0) return '';
    const lines = [];
    eventGroup.byNode.forEach((loggers, nodeId) => {
      const label = escapeHtml(groupLabels.get(nodeId) || nodeId);
      const list = formatLoggerListHtml(loggers, 8);
      lines.push(`${label}: ${list}`);
    });
    return lines.join('<br/>');
  };

  const deploymentSummary = useMemo(() => {
    if (selectedDeployments.length === 0) return 'All deployments';
    if (selectedDeployments.length <= 2) return selectedDeployments.join(', ');
    return `${selectedDeployments.length} deployments`;
  }, [selectedDeployments]);

  const updatePollIndex = (nextNodes) => {
    const prev = prevPositionsRef.current;
    let moved = false;
    const nextPositions = new Map();

    nextNodes.forEach((node) => {
      if (node.latitude === null || node.longitude === null) return;
      const current = { lat: node.latitude, lon: node.longitude };
      const prevPos = prev.get(node.system_id);
      if (prevPos) {
        const distance = haversineMeters(prevPos, current);
        if (distance > MOVE_THRESHOLD_METERS) {
          moved = true;
        }
      }
      nextPositions.set(node.system_id, current);
    });

    prevPositionsRef.current = nextPositions;

    setPollIndex((idx) => {
      if (moved) return 0;
      return clamp(idx + 1, 0, POLL_INTERVALS.length - 1);
    });
  };

  const fetchNodes = async () => {
    try {
      const effectiveEvent = events.length === 0 ? '' : selectedEvent;
      const resp = await fetch(buildNodesUrl(selectedDeployments, effectiveEvent), { cache: 'no-store' });
      if (!resp.ok) throw new Error('Failed to load nodes');
      const payload = await resp.json();
      setNodes(payload.nodes || []);
      setLastUpdated(new Date());
      updatePollIndex(payload.nodes || []);
      await fetchEvents();
      setStatusMessage('');
    } catch (err) {
      setStatusMessage('Unable to reach fleet API.');
    }
  };

  const fetchDeployments = async () => {
    try {
      const resp = await fetch(`${API_BASE}/deployments`, { cache: 'no-store' });
      if (!resp.ok) throw new Error('Failed to load deployments');
      const payload = await resp.json();
      const items = Array.isArray(payload.deployments) ? payload.deployments : [];
      const filtered = items
        .map((item) => (item ? String(item).trim() : ''))
        .filter((item) => item && !isPlaceholderDeployment(item));
      setDeployments(filtered);
    } catch (err) {
      setDeployments([]);
    }
  };

  const fetchEvents = async () => {
    try {
      const resp = await fetch(buildEventsUrl(selectedDeployments), { cache: 'no-store' });
      if (!resp.ok) throw new Error('Failed to load events');
      const payload = await resp.json();
      const items = Array.isArray(payload.events) ? payload.events : [];
      const eventIds = items.map((item) => item.event_id).filter(Boolean);
      setEvents(eventIds);
      if (eventIds.length === 0) {
        if (selectedEvent) setSelectedEvent('');
      } else if (selectedEvent && !eventIds.includes(selectedEvent)) {
        setSelectedEvent('');
      }
    } catch (err) {
      setEvents([]);
      if (selectedEvent) {
        setSelectedEvent('');
      }
    }
  };

  const fetchReportSummary = async () => {
    try {
      const resp = await fetch(`${API_BASE}/reports`, { cache: 'no-store' });
      if (!resp.ok) throw new Error('Failed to load reports');
      const payload = await resp.json();
      setReportMonths(Array.isArray(payload.months) ? payload.months : []);
      setReportEvents(Array.isArray(payload.events) ? payload.events : []);
      setReportMessage('');
    } catch (err) {
      setReportMonths([]);
      setReportEvents([]);
      setReportMessage('Reports unavailable.');
    }
  };

  const fetchMonthlyReports = async (monthKey) => {
    try {
      const resp = await fetch(`${API_BASE}/reports/monthly?month=${encodeURIComponent(monthKey)}`, {
        cache: 'no-store',
      });
      if (!resp.ok) throw new Error('Failed to load monthly reports');
      const payload = await resp.json();
      const reports = Array.isArray(payload.reports) ? payload.reports : [];
      setReportItems(
        reports.map((entry) => ({
          id: entry.node_key || entry.system_id || entry.node_id,
          label: entry.node_id || entry.system_id || entry.node_key || 'Unknown node',
          meta: `Updated ${formatReportTimestamp(entry.generated_at)}`,
          reportUrl: entry.report_html_url || entry.report_url,
          downloadUrl: entry.download_url
        }))
      );
      setReportMessage(reports.length ? '' : 'No monthly reports yet.');
    } catch (err) {
      setReportItems([]);
      setReportMessage('Unable to load monthly reports.');
    }
  };

  const fetchEventReports = async (eventId) => {
    try {
      const resp = await fetch(`${API_BASE}/reports/event?event_id=${encodeURIComponent(eventId)}`, {
        cache: 'no-store',
      });
      if (!resp.ok) throw new Error('Failed to load event reports');
      const payload = await resp.json();
      const reports = Array.isArray(payload.reports) ? payload.reports : [];
      setReportItems(
        reports.map((entry) => ({
          id: entry.node_key || entry.system_id || entry.node_id,
          label: entry.node_id || entry.system_id || entry.node_key || 'Unknown node',
          meta: `Updated ${formatReportTimestamp(entry.generated_at)}`,
          reportUrl: entry.report_url,
          downloadUrl: entry.download_url
        }))
      );
      setReportMessage(reports.length ? '' : 'No event reports yet.');
    } catch (err) {
      setReportItems([]);
      setReportMessage('Unable to load event reports.');
    }
  };

  const setManualLocation = async (systemId, lat, lon) => {
    try {
      const resp = await fetch(`${API_BASE}/nodes/manual`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ system_id: systemId, latitude: lat, longitude: lon }),
      });
      if (!resp.ok) throw new Error('Failed to save location');
      await fetchNodes();
    } catch (err) {
      setStatusMessage('Failed to save manual location.');
    }
  };

  const clearManualLocation = async (systemId) => {
    try {
      const resp = await fetch(`${API_BASE}/nodes/manual/${encodeURIComponent(systemId)}`, {
        method: 'DELETE',
      });
      if (!resp.ok) throw new Error('Failed to clear location');
      await fetchNodes();
    } catch (err) {
      setStatusMessage('Failed to clear manual location.');
    }
  };

  const toggleDeployment = (deploymentId) => {
    setSelectedDeployments((prev) => {
      if (prev.includes(deploymentId)) {
        return prev.filter((id) => id !== deploymentId);
      }
      return [...prev, deploymentId];
    });
  };

  const incrementMapUsage = (provider) => {
    if (!provider) return;
    setMapUsage((prev) => {
      const currentMonth = monthKeyUtc();
      const base = prev.monthKey === currentMonth ? prev : buildEmptyUsage(currentMonth);
      return {
        monthKey: currentMonth,
        counts: {
          ...base.counts,
          [provider]: (base.counts?.[provider] || 0) + 1
        }
      };
    });
  };

  const fetchMapStatus = async () => {
    try {
      const resp = await fetch('/api/fleet/map-tiles/status', { cache: 'no-store' });
      if (!resp.ok) throw new Error('status request failed');
      const data = await resp.json();
      setMapStatus(data);
      mapStatusRef.current = data;
      if (data.providers) {
        setMapProviders((prev) => {
          if (!prev) return data.providers;
          const same = ['mapbox', 'esri'].every(
            (key) =>
              prev?.[key]?.url === data.providers?.[key]?.url &&
              prev?.[key]?.attribution === data.providers?.[key]?.attribution &&
              prev?.[key]?.maxZoom === data.providers?.[key]?.maxZoom
          );
          return same ? prev : data.providers;
        });
      }
      if (data.month_key && data.month_key !== mapUsageRef.current?.monthKey) {
        setMapUsage(buildEmptyUsage(data.month_key));
      }
      const recommended = data.recommendedProvider || data.preferredProvider;
      const stored = window.localStorage.getItem(MAP_PROVIDER_STORAGE_KEY);
      const nextProvider = recommended || stored;
      const bothBlocked = data.blocked?.mapbox && data.blocked?.esri;
      const cloudUnavailable =
        data.warning && data.warning.toLowerCase().includes('fleet totals unavailable');
      if (!activeProviderRef.current && nextProvider) {
        setActiveProvider(nextProvider);
      } else if (
        nextProvider &&
        nextProvider !== activeProviderRef.current &&
        !bothBlocked &&
        !cloudUnavailable
      ) {
        setActiveProvider(nextProvider);
        if (activeProviderRef.current) {
          setStatusMessage(
            `Provider switched to ${data.providers?.[nextProvider]?.label || nextProvider}.`
          );
        }
      }
    } catch (err) {
      setMapStatus((prev) => ({
        ...(prev || {}),
        warning: 'Fleet totals unavailable.'
      }));
    }
  };

  const selectMapProvider = async (provider) => {
    if (!provider || provider === activeProviderRef.current) return;
    const label = mapProviders?.[provider]?.label || provider;
    const fallback = provider === 'mapbox' ? 'esri' : 'mapbox';
    const blocked = mapBlocked?.[provider];
    const fallbackBlocked = mapBlocked?.[fallback];

    if (blocked) {
      if (!fallbackBlocked) {
        setActiveProvider(fallback);
        setStatusMessage(
          `Provider near free-tier limit; switched to ${
            mapProviders?.[fallback]?.label || fallback
          }.`
        );
      } else {
        setStatusMessage(mapStatus?.warning || 'Both providers are near the free-tier limit.');
      }
      return;
    }

    setActiveProvider(provider);
    try {
      const resp = await fetch('/api/fleet/map-provider/preferred', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider })
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const recommended = data.recommendedProvider || fallback;
        if (recommended && recommended !== provider) {
          setActiveProvider(recommended);
          setStatusMessage(
            `Provider near free-tier limit; switched to ${
              mapProviders?.[recommended]?.label || recommended
            }.`
          );
        } else {
          setStatusMessage(data.error || 'Provider update failed.');
        }
        return;
      }
      if (data.warning) {
        setStatusMessage(data.warning);
      }
    } catch (err) {
      setStatusMessage(`Failed to update provider: ${err.message}`);
    }
  };

  useEffect(() => {
    if (!mapRef.current) {
      mapRef.current = L.map('map', {
        zoomControl: true,
        minZoom: 2,
        worldCopyJump: true,
      }).setView([34.05, -118.25], 5);
    }
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const handleResize = () => {
      map.invalidateSize({ animate: false });
    };

    const mapEl = document.getElementById('map');
    let observer = null;
    if (mapEl && 'ResizeObserver' in window) {
      observer = new ResizeObserver(() => handleResize());
      observer.observe(mapEl);
    }

    window.addEventListener('resize', handleResize);
    handleResize();

    return () => {
      window.removeEventListener('resize', handleResize);
      if (observer) {
        observer.disconnect();
      }
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapProviders) return;
    const provider =
      mapProviders[activeProvider] || mapProviders.esri || Object.values(mapProviders)[0];
    if (!provider || !provider.url) return;

    if (baseLayerRef.current) {
      baseLayerRef.current.off('tileloadstart');
      map.removeLayer(baseLayerRef.current);
      baseLayerRef.current = null;
    }

    const layer = L.tileLayer(provider.url, {
      attribution: provider.attribution,
      maxZoom: provider.maxZoom || 19,
      tileSize: provider.tileSize || 256,
      zoomOffset: provider.zoomOffset || 0,
    });
    layer.on('tileloadstart', () => incrementMapUsage(provider.id || activeProvider));
    layer.addTo(map);
    baseLayerRef.current = layer;
  }, [mapProviders, activeProvider]);

  useEffect(() => {
    fetchMapStatus();
    const interval = setInterval(fetchMapStatus, MAP_STATUS_POLL_MS);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    fetchDeployments();
  }, []);

  useEffect(() => {
    fetchReportSummary();
    const interval = setInterval(fetchReportSummary, 300000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    fetchNodes();
  }, [selectedDeployments, selectedEvent]);

  useEffect(() => {
    if (selectedReportMonth) {
      fetchMonthlyReports(selectedReportMonth);
      return;
    }
    if (selectedReportEvent) {
      fetchEventReports(selectedReportEvent);
      return;
    }
    setReportItems([]);
    setReportMessage('');
  }, [selectedReportMonth, selectedReportEvent]);

  useEffect(() => {
    if (pollTimeoutRef.current) {
      clearTimeout(pollTimeoutRef.current);
    }
    pollTimeoutRef.current = setTimeout(() => {
      fetchNodes();
    }, pollSeconds * 1000);
    return () => clearTimeout(pollTimeoutRef.current);
  }, [pollSeconds, lastUpdated, selectedDeployments, selectedEvent]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const markers = markersRef.current;
    const childMarkers = childMarkersRef.current;
    const activeIds = new Set();

    const clearChildMarkers = (key) => {
      const children = childMarkers.get(key);
      if (!children) return;
      children.forEach((child) => map.removeLayer(child));
      childMarkers.delete(key);
    };

    gpsGroups.forEach((group) => {
      const mapNode = group.mapNode;
      const displayNode = group.primary || mapNode;
      if (!mapNode || !displayNode) return;
      const key = group.key;
      activeIds.add(key);

      const coords = [mapNode.latitude, mapNode.longitude];
      const groupEventNode = getGroupEventNode(group);
      const groupEventId = groupEventNode?.event_id;
      const eventGroup = getEventGroup(groupEventId);
      const eventDetails = formatEventGroupHtml(eventGroup);
      const groupNodes = getGroupNodes(group);
      const groupSize = buildGroupMarkerSize(groupNodes.length);
      const icon = L.divIcon({
        className: 'marker-wrapper',
        html: buildGroupMarkerHtml(group, groupNodes, eventGroup, groupEventId),
        iconSize: [groupSize.width, groupSize.height],
        iconAnchor: [groupSize.width / 2, groupSize.height],
      });

      let marker = markers.get(key);
      if (!marker) {
        marker = L.marker(coords, {
          icon,
          draggable: displayNode.gps_source === 'manual',
        });
        marker.bindPopup(buildPopupHtml(displayNode, eventDetails, groupEventId));
        marker.addTo(map);
        markers.set(key, marker);
      } else {
        marker.setLatLng(coords);
        marker.setIcon(icon);
        if (marker.getPopup()) {
          marker.setPopupContent(buildPopupHtml(displayNode, eventDetails, groupEventId));
        }
      }

      marker.off('click');
      marker.on('click', () => {
        if (groupNodes.length > 1) {
          toggleGroupExpanded(key);
        }
      });

      if (displayNode.gps_source === 'manual') {
        if (marker.dragging) {
          marker.dragging.enable();
        }
        marker.off('dragend');
        marker.on('dragend', (event) => {
          const { lat, lng } = event.target.getLatLng();
          setManualLocation(displayNode.system_id, lat, lng);
        });
      } else if (marker.dragging) {
        marker.dragging.disable();
      }

      const shouldExpand = Boolean(expandedGroups[key]) && groupNodes.length > 1;
      if (!shouldExpand) {
        clearChildMarkers(key);
      } else {
        clearChildMarkers(key);
        const center = { lat: mapNode.latitude, lon: mapNode.longitude };
        const positions = buildSpiderfyPositions(center, groupNodes.length);
        const children = [];

        groupNodes.forEach((node, index) => {
          const pos = positions[index] || center;
          const childEventGroup = getEventGroup(node.event_id);
          const childEventLoggers = getNodeEventLoggers(childEventGroup, node);
          const childDetails = formatEventGroupHtml(childEventGroup);
          const childIcon = L.divIcon({
            className: 'marker-wrapper',
            html: buildMarkerHtml(node, childEventLoggers),
            iconSize: [150, 60],
            iconAnchor: [75, 60],
          });
          const childMarker = L.marker([pos.lat, pos.lon], { icon: childIcon });
          childMarker.bindPopup(buildPopupHtml(node, childDetails));
          childMarker.addTo(map);
          children.push(childMarker);
        });

        childMarkers.set(key, children);
      }
    });

    markers.forEach((marker, key) => {
      if (!activeIds.has(key)) {
        map.removeLayer(marker);
        markers.delete(key);
        clearChildMarkers(key);
      }
    });

    if (!hasFitRef.current && gpsGroups.length) {
      const bounds = L.latLngBounds(
        gpsGroups.map((group) => [group.mapNode.latitude, group.mapNode.longitude])
      );
      map.fitBounds(bounds.pad(0.2));
      hasFitRef.current = true;
    }
  }, [gpsGroups, expandedGroups]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const handler = (event) => {
      if (!placementTarget) return;
      const { lat, lng } = event.latlng;
      setManualLocation(placementTarget.system_id, lat, lng);
      setPlacementTarget(null);
    };

    map.on('click', handler);
    return () => map.off('click', handler);
  }, [placementTarget]);

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="sidebar-header">
          <div>
            <div className="eyebrow">Overdrive Telemetry</div>
            <h1>Fleet Map</h1>
            <div className="meta">
              Last update: {lastUpdated ? lastUpdated.toLocaleTimeString() : '--'}
            </div>
            <div className="meta">Polling: {pollSeconds}s</div>
          </div>
          <button className="btn" onClick={fetchNodes}>
            Refresh now
          </button>
        </div>

        {statusMessage ? <div className="status-banner">{statusMessage}</div> : null}

        <div className="panel">
          <div className="panel-title">Deployments</div>
          <details className="dropdown">
            <summary>{deploymentSummary}</summary>
            <div className="dropdown-menu">
              <div className="dropdown-actions">
                <button
                  className="btn ghost small"
                  onClick={() => setSelectedDeployments(deployments)}
                  type="button"
                >
                  Select all
                </button>
                <button
                  className="btn ghost small"
                  onClick={() => setSelectedDeployments([])}
                  type="button"
                >
                  Clear
                </button>
              </div>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={selectedDeployments.length === 0}
                  onChange={(event) => {
                    if (event.target.checked) setSelectedDeployments([]);
                  }}
                />
                <span>All deployments</span>
              </label>
              {deployments.length === 0 ? (
                <div className="empty">No deployments found.</div>
              ) : (
                deployments.map((deploymentId) => (
                  <label key={deploymentId} className="checkbox">
                    <input
                      type="checkbox"
                      checked={selectedDeployments.includes(deploymentId)}
                      onChange={() => toggleDeployment(deploymentId)}
                    />
                    <span>{deploymentId}</span>
                  </label>
                ))
              )}
            </div>
          </details>
        </div>

        {events.length > 0 ? (
          <div className="panel">
            <div className="panel-title">Active Events</div>
            <div className="event-select">
              <select
                value={selectedEvent}
                onChange={(event) => setSelectedEvent(event.target.value)}
              >
                <option value="">All events</option>
                {events.map((eventId) => (
                  <option key={eventId} value={eventId}>
                    {eventId}
                  </option>
                ))}
              </select>
              <div className="event-meta">{`${events.length} active`}</div>
            </div>
          </div>
        ) : null}

        <div className="panel">
          <div className="panel-title">On Map</div>
          <div className="node-list">
            {gpsGroups.length === 0 && <div className="empty">No GPS fixes yet.</div>}
            {gpsGroups.map((group) => {
              const node = group.primary || group.mapNode;
              if (!node) return null;
              const groupEventNode = getGroupEventNode(group);
              const groupEventId = groupEventNode?.event_id;
              const eventGroup = getEventGroup(groupEventId);
              const nodeEventLoggers = getNodeEventLoggers(eventGroup, group.key);
              const eventLoggerIds = groupEventId ? new Set(nodeEventLoggers) : null;
              const loggerItems = groupEventId
                ? group.loggers.filter((logger) => eventLoggerIds && eventLoggerIds.has(logger.system_id))
                : group.loggers;
              const loggerCount = groupEventId ? nodeEventLoggers.length : group.loggers.length;
              const groupNodes = getGroupNodes(group);
              const groupSystemIds = groupNodes
                .map((entry) => entry.system_id)
                .filter(Boolean);
              const groupSystemList = groupSystemIds.join(' · ');
              const isLogger = isLoggerNode(node);
              const hasAcuvim =
                isLogger && node.acuvim_updated_at !== null && node.acuvim_updated_at !== undefined;
              const statusClass = getStatusClass(node);
              const isExpanded = Boolean(expandedCards[group.key]);
              const compactSoc =
                node.soc !== null && node.soc !== undefined ? formatPercent(node.soc) : null;
              const compactPout = formatPower(node.pout);
              return (
                <div
                  key={group.key}
                  className={`node-card ${statusClass} ${groupEventId ? 'event' : ''} ${
                    isExpanded ? 'expanded' : 'collapsed'
                  }`}
                >
                  <div className="node-head">
                    <button
                      className="collapse-toggle"
                      type="button"
                      aria-expanded={isExpanded}
                      onClick={() => toggleCardExpanded(group.key)}
                    >
                      {isExpanded ? '-' : '+'}
                    </button>
                    <div className="node-head-main">
                      <div className="node-title">{node.system_id}</div>
                      {node.node_url ? (
                        <a
                          className="link node-open-link"
                          href={node.node_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Open node
                        </a>
                      ) : null}
                    </div>
                    <div className="node-compact-metrics">
                      {compactSoc ? <span>SOC {compactSoc}</span> : null}
                      <span>
                        P<sub>out</sub> {compactPout}
                      </span>
                    </div>
                    <span className={`pill ${statusClass}`}>
                      {node.alerts_count > 0 ? `${node.alerts_count} alert` : 'ok'}
                    </span>
                  </div>
                  {groupSystemIds.length > 1 ? (
                    <div className="node-system-list">{groupSystemList}</div>
                  ) : null}
                  <div className="node-body">
                    <div className="node-sub">{node.location || '--'}</div>
                    {groupEventId ? (
                      <div className="node-event">
                        <span>Event: {groupEventId}</span>
                        {!isLogger ? (
                          <span className="node-event-meta">{nodeEventLoggers.length} loggers</span>
                        ) : null}
                      </div>
                    ) : null}
                    {groupEventId && !isLogger ? (
                      <div className="node-event-loggers">
                        {formatLoggerListText(nodeEventLoggers, 4)}
                      </div>
                    ) : null}
                    <div className="node-metrics">
                      {isLogger ? (
                        <>
                          <span>Vavg {hasAcuvim ? formatVoltage(node.acuvim_vavg) : '--'}</span>
                          <span>Iavg {hasAcuvim ? formatCurrent(node.acuvim_iavg) : '--'}</span>
                          <span>P {hasAcuvim ? formatPower(node.acuvim_p) : '--'}</span>
                        </>
                      ) : (
                        <>
                          <span>SOC {formatPercent(node.soc)}</span>
                          <span>
                            P<sub>out</sub> {formatPower(node.pout)}
                          </span>
                          <span>GPS {formatAge(node.gps_age_sec)}</span>
                        </>
                      )}
                    </div>
                    {node.gps_source === 'manual' ? (
                      <div className="node-actions">
                        <button
                          className="btn ghost"
                          onClick={() => clearManualLocation(node.system_id)}
                        >
                          Clear manual
                        </button>
                      </div>
                    ) : null}
                    {loggerItems.length > 0 ? (
                      <details className="logger-group">
                        <summary>
                          {groupEventId ? 'Active loggers' : 'Loggers'} ({loggerCount})
                        </summary>
                        <div className="logger-list">
                          {loggerItems.map((logger) => {
                            const hasAcuvim =
                              logger.acuvim_updated_at !== null &&
                              logger.acuvim_updated_at !== undefined;
                            return (
                              <div key={logger.system_id} className="logger-item">
                                <div className="logger-title">{logger.system_id}</div>
                                <div className="logger-sub">{logger.location || '--'}</div>
                                <div className="logger-metrics">
                                  <span>
                                    Vavg {hasAcuvim ? formatVoltage(logger.acuvim_vavg) : '--'}
                                  </span>
                                  <span>
                                    Iavg {hasAcuvim ? formatCurrent(logger.acuvim_iavg) : '--'}
                                  </span>
                                  <span>P {hasAcuvim ? formatPower(logger.acuvim_p) : '--'}</span>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </details>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div className="panel">
          <div className="panel-title">No GPS Fix</div>
          <div className="node-list">
            {noGpsGroups.length === 0 && <div className="empty">All nodes have GPS.</div>}
            {noGpsGroups.map((group) => {
              const node = group.primary || group.mapNode;
              if (!node) return null;
              const groupEventNode = getGroupEventNode(group);
              const groupEventId = groupEventNode?.event_id;
              const eventGroup = getEventGroup(groupEventId);
              const nodeEventLoggers = getNodeEventLoggers(eventGroup, group.key);
              const eventLoggerIds = groupEventId ? new Set(nodeEventLoggers) : null;
              const loggerItems = groupEventId
                ? group.loggers.filter((logger) => eventLoggerIds && eventLoggerIds.has(logger.system_id))
                : group.loggers;
              const loggerCount = groupEventId ? nodeEventLoggers.length : group.loggers.length;
              const groupNodes = getGroupNodes(group);
              const groupSystemIds = groupNodes
                .map((entry) => entry.system_id)
                .filter(Boolean);
              const groupSystemList = groupSystemIds.join(' · ');
              const isLogger = isLoggerNode(node);
              const hasAcuvim =
                isLogger && node.acuvim_updated_at !== null && node.acuvim_updated_at !== undefined;
              const isExpanded = Boolean(expandedCards[group.key]);
              const compactSoc =
                node.soc !== null && node.soc !== undefined ? formatPercent(node.soc) : null;
              const compactPout = formatPower(node.pout);
              return (
                <div
                  key={group.key}
                  className={`node-card neutral ${isExpanded ? 'expanded' : 'collapsed'}`}
                >
                  <div className="node-head">
                    <button
                      className="collapse-toggle"
                      type="button"
                      aria-expanded={isExpanded}
                      onClick={() => toggleCardExpanded(group.key)}
                    >
                      {isExpanded ? '-' : '+'}
                    </button>
                    <div className="node-head-main">
                      <div className="node-title">{node.system_id}</div>
                      {node.node_url ? (
                        <a
                          className="link node-open-link"
                          href={node.node_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Open node
                        </a>
                      ) : null}
                    </div>
                    <div className="node-compact-metrics">
                      {compactSoc ? <span>SOC {compactSoc}</span> : null}
                      <span>
                        P<sub>out</sub> {compactPout}
                      </span>
                    </div>
                    <span className="pill neutral">offline</span>
                  </div>
                  {groupSystemIds.length > 1 ? (
                    <div className="node-system-list">{groupSystemList}</div>
                  ) : null}
                  <div className="node-body">
                    <div className="node-sub">{node.location || '--'}</div>
                    {groupEventId ? (
                      <div className="node-event">
                        <span>Event: {groupEventId}</span>
                        {!isLogger ? (
                          <span className="node-event-meta">{nodeEventLoggers.length} loggers</span>
                        ) : null}
                      </div>
                    ) : null}
                    {groupEventId && !isLogger ? (
                      <div className="node-event-loggers">
                        {formatLoggerListText(nodeEventLoggers, 4)}
                      </div>
                    ) : null}
                    <div className="node-metrics">
                      {isLogger ? (
                        <>
                          <span>Vavg {hasAcuvim ? formatVoltage(node.acuvim_vavg) : '--'}</span>
                          <span>Iavg {hasAcuvim ? formatCurrent(node.acuvim_iavg) : '--'}</span>
                          <span>P {hasAcuvim ? formatPower(node.acuvim_p) : '--'}</span>
                        </>
                      ) : (
                        <>
                          <span>SOC {formatPercent(node.soc)}</span>
                          <span>
                            P<sub>out</sub> {formatPower(node.pout)}
                          </span>
                        </>
                      )}
                    </div>
                    <div className="node-actions">
                      <button className="btn ghost" onClick={() => setPlacementTarget(node)}>
                        Place on map
                      </button>
                    </div>
                    {loggerItems.length > 0 ? (
                      <details className="logger-group">
                        <summary>
                          {groupEventId ? 'Active loggers' : 'Loggers'} ({loggerCount})
                        </summary>
                        <div className="logger-list">
                          {loggerItems.map((logger) => {
                            const hasAcuvim =
                              logger.acuvim_updated_at !== null &&
                              logger.acuvim_updated_at !== undefined;
                            return (
                              <div key={logger.system_id} className="logger-item">
                                <div className="logger-title">{logger.system_id}</div>
                                <div className="logger-sub">{logger.location || '--'}</div>
                                <div className="logger-metrics">
                                  <span>
                                    Vavg {hasAcuvim ? formatVoltage(logger.acuvim_vavg) : '--'}
                                  </span>
                                  <span>
                                    Iavg {hasAcuvim ? formatCurrent(logger.acuvim_iavg) : '--'}
                                  </span>
                                  <span>P {hasAcuvim ? formatPower(logger.acuvim_p) : '--'}</span>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </details>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </aside>

      <main className="map-wrap">
        <div className="report-filters">
          <label>
            <span>Month</span>
            <select
              value={selectedReportMonth}
              onChange={(event) => {
                const value = event.target.value;
                setSelectedReportMonth(value);
                if (value) setSelectedReportEvent('');
              }}
              disabled={Boolean(selectedReportEvent)}
            >
              <option value="">Select month</option>
              {reportMonths.map((month) => (
                <option key={month} value={month}>
                  {formatMonthKey(month)}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Event</span>
            <select
              value={selectedReportEvent}
              onChange={(event) => {
                const value = event.target.value;
                setSelectedReportEvent(value);
                if (value) setSelectedReportMonth('');
              }}
              disabled={Boolean(selectedReportMonth)}
            >
              <option value="">Select event</option>
              {reportEvents.map((entry) => (
                <option key={entry.event_id} value={entry.event_id}>
                  {entry.event_id}
                </option>
              ))}
            </select>
          </label>
          <div className="report-meta">
            {reportMonths.length} months, {reportEvents.length} events
          </div>
        </div>
        <div className="report-panel">
          <div className="report-panel-title">Reports</div>
          <div className="report-list">
            {reportMessage ? <div className="empty">{reportMessage}</div> : null}
            {!reportMessage && reportItems.length === 0 ? (
              <div className="empty">Select a month or event to view reports.</div>
            ) : null}
            {reportItems.map((item) => (
              <div key={item.id} className="report-row">
                <div>
                  <div className="report-title">{item.label}</div>
                  <div className="report-sub">{item.meta}</div>
                </div>
                <div className="report-actions">
                  {item.reportUrl ? (
                    <a className="link" href={item.reportUrl} target="_blank" rel="noreferrer">
                      Open
                    </a>
                  ) : null}
                  {item.downloadUrl ? (
                    <a className="link" href={item.downloadUrl} target="_blank" rel="noreferrer">
                      Download
                    </a>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        </div>
        <div className="map-controls">
          <div className="map-provider-toggle">
            {['mapbox', 'esri'].map((provider) => {
              const label = mapProviders?.[provider]?.label || provider;
              const blocked = mapBlocked?.[provider];
              const reason = blocked
                ? `${guardrailPctLabel}% free tier reached for ${mapMonthLabel}`
                : `Switch to ${label}`;
              return (
                <button
                  key={provider}
                  className={`map-provider-btn ${
                    activeProvider === provider ? 'active' : ''
                  } ${blocked ? 'blocked' : ''}`}
                  disabled={blocked}
                  title={reason}
                  onClick={() => selectMapProvider(provider)}
                >
                  {label}
                </button>
              );
            })}
          </div>
          <div className="map-usage-hud">
            {['mapbox', 'esri'].map((provider) => {
              const label = hudLabel(provider);
              const fleetValue = mapFleet?.[provider];
              return (
                <div
                  key={provider}
                  className={`map-usage-row ${mapBlocked?.[provider] ? 'blocked' : ''}`}
                >
                  <span className="map-usage-label">{label}</span>
                  <span className="map-usage-metrics">
                    L {formatCount(mapUsage.counts?.[provider])} | F{' '}
                    {fleetValue == null ? '--' : formatCount(fleetValue)} |{' '}
                    {formatPercent(mapPct?.[provider])}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
        {mapStatus?.warning ? (
          <div className={`map-warning ${mapBothBlocked ? 'critical' : ''}`}>
            {mapStatus.warning}
          </div>
        ) : null}
        <div id="map" className={`map-canvas ${placementTarget ? 'placing' : ''}`} />
        {placementTarget ? (
          <div className="placement-hint">
            <div>
              Click the map to place <strong>{placementTarget.system_id}</strong>.
            </div>
            <button className="btn ghost" onClick={() => setPlacementTarget(null)}>
              Cancel
            </button>
          </div>
        ) : null}
      </main>
    </div>
  );
}
