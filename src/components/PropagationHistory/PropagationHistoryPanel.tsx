import * as React from 'react';
import CircularProgress from '@mui/material/CircularProgress';
import IconButton from '@mui/material/IconButton';
import Tooltip from '@mui/material/Tooltip';
import RefreshIcon from '@mui/icons-material/Refresh';
import KeyboardArrowRightIcon from '@mui/icons-material/KeyboardArrowRight';

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

type ChartMode = 'probability' | 'snr';

interface ChartProps {
    data: PropagationHistoryPoint[];
    mode: ChartMode;
}

const PropagationHistoryChart = ({ data, mode }: ChartProps) => {
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

    const validPoints = sorted
        .map((point) => {
            if (mode === 'probability') {
                if (typeof point.probability !== 'number') {
                    return null;
                }
                const percent = Math.max(0, Math.min(point.probability * 100, 100));
                return { timestamp: point.timestamp, value: percent };
            }
            if (typeof point.snr !== 'number') {
                return null;
            }
            return { timestamp: point.timestamp, value: point.snr };
        })
        .filter((point): point is { timestamp: string; value: number } => point !== null);

    if (!validPoints.length) {
        return (
            <div className='propagationChartEmpty'>
                {mode === 'probability'
                    ? 'No probability data yet.'
                    : 'No propagation predictions for the past hour.'}
            </div>
        );
    }

    let axisMin: number;
    let axisMax: number;
    let gridStep: number;
    let axisSuffix: string;

    if (mode === 'probability') {
        axisMin = 0;
        axisMax = 100;
        gridStep = 10;
        axisSuffix = '%';
    } else {
        const values = validPoints.map((point) => point.value);
        const rawMin = Math.min(...values, -30);
        const rawMax = Math.max(...values, 10);
        gridStep = 5;
        axisSuffix = ' dB';
        axisMin = Math.floor(rawMin / gridStep) * gridStep;
        axisMax = Math.ceil(rawMax / gridStep) * gridStep;
        if (axisMax - axisMin < gridStep) {
            axisMax = axisMin + gridStep;
        }
    }

    const yRange = Math.max(axisMax - axisMin, 1);
    const xRange = Math.max(validPoints.length - 1, 1);
    const paddingX = 24;
    const paddingY = 16;

    const points = validPoints.map((point, idx) => {
        const x =
            paddingX + (idx / xRange) * (DEFAULT_WIDTH - paddingX * 2);
        const y =
            DEFAULT_HEIGHT -
            paddingY -
            ((point.value - axisMin) / yRange) * (DEFAULT_HEIGHT - paddingY * 2);
        return `${x},${y}`;
    }).join(' ');

    const gridLines: number[] = [];
    for (let v = axisMin; v <= axisMax + 1e-6; v += gridStep) {
        gridLines.push(parseFloat(v.toFixed(4)));
    }

    const firstLabel = formatTimeLabel(validPoints[0].timestamp);
    const lastLabel = formatTimeLabel(validPoints[validPoints.length - 1].timestamp);

    return (
        <div className='propagationChart'>
            <div className='propagationChartModeLabel'>
                {mode === 'probability' ? 'Probability' : 'SNR'}
            </div>
            <svg
                className='propagationChartSvg'
                viewBox={`0 0 ${DEFAULT_WIDTH} ${DEFAULT_HEIGHT}`}
                preserveAspectRatio="none"
            >
                <defs>
                    <linearGradient id={`propGradient-${mode}`} x1="0%" y1="0%" x2="0%" y2="100%">
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
                        ((value - axisMin) / yRange) * (DEFAULT_HEIGHT - paddingY * 2);
                    return (
                        <g key={`${mode}-${value}`}>
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
                                {`${value}${axisSuffix}`}
                            </text>
                        </g>
                    );
                })}

                <polyline
                    fill={`url(#propGradient-${mode})`}
                    stroke="none"
                    points={`${paddingX},${DEFAULT_HEIGHT - paddingY} ${points} ${DEFAULT_WIDTH - paddingX},${DEFAULT_HEIGHT - paddingY}`}
                    opacity={0.35}
                />

                <polyline
                    fill="none"
                    stroke="#64dfdf"
                    strokeWidth={2}
                    points={points}
                />

                {validPoints.map((point, idx) => {
                    const x =
                        paddingX + (idx / xRange) * (DEFAULT_WIDTH - paddingX * 2);
                    const y =
                        DEFAULT_HEIGHT -
                        paddingY -
                        ((point.value - axisMin) / yRange) * (DEFAULT_HEIGHT - paddingY * 2);
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

interface SsnInfo {
    value: number | null;
    source: 'observed' | 'fallback' | null;
}

const PropagationHistoryPanel = () => {
    const { contextData, setData } = useAppContext();
    const [history, setHistory] = React.useState<PropagationHistoryPoint[]>([]);
    const [loading, setLoading] = React.useState(false);
    const [error, setError] = React.useState<string | null>(null);
    const [infoMessage, setInfoMessage] = React.useState<string | null>(null);
    const [lastUpdated, setLastUpdated] = React.useState<Date | null>(null);
    const [chartMode, setChartMode] = React.useState<ChartMode>('probability');
    const [ssnInfo, setSsnInfo] = React.useState<SsnInfo>({ value: null, source: null });

    const ctxRef = React.useRef(contextData);
    React.useEffect(() => {
        ctxRef.current = contextData;
    }, [contextData]);

    const spotId = contextData.spotId;
    React.useEffect(() => {
        setInfoMessage(null);
        setChartMode('probability');
        if (!spotId) {
            setHistory([]);
            setError(null);
            setLastUpdated(null);
            lastSuccessfulSpotId.current = null;
            setSsnInfo({ value: null, source: null });
            return;
        }
        setHistory([]);
        setError(null);
        lastSuccessfulSpotId.current = null;
        setSsnInfo({ value: null, source: null });
    }, [spotId]);
    const lastSuccessfulSpotId = React.useRef<number | null>(null);

    const fetchHistory = React.useCallback(() => {
        if (!window.pywebview || !window.pywebview.api || !spotId) {
            setHistory([]);
            setError(null);
            setInfoMessage(null);
            setLastUpdated(null);
            setSsnInfo({ value: null, source: null });
            return;
        }

        setLoading(true);
        setError(null);
        setInfoMessage(null);

        const requestedSpotId = spotId;

        window.pywebview.api.get_propagation_history(spotId)
            .then((response: string) => {
                let payload: {
                    success: boolean;
                    message?: string;
                    history?: PropagationHistoryPoint[];
                    ssn?: number;
                    ssn_source?: string;
                };
                try {
                    payload = JSON.parse(response);
                } catch {
                    setError('Invalid propagation response.');
                    setHistory([]);
                    setLastUpdated(null);
                    lastSuccessfulSpotId.current = null;
                    setSsnInfo({ value: null, source: null });
                    return;
                }

                const ssnValue = typeof payload.ssn === 'number' ? payload.ssn : null;
                const ssnSource = payload.ssn_source === 'observed'
                    ? 'observed'
                    : payload.ssn_source === 'fallback'
                        ? 'fallback'
                        : null;
                setSsnInfo({ value: ssnValue, source: ssnSource });

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
                setSsnInfo({ value: null, source: null });
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
            const currentProb = typeof current.probability === 'number' ? current.probability : null;
            const bestProb = best && typeof best.probability === 'number' ? best.probability : null;
            if (currentProb === null) {
                return best;
            }
            if (bestProb === null || currentProb > bestProb) {
                return current;
            }
            return best;
        },
        null
    );

    const toPercent = (value: number | null | undefined) => {
        if (typeof value !== 'number' || Number.isNaN(value)) {
            return null;
        }
        return Math.round(Math.max(0, Math.min(value * 100, 100)));
    };

    const latestProbability = toPercent(latestPoint?.probability ?? null);
    const bestProbability = toPercent(bestPoint?.probability ?? null);
    const lastTimestamp = latestPoint ? formatTimeLabel(latestPoint.timestamp) : '--';

    const subtitle = contextData.qso
        ? `${contextData.qso.call} @ ${contextData.qso.sig_info || contextData.qso.reference || ''}`
        : 'Select a spot to view propagation estimates.';

    const latestLabel = latestProbability !== null ? `${latestProbability}%` : 'No probability data';
    const bestLabel = bestProbability !== null ? `${bestProbability}%` : 'No probability data';
    const updatedLabel = lastUpdated
        ? lastUpdated.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        : '--';

    const ssnDisplay = spotId && ssnInfo.value !== null ? Math.round(ssnInfo.value) : null;
    const ssnTitle = ssnInfo.source === 'observed'
        ? 'Observed NOAA SSN'
        : ssnInfo.source === 'fallback'
            ? 'Fallback SSN (config)'
            : '';

    const nextMode = chartMode === 'probability' ? 'SNR' : 'probability';
    const toggleChartMode = () => {
        setChartMode((prev) => (prev === 'probability' ? 'snr' : 'probability'));
    };

    return (
        <div className='propagation-history-panel'>
            <div className='propagation-history-panel__header'>
                <div>
                    <div className='propagation-history-panel__title'>Propagation History</div>
                    <div className='propagation-history-panel__subtitle'>
                        <span>{subtitle}</span>
                        {spotId && ssnDisplay !== null && (
                            <Tooltip title={ssnTitle}>
                                <span className='propagation-history-panel__ssn'>SSN: {ssnDisplay}</span>
                            </Tooltip>
                        )}
                    </div>
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
                    <div className='propagation-history-panel__chartSection'>
                        <PropagationHistoryChart data={history} mode={chartMode} />
                        <div className='propagation-history-panel__chartToggle'>
                            <Tooltip title={`Show ${nextMode.toUpperCase()} chart`}>
                                <span>
                                    <IconButton
                                        size='small'
                                        onClick={toggleChartMode}
                                        aria-label={`Show ${nextMode} chart`}
                                    >
                                        <KeyboardArrowRightIcon fontSize='small' />
                                    </IconButton>
                                </span>
                            </Tooltip>
                        </div>
                    </div>
                    <div className='propagation-history-panel__stats'>
                        <span>
                            <strong>Latest:</strong> {latestLabel}
                            {latestProbability !== null && lastTimestamp !== '--' ? ` @ ${lastTimestamp}` : ''}
                        </span>
                        <span><strong>Best:</strong> {bestLabel}</span>
                        <span className='propagation-history-panel__stats-samples'>
                            <strong>Samples:</strong> {history.length}
                            <span className='propagation-history-panel__stats-divider'>-</span>
                            <strong>Updated:</strong> {updatedLabel}
                        </span>
                    </div>
                </>
            )}
        </div>
    );
};

export default PropagationHistoryPanel;
