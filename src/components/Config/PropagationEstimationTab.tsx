import * as React from 'react';
import { Box, TextField, Typography, Switch, FormControlLabel, Button, Alert, Slider, Tooltip, IconButton, Grid, MenuItem } from '@mui/material';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import { useConfigContext } from './ConfigContextProvider';

const KERNEL_PROFILES = [
    {
        id: 'global',
        label: 'Profile 1 - Global Blend',
        description: 'Wide aperture that blends worldwide WSPR reports for steady rankings.'
    },
    {
        id: 'regional',
        label: 'Profile 2 - Regional/Continental',
        description: 'Balanced default tuned for most HF work across a continent.'
    },
    {
        id: 'local',
        label: 'Profile 3 - Local Skip Aware',
        description: 'Tighter window that trims overly optimistic near-field assumptions.'
    },
    {
        id: 'hyperlocal',
        label: 'Profile 4 - Hyper-Local',
        description: 'Best when you trust dense, nearby data and want highly responsive odds.'
    }
];

const ANTENNA_OPTIONS = [
    {
        id: 'BASE_ISOTROPE',
        label: 'Reference Isotropic (0 dBi)',
        notes: 'Neutral baseline for unknown stations; VOACAP isotropic antenna.'
    },
    {
        id: 'POTA_DIPOLE_5M',
        label: 'Portable Dipole (5 m AGL)',
        notes: 'Half-wave dipole on a 16 ft support; broadside bi-directional pattern.'
    },
    {
        id: 'POTA_DIPOLE_10M',
        label: 'Portable Dipole (10 m AGL)',
        notes: 'Common 33 ft mast height for a dipole or inverted-V.'
    },
    {
        id: 'DIPOLE_15M',
        label: 'Station Dipole (15 m AGL)',
        notes: 'Higher fixed-station dipole when you have a taller tower.'
    },
    {
        id: 'PORTABLE_VERTICAL_QUARTER_AVG',
        label: 'Quarter-Wave Vertical (Average ground)',
        notes: '0.25 lambda vertical over average ground with omni pattern.'
    },
    {
        id: 'PORTABLE_VERTICAL_QUARTER_GOOD',
        label: 'Quarter-Wave Vertical (Good ground)',
        notes: '0.25 lambda vertical modeled over good ground conductivity.'
    },
    {
        id: 'PORTABLE_VERTICAL_DIPOLE_HVD025',
        label: 'Half-Wave Vertical Dipole',
        notes: '0.5 lambda vertical dipole fed at 0.25 lambda AGL; omnidirectional.'
    }
];

