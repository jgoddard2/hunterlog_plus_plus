import * as React from 'react';
import CircularProgress from '@mui/material/CircularProgress';
import IconButton from '@mui/material/IconButton';
import Tooltip from '@mui/material/Tooltip';
import RefreshIcon from '@mui/icons-material/Refresh';

import { useAppContext } from '../AppContext';
import { PropagationHistoryPoint } from '../../@types/Spots';

import './PropagationHistoryPanel.scss';

const DEFAULT_WIDTH = 320;
const DEFAULT_HEIGHT = 150;

function formatTimeLabel(iso: string) {
    const d = new Date(iso);
    if (isNaN(d.getTime())) {
        return '';
    }
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function classifyCapability(snr: number | null | undefined) {
    if (snr === undefined || snr === null) {
        return 'No data';
    }
    if (snr >= 10) {
        return 'SSB';
    }
    if (snr >= -15) {
        return 'Digital';
    }
    return 'N/R';
}

interface ChartProps {
    data: PropagationHistoryPoint[];
}

const PropagationHistoryChart = ({ data }: ChartProps) => {
    if (!data || data.length === 0) {
        return (
            <div className='propagationChartEmpty'>
                No propagation predictions for the past hour.
            </div>
        );
    }

    const sorted = [...data].sort(
        (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
    );
    const snrValues = sorted.map((point) => point.snr);
    const minSNR = Math.min(...snrValues, -30);
    const maxSNR = Math.max(...snrValues, 10);
    const yRange = Math.max(maxSNR - minSNR, 1);
    const xRange = Math.max(sorted.length - 1, 1);

    const paddingX = 24;
    const paddingY = 16;

    const points = sorted.map((point, idx) => {
        const x =
            paddingX + (idx / xRange) * (DEFAULT_WIDTH - paddingX * 2);
        const y =
            DEFAULT_HEIGHT -
            paddingY -
            ((point.snr - minSNR) / yRange) * (DEFAULT_HEIGHT - paddingY * 2);
        return `${x},${y}`;
    }).join(' ');

    const gridLines: number[] = [];
    const gridStep = 5;
    const start = Math.floor(minSNR / gridStep) * gridStep;
    for (let v = start; v <= maxSNR; v += gridStep) {
        gridLines.push(v);
    }

    const firstLabel = formatTimeLabel(sorted[0].timestamp);
    const lastLabel = formatTimeLabel(sorted[sorted.length - 1].timestamp);

    return (
        <div className='propagationChart'>
            <svg
                className='propagationChartSvg'
                viewBox={`0 0 ${DEFAULT_WIDTH} ${DEFAULT_HEIGHT}`}
                preserveAspectRatio="none"
            >
                <defs>
                    <linearGradient id="propGradient" x1="0%" y1="0%" x2="0%" y2="100%">
                        <stop offset="0%" stopColor="#80ffdb" stopOpacity="0.35" />
                        <stop offset="100%" stopColor="#5390d9" stopOpacity="0" />
                    </linearGradient>
                </defs>
                <rect
                    x={0}
                    y={0}
                    width={DEFAULT_WIDTH}
                    height={DEFAULT_HEIGHT}
                    rx={10}
                    ry={10}
                    className='propagationChartBackground'
                />
                {gridLines.map((value) => {
                    const y =
                        DEFAULT_HEIGHT -
                        paddingY -
                        ((value - minSNR) / yRange) * (DEFAULT_HEIGHT - paddingY * 2);
                    return (
                        <g key={value}>
                            <line
                                x1={paddingX}
                                x2={DEFAULT_WIDTH - paddingX}
                                y1={y}
                                y2={y}
                                className='propagationChartGridLine'
                            />
                            <text
                                x={4}
                                y={y + 4}
                                className='propagationChartAxisLabel'
                            >
                                {value} dB
                            </text>
                        </g>
                    );
                })}

                <polyline
                    fill="url(#propGradient)"
                    stroke="none"
                    points={`${paddingX},${DEFAULT_HEIGHT - paddingY} ${points} ${DEFAULT_WIDTH - paddingX},${DEFAULT_HEIGHT - paddingY}`}
                    opacity={0.4}
                />

                <polyline
                    fill="none"
                    stroke="#64dfdf"
                    strokeWidth={2}
                    points={points}
                />

                {sorted.map((point, idx) => {
                    const x =
                        paddingX + (idx / xRange) * (DEFAULT_WIDTH - paddingX * 2);
                    const y =
                        DEFAULT_HEIGHT -
                        paddingY -
                        ((point.snr - minSNR) / yRange) * (DEFAULT_HEIGHT - paddingY * 2);
                    return (
                        <circle
                            key={`${point.timestamp}-${idx}`}
                            cx={x}
                            cy={y}
                            r={3}
                            className='propagationChartPoint'
                        />
                    );
                })}

                <line
                    x1={paddingX}
                    x2={DEFAULT_WIDTH - paddingX}
                    y1={DEFAULT_HEIGHT - paddingY}
                    y2={DEFAULT_HEIGHT - paddingY}
                    className='propagationChartAxis'
                />
                <text
                    x={paddingX}
                    y={DEFAULT_HEIGHT - 2}
                    className='propagationChartAxisLabel'
                >
                    {firstLabel}
                </text>
                <text
                    x={DEFAULT_WIDTH - paddingX}
                    y={DEFAULT_HEIGHT - 2}
                    className='propagationChartAxisLabel'
                    textAnchor='end'
                >
                    {lastLabel}
                </text>
            </svg>
        </div>
    );
};

const PropagationHistoryPanel = () => {
    const { contextData, setData } = useAppContext();
    const [history, setHistory] = React.useState<PropagationHistoryPoint[]>([]);
    const [loading, setLoading] = React.useState(false);
    const [error, setError] = React.useState<string | null>(null);
    const [infoMessage, setInfoMessage] = React.useState<string | null>(null);
    const [lastUpdated, setLastUpdated] = React.useState<Date | null>(null);

    const ctxRef = React.useRef(contextData);
    React.useEffect(() => {
        ctxRef.current = contextData;
    }, [contextData]);

    const spotId = contextData.spotId;
    React.useEffect(() => {
        setInfoMessage(null);
        if (!spotId) {
            setHistory([]);
            setError(null);
            setLastUpdated(null);
            lastSuccessfulSpotId.current = null;
            return;
        }
        // When the user selects a new spot, clear stale history until fresh data arrives.
        setHistory([]);
        setError(null);
        lastSuccessfulSpotId.current = null;
    }, [spotId]);
    const lastSuccessfulSpotId = React.useRef<number | null>(null);

    const fetchHistory = React.useCallback(() => {
        if (!window.pywebview || !window.pywebview.api || !spotId) {
            setHistory([]);
            setError(null);
            setInfoMessage(null);
            setLastUpdated(null);
            return;
        }

        setLoading(true);
        setError(null);
        setInfoMessage(null);

        const requestedSpotId = spotId;

        window.pywebview.api.get_propagation_history(spotId)
            .then((response: string) => {
                let payload: { success: boolean; message?: string; history?: PropagationHistoryPoint[] };
                try {
                    payload = JSON.parse(response);
                } catch {
                    setError('Invalid propagation response.');
                    setHistory([]);
                    setLastUpdated(null);
                    lastSuccessfulSpotId.current = null;
                    return;
                }

                if (!payload.success) {
                    if (payload.message === 'spot not found') {
                        if (lastSuccessfulSpotId.current === requestedSpotId) {
                            setInfoMessage('Spot no longer available; showing last known data.');
                            setError(null);
                        } else {
                            setHistory([]);
                            setError('Spot not found.');
                            setLastUpdated(null);
                            lastSuccessfulSpotId.current = null;
                        }
                    } else {
                        setHistory([]);
                        setError(payload.message || 'No propagation history available.');
                        setLastUpdated(null);
                        lastSuccessfulSpotId.current = null;
                    }
                    return;
                }

                const parsed = Array.isArray(payload.history)
                    ? payload.history as PropagationHistoryPoint[]
                    : [];

                const sorted = [...parsed].sort(
                    (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
                );

                setHistory(sorted);
                setLastUpdated(new Date());
                lastSuccessfulSpotId.current = requestedSpotId;
                setError(null);
                setInfoMessage(null);
            })
            .catch(() => {
                setError('Unable to retrieve propagation history.');
                setInfoMessage(null);
                setHistory([]);
                setLastUpdated(null);
                lastSuccessfulSpotId.current = null;
            })
            .finally(() => setLoading(false));
    }, [spotId]);

    React.useEffect(() => {
        fetchHistory();
        const interval = setInterval(fetchHistory, 60_000);
        return () => clearInterval(interval);
    }, [fetchHistory]);

    const latestPoint = history.length ? history[history.length - 1] : null;
    const bestPoint = history.reduce<PropagationHistoryPoint | null>(
        (best, current) => {
            if (!best || current.snr > best.snr) {
                return current;
            }
            return best;
        },
        null
    );

    const subtitle = contextData.qso
        ? `${contextData.qso.call} @ ${contextData.qso.sig_info || contextData.qso.reference || ''}`
        : 'Select a spot to view propagation estimates.';

    const latestLabel = latestPoint
        ? `${latestPoint.snr >= 0 ? '+' : ''}${latestPoint.snr.toFixed(1)} dB (${classifyCapability(latestPoint.snr)})`
        : 'No recent prediction';

    const lastTimestamp = latestPoint ? formatTimeLabel(latestPoint.timestamp) : '—';
    const bestLabel = bestPoint
        ? `${bestPoint.snr >= 0 ? '+' : ''}${bestPoint.snr.toFixed(1)} dB`
        : '—';

    return (
        <div className='propagation-history-panel'>
            <div className='propagation-history-panel__header'>
                <div>
                    <div className='propagation-history-panel__title'>Propagation History</div>
                    <div className='propagation-history-panel__subtitle'>{subtitle}</div>
                </div>
                <Tooltip title={spotId ? 'Refresh propagation data' : 'Select a spot to enable'}>
                    <span>
                        <IconButton
                            size='small'
                            onClick={fetchHistory}
                            disabled={!spotId || loading}
                        >
                            {loading ? <CircularProgress size={16} /> : <RefreshIcon fontSize='small' />}
                        </IconButton>
                    </span>
                </Tooltip>
            </div>

            {!spotId && (
                <div className='propagationChartEmpty'>
                    Choose a spot in the table to see its propagation trend.
                </div>
            )}

            {spotId && error && (
                <div className='propagation-history-panel__error'>{error}</div>
            )}

            {spotId && infoMessage && (
                <div className='propagation-history-panel__info'>{infoMessage}</div>
            )}

            {spotId && !error && (
                <>
                    <PropagationHistoryChart data={history} />
                    <div className='propagation-history-panel__stats'>
                        <span><strong>Latest:</strong> {latestLabel} @ {lastTimestamp}</span>
                        <span><strong>Best:</strong> {bestLabel}</span>
                        <span><strong>Samples:</strong> {history.length}</span>
                        <span>
                            <strong>Updated:</strong>{' '}
                            {lastUpdated
                                ? lastUpdated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                                : '—'}
                        </span>
                    </div>
                </>
            )}
        </div>
    );
};

export default PropagationHistoryPanel;
