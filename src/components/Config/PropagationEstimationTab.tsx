import * as React from 'react';
import { Box, TextField, Typography, Switch, FormControlLabel, MenuItem, Button, Alert } from '@mui/material';
import { useConfigContext } from './ConfigContextProvider';

export default function PropagationEstimationTab() {
    const { config, setConfig } = useConfigContext();
    const [testingConnection, setTestingConnection] = React.useState(false);
    const [connectionStatus, setConnectionStatus] = React.useState<'idle' | 'success' | 'error'>('idle');

    const handleEnabledChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        console.log('[PropagationTab] Toggle changed to:', event.target.checked);
        console.log('[PropagationTab] Current config.prop_enabled:', config.prop_enabled);
        const newConfig = { ...config, prop_enabled: event.target.checked };
        console.log('[PropagationTab] Setting new config:', newConfig.prop_enabled);
        setConfig(newConfig);
    };

    const handleDataSourceChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        setConfig({ ...config, prop_data_source: event.target.value });
    };

    const handleRefreshMinutesChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        const val = parseInt(event.target.value);
        if (!isNaN(val) && val >= 5 && val <= 60) {
            setConfig({ ...config, prop_refresh_minutes: val });
        }
    };

    const handleSsbThresholdChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        const val = parseInt(event.target.value);
        if (!isNaN(val)) {
            setConfig({ ...config, prop_ssb_threshold: val });
        }
    };

    const handleDigitalThresholdChange = (event: React.ChangeEvent<HTMLInputElement>) => {
        const val = parseInt(event.target.value);
        if (!isNaN(val)) {
            setConfig({ ...config, prop_digital_threshold: val });
        }
    };

    const handleTestConnection = async () => {
        setTestingConnection(true);
        setConnectionStatus('idle');

        try {
            if (window.pywebview !== undefined && window.pywebview.api !== null) {
                const result = await window.pywebview.api.test_propagation_connection();
                setConnectionStatus(result ? 'success' : 'error');
            }
        } catch (error) {
            console.error('Error testing connection:', error);
            setConnectionStatus('error');
        } finally {
            setTestingConnection(false);
        }
    };

    return (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
            <Typography variant="h6">Propagation Estimation</Typography>

            <Typography variant="body2" color="text.secondary">
                Real-time propagation estimation based on digital mode reports.
            </Typography>

            <FormControlLabel
                control={
                    <Switch
                        checked={!!config.prop_enabled}
                        onChange={handleEnabledChange}
                    />
                }
                label="Enable Propagation Feature"
            />

            <TextField
                select
                label="Data Source"
                value={config.prop_data_source || 'pskreporter'}
                onChange={handleDataSourceChange}
                helperText="Select propagation data source"
                disabled={!config.prop_enabled}
                size="small"
            >
                <MenuItem value="pskreporter">PSKReporter (Recommended)</MenuItem>
                <MenuItem value="wspr">WSPRnet</MenuItem>
                <MenuItem value="rbn">Reverse Beacon Network</MenuItem>
            </TextField>

            <TextField
                label="Refresh Interval (minutes)"
                type="number"
                value={config.prop_refresh_minutes || 10}
                onChange={handleRefreshMinutesChange}
                helperText="How often to fetch data (5-60 min)"
                inputProps={{ min: 5, max: 60 }}
                disabled={!config.prop_enabled}
                size="small"
            />

            <Typography variant="subtitle2" sx={{ mt: 1 }}>SNR Thresholds</Typography>

            <Box sx={{ display: 'flex', gap: 2 }}>
                <TextField
                    label="SSB (dB)"
                    type="number"
                    value={config.prop_ssb_threshold || 10}
                    onChange={handleSsbThresholdChange}
                    helperText="Min SNR for voice"
                    disabled={!config.prop_enabled}
                    size="small"
                    sx={{ flex: 1 }}
                />

                <TextField
                    label="Digital (dB)"
                    type="number"
                    value={config.prop_digital_threshold || -15}
                    onChange={handleDigitalThresholdChange}
                    helperText="Min SNR for digital"
                    disabled={!config.prop_enabled}
                    size="small"
                    sx={{ flex: 1 }}
                />
            </Box>

            <Button
                variant="outlined"
                onClick={handleTestConnection}
                disabled={!config.prop_enabled || testingConnection}
                size="small"
                sx={{ alignSelf: 'flex-start' }}
            >
                {testingConnection ? 'Testing...' : 'Test Connection'}
            </Button>

            {connectionStatus === 'success' && (
                <Alert severity="success">Connected to {config.prop_data_source}!</Alert>
            )}

            {connectionStatus === 'error' && (
                <Alert severity="error">Connection failed. Check internet.</Alert>
            )}

            <Box sx={{ mt: 1, p: 1.5, bgcolor: 'background.paper', borderRadius: 1 }}>
                <Typography variant="subtitle2" gutterBottom>Color Legend:</Typography>
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.3 }}>
                    <Typography variant="body2">
                        <span style={{ color: 'green', fontWeight: 'bold' }}>● Green</span> - SSB (SNR ≥ {config.prop_ssb_threshold || 10}dB)
                    </Typography>
                    <Typography variant="body2">
                        <span style={{ color: 'orange', fontWeight: 'bold' }}>● Orange</span> - Digital (SNR ≥ {config.prop_digital_threshold || -15}dB)
                    </Typography>
                    <Typography variant="body2">
                        <span style={{ color: 'red', fontWeight: 'bold' }}>● Red</span> - Not reachable
                    </Typography>
                    <Typography variant="body2">
                        <span style={{ color: 'black', fontWeight: 'bold' }}>● Black</span> - No data
                    </Typography>
                </Box>
            </Box>
        </Box>
    );
}
