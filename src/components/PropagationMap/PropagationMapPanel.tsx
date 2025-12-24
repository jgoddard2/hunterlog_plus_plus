import * as React from 'react';
import { CircularProgress, FormControl, IconButton, InputLabel, MenuItem, Select, SelectChangeEvent, Tooltip, Typography } from '@mui/material';
import RefreshIcon from '@mui/icons-material/Refresh';
import ArrowBackIosNewIcon from '@mui/icons-material/ArrowBackIosNew';
import ArrowForwardIosIcon from '@mui/icons-material/ArrowForwardIos';
import ParkIcon from '@mui/icons-material/Park';
import LandscapeIcon from '@mui/icons-material/Landscape';
import Brightness3Icon from '@mui/icons-material/Brightness3';
import HomeIcon from '@mui/icons-material/Home';
import { MapContainer, Marker, Popup, Rectangle, TileLayer, useMap } from 'react-leaflet';
import L from 'leaflet';
import ReactDOMServer from 'react-dom/server';
import 'leaflet/dist/leaflet.css';

import { useAppContext } from '../AppContext';
import './PropagationMapPanel.scss';

const TILE_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const TILE_URL = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';

const MODE_OPTIONS = [
    'FM',
    'AM',
    'SSB',
    'CW',
    'FT8',
    'FT4'
];

interface MapGridCell {
    lat: number;
    lon: number;
    probability: number;
}

interface MapSpot {
    spot_id: number;
    lat: number;
    lon: number;
    source: string;
    activator: string;
    reference: string;
    mode: string;
    probability?: number | null;
}

interface MapPayload {
    grid_step_deg: number;
    grid: MapGridCell[];
    user: { lat: number; lon: number; grid: string };
    band_name: string;
    mode: string;
    spots: MapSpot[];
    created_at?: string;
    cache_index?: number;
    cache_total?: number;
}

const makeMuiIcon = (element: React.ReactElement, className: string) => L.divIcon({
    className: `prop-map-icon ${className}`,
    html: ReactDOMServer.renderToStaticMarkup(element),
    iconSize: [24, 24],
    iconAnchor: [12, 12]
});

const ICONS = {
    home: makeMuiIcon(<HomeIcon sx={{ fontSize: 20, color: '#fff' }} />, 'prop-map-icon--home'),
    pota: makeMuiIcon(<ParkIcon sx={{ fontSize: 20, color: '#1976d2' }} />, 'prop-map-icon--pota'),
    sota: makeMuiIcon(<LandscapeIcon sx={{ fontSize: 20, color: '#9c27b0' }} />, 'prop-map-icon--sota'),
    wwff: makeMuiIcon(<Brightness3Icon sx={{ fontSize: 20, color: '#2e7d32' }} />, 'prop-map-icon--wwff'),
    other: makeMuiIcon(<Brightness3Icon sx={{ fontSize: 18, color: '#455a64' }} />, 'prop-map-icon--other')
};

const MapViewUpdater = ({ center, zoom }: { center: [number, number] | null; zoom?: number }) => {
    const map = useMap();
    React.useEffect(() => {
        if (center) {
            map.setView(center, zoom ?? 2);
        }
    }, [center, zoom, map]);
    return null;
};

const MapViewTracker = ({ onChange }: { onChange: (center: [number, number], zoom: number) => void }) => {
    const map = useMap();
    React.useEffect(() => {
        const update = () => {
            const center = map.getCenter();
            onChange([center.lat, center.lng], map.getZoom());
        };
        map.on('moveend', update);
        map.on('zoomend', update);
        return () => {
            map.off('moveend', update);
            map.off('zoomend', update);
        };
    }, [map, onChange]);
    return null;
};

