import * as React from 'react';
import { UserConfig } from '../../@types/Config';

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
    prop_enabled: false,
    prop_data_source: 'pskreporter',
    prop_refresh_minutes: 10,
    prop_ssb_threshold: 10,
    prop_digital_threshold: -15
};

export interface ConfigContextType {
    config: UserConfig;
    setConfig: (newCfg: UserConfig) => void;
}

export const ConfigContext = React.createContext<ConfigContextType | null>(null);

export const ConfigContextProvider = ({ children }: any) => {
    const [configData, setConfigData] = React.useState<UserConfig>(defData);

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
            prop_data_source: ctx.prop_data_source,
            prop_refresh_minutes: ctx.prop_refresh_minutes,
            prop_ssb_threshold: ctx.prop_ssb_threshold,
            prop_digital_threshold: ctx.prop_digital_threshold
        };
        setConfigData(newContext);
    };

    const initialValue: ConfigContextType = {
        config: configData,
        setConfig: x
    }

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
