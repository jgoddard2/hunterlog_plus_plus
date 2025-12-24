import * as React from 'react';
import { ConfigVer2, UserConfig } from '../../@types/Config';

const defData: UserConfig = {
    my_call: '',
    my_grid6: '',
    default_pwr: 0,
    flr_host: '',
    flr_port: 0,
    adif_host: '',
    adif_port: 0,
    logger_type: 0,
    size_x: 0,
    size_y: 0,
    is_max: false,
    cw_mode: '',
    ftx_mode: '',
    qth_string: '',
    rig_if_type: '',
    scan_wait_time: 5,
    prop_enabled: true,
    prop_refresh_minutes: 3,
    prop_default_ssn: 61,
    prop_ssb_threshold: 6,
    prop_digital_threshold: -15,
    prop_distance_scale_km: 3000,
    prop_azimuth_scale_deg: 60,
    prop_history_minutes: 30,
    prop_kernel_profile_id: 'regional',
    prop_tx_antenna_profile_id: 'POTA_DIPOLE_10M',
    prop_rx_antenna_profile_id: 'POTA_DIPOLE_5M',
    prop_ssn_override_enabled: false
};

export interface ConfigContextType {
    config: UserConfig;
    setConfig: (newCfg: UserConfig) => void;
}

export const ConfigContext = React.createContext<ConfigContextType | null>(null);

export const ConfigContextProvider = ({ children }: any) => {
    const [configData, setConfigData] = React.useState<UserConfig>(defData);

    const mapRowsToConfig = React.useCallback((rows: ConfigVer2[]): UserConfig => {
        const getVar = (key: string): string => rows.find((r) => r.key === key)?.val ?? '';
        const toNumber = (val: string, fallback: number): number => {
            const parsed = Number(val);
            return Number.isNaN(parsed) ? fallback : parsed;
        };
        const toBool = (val: string, fallback: boolean): boolean => {
            if (val === undefined || val === null || val === '') {
                return fallback;
            }
            return !(val.toString().toLowerCase() === 'false');
        };

        return {
            my_call: getVar('my_call') || defData.my_call,
            my_grid6: getVar('my_grid6') || defData.my_grid6,
            default_pwr: toNumber(getVar('default_pwr'), defData.default_pwr),
            flr_host: getVar('flr_host') || defData.flr_host,
            flr_port: toNumber(getVar('flr_port'), defData.flr_port),
            adif_host: getVar('adif_host') || defData.adif_host,
            adif_port: toNumber(getVar('adif_port'), defData.adif_port),
            logger_type: toNumber(getVar('logger_type'), defData.logger_type),
            size_x: toNumber(getVar('size_x'), defData.size_x),
            size_y: toNumber(getVar('size_y'), defData.size_y),
            is_max: toBool(getVar('is_max'), defData.is_max),
            cw_mode: getVar('cw_mode') || defData.cw_mode,
            ftx_mode: getVar('ftx_mode') || defData.ftx_mode,
            qth_string: getVar('qth_string') || defData.qth_string,
            rig_if_type: getVar('rig_if_type') || defData.rig_if_type,
            scan_wait_time: toNumber(getVar('scan_wait_time'), defData.scan_wait_time),
            prop_enabled: toBool(getVar('prop_enabled'), defData.prop_enabled),
            prop_refresh_minutes: toNumber(getVar('prop_refresh_minutes'), defData.prop_refresh_minutes),
            prop_default_ssn: toNumber(getVar('prop_default_ssn'), defData.prop_default_ssn),
            prop_ssb_threshold: toNumber(getVar('prop_ssb_threshold'), defData.prop_ssb_threshold),
            prop_digital_threshold: toNumber(getVar('prop_digital_threshold'), defData.prop_digital_threshold),
            prop_distance_scale_km: toNumber(getVar('prop_distance_scale_km'), defData.prop_distance_scale_km),
            prop_azimuth_scale_deg: toNumber(getVar('prop_azimuth_scale_deg'), defData.prop_azimuth_scale_deg),
            prop_history_minutes: toNumber(getVar('prop_history_minutes'), defData.prop_history_minutes),
            prop_kernel_profile_id: getVar('prop_kernel_profile_id') || defData.prop_kernel_profile_id,
            prop_tx_antenna_profile_id: getVar('prop_tx_antenna_profile_id') || defData.prop_tx_antenna_profile_id,
            prop_rx_antenna_profile_id: getVar('prop_rx_antenna_profile_id') || defData.prop_rx_antenna_profile_id,
            prop_ssn_override_enabled: defData.prop_ssn_override_enabled
        };
    }, []);

    const x = (ctx: UserConfig) => {
        const newContext: UserConfig = {
            my_call: ctx.my_call,
            my_grid6: ctx.my_grid6,
            default_pwr: ctx.default_pwr,
            flr_host: ctx.flr_host,
            flr_port: ctx.flr_port,
            adif_host: ctx.adif_host,
            adif_port: ctx.adif_port,
            logger_type: ctx.logger_type,
            size_x: ctx.size_x,
            size_y: ctx.size_y,
            is_max: ctx.is_max,
            cw_mode: ctx.cw_mode,
            ftx_mode: ctx.ftx_mode,
            qth_string: ctx.qth_string,
            rig_if_type: ctx.rig_if_type,
            scan_wait_time: ctx.scan_wait_time,
            prop_enabled: ctx.prop_enabled,
            prop_refresh_minutes: ctx.prop_refresh_minutes,
            prop_default_ssn: ctx.prop_default_ssn ?? 61,
            prop_ssb_threshold: ctx.prop_ssb_threshold,
            prop_digital_threshold: ctx.prop_digital_threshold,
            prop_distance_scale_km: ctx.prop_distance_scale_km,
            prop_azimuth_scale_deg: ctx.prop_azimuth_scale_deg,
            prop_history_minutes: ctx.prop_history_minutes,
            prop_kernel_profile_id: ctx.prop_kernel_profile_id || 'regional',
            prop_tx_antenna_profile_id: ctx.prop_tx_antenna_profile_id || 'POTA_DIPOLE_10M',
            prop_rx_antenna_profile_id: ctx.prop_rx_antenna_profile_id || 'POTA_DIPOLE_5M',
            prop_ssn_override_enabled: ctx.prop_ssn_override_enabled ?? false
        };
        setConfigData(newContext);
    };

    const initialValue: ConfigContextType = {
        config: configData,
        setConfig: x
    }

    React.useEffect(() => {
        if (typeof window === 'undefined') {
            return;
        }

        const loadConfig = () => {
            if (!window.pywebview?.api?.get_user_config2) {
                return;
            }
            window.pywebview.api.get_user_config2().then((response: string) => {
                if (!response) {
                    return;
                }
                try {
                    const rows = JSON.parse(response) as ConfigVer2[];
                    setConfigData(mapRowsToConfig(rows));
                } catch (error) {
                    console.error('[ConfigContextProvider] Failed to parse user config', error);
                }
            }).catch((error: unknown) => {
                console.error('[ConfigContextProvider] Failed to load user config', error);
            });
        };

        if (window.pywebview?.api) {
            loadConfig();
            return;
        }

        window.addEventListener('pywebviewready', loadConfig);
        return () => {
            window.removeEventListener('pywebviewready', loadConfig);
        };
    }, [mapRowsToConfig]);

    return (
        <ConfigContext.Provider value={initialValue}>
            {children}
        </ConfigContext.Provider>
    )
}


export const useConfigContext = () => {
    const context = React.useContext(ConfigContext);

    if (!context) {
        throw new Error("useConfigContext must be used inside the AppContextProvider");
    }

    return context;
};