const probabilityToColor = (probability: number) => {
    const clamped = Math.max(0, Math.min(probability, 1));
    if (clamped < 0.25) {
        const t = clamped / 0.25;
        const r = Math.round(183 + (72 * (1 - t)));
        const g = Math.round(28 + (32 * t));
        const b = Math.round(28 + (32 * t));
        return `rgb(${r}, ${g}, ${b})`;
    }
    if (clamped < 0.75) {
        const t = (clamped - 0.25) / 0.5;
        const r = Math.round(251 + (4 * t));
        const g = Math.round(140 + (80 * t));
        const b = Math.round(0 + (20 * t));
        return `rgb(${r}, ${g}, ${b})`;
    }
    const t = (clamped - 0.75) / 0.25;
    const r = Math.round(144 * (1 - t));
    const g = Math.round(190 + (50 * t));
    const b = Math.round(94 * (1 - t));
    return `rgb(${r}, ${g}, ${b})`;
};

const resolveSpotIcon = (source?: string) => {
    const key = (source || '').toUpperCase();
    if (key === 'POTA') {
        return ICONS.pota;
    }
    if (key === 'SOTA') {
        return ICONS.sota;
    }
    if (key === 'WWFF') {
        return ICONS.wwff;
    }
    return ICONS.other;
};

interface PropagationMapPanelProps {
    active: boolean;
}

const getInitialMode = () => {
    if (typeof window === 'undefined') {
        return 'SSB';
    }
    const saved = window.localStorage.getItem('PROP_MAP_MODE');
    return saved || 'SSB';
};

