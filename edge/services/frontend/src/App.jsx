import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { deleteJson, getJson, postJson, uploadForm } from './api.js';
import { formatAlertName } from './alertNames.js';
import MapPanel from './MapPanel.jsx';

const SERVICE_OPTIONS = [
  'Pro6005-2',
  'Logger 0',
  'Logger 1',
  'Logger 2',
  'Logger 3',
  'Logger 4',
  'Logger 5',
  'Logger 6',
  'Logger 7',
  'Logger 8',
  'Logger 9'
];

const FALLBACK_MAP_TILE_URL =
  import.meta.env.VITE_MAP_TILE_URL ||
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}';
const FALLBACK_MAP_TILE_ATTRIBUTION =
  import.meta.env.VITE_MAP_TILE_ATTRIBUTION || 'Tiles (c) Esri';
const MAP_DEFAULT_ZOOM = Number(import.meta.env.VITE_MAP_DEFAULT_ZOOM || 16);
const FALLBACK_MAP_TILE_CACHE_PREFIX =
  import.meta.env.VITE_MAP_TILE_CACHE_PREFIX ||
  (FALLBACK_MAP_TILE_URL.includes('{')
    ? FALLBACK_MAP_TILE_URL.split('{', 1)[0]
    : FALLBACK_MAP_TILE_URL);
const MAP_CACHE_WIDE_RADIUS = Number(import.meta.env.VITE_MAP_CACHE_RADIUS_WIDE || 1000);
const MAP_CACHE_DETAIL_RADIUS = Number(import.meta.env.VITE_MAP_CACHE_RADIUS_DETAIL || 200);
const MAP_CACHE_WIDE_ZOOM = Number(import.meta.env.VITE_MAP_CACHE_ZOOM_WIDE || 16);
const MAP_CACHE_DETAIL_ZOOM = Number(import.meta.env.VITE_MAP_CACHE_ZOOM_DETAIL || 18);
const MAP_CACHE_MAX_TILES = Number(import.meta.env.VITE_MAP_CACHE_MAX_TILES || 200);
const MAP_CACHE_CONCURRENCY = Number(import.meta.env.VITE_MAP_CACHE_CONCURRENCY || 6);
const MAP_CACHE_LEVELS = [
  { zoom: MAP_CACHE_DETAIL_ZOOM, radiusMeters: MAP_CACHE_DETAIL_RADIUS },
  { zoom: MAP_CACHE_WIDE_ZOOM, radiusMeters: MAP_CACHE_WIDE_RADIUS }
];
const MAP_USAGE_STORAGE_KEY = 'ovr_map_usage_v1';
const MAP_PROVIDER_STORAGE_KEY = 'ovr_map_provider_v1';
const MAP_STATUS_POLL_MS = 15000;
const MAP_USAGE_FLUSH_MS = 30000;

const DEFAULT_LOGGER = { id: 0, service: '', location: '' };
const INVERTER_MODE_LABELS = { 3: 'On', 2: 'Inverter Only', 1: 'Charger Only', 4: 'Off' };
const INVERTER_MODE_VALUES = { 3: 'on', 2: 'inverter_only', 1: 'charger_only', 4: 'off' };
const EARTH_RADIUS_METERS = 6378137;
const MAX_LATITUDE = 85.05112878;

const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const toRad = (deg) => (deg * Math.PI) / 180;

const lonToTileX = (lon, zoom) => {
  const n = 2 ** zoom;
  return Math.floor(((lon + 180) / 360) * n);
};

const latToTileY = (lat, zoom) => {
  const latRad = toRad(lat);
  const n = 2 ** zoom;
  const y = (1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2;
  return Math.floor(y * n);
};

const buildTileUrl = (template, z, x, y) =>
  template
    .replace(/{z}/g, z)
    .replace(/{x}/g, x)
    .replace(/{y}/g, y)
    .replace(/{s}/g, 'a');

const tilePrefixFromTemplate = (template) => {
  if (!template) return '';
  return template.includes('{') ? template.split('{', 1)[0] : template;
};

const getTileBounds = (lat, lon, radiusMeters, zoom) => {
  const latRad = toRad(lat);
  const deltaLat = (radiusMeters / EARTH_RADIUS_METERS) * (180 / Math.PI);
  const deltaLon =
    (radiusMeters / (EARTH_RADIUS_METERS * Math.cos(latRad))) * (180 / Math.PI);

  const minLat = clamp(lat - deltaLat, -MAX_LATITUDE, MAX_LATITUDE);
  const maxLat = clamp(lat + deltaLat, -MAX_LATITUDE, MAX_LATITUDE);
  const minLon = lon - deltaLon;
  const maxLon = lon + deltaLon;

  const n = 2 ** zoom;
  const minX = clamp(lonToTileX(minLon, zoom), 0, n - 1);
  const maxX = clamp(lonToTileX(maxLon, zoom), 0, n - 1);
  const minY = clamp(latToTileY(maxLat, zoom), 0, n - 1);
  const maxY = clamp(latToTileY(minLat, zoom), 0, n - 1);

  return { minX, maxX, minY, maxY };
};

const prefetchTileUrls = async (urls, concurrency) => {
  const queue = [...urls];
  const limit = Number.isFinite(concurrency) ? Math.max(1, concurrency) : 1;
  let active = 0;

  return new Promise((resolve) => {
    const next = () => {
      if (queue.length === 0 && active === 0) {
        resolve();
        return;
      }
      while (active < limit && queue.length > 0) {
        const url = queue.shift();
        active += 1;
        fetch(url, { mode: 'no-cors' })
          .catch(() => {})
          .finally(() => {
            active -= 1;
            next();
          });
      }
    };
    next();
  });
};

const waitForServiceWorker = async (timeoutMs = 1500) => {
  if (!('serviceWorker' in navigator)) return;
  if (navigator.serviceWorker.controller) return;
  await Promise.race([
    navigator.serviceWorker.ready,
    new Promise((resolve) => setTimeout(resolve, timeoutMs))
  ]);
};

const monthKeyUtc = () => new Date().toISOString().slice(0, 7);

const buildEmptyUsage = (monthKey) => ({
  monthKey,
  counts: { mapbox: 0, esri: 0 },
  pending: { mapbox: 0, esri: 0 }
});

const loadMapUsage = () => {
  if (typeof window === 'undefined') return buildEmptyUsage(monthKeyUtc());
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
      },
      pending: {
        mapbox: Number(parsed.pending?.mapbox || 0),
        esri: Number(parsed.pending?.esri || 0)
      }
    };
  } catch (err) {
    return buildEmptyUsage(monthKey);
  }
};

const persistMapUsage = (usage) => {
  if (typeof window === 'undefined') return;
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

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  return `${Number(value).toFixed(1)}%`;
}

function formatMonthKey(value) {
  if (!value) return 'this month';
  const [year, month] = String(value).split('-').map((part) => Number(part));
  if (!year || !month) return value;
  const date = new Date(Date.UTC(year, month - 1, 1));
  return date.toLocaleString(undefined, { month: 'long', year: 'numeric' });
}

function formatPower(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return '--';
  const num = Number(value);
  const abs = Math.abs(num);
  if (abs >= 1000) {
    return `${(num / 1000).toFixed(2)} kW`;
  }
  return `${num.toFixed(0)} W`;
}

function formatTimestampNs(tsNs) {
  if (!tsNs) return '-';
  const date = new Date(tsNs / 1e6);
  return date.toLocaleString();
}

function formatAge(tsNs) {
  if (!tsNs) return '';
  const ageSec = Math.max(0, Math.round((Date.now() - tsNs / 1e6) / 1000));
  if (ageSec < 60) return ` (updated ${ageSec}s ago)`;
  const ageMin = Math.round(ageSec / 60);
  if (ageMin < 60) return ` (updated ${ageMin}m ago)`;
  const ageHr = Math.round(ageMin / 60);
  return ` (updated ${ageHr}h ago)`;
}

function normalizeLoggers(entries, activeServices) {
  const used = new Set();
  return entries.map((entry) => {
    let service = entry.service;
    if (service && (activeServices.has(service) || used.has(service))) {
      service = '';
    }
    if (service) used.add(service);
    return { ...entry, service };
  });
}

