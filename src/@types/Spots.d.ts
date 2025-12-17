export interface SpotRow {
    spotId: number,
    activator: string,
    frequency: string,
    mode: string,
    reference: string,
    parkName: any,
    spotTime: string,
    spotter: string,
    comments: string,
    source: string,
    invalid: any,
    name: string,
    locationDesc: string,
    grid4: string,
    grid6: string,
    latitude: number
    longitude: number,
    count: number,
    expire: number,
    hunted: boolean,
    hunted_bands: string,
    park_hunts: number,
    op_hunts: number,
    loc_hunts: number,
    loc_total: number,
    is_qrt: boolean,
    act_cmts: string,
    cw_wpm: number,
    spot_source: string
    propagation_snr?: number,
    propagation_status?: string,
    propagation_probability?: number,
    propagation_support?: number,
};

export interface PropagationHistoryPoint {
    timestamp: string,
    snr?: number,
    probability?: number
}