export default function PropagationMapPanel({ active }: PropagationMapPanelProps) {
    const { contextData, setData } = useAppContext();
    const [mode, setMode] = React.useState(getInitialMode);
    const [mapData, setMapData] = React.useState<MapPayload | null>(null);
    const [loading, setLoading] = React.useState(false);
    const [error, setError] = React.useState<string | null>(null);
    const [cacheIndex, setCacheIndex] = React.useState(0);
    const [cacheTotal, setCacheTotal] = React.useState(0);
    const [createdAt, setCreatedAt] = React.useState<string | null>(null);
    const [mapView, setMapView] = React.useState<{ center: [number, number]; zoom: number } | null>(null);

    const loadMap = React.useCallback(async (indexOverride?: number) => {
        if (!window.pywebview?.api?.get_propagation_map) {
            setError('Propagation map unavailable.');
            setMapData(null);
            return;
        }
        setLoading(true);
        setError(null);
        const requestMap = async (indexToRequest: number) => {
            const response = await window.pywebview.api.get_propagation_map(mode, indexToRequest);
            if (!response) {
                throw new Error('Empty response');
            }
            const payload = typeof response === 'string' ? JSON.parse(response) : response;
            if (!payload?.success) {
                const message = payload?.message || 'Unable to load map.';
                throw new Error(message);
            }
            return payload as MapPayload;
        };

        try {
            const indexToRequest = typeof indexOverride === 'number' ? indexOverride : cacheIndex;
            let payload = await requestMap(indexToRequest);
            setMapData(payload);
            setCacheIndex(payload.cache_index ?? 0);
            setCacheTotal(payload.cache_total ?? 0);
            setCreatedAt(payload.created_at ?? null);
            if (!mapView && payload?.user?.lat !== undefined && payload?.user?.lon !== undefined) {
                setMapView({ center: [payload.user.lat, payload.user.lon], zoom: 2 });
            }
        } catch (err) {
            try {
                const payload = await requestMap(0);
                setMapData(payload);
                setCacheIndex(payload.cache_index ?? 0);
                setCacheTotal(payload.cache_total ?? 0);
                setCreatedAt(payload.created_at ?? null);
                if (!mapView && payload?.user?.lat !== undefined && payload?.user?.lon !== undefined) {
                    setMapView({ center: [payload.user.lat, payload.user.lon], zoom: 2 });
                }
            } catch (fallbackErr) {
                console.error('Failed to load propagation map', fallbackErr);
                const message = fallbackErr instanceof Error ? fallbackErr.message : 'Unable to load map.';
                setError(message);
                setMapData(null);
            }
        } finally {
            setLoading(false);
        }
    }, [mode, cacheIndex, mapView]);

    React.useEffect(() => {
        if (!active) {
            return;
        }
        loadMap();
        const interval = setInterval(loadMap, 60_000);
        return () => clearInterval(interval);
    }, [
        active,
        mode,
        contextData.bandFilter,
        contextData.regionFilter,
        contextData.locationFilter,
        contextData.qrtFilter,
        contextData.huntedFilter,
        contextData.onlyNewFilter,
        contextData.continentFilter,
        contextData.sigFilter,
        contextData.probabilityFilter,
        loadMap
    ]);
    React.useEffect(() => {
        if (!active) {
            return;
        }
        const handleKey = (event: KeyboardEvent) => {
            const target = event.target as HTMLElement | null;
            if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA')) {
                return;
            }
            if (event.key === 'ArrowLeft') {
                handleCacheStep('prev');
            } else if (event.key === 'ArrowRight') {
                handleCacheStep('next');
            }
        };
        window.addEventListener('keydown', handleKey);
        return () => window.removeEventListener('keydown', handleKey);
    }, [active, cacheIndex, cacheTotal, loading]);

    const handleModeChange = (event: SelectChangeEvent) => {
        const nextMode = event.target.value as string;
        setMode(nextMode);
        if (typeof window !== 'undefined') {
            window.localStorage.setItem('PROP_MAP_MODE', nextMode);
        }
        setCacheIndex(0);
        setMapView(null);
    };

    const handleCacheStep = (direction: 'prev' | 'next') => {
        if (cacheTotal <= 1) {
            return;
        }
        const nextIndex = direction === 'prev'
            ? Math.min(cacheIndex + 1, cacheTotal - 1)
            : Math.max(cacheIndex - 1, 0);
        if (nextIndex !== cacheIndex) {
            setCacheIndex(nextIndex);
            loadMap(nextIndex);
        }
    };

    const selectSpot = (spotId: number) => {
        const next = { ...contextData, spotId };
        setData(next);
    };

    const handleViewChange = (center: [number, number], zoom: number) => {
        setMapView({ center, zoom });
    };

    const gridStep = mapData?.grid_step_deg ?? 5;
    const halfStep = gridStep / 2;
    const mapCenter = mapData?.user ? [mapData.user.lat, mapData.user.lon] as [number, number] : null;

    return (
        <div className='propagation-map-panel'>
            <div className='propagation-map-panel__header'>
                <div>
                    <div className='propagation-map-panel__title'>Propagation Map (Based on WSPR beacons)</div>
                    <div className='propagation-map-panel__subtitle'>
                        {mapData?.band_name ? `Band: ${mapData.band_name.toUpperCase()}` : 'Band: --'}
                        {createdAt ? ` | ${new Date(createdAt).toLocaleString()}` : ''}
                    </div>
                </div>
                <div className='propagation-map-panel__controls'>
                    <Tooltip title="Select the operating mode used for probability shading">
                        <FormControl size='small'>
                            <InputLabel id="prop-map-mode-label">Mode</InputLabel>
                            <Select
                                labelId="prop-map-mode-label"
                                value={mode}
                                label="Mode"
                                onChange={handleModeChange}
                            >
                                {MODE_OPTIONS.map((opt) => (
                                    <MenuItem key={opt} value={opt}>{opt}</MenuItem>
                                ))}
                            </Select>
                        </FormControl>
                    </Tooltip>
                    <Tooltip title="Refresh map">
                        <span>
                            <IconButton size='small' onClick={() => loadMap()} disabled={loading}>
                                {loading ? <CircularProgress size={16} /> : <RefreshIcon fontSize='small' />}
                            </IconButton>
                        </span>
                    </Tooltip>
                </div>
            </div>

            {error && (
                <div className='propagation-map-panel__error'>{error}</div>
            )}

            {!error && (
                <div className='propagation-map-panel__map-row'>
                    <MapContainer
                        className='propagation-map-panel__map'
                        center={mapView?.center ?? mapCenter ?? [0, 0]}
                        zoom={mapView?.zoom ?? 2}
                        scrollWheelZoom
                    >
                        <TileLayer attribution={TILE_ATTRIBUTION} url={TILE_URL} />
                        <MapViewUpdater center={mapView?.center ?? mapCenter} zoom={mapView?.zoom} />
                        <MapViewTracker onChange={handleViewChange} />
                        {mapData?.grid?.map((cell, idx) => {
                            const bounds: [[number, number], [number, number]] = [
                                [cell.lat - halfStep, cell.lon - halfStep],
                                [cell.lat + halfStep, cell.lon + halfStep]
                            ];
                            return (
                                <Rectangle
                                    key={`cell-${idx}`}
                                    bounds={bounds}
                                    pathOptions={{
                                        color: 'transparent',
                                        fillColor: probabilityToColor(cell.probability),
                                        fillOpacity: 0.5,
                                        weight: 0
                                    }}
                                />
                            );
                        })}
                        {mapData?.spots?.map((spot) => (
                            <Marker
                                key={`spot-${spot.spot_id}`}
                                position={[spot.lat, spot.lon]}
                                icon={resolveSpotIcon(spot.source)}
                            >
                                <Popup>
                                    <Typography variant="subtitle2" sx={{ lineHeight: 1.1 }}>
                                        <span
                                            className='propagation-map-panel__link'
                                            onClick={(event) => {
                                                event.preventDefault();
                                                event.stopPropagation();
                                                selectSpot(spot.spot_id);
                                            }}
                                        >
                                            {spot.activator}
                                        </span>
                                    </Typography>
                                    <Typography variant="body2" sx={{ lineHeight: 1.1 }}>{spot.reference}</Typography>
                                    <Typography variant="caption" sx={{ display: 'block' }}>Mode: {spot.mode}</Typography>
                                    {typeof spot.probability === 'number' && (
                                        <Typography variant="caption" sx={{ display: 'block' }}>
                                            Probability: {(spot.probability * 100).toFixed(0)}%
                                        </Typography>
                                    )}
                                </Popup>
                            </Marker>
                        ))}
                        {mapData?.user && (
                            <Marker position={[mapData.user.lat, mapData.user.lon]} icon={ICONS.home}>
                                <Popup>
                                    <Typography variant="subtitle2">Home</Typography>
                                    <Typography variant="caption">{mapData.user.grid}</Typography>
                                </Popup>
                            </Marker>
                        )}
                    </MapContainer>
                    <div className='propagation-map-panel__cache'>
                        <Tooltip title="Older map">
                            <span>
                                <IconButton
                                    size='small'
                                    onClick={() => handleCacheStep('prev')}
                                    disabled={loading || cacheIndex >= cacheTotal - 1}
                                >
                                    <ArrowBackIosNewIcon fontSize='small' />
                                </IconButton>
                            </span>
                        </Tooltip>
                        <Tooltip title="Newer map">
                            <span>
                                <IconButton
                                    size='small'
                                    onClick={() => handleCacheStep('next')}
                                    disabled={loading || cacheIndex <= 0}
                                >
                                    <ArrowForwardIosIcon fontSize='small' />
                                </IconButton>
                            </span>
                        </Tooltip>
                        {cacheTotal > 0 && (
                            <div className='propagation-map-panel__cache-label'>
                                {cacheIndex + 1} / {cacheTotal}
                            </div>
                        )}
                    </div>
                </div>
            )}
            <div className='propagation-map-panel__legend'>
                <span className='propagation-map-panel__legend-item'>
                    <span className='propagation-map-panel__swatch propagation-map-panel__swatch--low' />
                    0-24%
                </span>
                <span className='propagation-map-panel__legend-item'>
                    <span className='propagation-map-panel__swatch propagation-map-panel__swatch--mid' />
                    25-74%
                </span>
                <span className='propagation-map-panel__legend-item'>
                    <span className='propagation-map-panel__swatch propagation-map-panel__swatch--high' />
                    75-100%
                </span>
            </div>
        </div>
    );
}