export default function App() {
  const [activeTab, setActiveTab] = useState('events');
  const [summary, setSummary] = useState({
    soc: null,
    pin: null,
    pout: null,
    alerts: []
  });
  const [dashboardSystems, setDashboardSystems] = useState([]);
  const [activeEventId, setActiveEventId] = useState('');
  const [activeLoggers, setActiveLoggers] = useState([]);
  const [eventIdInput, setEventIdInput] = useState('');
  const [formLoggers, setFormLoggers] = useState([{ ...DEFAULT_LOGGER }]);
  const [noteText, setNoteText] = useState('');
  const [noteService, setNoteService] = useState('');
  const [notes, setNotes] = useState([]);
  const [notesVisible, setNotesVisible] = useState(true);
  const [selectedNoteIds, setSelectedNoteIds] = useState([]);
  const [images, setImages] = useState([]);
  const [imagesVisible, setImagesVisible] = useState(false);
  const [selectedImageIds, setSelectedImageIds] = useState([]);
  const [imageFile, setImageFile] = useState(null);
  const [imagePreview, setImagePreview] = useState('');
  const [imageCaption, setImageCaption] = useState('');
  const [editingImageId, setEditingImageId] = useState(null);
  const [editingImageCaption, setEditingImageCaption] = useState('');
  const [lightboxImage, setLightboxImage] = useState(null);
  const [gxSettings, setGxSettings] = useState(null);
  const [gxSystems, setGxSystems] = useState([]);
  const [gxSystemId, setGxSystemId] = useState('');
  const [gxInputs, setGxInputs] = useState({
    battery_charge_current: '',
    inverter_mode: 'on',
    ac_input_current_limit: '',
    inverter_output_voltage: ''
  });
  const [gxStatus, setGxStatus] = useState({ message: '', level: 'info' });
  const [gps, setGps] = useState({ latitude: null, longitude: null, updatedAt: null });
  const [mapStatus, setMapStatus] = useState(null);
  const [mapProviders, setMapProviders] = useState(() => ({
    esri: {
      id: 'esri',
      label: 'Esri World Imagery',
      url: FALLBACK_MAP_TILE_URL,
      attribution: FALLBACK_MAP_TILE_ATTRIBUTION,
      maxZoom: 19,
      tileSize: 256,
      zoomOffset: 0
    }
  }));
  const [activeProvider, setActiveProvider] = useState(() => {
    if (typeof window === 'undefined') return 'esri';
    return window.localStorage.getItem(MAP_PROVIDER_STORAGE_KEY) || 'esri';
  });
  const [mapUsage, setMapUsage] = useState(() => loadMapUsage());
  const [mapCacheStatus, setMapCacheStatus] = useState('');
  const [statusModal, setStatusModal] = useState(null);

  const loggerIdRef = useRef(1);
  const statusTimerRef = useRef(null);
  const gxPollRef = useRef({ handle: null, startedAt: 0, expected: null, setting: '' });
  const imageInputRef = useRef(null);
  const mapWarmRef = useRef({ eventId: '', provider: '', inFlight: false });
  const mapUsageRef = useRef(mapUsage);
  const mapStatusRef = useRef(null);
  const activeProviderRef = useRef(activeProvider);
  const activeServices = useMemo(
    () => new Set(activeLoggers.map((logger) => logger.system_id)),
    [activeLoggers]
  );
  const tilePrefixes = useMemo(() => {
    const prefixes = new Set();
    if (FALLBACK_MAP_TILE_CACHE_PREFIX) {
      prefixes.add(FALLBACK_MAP_TILE_CACHE_PREFIX);
    }
    if (mapProviders) {
      Object.values(mapProviders).forEach((provider) => {
        const prefix = tilePrefixFromTemplate(provider?.url);
        if (prefix) prefixes.add(prefix);
      });
    }
    return [...prefixes].filter(
      (prefix) =>
        prefix.length > 10 &&
        (prefix.startsWith('http://') || prefix.startsWith('https://'))
    );
  }, [mapProviders]);
  const mapTileCacheEnabled = tilePrefixes.length > 0;
  const activeTileProvider = useMemo(() => {
    if (mapProviders && activeProvider && mapProviders[activeProvider]) {
      return mapProviders[activeProvider];
    }
    return {
      id: 'esri',
      label: 'Esri World Imagery',
      url: FALLBACK_MAP_TILE_URL,
      attribution: FALLBACK_MAP_TILE_ATTRIBUTION,
      maxZoom: 19,
      tileSize: 256,
      zoomOffset: 0
    };
  }, [mapProviders, activeProvider]);

  const noteServiceOptions = useMemo(
    () => activeLoggers.map((logger) => logger.system_id),
    [activeLoggers]
  );

  const alertsText = useMemo(() => {
    const alerts = summary.alerts || [];
    if (alerts.length === 0) return 'None';
    const formatted = alerts.map(formatAlertName);
    if (alerts.length <= 2) return formatted.join(', ');
    return `${alerts.length} active`;
  }, [summary.alerts]);
  const mapBlocked = mapStatus?.blocked || {};
  const mapFleet = mapStatus?.fleet || {};
  const mapLocal = mapStatus?.local || {};
  const mapPct = mapStatus?.pct || {};
  const satelliteAllowed = mapStatus?.satelliteAllowed !== false;
  const mapMonthLabel = formatMonthKey(mapStatus?.month_key);
  const guardrailPct = mapStatus?.thresholds?.guardrailPct ?? 0.95;
  const guardrailPctLabel = Math.round(guardrailPct * 100);
  const mapBothBlocked = Boolean(mapBlocked?.mapbox && mapBlocked?.esri);
  const mapWarning =
    mapStatus?.warning || (!satelliteAllowed ? 'Satellite imagery disabled due to budget limits.' : '');
  const hudLabel = (provider) => (provider === 'mapbox' ? 'Mapbox' : 'Esri World');
  const localHudCount = (provider) => {
    const serverValue = mapLocal?.[provider];
    if (serverValue == null) {
      return mapUsage.counts?.[provider];
    }
    return serverValue;
  };

  const summarySocClass = useMemo(() => {
    const soc = Number(summary.soc);
    if (Number.isNaN(soc)) return '';
    if (soc >= 40) return 'soc-good';
    if (soc >= 25) return 'soc-warn';
    return 'soc-bad';
  }, [summary.soc]);

  const summaryAlertClass = useMemo(() => {
    const alerts = summary.alerts || [];
    return alerts.length > 0 ? 'alert-bad' : '';
  }, [summary.alerts]);

  const isMultiSystem = dashboardSystems.length > 1;
  const singleSystem = dashboardSystems.length === 1 ? dashboardSystems[0] : null;

  const isEventActive = Boolean(activeEventId);

  const showModal = (message, level = 'success', action) => {
    if (statusTimerRef.current) {
      clearTimeout(statusTimerRef.current);
      statusTimerRef.current = null;
    }
    setStatusModal({ message, level, action });
    if (!action) {
      statusTimerRef.current = setTimeout(() => {
        setStatusModal(null);
      }, 3000);
    }
  };

  const dismissModal = () => {
    if (statusTimerRef.current) {
      clearTimeout(statusTimerRef.current);
      statusTimerRef.current = null;
    }
    setStatusModal(null);
  };

  useEffect(() => {
    mapUsageRef.current = mapUsage;
    persistMapUsage(mapUsage);
  }, [mapUsage]);

  useEffect(() => {
    activeProviderRef.current = activeProvider;
    if (!activeProvider) return;
    try {
      window.localStorage.setItem(MAP_PROVIDER_STORAGE_KEY, activeProvider);
    } catch (err) {
      // Ignore localStorage failures.
    }
  }, [activeProvider]);

  useEffect(() => {
    if (!('serviceWorker' in navigator)) return;
    let cancelled = false;

    navigator.serviceWorker
      .register('/sw.js')
      .then(() => navigator.serviceWorker.ready)
      .then((registration) => {
        if (cancelled) return;
        const worker = registration.active || registration.waiting || registration.installing;
        if (worker) {
          worker.postMessage({
            type: 'OVR_TILE_CONFIG',
            tilePrefixes,
            cacheEnabled: mapTileCacheEnabled
          });
        }
      })
      .catch((err) => {
        console.warn('Service worker registration failed:', err);
      });

    return () => {
      cancelled = true;
    };
  }, [tilePrefixes, mapTileCacheEnabled]);

  useEffect(() => {
    fetchMapStatus();
    const interval = setInterval(fetchMapStatus, MAP_STATUS_POLL_MS);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    const interval = setInterval(() => {
      flushMapUsage(false);
    }, MAP_USAGE_FLUSH_MS);
    const handlePageHide = () => flushMapUsage(true);
    const handleVisibility = () => {
      if (document.visibilityState === 'hidden') {
        flushMapUsage(true);
      }
    };

    window.addEventListener('pagehide', handlePageHide);
    document.addEventListener('visibilitychange', handleVisibility);
    return () => {
      clearInterval(interval);
      window.removeEventListener('pagehide', handlePageHide);
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, []);

  const loadSummary = async () => {
    try {
      const data = await getJson('/summary');
      setSummary({
        soc: data.soc,
        pin: data.pin,
        pout: data.pout,
        alerts: data.alerts || []
      });
    } catch (err) {
      console.warn('Failed to load summary:', err);
    }
  };

  const loadDashboard = async () => {
    try {
      const data = await getJson('/dashboard');
      setDashboardSystems(data.systems || []);
    } catch (err) {
      console.warn('Failed to load dashboard:', err);
    }
  };

  const loadActiveLocations = async () => {
    try {
      const data = await getJson('/status');
      const events = data.active_events || [];
      if (events.length === 0) {
        setActiveEventId('');
        setActiveLoggers([]);
        return;
      }

      const currentEventId = events[0].event_id;
      const loggers = events.filter((event) => event.event_id === currentEventId);
      setActiveEventId(currentEventId);
      setActiveLoggers(loggers);
    } catch (err) {
      console.warn('Failed to load active events:', err);
      setActiveEventId('');
      setActiveLoggers([]);
    }
  };

  const loadNotes = async (eventId) => {
    if (!eventId) {
      setNotes([]);
      return;
    }
    try {
      const data = await getJson(`/notes?event_id=${encodeURIComponent(eventId)}&limit=50`);
      setNotes(data.notes || []);
    } catch (err) {
      console.warn('Failed to load notes:', err);
      setNotes([]);
    }
  };

  const loadImages = async () => {
    try {
      const data = await getJson('/images?limit=50');
      setImages(data.images || []);
    } catch (err) {
      showModal(`Failed to load images: ${err.message}`, 'error');
    }
  };

  const loadGps = async (refresh = false) => {
    try {
      const endpoint = refresh ? '/gx/gps?refresh=1' : '/gx/gps';
      const data = await getJson(endpoint);
      if (data.error) {
        throw new Error(data.error);
      }
      const lat = Number(data.latitude);
      const lon = Number(data.longitude);
      if (Number.isNaN(lat) || Number.isNaN(lon)) {
        throw new Error('Invalid GPS coordinates');
      }
      const payload = { latitude: lat, longitude: lon, updatedAt: data.updated_at };
      setGps(payload);
      return payload;
    } catch (err) {
      setGps({ latitude: null, longitude: null, updatedAt: null });
      console.warn('Failed to load GPS:', err);
      return null;
    }
  };

  const incrementMapUsage = useCallback((provider) => {
    if (!provider) return;
    setMapUsage((prev) => {
      const currentMonth = monthKeyUtc();
      const base = prev.monthKey === currentMonth ? prev : buildEmptyUsage(currentMonth);
      return {
        monthKey: currentMonth,
        counts: {
          ...base.counts,
          [provider]: (base.counts?.[provider] || 0) + 1
        },
        pending: {
          ...base.pending,
          [provider]: (base.pending?.[provider] || 0) + 1
        }
      };
    });
  }, []);

  const flushMapUsage = async (useBeacon = false) => {
    const usage = mapUsageRef.current;
    if (!usage) return;
    const entries = Object.entries(usage.pending || {}).filter(([, count]) => count > 0);
    if (entries.length === 0) return;

    const status = mapStatusRef.current;
    const payloadBase = {
      ts: Date.now(),
      month_key: usage.monthKey,
      node_id: status?.node_id || status?.nodeId,
      deployment_id: status?.deployment_id || status?.deploymentId
    };

    if (useBeacon && navigator.sendBeacon) {
      entries.forEach(([provider, count]) => {
        const payload = JSON.stringify({ provider, count, ...payloadBase });
        navigator.sendBeacon(
          '/api/map-tiles/increment',
          new Blob([payload], { type: 'application/json' })
        );
      });
      return;
    }

    const nextPending = { ...usage.pending };
    for (const [provider, count] of entries) {
      try {
        const resp = await fetch('/api/map-tiles/increment', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider, count, ...payloadBase })
        });
        if (resp.ok) {
          nextPending[provider] = 0;
        }
      } catch (err) {
        // Keep pending count for retry.
      }
    }

    setMapUsage((prev) => {
      if (prev.monthKey !== usage.monthKey) return prev;
      return { ...prev, pending: nextPending };
    });
  };

  const fetchMapStatus = async () => {
    try {
      const data = await getJson('/map-tiles/status');
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
      const allowTiles = data.satelliteAllowed !== false;
      const stored =
        activeProviderRef.current ||
        (typeof window !== 'undefined'
          ? window.localStorage.getItem(MAP_PROVIDER_STORAGE_KEY)
          : null);
      const nextProvider = recommended || stored;
      const bothBlocked = data.blocked?.mapbox && data.blocked?.esri;
      const cloudUnavailable =
        data.warning && data.warning.toLowerCase().includes('cloud totals unavailable');
      if (!activeProviderRef.current && nextProvider) {
        setActiveProvider(nextProvider);
      } else if (
        nextProvider &&
        nextProvider !== activeProviderRef.current &&
        !bothBlocked &&
        !cloudUnavailable &&
        allowTiles
      ) {
        setActiveProvider(nextProvider);
        if (activeProviderRef.current) {
          showModal(`Provider switched to ${data.providers?.[nextProvider]?.label || nextProvider}.`, 'warning');
        }
      }
    } catch (err) {
      setMapStatus((prev) => ({
        ...(prev || {}),
        warning: 'Cloud totals unavailable.'
      }));
    }
  };

  const warmMapCache = async ({ eventId, lat, lon, reason }) => {
    if (!mapTileCacheEnabled) {
      if (reason === 'manual') {
        showModal('Map caching is disabled for this tile URL.', 'error');
      }
      return;
    }
    if (!satelliteAllowed) {
      if (reason === 'manual') {
        showModal('Satellite imagery is disabled due to budget limits.', 'error');
      }
      return;
    }
    if (!eventId) {
      if (reason === 'manual') {
        showModal('Start an event before warming the map cache.', 'error');
      }
      return;
    }
    if (lat == null || lon == null) {
      if (reason === 'manual') {
        showModal('GPS fix required to warm the map cache.', 'error');
      }
      return;
    }
    if (mapWarmRef.current.inFlight) return;
    const providerId = activeProvider || activeTileProvider?.id || 'esri';
    if (mapWarmRef.current.eventId === eventId && mapWarmRef.current.provider === providerId) {
      if (reason === 'manual') {
        showModal('Map cache already warmed for this event.', 'success');
      }
      return;
    }

    mapWarmRef.current.inFlight = true;
    setMapCacheStatus('Warming map cache for offline use...');

    try {
      await waitForServiceWorker();
      const tileUrls = new Set();
      for (const level of MAP_CACHE_LEVELS) {
        if (!level || !Number.isFinite(level.zoom) || !Number.isFinite(level.radiusMeters)) {
          continue;
        }
        const bounds = getTileBounds(lat, lon, level.radiusMeters, level.zoom);
        for (let x = bounds.minX; x <= bounds.maxX; x += 1) {
          for (let y = bounds.minY; y <= bounds.maxY; y += 1) {
            tileUrls.add(buildTileUrl(activeTileProvider.url, level.zoom, x, y));
          }
        }
      }

      const urlList = Array.from(tileUrls);
      const limited = urlList.slice(0, MAP_CACHE_MAX_TILES);
      await prefetchTileUrls(limited, MAP_CACHE_CONCURRENCY);
      mapWarmRef.current.eventId = eventId;
      mapWarmRef.current.provider = providerId;
      const summary = MAP_CACHE_LEVELS.filter(
        (level) => Number.isFinite(level.zoom) && Number.isFinite(level.radiusMeters)
      )
        .map((level) => `z${level.zoom} (${Math.round(level.radiusMeters)}m)`)
        .join(', ');
      const summaryText = summary ? ` (${summary})` : '';
      setMapCacheStatus(`Cached ${limited.length} tiles for offline use${summaryText}.`);
      if (reason === 'manual') {
        showModal('Map cache warmed for offline use.', 'success');
      }
    } catch (err) {
      console.warn('Failed to warm map cache:', err);
      setMapCacheStatus('Map cache warm-up failed. Check connection.');
      if (reason === 'manual') {
        showModal('Map cache warm-up failed.', 'error');
      }
    } finally {
      mapWarmRef.current.inFlight = false;
    }
  };

  const selectMapProvider = async (provider) => {
    if (!provider || provider === activeProviderRef.current) return;
    const label = mapProviders?.[provider]?.label || provider;
    const fallback = provider === 'mapbox' ? 'esri' : 'mapbox';
    const blocked = mapStatus?.blocked?.[provider];
    const fallbackBlocked = mapStatus?.blocked?.[fallback];

    if (blocked) {
      if (!fallbackBlocked) {
        setActiveProvider(fallback);
        showModal(
          `Provider near free-tier limit; switched to ${mapProviders?.[fallback]?.label || fallback}.`,
          'warning'
        );
      } else {
        showModal(mapStatus?.warning || 'Both providers are near the free-tier limit.', 'warning');
      }
      return;
    }

    setActiveProvider(provider);
    try {
      const resp = await fetch('/api/map-provider/preferred', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider })
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const recommended = data.recommendedProvider || fallback;
        if (recommended && recommended !== provider) {
          setActiveProvider(recommended);
          showModal(
            `Provider near free-tier limit; switched to ${
              mapProviders?.[recommended]?.label || recommended
            }.`,
            'warning'
          );
        } else {
          showModal(data.error || 'Provider update failed.', 'error');
        }
        return;
      }
      if (data.warning) {
        showModal(data.warning, 'warning');
      }
    } catch (err) {
      showModal(`Failed to update provider: ${err.message}`, 'error');
    }
  };

  const loadGxSystems = async () => {
    try {
      const data = await getJson('/systems');
      const systems = data.systems || [];
      setGxSystems(systems);
      // Auto-select first system if none selected
      if (systems.length > 0 && !gxSystemId) {
        setGxSystemId(systems[0].system_id);
      }
      return systems;
    } catch (err) {
      console.warn('Failed to load GX systems:', err);
      return [];
    }
  };

  const loadGXSettings = async (systemId) => {
    try {
      const targetId = systemId || gxSystemId;
      const url = targetId ? `/gx/settings?system_id=${encodeURIComponent(targetId)}` : '/gx/settings';
      const data = await getJson(url);
      setGxSettings(data);
    } catch (err) {
      showModal(`Error loading settings: ${err.message}`, 'error');
    }
  };

  const stopGxSettingPoll = () => {
    if (gxPollRef.current.handle) {
      clearTimeout(gxPollRef.current.handle);
      gxPollRef.current.handle = null;
    }
  };

  const normalizeExpectedValue = (settingName, value) => {
    if (settingName === 'inverter_mode') {
      const modeMap = { on: 3, charger_only: 1, inverter_only: 2, off: 4 };
      if (typeof value === 'string' && value in modeMap) {
        return modeMap[value];
      }
    }
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  };

  const isSettingApplied = (settingName, expectedValue, settings) => {
    const setting = settings?.[settingName];
    if (!setting || setting.value === null || setting.value === undefined) return false;
    const actual = Number(setting.value);
    if (!Number.isFinite(actual) || expectedValue === null) return false;
    const tolerance = settingName === 'inverter_output_voltage' ? 0.5 : 0.1;
    return Math.abs(actual - expectedValue) <= tolerance;
  };

  const startGxSettingPoll = (settingName, expectedValue) => {
    stopGxSettingPoll();
    gxPollRef.current.startedAt = Date.now();
    gxPollRef.current.setting = settingName;
    gxPollRef.current.expected = normalizeExpectedValue(settingName, expectedValue);
    setGxStatus({
      message: 'Update sent. Current values may take a moment to refresh. Polling for confirmation...',
      level: 'info'
    });

    const tick = async () => {
      try {
        const settings = await getJson('/gx/settings');
        setGxSettings(settings);
        if (
          gxPollRef.current.expected !== null &&
          isSettingApplied(settingName, gxPollRef.current.expected, settings)
        ) {
          setGxStatus({ message: 'Current value updated.', level: 'success' });
          stopGxSettingPoll();
          return;
        }
      } catch (err) {
        console.warn('GX settings poll failed:', err);
      }

      if (Date.now() - gxPollRef.current.startedAt >= 20000) {
        setGxStatus({
          message: 'Update sent. Current values may still be syncing. Consider refreshing values.',
          level: 'warn'
        });
        stopGxSettingPoll();
        return;
      }

      gxPollRef.current.handle = setTimeout(tick, 2000);
    };

    gxPollRef.current.handle = setTimeout(tick, 1000);
  };

  const setGXSetting = async (settingName, value) => {
    if (!value) {
      showModal('Please enter a value', 'error');
      return;
    }
    const targetSystem = gxSystemId || (gxSystems[0]?.system_id);
    const displaySystem = targetSystem ? ` on ${targetSystem}` : '';
    if (!window.confirm(`Set ${settingName.replace(/_/g, ' ')} to ${value}${displaySystem}?`)) return;

    try {
      const payload = { setting: settingName, value };
      if (targetSystem) payload.system_id = targetSystem;
      const result = await postJson('/gx/setting', payload);
      showModal(result.message || 'Setting updated', 'success');
      startGxSettingPoll(settingName, value);
      setGxInputs((prev) => ({ ...prev, [settingName]: '' }));
    } catch (err) {
      showModal(`Error: ${err.message}`, 'error');
    }
  };

  const setGXSettingSelect = async (settingName, value) => {
    const targetSystem = gxSystemId || (gxSystems[0]?.system_id);
    const displaySystem = targetSystem ? ` on ${targetSystem}` : '';
    if (!window.confirm(`Change inverter mode to "${value.replace(/_/g, ' ')}"${displaySystem}?`)) return;

    try {
      const payload = { setting: settingName, value };
      if (targetSystem) payload.system_id = targetSystem;
      const result = await postJson('/gx/setting', payload);
      showModal(result.message || 'Setting updated', 'success');
      startGxSettingPoll(settingName, value);
      setGxInputs((prev) => ({ ...prev, inverter_mode: value }));
    } catch (err) {
      showModal(`Error: ${err.message}`, 'error');
    }
  };
  useEffect(() => {
    loadSummary();
    loadDashboard();
    loadGps(false);
    loadActiveLocations();

    const fastTimer = setInterval(loadSummary, 1000);
    const slowTimer = setInterval(loadSummary, 10000);
    const dashboardTimer = setInterval(loadDashboard, 2000);

    return () => {
      clearInterval(fastTimer);
      clearInterval(slowTimer);
      clearInterval(dashboardTimer);
      stopGxSettingPoll();
    };
  }, []);

  // Switch away from dashboard tab if no longer multi-system
  useEffect(() => {
    if (!isMultiSystem && activeTab === 'dashboard') {
      setActiveTab('events');
    }
  }, [isMultiSystem, activeTab]);

  useEffect(() => {
    if (activeEventId) {
      setEventIdInput(activeEventId);
    }
    setFormLoggers([{ ...DEFAULT_LOGGER }]);
    setSelectedNoteIds([]);
    setSelectedImageIds([]);
    setNoteService('');
    if (!activeEventId || mapWarmRef.current.eventId !== activeEventId) {
      mapWarmRef.current.eventId = '';
      mapWarmRef.current.inFlight = false;
      setMapCacheStatus('');
    }
  }, [activeEventId]);

  useEffect(() => {
    setFormLoggers((prev) => normalizeLoggers(prev, activeServices));
  }, [activeServices]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    const params = new URLSearchParams(window.location.search);
    const eventParam = params.get('event_id');
    if (eventParam && !eventIdInput) {
      setEventIdInput(eventParam);
    }
  }, []);

  useEffect(() => {
    if (activeEventId) {
      loadNotes(activeEventId);
    } else if (eventIdInput) {
      loadNotes(eventIdInput);
    } else {
      setNotes([]);
    }
  }, [activeEventId, eventIdInput]);

  useEffect(() => {
    if (!gxSettings?.inverter_mode) return;
    const modeValue = INVERTER_MODE_VALUES[Number(gxSettings.inverter_mode.value)] || 'on';
    setGxInputs((prev) => ({ ...prev, inverter_mode: modeValue }));
  }, [gxSettings?.inverter_mode?.value]);

  useEffect(() => {
    if (!imageFile) {
      setImagePreview('');
      return;
    }
    const url = URL.createObjectURL(imageFile);
    setImagePreview(url);
    return () => URL.revokeObjectURL(url);
  }, [imageFile]);

  useEffect(() => {
    setSelectedNoteIds((prev) => prev.filter((id) => notes.some((note) => note.id === id)));
  }, [notes]);

  useEffect(() => {
    setSelectedImageIds((prev) => prev.filter((id) => images.some((img) => img.id === id)));
  }, [images]);

  const handleLoggerChange = (id, field, value) => {
    setFormLoggers((prev) =>
      prev.map((entry) => (entry.id === id ? { ...entry, [field]: value } : entry))
    );
  };

  const addLogger = () => {
    setFormLoggers((prev) => [
      ...prev,
      { id: loggerIdRef.current++, service: '', location: '' }
    ]);
  };

  const removeLogger = (id) => {
    setFormLoggers((prev) => prev.filter((entry) => entry.id !== id));
  };

  const getSelectedLoggers = () =>
    formLoggers
      .filter((logger) => logger.service)
      .map((logger) => ({
        service: logger.service,
        location: logger.location.trim()
      }));

  const startEvent = async () => {
    const loggers = getSelectedLoggers();
    if (loggers.length === 0) {
      showModal('At least one logger required', 'error');
      return;
    }

    try {
      let eventId = eventIdInput.trim();
      for (let i = 0; i < loggers.length; i += 1) {
        const logger = loggers[i];
        const payload = {
          system_id: logger.service,
          location: logger.location || '',
          note: i === 0 ? noteText.trim() : ''
        };
        if (eventId) {
          payload.event_id = eventId;
        }
        const result = await postJson('/event/start', payload);
        if (!eventId) {
          eventId = result?.event_id || '';
          if (eventId) {
            setEventIdInput(eventId);
          }
        }
      }
      if (!eventId) {
        throw new Error('Missing event ID in response');
      }
      showModal(`Event "${eventId}" started with ${loggers.length} logger(s)`, 'success');
      setNoteText('');
      const gpsData = await loadGps(true);
      await loadActiveLocations();
      const lat = gpsData?.latitude ?? gps.latitude;
      const lon = gpsData?.longitude ?? gps.longitude;
      if (lat != null && lon != null) {
        warmMapCache({ eventId, lat, lon, reason: 'auto' });
      }
    } catch (err) {
      showModal(`Error: ${err.message}`, 'error');
    }
  };

  const addLoggerToEvent = async () => {
    const eventId = activeEventId || eventIdInput.trim();
    const loggers = getSelectedLoggers();
    if (!eventId) {
      showModal('Event ID not found', 'error');
      return;
    }
    if (loggers.length === 0) {
      showModal('At least one logger required', 'error');
      return;
    }

    try {
      for (const logger of loggers) {
        await postJson('/event/start', {
          system_id: logger.service,
          event_id: eventId,
          location: logger.location || ''
        });
      }
      showModal(`Added ${loggers.length} logger(s) to event "${eventId}"`, 'success');
      await loadActiveLocations();
    } catch (err) {
      showModal(`Error: ${err.message}`, 'error');
    }
  };

  const endAllLoggers = async () => {
    const eventId = activeEventId || eventIdInput.trim();
    if (!eventId) {
      showModal('Event ID not found', 'error');
      return;
    }
    if (!window.confirm(`End ALL loggers for event "${eventId}"?`)) return;

    try {
      const result = await postJson('/event/end_all', { event_id: eventId });
      const reportLink = result.report_pdf_url || result.report_url;
      if (reportLink) {
        showModal(
          `Event "${eventId}" ended (${result.loggers_ended} loggers)`,
          'success',
          { label: result.report_pdf_url ? 'View PDF' : 'View Report', url: reportLink }
        );
      } else {
        showModal(`Event "${eventId}" ended (${result.loggers_ended} loggers)`, 'success');
      }
      setEventIdInput('');
      setNoteText('');
      setFormLoggers([{ ...DEFAULT_LOGGER }]);
      await loadActiveLocations();
    } catch (err) {
      showModal(`Error: ${err.message}`, 'error');
    }
  };

  const setLocation = async () => {
    const loggers = getSelectedLoggers();
    let successCount = 0;

    try {
      for (const logger of loggers) {
        if (!logger.location) continue;
        await postJson('/location/set', {
          system_id: logger.service,
          location: logger.location
        });
        successCount += 1;
      }
      if (successCount > 0) {
        showModal(`${successCount} location(s) set`, 'success');
      } else {
        showModal('No locations to set', 'error');
      }
    } catch (err) {
      showModal(`Error: ${err.message}`, 'error');
    }
  };

  const removeActiveLogger = async (systemId) => {
    if (!window.confirm(`Remove ${systemId} from this event?`)) return;
    try {
      await postJson('/location/clear', { system_id: systemId });
      showModal(`${systemId} removed`, 'success');
      await loadActiveLocations();
    } catch (err) {
      showModal(`Error: ${err.message}`, 'error');
    }
  };

  const addNote = async () => {
    const eventId = activeEventId || eventIdInput.trim();
    let msg = noteText.trim();
    if (!msg) {
      showModal('Note required', 'error');
      return;
    }
    if (noteService) {
      msg = `[${noteService}] ${msg}`;
    }
    try {
      await postJson('/note', { event_id: eventId || undefined, msg });
      showModal('Note added', 'success');
      setNoteText('');
      setNoteService('');
      await loadNotes(eventId || eventIdInput.trim());
    } catch (err) {
      showModal(`Error: ${err.message}`, 'error');
    }
  };

  const deleteNote = async (note) => {
    if (!window.confirm('Delete this note?')) return;
    try {
      await postJson('/audit/delete', {
        system_id: note.system_id,
        event_id: note.event_id,
        note_id: note.id,
        note_text: note.note
      });
      showModal('Note deleted', 'success');
      await loadNotes(note.event_id);
    } catch (err) {
      showModal(`Error: ${err.message}`, 'error');
    }
  };

  const toggleSelectAllNotes = (checked) => {
    if (!checked) {
      setSelectedNoteIds([]);
      return;
    }
    setSelectedNoteIds(notes.map((note) => note.id));
  };

  const deleteSelectedNotes = async () => {
    if (selectedNoteIds.length === 0) {
      showModal('No notes selected', 'error');
      return;
    }
    const count = selectedNoteIds.length;
    if (!window.confirm(`Delete ${count} note${count > 1 ? 's' : ''}?`)) return;

    let successCount = 0;
    let failCount = 0;
    for (const note of notes) {
      if (!selectedNoteIds.includes(note.id)) continue;
      try {
        await postJson('/audit/delete', {
          system_id: note.system_id,
          event_id: note.event_id,
          note_id: note.id,
          note_text: note.note
        });
        successCount += 1;
      } catch (err) {
        failCount += 1;
        console.error('Failed to delete note:', err);
      }
    }

    if (successCount > 0) {
      showModal(`Deleted ${successCount} note${successCount > 1 ? 's' : ''}`, 'success');
    }
    if (failCount > 0) {
      showModal(`Failed to delete ${failCount} note${failCount > 1 ? 's' : ''}`, 'error');
    }
    setSelectedNoteIds([]);
    await loadNotes(activeEventId || eventIdInput.trim());
  };

  const uploadImage = async () => {
    if (!imageFile) {
      showModal('Please select an image first', 'error');
      return;
    }

    const eventId = activeEventId || eventIdInput.trim();
    const formData = new FormData();
    formData.append('image', imageFile);
    if (eventId) formData.append('event_id', eventId);
    if (imageCaption) formData.append('caption', imageCaption);

    try {
      const result = await uploadForm('/image/upload', formData);
      showModal(`Image uploaded (${(result.size / 1024).toFixed(1)}KB)`, 'success');
      setImageFile(null);
      setImageCaption('');
      if (imageInputRef.current) {
        imageInputRef.current.value = '';
      }
      if (imagesVisible) {
        await loadImages();
      }
    } catch (err) {
      showModal(`Upload error: ${err.message}`, 'error');
    }
  };

  const toggleSelectAllImages = (checked) => {
    if (!checked) {
      setSelectedImageIds([]);
      return;
    }
    setSelectedImageIds(images.map((img) => img.id));
  };

  const deleteSelectedImages = async () => {
    if (selectedImageIds.length === 0) {
      showModal('No images selected', 'error');
      return;
    }
    const count = selectedImageIds.length;
    if (!window.confirm(`Delete ${count} image${count > 1 ? 's' : ''}?`)) return;

    let successCount = 0;
    let failCount = 0;
    for (const imageId of selectedImageIds) {
      try {
        await deleteJson(`/image/${imageId}`);
        successCount += 1;
      } catch (err) {
        failCount += 1;
        console.error('Failed to delete image:', err);
      }
    }

    if (successCount > 0) {
      showModal(`Deleted ${successCount} image${successCount > 1 ? 's' : ''}`, 'success');
    }
    if (failCount > 0) {
      showModal(`Failed to delete ${failCount} image${failCount > 1 ? 's' : ''}`, 'error');
    }
    setSelectedImageIds([]);
    await loadImages();
  };

  const deleteImage = async (imageId) => {
    if (!window.confirm('Delete this image?')) return;
    try {
      await deleteJson(`/image/${imageId}`);
      showModal('Image deleted', 'success');
      await loadImages();
    } catch (err) {
      showModal(`Delete error: ${err.message}`, 'error');
    }
  };

  const startEditImageCaption = (image) => {
    setEditingImageId(image.id);
    setEditingImageCaption(image.caption || '');
  };

  const cancelEditImageCaption = () => {
    setEditingImageId(null);
    setEditingImageCaption('');
  };

  const saveImageCaption = async (image) => {
    try {
      await postJson(`/image/${image.id}/caption`, {
        caption: editingImageCaption.trim()
      });
      showModal('Caption updated', 'success');
      cancelEditImageCaption();
      await loadImages();
    } catch (err) {
      showModal(`Caption update failed: ${err.message}`, 'error');
    }
  };

  const renderLoggerSelectOptions = (currentService) => {
    const selected = new Set(formLoggers.map((entry) => entry.service).filter(Boolean));
    return SERVICE_OPTIONS.map((option) => {
      const disabled =
        activeServices.has(option) || (selected.has(option) && option !== currentService);
      return (
        <option key={option} value={option} disabled={disabled}>
          {option}
        </option>
      );
    });
  };

  const inverterModeValue = useMemo(() => {
    const value = gxSettings?.inverter_mode?.value;
    if (value === null || value === undefined) return 'on';
    return INVERTER_MODE_VALUES[Number(value)] || 'on';
  }, [gxSettings]);
  return (
    <div className="container">
      <div className="logo-header">
        <img
          src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 640'%3E%3Crect fill='%23000' width='640' height='640'/%3E%3Ctext x='50%25' y='35%25' fill='%23fff' font-size='180' font-weight='900' text-anchor='middle' font-family='Arial, sans-serif'%3EOVR%3C/text%3E%3Crect x='82' y='275' width='476' height='20' fill='%2340C463'/%3E%3Ctext x='50%25' y='75%25' fill='%23fff' font-size='180' font-weight='900' text-anchor='middle' font-family='Arial, sans-serif'%3EDRV%3C/text%3E%3Ctext x='50%25' y='87%25' fill='%2340C463' font-size='32' font-weight='600' text-anchor='middle' font-family='Arial, sans-serif'%3EENERGY SOLUTIONS%3C/text%3E%3C/svg%3E"
          alt="Overdrive Energy Solutions"
        />
      </div>

      {!isMultiSystem && (
        <div className="top-summary">
          <div className={`summary-item ${summarySocClass}`}>
            <span className="summary-label">SOC</span>
            <span className="summary-value">{formatPercent(summary.soc)}</span>
          </div>
          {singleSystem && (
            <div className="summary-item">
              <span className="summary-label">Voltage</span>
              <span className="summary-value">
                {singleSystem.voltage !== null ? `${singleSystem.voltage.toFixed(1)}V` : '--'}
              </span>
            </div>
          )}
          <div className="summary-item">
            <span className="summary-label">P in</span>
            <span className="summary-value">{formatPower(summary.pin)}</span>
          </div>
          <div className="summary-item">
            <span className="summary-label">P out</span>
            <span className="summary-value">{formatPower(summary.pout)}</span>
          </div>
          {singleSystem && (
            <div className="summary-item">
              <span className="summary-label">Mode</span>
              <span className="summary-value">
                {INVERTER_MODE_LABELS[singleSystem.mode] || '--'}
              </span>
            </div>
          )}
          <div className={`summary-item ${summaryAlertClass}`}>
            <span className="summary-label">Alerts</span>
            <span className="summary-value summary-alert" title={(summary.alerts || []).map(formatAlertName).join(', ')}>
              {alertsText}
            </span>
          </div>
        </div>
      )}

      <div className="tabs">
        <button className={`tab ${activeTab === 'events' ? 'active' : ''}`} onClick={() => setActiveTab('events')}>
          Events
        </button>
        {isMultiSystem && (
          <button
            className={`tab ${activeTab === 'dashboard' ? 'active' : ''}`}
            onClick={() => {
              setActiveTab('dashboard');
              loadDashboard();
            }}
          >
            Dashboard
          </button>
        )}
        <button
          className={`tab ${activeTab === 'control' ? 'active' : ''}`}
          onClick={async () => {
            setActiveTab('control');
            const systems = await loadGxSystems();
            if (systems.length > 0) {
              loadGXSettings(systems[0].system_id);
            } else {
              loadGXSettings();
            }
          }}
        >
          GX Control
        </button>
        <button
          className={`tab ${activeTab === 'map' ? 'active' : ''}`}
          onClick={() => {
            setActiveTab('map');
            loadGps(false);
          }}
        >
          Map
        </button>
      </div>

      <div className={`tab-content ${activeTab === 'dashboard' ? 'active' : ''}`}>
        <div className="dashboard-grid">
          {dashboardSystems.length === 0 ? (
            <div className="info-box">No GX systems discovered</div>
          ) : (
            dashboardSystems.map((sys) => {
              const socClass = sys.soc >= 80 ? 'soc-good' : sys.soc >= 30 ? 'soc-warn' : 'soc-bad';
              const modeLabel = INVERTER_MODE_LABELS[sys.mode] || `Mode ${sys.mode}`;
              return (
                <div key={sys.system_id} className="dashboard-card">
                  <div className="dashboard-card-header">
                    <span className="dashboard-system-name">{sys.system_id}</span>
                    {sys.alerts_count > 0 && (
                      <span
                        className="dashboard-alert-badge"
                        title={(sys.alerts || []).join(', ')}
                      >
                        {sys.alerts_count}
                      </span>
                    )}
                  </div>
                  <div className="dashboard-card-body">
                    <div className={`dashboard-metric ${socClass}`}>
                      <span className="dashboard-metric-label">SOC</span>
                      <span className="dashboard-metric-value">
                        {sys.soc !== null ? `${Math.round(sys.soc)}%` : '--'}
                      </span>
                    </div>
                    <div className="dashboard-metric">
                      <span className="dashboard-metric-label">Voltage</span>
                      <span className="dashboard-metric-value">
                        {sys.voltage !== null ? `${sys.voltage.toFixed(1)}V` : '--'}
                      </span>
                    </div>
                    <div className="dashboard-metric">
                      <span className="dashboard-metric-label">P in</span>
                      <span className="dashboard-metric-value">{formatPower(sys.pin)}</span>
                    </div>
                    <div className="dashboard-metric">
                      <span className="dashboard-metric-label">P out</span>
                      <span className="dashboard-metric-value">{formatPower(sys.pout)}</span>
                    </div>
                    <div className="dashboard-metric">
                      <span className="dashboard-metric-label">Mode</span>
                      <span className="dashboard-metric-value">{modeLabel}</span>
                    </div>
                    {sys.alerts && sys.alerts.length > 0 && (
                      <div className="dashboard-metric dashboard-alerts">
                        <span className="dashboard-metric-label">Alerts</span>
                        <div className="dashboard-alerts-list">
                          {sys.alerts.map((alert, idx) => (
                            <span key={idx} className="dashboard-alert-chip" title={alert}>
                              {formatAlertName(alert)}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      <div className={`tab-content ${activeTab === 'events' ? 'active' : ''}`}>
        {isEventActive && (
          <div className="info-box" id="statusBox">
            <h3>
              Active Event: <span>{activeEventId}</span>
            </h3>
            <div id="activeLoggersContainer">
              {activeLoggers.map((logger) => (
                <div key={logger.system_id} className="active-logger-item">
                  <div className="active-logger-info">
                    <div className="active-logger-label">{logger.system_id}</div>
                    <div className="active-logger-location">
                      {logger.location || 'No location set'}
                    </div>
                  </div>
                  <button
                    className="btn-remove-logger"
                    onClick={() => removeActiveLogger(logger.system_id)}
                  >
                    Remove
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="events-layout">
          <div className="events-main">
            {!isEventActive && (
              <div className="form-group">
                <label htmlFor="eventId">Site *</label>
                <input
                  type="text"
                  id="eventId"
                  placeholder="e.g., warehouse, customer_site_a"
                  value={eventIdInput}
                  onChange={(e) => setEventIdInput(e.target.value)}
                />
              </div>
            )}

            <div id="loggersContainer">
              {formLoggers.map((logger, idx) => (
                <div className="logger-entry" key={logger.id}>
                  <div className="form-group">
                    <div className="logger-header">
                      <label htmlFor={`service_${logger.id}`}>Service</label>
                      {idx > 0 && (
                        <button
                          className="remove-logger"
                          type="button"
                          onClick={() => removeLogger(logger.id)}
                        >
                          Remove
                        </button>
                      )}
                    </div>
                    <select
                      id={`service_${logger.id}`}
                      value={logger.service}
                      onChange={(e) => handleLoggerChange(logger.id, 'service', e.target.value)}
                    >
                      <option value="">-- Select Service --</option>
                      {renderLoggerSelectOptions(logger.service)}
                    </select>
                  </div>
                  <div className="form-group">
                    <label htmlFor={`location_${logger.id}`}>Location (optional)</label>
                    <input
                      type="text"
                      id={`location_${logger.id}`}
                      placeholder="e.g., bay_3, north_yard"
                      value={logger.location}
                      onChange={(e) => handleLoggerChange(logger.id, 'location', e.target.value)}
                    />
                  </div>
                </div>
              ))}
            </div>

            <button className="add-logger-btn" type="button" onClick={addLogger}>
              + Add Additional Logger
            </button>

            <div className="form-group note-entry note-entry-mobile">
              <label htmlFor="noteMobile">Note (optional)</label>
              <select
                id="noteServiceMobile"
                style={{ marginBottom: '8px', fontSize: '14px', padding: '8px' }}
                value={noteService}
                onChange={(e) => setNoteService(e.target.value)}
              >
                <option value="">General note (all loggers)</option>
                {noteServiceOptions.map((service) => (
                  <option key={service} value={service}>
                    {service}
                  </option>
                ))}
              </select>
              <textarea
                id="noteMobile"
                placeholder="Add a note or observation..."
                value={noteText}
                onChange={(e) => setNoteText(e.target.value)}
              />
            </div>

            <button className="btn btn-start" onClick={isEventActive ? addLoggerToEvent : startEvent}>
              {isEventActive ? '+ ADD LOGGER' : 'START EVENT'}
            </button>
            {isEventActive && (
              <button className="btn btn-end" onClick={endAllLoggers}>
                End Event
              </button>
            )}
            <button className="btn btn-location" onClick={setLocation}>
              SET LOCATION
            </button>
            <button className="btn btn-note btn-note-mobile" onClick={addNote}>
              ADD NOTE
            </button>
          </div>

          <div className="events-side">
            <div className="form-group note-entry note-entry-desktop">
              <label htmlFor="noteDesktop">Note (optional)</label>
              <select
                id="noteServiceDesktop"
                style={{ marginBottom: '8px', fontSize: '14px', padding: '8px' }}
                value={noteService}
                onChange={(e) => setNoteService(e.target.value)}
              >
                <option value="">General note (all loggers)</option>
                {noteServiceOptions.map((service) => (
                  <option key={service} value={service}>
                    {service}
                  </option>
                ))}
              </select>
              <textarea
                id="noteDesktop"
                placeholder="Add a note or observation..."
                value={noteText}
                onChange={(e) => setNoteText(e.target.value)}
              />
              <button className="btn btn-note btn-note-desktop" onClick={addNote}>
                ADD NOTE
              </button>
            </div>
            {notes.length > 0 && (
              <div className="notes-section">
                <div className="notes-header">
                  <h3>Event Notes</h3>
                  <div className="notes-toolbar">
                    {notes.length > 1 && (
                      <button
                        className="btn"
                        style={{ padding: '8px 16px', fontSize: '14px', background: '#ef4444' }}
                        onClick={deleteSelectedNotes}
                        disabled={selectedNoteIds.length === 0}
                      >
                        Delete Selected
                      </button>
                    )}
                    <button
                      className="btn"
                      style={{ padding: '8px 16px', fontSize: '14px' }}
                      onClick={() => setNotesVisible((prev) => !prev)}
                    >
                      {notesVisible ? 'Hide Notes' : 'Show Notes'}
                    </button>
                  </div>
                </div>
                <div className="notes-container" style={{ display: notesVisible ? 'block' : 'none' }}>
                  {notes.length > 1 && (
                    <div className="notes-list-header">
                      <label style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        <input
                          type="checkbox"
                          checked={selectedNoteIds.length === notes.length}
                          onChange={(e) => toggleSelectAllNotes(e.target.checked)}
                        />
                        <span>Select All</span>
                      </label>
                    </div>
                  )}
                  {notes.map((note) => {
                    const date = new Date(note.timestamp / 1e6).toLocaleString();
                    return (
                      <div className="note-card" key={note.id}>
                        {notes.length > 1 && (
                          <input
                            type="checkbox"
                            checked={selectedNoteIds.includes(note.id)}
                            onChange={(e) => {
                              if (e.target.checked) {
                                setSelectedNoteIds((prev) => [...prev, note.id]);
                              } else {
                                setSelectedNoteIds((prev) =>
                                  prev.filter((id) => id !== note.id)
                                );
                              }
                            }}
                          />
                        )}
                        <div>
                          <div className="note-meta">
                            {note.system_id} - {date}
                          </div>
                          <div className="note-text">{note.note}</div>
                        </div>
                        <div className="note-actions">
                          <button className="note-delete" onClick={() => deleteNote(note)}>
                            Delete
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            <div className="form-group" style={{ marginTop: '20px' }}>
              <label htmlFor="imageFile">Upload Image (optional)</label>
              <input
                type="file"
                id="imageFile"
                ref={imageInputRef}
                accept="image/*"
                capture="environment"
                onChange={(e) => setImageFile(e.target.files?.[0] || null)}
              />
              {imagePreview && (
                <div style={{ marginBottom: '10px', marginTop: '10px' }}>
                  <img
                    src={imagePreview}
                    alt="Preview"
                    style={{ maxWidth: '100%', maxHeight: '200px', borderRadius: '8px' }}
                  />
                </div>
              )}
              <input
                type="text"
                id="imageCaption"
                placeholder="Image caption (optional)"
                value={imageCaption}
                onChange={(e) => setImageCaption(e.target.value)}
                style={{ marginBottom: '10px' }}
              />
              <button
                className="btn"
                onClick={uploadImage}
                style={{ background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)' }}
              >
                UPLOAD IMAGE
              </button>
            </div>

            <div className="form-group" style={{ marginTop: '20px' }}>
              <button
                className="btn"
                onClick={async () => {
                  setImagesVisible(true);
                  await loadImages();
                }}
                style={{ background: 'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)' }}
              >
                VIEW IMAGES
              </button>
            </div>

            {imagesVisible && (
              <div className="image-gallery" style={{ display: 'block' }}>
                <div className="notes-header">
                  <h3>Image Gallery</h3>
                  {images.length > 1 && (
                    <button
                      className="btn"
                      style={{ padding: '8px 16px', fontSize: '14px', background: '#ef4444' }}
                      onClick={deleteSelectedImages}
                      disabled={selectedImageIds.length === 0}
                    >
                      Delete Selected
                    </button>
                  )}
                </div>
                {images.length > 1 && (
                  <div className="notes-list-header">
                    <label style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <input
                        type="checkbox"
                        checked={selectedImageIds.length === images.length}
                        onChange={(e) => toggleSelectAllImages(e.target.checked)}
                      />
                      <span>Select All</span>
                    </label>
                  </div>
                )}
                <div className="image-grid">
                  {images.length === 0 && (
                    <p style={{ color: '#6b7280', gridColumn: '1 / -1' }}>
                      No images uploaded yet
                    </p>
                  )}
                  {images.map((img) => {
                    const date = new Date(img.timestamp / 1e6).toLocaleString();
                    const isEditing = editingImageId === img.id;
                    return (
                      <div
                        key={img.id}
                        className="image-card"
                        onClick={() => {
                          if (!isEditing) {
                            setLightboxImage(img);
                          }
                        }}
                      >
                        {images.length > 1 && (
                          <input
                            type="checkbox"
                            className="image-checkbox"
                            checked={selectedImageIds.includes(img.id)}
                            onChange={(e) => {
                              e.stopPropagation();
                              if (e.target.checked) {
                                setSelectedImageIds((prev) => [...prev, img.id]);
                              } else {
                                setSelectedImageIds((prev) =>
                                  prev.filter((id) => id !== img.id)
                                );
                              }
                            }}
                            onClick={(e) => e.stopPropagation()}
                          />
                        )}
                        <img
                          className="image-thumb"
                          src={`/images/${img.filename}`}
                          alt={img.caption || img.original_filename}
                        />
                        {isEditing ? (
                          <div
                            className="image-caption-edit"
                            onClick={(e) => e.stopPropagation()}
                          >
                            <input
                              type="text"
                              value={editingImageCaption}
                              placeholder="Add a caption..."
                              onChange={(e) => setEditingImageCaption(e.target.value)}
                            />
                            <div className="image-caption-actions">
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  saveImageCaption(img);
                                }}
                              >
                                Save
                              </button>
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  cancelEditImageCaption();
                                }}
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <>
                            <div
                              className="image-caption"
                              title={img.caption || img.original_filename}
                            >
                              {img.caption || img.original_filename}
                            </div>
                            <button
                              type="button"
                              className="image-caption-edit-btn"
                              onClick={(e) => {
                                e.stopPropagation();
                                startEditImageCaption(img);
                              }}
                            >
                              {img.caption ? 'Edit caption' : 'Add caption'}
                            </button>
                          </>
                        )}
                        <div className="image-timestamp">{date}</div>
                        <button
                          className="image-delete"
                          onClick={(e) => {
                            e.stopPropagation();
                            deleteImage(img.id);
                          }}
                        >
                          x
                        </button>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
      <div className={`tab-content ${activeTab === 'map' ? 'active' : ''}`}>
          <div className="info-box map-info-box">
            <div className="map-info-row">
              <div className="map-info-details">
                <h3>System Map</h3>
                <div className="info-item">
                  <strong>GPS:</strong>{' '}
                  {gps.latitude == null
                    ? 'No GPS fix'
                    : `${gps.latitude.toFixed(6)}, ${gps.longitude.toFixed(6)}`}
                </div>
                <div className="info-item">
                  <strong>Last Fix:</strong> {formatTimestampNs(gps.updatedAt)}
                </div>
              </div>
              <div className="map-info-controls">
                <div className="map-provider-toggle">
                  {['mapbox', 'esri'].map((provider) => {
                    const label = mapProviders?.[provider]?.label || provider;
                    const blocked = mapBlocked?.[provider];
                    const reason = !satelliteAllowed
                      ? 'Satellite imagery disabled due to budget limits.'
                      : blocked
                        ? `${guardrailPctLabel}% free tier reached for ${mapMonthLabel}`
                        : `Switch to ${label}`;
                    return (
                      <button
                        key={provider}
                        className={`map-provider-btn ${
                          activeProvider === provider ? 'active' : ''
                        } ${blocked ? 'blocked' : ''}`}
                        disabled={blocked || !satelliteAllowed}
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
                          L {formatCount(localHudCount(provider))} | F{' '}
                          {fleetValue == null ? '--' : formatCount(fleetValue)} |{' '}
                          {formatPercent(mapPct?.[provider])}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>
          </div>
        {mapWarning && (
          <div className={`map-warning ${mapBothBlocked ? 'critical' : ''}`}>{mapWarning}</div>
        )}
          <div className="map-shell">
            <MapPanel
              latitude={gps.latitude}
              longitude={gps.longitude}
              systemIds={dashboardSystems.map((s) => s.system_id)}
              providers={mapProviders}
              activeProvider={activeProvider}
              onTileAttempt={incrementMapUsage}
              defaultZoom={MAP_DEFAULT_ZOOM}
              tilesEnabled={satelliteAllowed}
              isActive={activeTab === 'map'}
            />
          </div>
        <div className="map-actions">
          <button className="btn" onClick={() => loadGps(true)}>
            Refresh GPS
          </button>
          <button
            className="btn"
            onClick={() =>
              warmMapCache({
                eventId: activeEventId || eventIdInput.trim(),
                lat: gps.latitude,
                lon: gps.longitude,
                reason: 'manual'
              })
            }
            disabled={
              !isEventActive ||
              !mapTileCacheEnabled ||
              gps.latitude == null ||
              gps.longitude == null
            }
          >
            Warm Map Cache
          </button>
          <a
            href={
              gps.latitude == null
                ? '#'
                : `https://www.google.com/maps/dir/?api=1&destination=${gps.latitude},${gps.longitude}`
            }
            target="_blank"
            rel="noopener"
          >
            Open in Google Maps
          </a>
        </div>
        <div className="map-meta">
          Auto-warm caches around the last GPS fix (about 1km at z16 and 200m at z18).
        </div>
        {mapCacheStatus && <div className="map-meta">{mapCacheStatus}</div>}
      </div>

      <div className={`tab-content ${activeTab === 'control' ? 'active' : ''}`}>
        <div className="info-box" style={{ background: '#fef3c7', border: '1px solid #f59e0b' }}>
          <strong>Warning:</strong> Changes affect the GX device immediately. Use with caution.
        </div>

        {gxSystems.length > 1 && (
          <div className="form-group" style={{ marginBottom: '20px' }}>
            <label htmlFor="gxSystemSelect">Target System</label>
            <select
              id="gxSystemSelect"
              className="control-input"
              style={{ padding: '12px', maxWidth: '300px' }}
              value={gxSystemId}
              onChange={(e) => {
                setGxSystemId(e.target.value);
                loadGXSettings(e.target.value);
              }}
            >
              {gxSystems.map((sys) => (
                <option key={sys.system_id} value={sys.system_id}>
                  {sys.system_id}
                </option>
              ))}
            </select>
          </div>
        )}

        {gxStatus.message && (
          <div className={`gx-status gx-${gxStatus.level}`}>{gxStatus.message}</div>
        )}

        <div className="control-grid">
          <div className="control-item">
            <label>Battery Charge Current Limit</label>
            <div className="description">Maximum charging current from grid/generator (Amps)</div>
            <div className="current-value">
              Current: {gxSettings?.battery_charge_current?.value ?? 'Loading...'}
              {formatAge(gxSettings?.battery_charge_current?.updated_at)}
            </div>
            <input
              type="number"
              className="control-input"
              placeholder="e.g., 50"
              min="0"
              max="600"
              value={gxInputs.battery_charge_current}
              onChange={(e) =>
                setGxInputs((prev) => ({
                  ...prev,
                  battery_charge_current: e.target.value
                }))
              }
            />
            <button
              className="btn"
              onClick={() => setGXSetting('battery_charge_current', gxInputs.battery_charge_current)}
              style={{ width: '100%', marginTop: '10px' }}
            >
              SET
            </button>
          </div>

          <div className="control-item">
            <label>Inverter Mode</label>
            <div className="description">Control inverter operation mode</div>
            <div className="current-value">
              Current: {INVERTER_MODE_LABELS[Number(gxSettings?.inverter_mode?.value)] || 'Loading...'}
              {formatAge(gxSettings?.inverter_mode?.updated_at)}
            </div>
            <select
              className="control-input"
              style={{ padding: '12px' }}
              value={gxInputs.inverter_mode || inverterModeValue}
              onChange={(e) =>
                setGxInputs((prev) => ({
                  ...prev,
                  inverter_mode: e.target.value
                }))
              }
            >
              <option value="on">On (Inverter + Charger)</option>
              <option value="inverter_only">Inverter Only</option>
              <option value="charger_only">Charger Only</option>
              <option value="off">Off</option>
            </select>
            <button
              className="btn"
              onClick={() =>
                setGXSettingSelect('inverter_mode', gxInputs.inverter_mode || inverterModeValue)
              }
              style={{ width: '100%', marginTop: '10px' }}
            >
              SET
            </button>
          </div>

          <div className="control-item">
            <label>AC Input Current Limit</label>
            <div className="description">Maximum current draw from AC input (Amps)</div>
            <div className="current-value">
              Current: {gxSettings?.ac_input_current_limit?.value ?? 'Loading...'}
              {formatAge(gxSettings?.ac_input_current_limit?.updated_at)}
            </div>
            <input
              type="number"
              className="control-input"
              placeholder="e.g., 30"
              min="0"
              max="200"
              step="0.1"
              value={gxInputs.ac_input_current_limit}
              onChange={(e) =>
                setGxInputs((prev) => ({
                  ...prev,
                  ac_input_current_limit: e.target.value
                }))
              }
            />
            <button
              className="btn"
              onClick={() =>
                setGXSetting('ac_input_current_limit', gxInputs.ac_input_current_limit)
              }
              style={{ width: '100%', marginTop: '10px' }}
            >
              SET
            </button>
          </div>

          <div className="control-item">
            <label>Inverter Output Voltage</label>
            <div className="description">AC output voltage setpoint (Volts)</div>
            <div className="current-value">
              Current: {gxSettings?.inverter_output_voltage?.value ?? 'Loading...'}
              {formatAge(gxSettings?.inverter_output_voltage?.updated_at)}
            </div>
            <input
              type="number"
              className="control-input"
              placeholder="e.g., 120"
              min="100"
              max="240"
              step="1"
              value={gxInputs.inverter_output_voltage}
              onChange={(e) =>
                setGxInputs((prev) => ({
                  ...prev,
                  inverter_output_voltage: e.target.value
                }))
              }
            />
            <button
              className="btn"
              onClick={() =>
                setGXSetting('inverter_output_voltage', gxInputs.inverter_output_voltage)
              }
              style={{ width: '100%', marginTop: '10px' }}
            >
              SET
            </button>
          </div>
        </div>

        <button
          className="btn"
          onClick={() => loadGXSettings(gxSystemId)}
          style={{ background: 'linear-gradient(135deg, #10b981 0%, #059669 100%)' }}
        >
          REFRESH VALUES
        </button>
      </div>

      {statusModal && (
        <div className="status-modal" onClick={dismissModal}>
          <div
            className={`status-modal__content ${statusModal.level}`}
            onClick={(e) => e.stopPropagation()}
          >
            <div>{statusModal.message}</div>
            {statusModal.action && (
              <button
                className="status-modal__button"
                onClick={(e) => {
                  e.stopPropagation();
                  window.open(statusModal.action.url, '_blank', 'noopener');
                }}
              >
                {statusModal.action.label}
              </button>
            )}
          </div>
        </div>
      )}

      {lightboxImage && (
        <div className="lightbox" onClick={() => setLightboxImage(null)}>
          <img src={`/images/${lightboxImage.filename}`} alt={lightboxImage.caption || ''} />
          <div className="lightbox-caption">
            {lightboxImage.caption || lightboxImage.original_filename}
          </div>
          <div className="lightbox-meta">
            {new Date(lightboxImage.timestamp / 1e6).toLocaleString()}
          </div>
        </div>
      )}
    </div>
  );
}
