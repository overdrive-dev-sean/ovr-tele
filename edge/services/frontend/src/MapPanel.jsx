import { useEffect, useRef } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import markerIcon2x from 'leaflet/dist/images/marker-icon-2x.png';
import markerIcon from 'leaflet/dist/images/marker-icon.png';
import markerShadow from 'leaflet/dist/images/marker-shadow.png';

L.Icon.Default.mergeOptions({
  iconRetinaUrl: markerIcon2x,
  iconUrl: markerIcon,
  shadowUrl: markerShadow
});

export default function MapPanel({
  latitude,
  longitude,
  systemIds = [],
  providers,
  activeProvider,
  onTileAttempt,
  defaultZoom,
  tilesEnabled,
  isActive
}) {
  const mapRef = useRef(null);
  const markerRef = useRef(null);
  const layerRef = useRef(null);
  const tileAttemptRef = useRef(onTileAttempt);

  useEffect(() => {
    if (!mapRef.current) return;
    if (mapRef.current._leaflet_map) return;

    const map = L.map(mapRef.current, { zoomControl: true });
    map.setView([0, 0], 2);
    mapRef.current._leaflet_map = map;
  }, []);

  useEffect(() => {
    tileAttemptRef.current = onTileAttempt;
  }, [onTileAttempt]);

  useEffect(() => {
    const map = mapRef.current?._leaflet_map;
    if (!map) return;
    if (tilesEnabled === false) {
      if (layerRef.current) {
        layerRef.current.off('tileloadstart');
        map.removeLayer(layerRef.current);
        layerRef.current = null;
      }
      return;
    }
    if (!providers) return;
    const provider =
      providers[activeProvider] || providers.esri || Object.values(providers)[0];
    if (!provider || !provider.url) return;

    if (layerRef.current) {
      layerRef.current.off('tileloadstart');
      map.removeLayer(layerRef.current);
      layerRef.current = null;
    }

    const layer = L.tileLayer(provider.url, {
      attribution: provider.attribution,
      maxZoom: provider.maxZoom || 20,
      tileSize: provider.tileSize || 256,
      zoomOffset: provider.zoomOffset || 0
    });
    if (tileAttemptRef.current) {
      const providerId = provider.id || activeProvider;
      layer.on('tileloadstart', () => tileAttemptRef.current(providerId));
    }
    layer.addTo(map);
    layerRef.current = layer;
  }, [providers, activeProvider, tilesEnabled]);

  useEffect(() => {
    const map = mapRef.current?._leaflet_map;
    if (!map) return;
    if (latitude == null || longitude == null) return;

    const coords = [latitude, longitude];
    const label = systemIds.length > 0 ? systemIds.join(', ') : 'Node';

    if (!markerRef.current) {
      markerRef.current = L.marker(coords).addTo(map);
      markerRef.current.bindTooltip(label, {
        permanent: true,
        direction: 'top',
        className: 'system-marker-label'
      });
    } else {
      markerRef.current.setLatLng(coords);
      markerRef.current.setTooltipContent(label);
    }
    map.setView(coords, defaultZoom);
  }, [latitude, longitude, systemIds, defaultZoom]);

  useEffect(() => {
    const map = mapRef.current?._leaflet_map;
    if (!map || !isActive) return;
    const coords =
      latitude != null && longitude != null ? [latitude, longitude] : null;
    const timer = setTimeout(() => {
      map.invalidateSize();
      if (coords && Number.isFinite(defaultZoom)) {
        map.setView(coords, defaultZoom);
      }
    }, 0);
    return () => clearTimeout(timer);
  }, [isActive, latitude, longitude, defaultZoom]);

  return <div ref={mapRef} className="map-container" />;
}