export default function PropagationEstimationTab() {
    const { config, setConfig } = useConfigContext();
    const [testingConnection, setTestingConnection] = React.useState(false);
    const [connectionStatus, setConnectionStatus] = React.useState<'idle' | 'success' | 'error'>('idle');
    const isPropEnabled = config.prop_enabled ?? true;

    const handleEnabledChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        const newConfig = { ...config, prop_enabled: event.target.checked };
        setConfig(newConfig);
    };

    const handleRefreshMinutesChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        const val = parseInt(event.target.value);
        if (!isNaN(val) && val >= 1 && val <= 60) {
            setConfig({ ...config, prop_refresh_minutes: val });
        }
    };

    const handleDefaultSsnChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        const val = parseFloat(event.target.value);
        if (!isNaN(val) && val > 0) {
            setConfig({ ...config, prop_default_ssn: val });
        }
    };

    const handleHistoryWindowChange = (_event: Event, value: number | number[]) => {
        const val = Array.isArray(value) ? value[0] : value;
        setConfig({ ...config, prop_history_minutes: val });
    };

    const handleKernelProfileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        setConfig({ ...config, prop_kernel_profile_id: event.target.value });
    };

    const handleTxAntennaChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        setConfig({ ...config, prop_tx_antenna_profile_id: event.target.value });
    };

    const handleRxAntennaChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        setConfig({ ...config, prop_rx_antenna_profile_id: event.target.value });
    };

    const handleTestConnection = async () => {
        setTestingConnection(true);
        setConnectionStatus('idle');

        try {
            if (window.pywebview !== undefined && window.pywebview.api !== null) {
                const result = await window.pywebview.api.test_propagation_connection();
                const parsed = typeof result === 'string' ? JSON.parse(result) : result;
                const connected = parsed?.connected ?? false;
                setConnectionStatus(connected ? 'success' : 'error');
            }
        } catch (error) {
            console.error('Error testing connection:', error);
            setConnectionStatus('error');
        } finally {
            setTestingConnection(false);
        }
    };

    const selectedKernelProfile = React.useMemo(
        () => KERNEL_PROFILES.find((profile) => profile.id === (config.prop_kernel_profile_id || 'regional')) ?? KERNEL_PROFILES[1],
        [config.prop_kernel_profile_id]
    );
    const selectedTxAntenna = React.useMemo(
        () => ANTENNA_OPTIONS.find((option) => option.id === (config.prop_tx_antenna_profile_id || 'POTA_DIPOLE_10M')) ?? ANTENNA_OPTIONS[2],
        [config.prop_tx_antenna_profile_id]
    );
    const selectedRxAntenna = React.useMemo(
        () => ANTENNA_OPTIONS.find((option) => option.id === (config.prop_rx_antenna_profile_id || 'POTA_DIPOLE_5M')) ?? ANTENNA_OPTIONS[1],
        [config.prop_rx_antenna_profile_id]
    );

    return (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <Typography variant="h6">Propagation Estimation </Typography>

            <Typography variant="body2" color="text.secondary">
                Uses VOACAP modeling blended with real-time WSPR reception reports to estimate whether a spot's path will open. Adjust the refresh interval and blending controls to mirror your operating style.
            </Typography>

            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                <Grid container spacing={2}>
                    <Grid item xs={12} md={6}>
                        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
                            <FormControlLabel
                                control={
                                    <Switch
                                        checked={isPropEnabled}
                                        onChange={handleEnabledChange}
                                    />
                                }
                                label="Enable Propagation Feature"
                            />

                            <TextField
                                label="Refresh Interval (minutes)"
                                type="number"
                                value={config.prop_refresh_minutes ?? 3}
                                onChange={handleRefreshMinutesChange}
                                helperText="How often to fetch data (1-60 min)"
                                inputProps={{ min: 1, max: 60 }}
                                disabled={!isPropEnabled}
                                size="small"
                            />

                            <TextField
                                label="Fallback SSN"
                                type="number"
                                value={config.prop_default_ssn ?? 61}
                                onChange={handleDefaultSsnChange}
                                helperText="Used when NOAA SSN cannot be retrieved"
                                inputProps={{ min: 1, step: 1 }}
                                disabled={!isPropEnabled}
                                size="small"
                            />
                        </Box>
                    </Grid>

                    <Grid item xs={12} md={6}>
                        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
                            <Button
                                variant="outlined"
                                onClick={handleTestConnection}
                                disabled={!isPropEnabled || testingConnection}
                                size="small"
                                sx={{ alignSelf: 'flex-start' }}
                            >
                                {testingConnection ? 'Testing...' : 'Test WSPR Connection'}
                            </Button>

                            {connectionStatus === 'success' && (
                                <Alert severity="success">Connected to WSPR Rocks!</Alert>
                            )}

                            {connectionStatus === 'error' && (
                                <Alert severity="error">Connection failed. Check internet.</Alert>
                            )}

                            <Box sx={{ p: 1.5, bgcolor: 'background.paper', borderRadius: 1 }}>
                                <Typography variant="subtitle2" gutterBottom>Color Legend</Typography>
                                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.3 }}>
                                    <Typography variant="body2">
                                        <span style={{ color: 'green', fontWeight: 'bold' }}>Green</span> - Prob A &gt; 75% (high confidence)
                                    </Typography>
                                    <Typography variant="body2">
                                        <span style={{ color: '#f9a825', fontWeight: 'bold' }}>Yellow</span> - Prob A between 25% and 75%
                                    </Typography>
                                    <Typography variant="body2">
                                        <span style={{ color: 'red', fontWeight: 'bold' }}>Red</span> - Prob A &lt; 25%
                                    </Typography>
                                    <Typography variant="body2">
                                        <span style={{ color: '#757575', fontWeight: 'bold' }}>Grey</span> - No recent data
                                    </Typography>
                                </Box>
                            </Box>
                        </Box>
                    </Grid>

                    <Grid item xs={12}>
                        <Box sx={{ p: 1.5, bgcolor: 'background.default', borderRadius: 1 }}>
                            <Typography variant="subtitle2" sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                                Propagation Prediction Options
                                <Tooltip title="We start with VOACAP's prediction for your station setup, then nudge it with current WSPR reception reports using an endpoint-aware kernel. Pick a blending profile and antenna pair that best matches you and the activator.">
                                    <IconButton size="small">
                                        <InfoOutlinedIcon fontSize="small" />
                                    </IconButton>
                                </Tooltip>
                            </Typography>

                            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, mt: 1 }}>
                                <Box>
                                    <Typography variant="body2">History window (minutes)</Typography>
                                    <Slider
                                        value={config.prop_history_minutes ?? 30}
                                        onChange={handleHistoryWindowChange}
                                        min={0}
                                        max={60}
                                        step={5}
                                        valueLabelDisplay="auto"
                                        disabled={!isPropEnabled}
                                    />
                                    <Typography variant="caption">
                                        Set to 0 to keep current behavior. Values above 0 fetch up to an hour of chunked predictions.
                                    </Typography>
                                </Box>

                                <Box>
                                    <TextField
                                        select
                                        fullWidth
                                        label="Kernel profile"
                                        value={config.prop_kernel_profile_id || 'regional'}
                                        onChange={handleKernelProfileChange}
                                        helperText={selectedKernelProfile?.description}
                                        disabled={!isPropEnabled}
                                        size="small"
                                    >
                                        {KERNEL_PROFILES.map((profile) => (
                                            <MenuItem key={profile.id} value={profile.id}>
                                                {profile.label}
                                            </MenuItem>
                                        ))}
                                    </TextField>
                                </Box>

                                <Box sx={{ display: 'flex', flexDirection: { xs: 'column', sm: 'row' }, gap: 2 }}>
                                    <TextField
                                        select
                                        label="My antenna profile"
                                        value={config.prop_tx_antenna_profile_id || 'POTA_DIPOLE_10M'}
                                        onChange={handleTxAntennaChange}
                                        helperText={selectedTxAntenna?.notes}
                                        disabled={!isPropEnabled}
                                        size="small"
                                        sx={{ flex: 1 }}
                                    >
                                        {ANTENNA_OPTIONS.map((option) => (
                                            <MenuItem key={option.id} value={option.id}>
                                                {option.label}
                                            </MenuItem>
                                        ))}
                                    </TextField>

                                    <TextField
                                        select
                                        label="Activator antenna profile"
                                        value={config.prop_rx_antenna_profile_id || 'POTA_DIPOLE_5M'}
                                        onChange={handleRxAntennaChange}
                                        helperText={selectedRxAntenna?.notes}
                                        disabled={!isPropEnabled}
                                        size="small"
                                        sx={{ flex: 1 }}
                                    >
                                        {ANTENNA_OPTIONS.map((option) => (
                                            <MenuItem key={option.id} value={option.id}>
                                                {option.label}
                                            </MenuItem>
                                        ))}
                                    </TextField>
                                </Box>
                            </Box>
                        </Box>
                    </Grid>
                </Grid>
            </Box>
        </Box>
    );
}
