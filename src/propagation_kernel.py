"""
Kernel smoothing algorithm for propagation SNR prediction.

This module implements a propagation prediction system that uses
kernel smoothing to predict SNR values based on path geometry similarity
rather than direct callsign matching.

The algorithm:
1. For each spot, calculates the path geometry (distance, azimuth) from user to activator
2. Compares this path to all available WSPR reports
3. Uses a weighted average where weights depend on path similarity
4. Higher weight for reports with similar distance AND azimuth

Based on the approach described in earlier kernel smoothing experiments.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import datetime as dt
import math
import logging as L

import numpy as np

from propagation_utils import grid_to_latlon

try:
    from dvoacap.path_geometry import GeoPoint
    from dvoacap.prediction_engine import PredictionEngine
except ImportError:  # pragma: no cover - environment specific
    GeoPoint = None
    PredictionEngine = None
    # Delay import error until the first VOACAP call so the UI can surface
    # a helpful error message instead of failing at module import time.

logging = L.getLogger(__name__)

# ==============================
# Configuration / Tuning Parameters
# ==============================

# Distance scale (km) - controls how different path distances can be before weight drops
# Larger value = more tolerant of distance differences
D0_KM = 3000.0

# Azimuth scale (degrees) - controls how different path bearings can be before weight drops  
# Larger value = more tolerant of azimuth differences
T0_DEG = 60.0

# Minimum weight threshold - reports with weight below this are ignored
MIN_WEIGHT = 0.01

# Endpoint-aware kernel defaults
PSKREPORTER_REFERENCE_BW_HZ = 2500.0
KERNEL_EA_TX_RADIUS_KM = 200.0
KERNEL_EA_RX_RADIUS_KM = 200.0
KERNEL_EA_MIN_WEIGHT = 1e-4
KERNEL_EA_TOP_K = 300
KERNEL_EA_GAMMA = 2.0
KERNEL_EA_MIN_N_EFF = 5.0
DEFAULT_WSPR_TX_POWER_DBM = 33.0  # ~2W when TX power is missing

KERNEL_EA_PROFILE_GLOBAL = {
    "r_tx_km": 3000.0,
    "r_rx_km": 3000.0,
    "min_weight": 1e-12,
    "min_n_eff": 20.0,
}
KERNEL_EA_PROFILE_REGIONAL = {
    "r_tx_km": 1500.0,
    "r_rx_km": 1500.0,
    "min_weight": 1e-10,
    "min_n_eff": 15.0,
}
KERNEL_EA_PROFILE_LOCAL = {
    "r_tx_km": 600.0,
    "r_rx_km": 600.0,
    "min_weight": 1e-8,
    "min_n_eff": 10.0,
}
KERNEL_EA_PROFILE_HYPERLOCAL = {
    "r_tx_km": 200.0,
    "r_rx_km": 200.0,
    "min_weight": 1e-6,
    "min_n_eff": 6.0,
}

KERNEL_EA_PROFILES = {
    "global": KERNEL_EA_PROFILE_GLOBAL,
    "regional": KERNEL_EA_PROFILE_REGIONAL,
    "local": KERNEL_EA_PROFILE_LOCAL,
    "hyperlocal": KERNEL_EA_PROFILE_HYPERLOCAL,
}
DEFAULT_KERNEL_PROFILE_ID = "regional"

VOACAP_ANTENNA_PROFILES = [
    {
        "id": "BASE_ISOTROPE",
        "tx_antenna_code": "ISOTROPE",
        "rx_antenna_code": "ISOTROPE",
        "polarization": "theoretical",
        "geometry": {"gain_dbi": 0.0, "pattern": "omni"},
        "height_agl_m": None,
        "ground": None,
        "min_toa_deg_suggestion": 0.1,
        "notes": "Benchmark / unknown-station baseline. VOACAP calls this 0 dBi isotropic.",
    },
    {
        "id": "POTA_DIPOLE_5M",
        "tx_antenna_code": "D05M",
        "rx_antenna_code": "ISOTROPE",
        "polarization": "horizontal",
        "geometry": {"type": "half-wave dipole", "height_agl_m": 5, "pattern": "broadside (bi-directional)"},
        "ground": "typical",
        "min_toa_deg_suggestion": 3.0,
        "notes": "Portable inverted-V/dipole on ~16 ft support. DxxM means dipole at xx meters AGL.",
    },
    {
        "id": "POTA_DIPOLE_10M",
        "tx_antenna_code": "D10M",
        "rx_antenna_code": "ISOTROPE",
        "polarization": "horizontal",
        "geometry": {"type": "half-wave dipole", "height_agl_m": 10, "pattern": "broadside (bi-directional)"},
        "ground": "typical",
        "min_toa_deg_suggestion": 3.0,
        "notes": "Very common portable mast height (~33 ft).",
    },
    {
        "id": "DIPOLE_15M",
        "tx_antenna_code": "D15M",
        "rx_antenna_code": "ISOTROPE",
        "polarization": "horizontal",
        "geometry": {"type": "half-wave dipole", "height_agl_m": 15, "pattern": "broadside (bi-directional)"},
        "ground": "typical",
        "min_toa_deg_suggestion": 3.0,
        "notes": "Higher horizontal dipole for home stations.",
    },
    {
        "id": "PORTABLE_VERTICAL_QUARTER_AVG",
        "tx_antenna_code": "V14",
        "rx_antenna_code": "ISOTROPE",
        "polarization": "vertical",
        "geometry": {"type": "quarter-wave vertical", "length_lambda": 0.25, "pattern": "omni"},
        "ground": "Average",
        "min_toa_deg_suggestion": 0.1,
        "notes": "Quarter-wave vertical over Average ground.",
    },
    {
        "id": "PORTABLE_VERTICAL_QUARTER_GOOD",
        "tx_antenna_code": "V14GD",
        "rx_antenna_code": "ISOTROPE",
        "polarization": "vertical",
        "geometry": {"type": "quarter-wave vertical", "length_lambda": 0.25, "pattern": "omni"},
        "ground": "Good",
        "min_toa_deg_suggestion": 0.1,
        "notes": "Quarter-wave vertical over Good ground.",
    },
    {
        "id": "PORTABLE_VERTICAL_DIPOLE_HVD025",
        "tx_antenna_code": "HVD025",
        "rx_antenna_code": "ISOTROPE",
        "polarization": "vertical",
        "geometry": {"type": "half-wave vertical dipole", "feedpoint_height_lambda": 0.25, "pattern": "omni"},
        "ground": "typical",
        "min_toa_deg_suggestion": 0.1,
        "notes": "Half-wave vertical dipole fed at 0.25 wavelengths AGL.",
    },
]

VOACAP_ANTENNA_PROFILE_LOOKUP = {profile["id"]: profile for profile in VOACAP_ANTENNA_PROFILES}
DEFAULT_TX_ANTENNA_PROFILE_ID = "POTA_DIPOLE_10M"
DEFAULT_RX_ANTENNA_PROFILE_ID = "POTA_DIPOLE_5M"


def resolve_kernel_profile(profile_id: Optional[str]) -> dict[str, float]:
    """Return the kernel EA profile dict for the requested id."""
    if not profile_id:
        return KERNEL_EA_PROFILES[DEFAULT_KERNEL_PROFILE_ID]
    key = str(profile_id).lower()
    return KERNEL_EA_PROFILES.get(key, KERNEL_EA_PROFILES[DEFAULT_KERNEL_PROFILE_ID])


def resolve_voacap_antenna_profile(profile_id: Optional[str], default_id: str) -> dict[str, object]:
    """Return a VOACAP antenna profile entry for the requested id."""
    key = (profile_id or default_id or "").upper()
    if key in VOACAP_ANTENNA_PROFILE_LOOKUP:
        return VOACAP_ANTENNA_PROFILE_LOOKUP[key]
    return VOACAP_ANTENNA_PROFILE_LOOKUP[default_id]

# VOACAP defaults
MIN_TAKEOFF_DEG = 3.0
RESIDENTIAL_NOISE_DB = 145.0
REQUIRED_SNR_DBHZ = 13.0
REQUIRED_RELIABILITY = 0.9
DEFAULT_SSN = 61.0
K0_DEFAULT = 20.0
K0_UNREACHABLE = 80.0

MODE_THRESHOLDS_DB = {
    'ssb': 6.0,
    'digital': -15.0,
}

BAND_CENTER_FREQ_MHZ = {
    "160m": 1.85,
    "80m": 3.57,
    "60m": 5.357,
    "40m": 7.074,
    "30m": 10.136,
    "20m": 14.074,
    "17m": 18.1,
    "15m": 21.074,
    "12m": 24.915,
    "10m": 28.074,
    "6m": 50.313,
}


# ==============================
# Data structures
# ==============================

@dataclass
class PropagationReport:
    """A single WSPR reception report with path geometry."""
    timestamp_utc: str
    band: str
    snr_db: float
    
    tx_call: str
    tx_grid: str
    tx_lat: float
    tx_lon: float
    
    rx_call: str
    rx_grid: str
    rx_lat: float
    rx_lon: float
    
    distance_km: float
    azimuth_deg: float
    tx_power_dbm: Optional[float] = None


@dataclass
class SpotPath:
    """Path geometry for a spot (user -> activator)."""
    spot_id: int
    activator_call: str
    activator_grid: str
    
    distance_km: float
    azimuth_deg: float
    band: str
    mode: Optional[str] = None
    frequency_mhz: Optional[float] = None
    target_lat: Optional[float] = None
    target_lon: Optional[float] = None


@dataclass
class EndpointKernelArrays:
    """Vectorised representation of WSPR reports for endpoint-aware smoothing."""
    tx_lat: np.ndarray
    tx_lon: np.ndarray
    rx_lat: np.ndarray
    rx_lon: np.ndarray
    snr_db: np.ndarray
    tx_power_dbm: np.ndarray
    channel_score: np.ndarray


@dataclass
class PropagationEstimate:
    """Combined Kernel EA + VOACAP prediction for a spot."""
    snr: Optional[float]
    probability: Optional[float]
    support: Optional[float]
    kernel_probability: Optional[float] = None
    voacap_probability: Optional[float] = None
    voacap_snr: Optional[float] = None


# ==============================
# Geometry calculations
# ==============================

def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate initial great-circle bearing from point 1 to point 2.
    
    Args:
        lat1: Latitude of point 1 in degrees
        lon1: Longitude of point 1 in degrees
        lat2: Latitude of point 2 in degrees
        lon2: Longitude of point 2 in degrees
        
    Returns:
        Bearing in degrees (0-360), where 0 = North, 90 = East
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    
    y = math.sin(dlambda) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2) -
         math.sin(phi1) * math.cos(phi2) * math.cos(dlambda))
    
    brng = math.degrees(math.atan2(y, x))
    return (brng + 360.0) % 360.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two latitude/longitude points in km."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


# ==============================
# Kernel smoothing algorithm
# ==============================

def spot_weight(report: PropagationReport, 
                target_dist_km: float,
                target_az_deg: float,
                distance_scale_km: float = D0_KM,
                azimuth_scale_deg: float = T0_DEG) -> float:
    """
    Calculate similarity weight for a report relative to a target path.
    
    Uses Gaussian kernels in both distance and azimuth dimensions.
    Weight is high when report's path is similar to target path.
    
    Args:
        report: Propagation report with path geometry
        target_dist_km: Target path distance in km
        target_az_deg: Target path azimuth in degrees
        
    Returns:
        Weight value between 0.0 and 1.0
    """
    # Guard against zero scales
    distance_scale = max(distance_scale_km, 1.0)
    azimuth_scale = max(azimuth_scale_deg, 1.0)

    # Distance similarity
    delta_d = abs(report.distance_km - target_dist_km)
    w_d = math.exp(-(delta_d / distance_scale) ** 2)
    
    # Azimuth similarity (handle wrap-around at 0/360)
    delta_theta_raw = abs(report.azimuth_deg - target_az_deg)
    delta_theta = min(delta_theta_raw, 360.0 - delta_theta_raw)
    w_t = math.exp(-(delta_theta / azimuth_scale) ** 2)
    
    # Combined weight
    return w_d * w_t


def predict_snr_for_spot(spot_path: SpotPath,
                         reports: List[PropagationReport],
                         distance_scale_km: float = D0_KM,
                         azimuth_scale_deg: float = T0_DEG) -> Optional[float]:
    """
    Predict SNR for a single spot using kernel smoothing over all reports.
    
    Args:
        spot_path: Path geometry from user to activator
        reports: List of all available propagation reports (same band)
        
    Returns:
        Predicted SNR in dB, or None if no usable data
    """
    if not reports:
        return None
    
    weighted_sum = 0.0
    weight_total = 0.0
    
    for report in reports:
        w = spot_weight(
            report,
            spot_path.distance_km,
            spot_path.azimuth_deg,
            distance_scale_km,
            azimuth_scale_deg
        )
        
        # Skip reports with negligible weight
        if w < MIN_WEIGHT:
            continue
            
        weighted_sum += w * report.snr_db
        weight_total += w
    
    if weight_total == 0.0:
        return None
    
    return weighted_sum / weight_total


def batch_predict_snr(spot_paths: List[SpotPath],
                      reports_by_band: Dict[str, List[PropagationReport]],
                      distance_scale_km: float = D0_KM,
                      azimuth_scale_deg: float = T0_DEG) -> Dict[int, float]:
    """
    Predict SNR for multiple spots efficiently.
    
    Args:
        spot_paths: List of spot path geometries
        reports_by_band: Dictionary mapping band name to list of reports
        
    Returns:
        Dictionary mapping spot_id to predicted SNR (or None if no prediction)
    """
    predictions = {}
    
    for spot_path in spot_paths:
        # Get reports for this spot's band
        reports = reports_by_band.get(spot_path.band, [])
        
        # Predict SNR
        snr = predict_snr_for_spot(
            spot_path,
            reports,
            distance_scale_km,
            azimuth_scale_deg
        )
        predictions[spot_path.spot_id] = snr
    
    logging.debug(f"[KERNEL] Predicted SNR for {len(spot_paths)} spots, "
                 f"{sum(1 for v in predictions.values() if v is not None)} have predictions")
    
    return predictions


# ==============================
# SNR classification
# ==============================

def classify_snr(snr: Optional[float],
                ssb_threshold: float = 6.0,
                digital_threshold: float = -15.0) -> str:
    """
    Classify SNR into capability categories.
    
    Args:
        snr: Predicted SNR in dB (or None)
        ssb_threshold: Minimum SNR for SSB modes
        digital_threshold: Minimum SNR for digital modes
        
    Returns:
        Status string: 'ssb', 'digital', 'not_reachable', or 'no_data'
    """
    if snr is None:
        return 'no_data'
    
    if snr >= ssb_threshold:
        return 'ssb'
    elif snr >= digital_threshold:
        return 'digital'
    else:
        return 'not_reachable'


# ==============================
# Endpoint-aware Kernel + VOACAP helpers
# ==============================


def watts_to_dbm(power_watts: float) -> float:
    if power_watts <= 0:
        return 0.0
    return 10.0 * math.log10(power_watts) + 30.0


def dbm_to_watts(dbm: float) -> float:
    return 10 ** ((dbm - 30.0) / 10.0)


def snr_threshold_to_voacap_required_snr_dbhz(threshold_db: float,
                                              reference_bw_hz: float = PSKREPORTER_REFERENCE_BW_HZ) -> float:
    return threshold_db + 10.0 * math.log10(max(1.0, reference_bw_hz))


def clamp_probability(p: float, eps: float = 1e-4) -> float:
    return max(eps, min(1.0 - eps, float(p)))


def shrink_with_voacap_prior(p_kernel: float,
                             n_eff: float,
                             p_voacap: Optional[float],
                             k0: float = K0_DEFAULT) -> float:
    if p_voacap is None:
        return clamp_probability(p_kernel)
    p_kernel = clamp_probability(p_kernel)
    p_voacap = clamp_probability(p_voacap)
    n_eff = max(0.0, float(n_eff))
    return (p_kernel * n_eff + p_voacap * k0) / (n_eff + k0)


def shrink_snr_with_voacap_prior(snr_kernel: Optional[float],
                                 n_eff: float,
                                 snr_voacap: Optional[float],
                                 k0: float = K0_DEFAULT) -> Optional[float]:
    if snr_kernel is None and snr_voacap is None:
        return None
    if snr_kernel is None:
        return snr_voacap
    if snr_voacap is None:
        return snr_kernel
    n_eff = max(0.0, float(n_eff))
    w = n_eff / (n_eff + max(k0, 1e-6))
    return w * float(snr_kernel) + (1.0 - w) * float(snr_voacap)


SSB_MODE_PREFIXES = ('SSB', 'USB', 'LSB', 'AM', 'FM', 'PHONE', 'PH', 'VOICE')


def categorize_operation_mode(mode: Optional[str]) -> str:
    if not mode:
        return 'digital'
    text = mode.strip().upper()
    if any(text.startswith(prefix) for prefix in SSB_MODE_PREFIXES):
        return 'ssb'
    return 'digital'


def _resolve_frequency_mhz(freq_hint: Optional[float], band: str) -> float:
    if freq_hint and freq_hint > 0:
        return freq_hint
    return BAND_CENTER_FREQ_MHZ.get(band.lower(), 14.074)


def build_endpoint_kernel_arrays(reports: List[PropagationReport]) -> Optional[EndpointKernelArrays]:
    if not reports:
        return None
    tx_lat = np.array([float(r.tx_lat) for r in reports], dtype=float)
    tx_lon = np.array([float(r.tx_lon) for r in reports], dtype=float)
    rx_lat = np.array([float(r.rx_lat) for r in reports], dtype=float)
    rx_lon = np.array([float(r.rx_lon) for r in reports], dtype=float)
    snr_db = np.array([float(r.snr_db) for r in reports], dtype=float)
    tx_power = np.array([
        float(r.tx_power_dbm) if r.tx_power_dbm is not None else DEFAULT_WSPR_TX_POWER_DBM
        for r in reports
    ], dtype=float)
    channel_score = snr_db - tx_power
    return EndpointKernelArrays(
        tx_lat=tx_lat,
        tx_lon=tx_lon,
        rx_lat=rx_lat,
        rx_lon=rx_lon,
        snr_db=snr_db,
        tx_power_dbm=tx_power,
        channel_score=channel_score,
    )


def predict_endpoint_kernel(dataset: EndpointKernelArrays,
                            user_lat: float,
                            user_lon: float,
                            target_lat: float,
                            target_lon: float,
                            user_power_dbm: float,
                            *,
                            r_tx_km: float = KERNEL_EA_TX_RADIUS_KM,
                            r_rx_km: float = KERNEL_EA_RX_RADIUS_KM,
                            min_weight: float = KERNEL_EA_MIN_WEIGHT,
                            top_k: Optional[int] = KERNEL_EA_TOP_K,
                            sharpen_gamma: float = KERNEL_EA_GAMMA,
                            threshold_db: float = MODE_THRESHOLDS_DB['ssb'],
                            ) -> Tuple[Optional[float], Optional[float], Dict[str, float]]:
    if dataset.tx_lat.size == 0:
        return None, None, {"sum_w": 0.0, "n_eff": 0.0, "used_points": 0}

    d_tx = np.array([
        haversine_km(lat, lon, user_lat, user_lon)
        for lat, lon in zip(dataset.tx_lat, dataset.tx_lon)
    ], dtype=float)
    d_rx = np.array([
        haversine_km(lat, lon, target_lat, target_lon)
        for lat, lon in zip(dataset.rx_lat, dataset.rx_lon)
    ], dtype=float)

    weights = np.exp(-((d_tx / max(1.0, r_tx_km)) ** 2))
    weights *= np.exp(-((d_rx / max(1.0, r_rx_km)) ** 2))

    if sharpen_gamma and sharpen_gamma != 1.0:
        weights = weights ** sharpen_gamma

    if top_k is not None and weights.size > top_k:
        idx = np.argpartition(weights, -top_k)[-top_k:]
        weights = weights[idx]
        d_tx = d_tx[idx]
        d_rx = d_rx[idx]
        channel_score = dataset.channel_score[idx]
    else:
        channel_score = dataset.channel_score

    mask = (weights >= min_weight) & np.isfinite(channel_score)
    if not np.any(mask):
        return None, None, {"sum_w": 0.0, "n_eff": 0.0, "used_points": 0}

    w = weights[mask]
    c_use = channel_score[mask]
    sum_w = float(np.sum(w))
    sum_w2 = float(np.sum(w * w))
    if sum_w <= 0.0:
        return None, None, {"sum_w": 0.0, "n_eff": 0.0, "used_points": 0}
    n_eff = (sum_w * sum_w / sum_w2) if sum_w2 > 0 else 0.0

    c_hat = float(np.sum(w * c_use) / sum_w)
    snr_hat = c_hat + user_power_dbm
    p_mode = float(np.sum(w * (c_use + user_power_dbm >= threshold_db)) / sum_w)
    support = {"sum_w": sum_w, "n_eff": n_eff, "used_points": int(mask.sum())}

    return snr_hat, clamp_probability(p_mode), support


def predict_voacap_for_path(hunter_grid: str,
                            target_grid: str,
                            freq_mhz: float,
                            ssn: float,
                            tx_power_watts: float,
                            reference_bw_hz: float,
                            snr_threshold_dbhz: float,
                            tx_antenna: Optional[dict] = None,
                            rx_antenna: Optional[dict] = None) -> Tuple[Optional[float], Optional[float]]:
    if PredictionEngine is None or GeoPoint is None:
        raise RuntimeError("dvoacap is not installed; please add it to requirements.")
    if not hunter_grid or not target_grid:
        return None, None
    try:
        hunter_lat, hunter_lon = grid_to_latlon(hunter_grid)
        target_lat, target_lon = grid_to_latlon(target_grid)
    except Exception as exc:
        logging.debug("[VOACAP] Unable to convert grids %s/%s: %s", hunter_grid, target_grid, exc)
        return None, None

    now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    seconds = now.hour * 3600 + now.minute * 60 + now.second + now.microsecond / 1_000_000.0
    utc_fraction = seconds / 86400.0

    engine = PredictionEngine()
    params = engine.params
    params.ssn = float(ssn)
    params.month = now.month
    params.tx_power = float(tx_power_watts) if tx_power_watts > 0 else 1.0

    if tx_antenna and tx_antenna.get("tx_antenna_code"):
        setattr(params, "tx_antenna_code", tx_antenna["tx_antenna_code"])
    if rx_antenna and rx_antenna.get("rx_antenna_code"):
        setattr(params, "rx_antenna_code", rx_antenna["rx_antenna_code"])

    tx_min_angle = float(tx_antenna.get("min_toa_deg_suggestion")) if tx_antenna and tx_antenna.get("min_toa_deg_suggestion") is not None else MIN_TAKEOFF_DEG
    rx_min_angle = float(rx_antenna.get("min_toa_deg_suggestion")) if rx_antenna and rx_antenna.get("min_toa_deg_suggestion") is not None else MIN_TAKEOFF_DEG
    min_takeoff_deg = max(MIN_TAKEOFF_DEG, tx_min_angle, rx_min_angle)

    params.tx_location = GeoPoint.from_degrees(hunter_lat, hunter_lon)
    params.min_angle = math.radians(min_takeoff_deg)
    params.man_made_noise_at_3mhz = RESIDENTIAL_NOISE_DB
    params.required_snr = REQUIRED_SNR_DBHZ
    params.required_reliability = REQUIRED_RELIABILITY

    rx_point = GeoPoint.from_degrees(target_lat, target_lon)
    engine.predict(rx_location=rx_point, utc_time=utc_fraction, frequencies=[float(freq_mhz)])

    if not engine.predictions:
        return None, None

    pred = engine.predictions[0]
    snr_db = float(pred.signal.snr_db)
    path = getattr(engine, "path", None)
    if path is not None and getattr(path, "dist", 0) >= PredictionEngine.RAD_7000_KM:
        best_mode = getattr(engine, "_best_mode", None)
        if best_mode and getattr(best_mode, "signal", None):
            snr_db = float(best_mode.signal.snr_db)

    snr_db -= 10.0 * math.log10(max(reference_bw_hz, 1.0))

    probability = None
    try:
        params.required_snr = float(snr_threshold_dbhz)
        probability = float(engine._calc_service_prob())
    except Exception as exc:
        logging.debug("[VOACAP] Unable to calculate service probability: %s", exc)

    return snr_db, probability


def batch_predict_kernel_ea(
    spot_paths: List[SpotPath],
    reports_by_band: Dict[str, List[PropagationReport]],
    *,
    hunter_grid: str,
    user_lat: float,
    user_lon: float,
    user_power_dbm: float,
    user_power_watts: float,
    ssn: float = DEFAULT_SSN,
    reference_bw_hz: float = PSKREPORTER_REFERENCE_BW_HZ,
    mode_thresholds: Dict[str, float] = MODE_THRESHOLDS_DB,
    min_support: Optional[float] = None,
    kernel_profile: Optional[dict] = None,
    tx_antenna_profile: Optional[dict] = None,
    rx_antenna_profile: Optional[dict] = None,
) -> Dict[int, PropagationEstimate]:
    predictions: Dict[int, PropagationEstimate] = {}
    if not spot_paths:
        return predictions

    profile_settings = kernel_profile or resolve_kernel_profile(None)
    r_tx_km = float(profile_settings.get("r_tx_km", KERNEL_EA_TX_RADIUS_KM))
    r_rx_km = float(profile_settings.get("r_rx_km", KERNEL_EA_RX_RADIUS_KM))
    min_weight = float(profile_settings.get("min_weight", KERNEL_EA_MIN_WEIGHT))
    profile_min_support = float(profile_settings.get("min_n_eff", KERNEL_EA_MIN_N_EFF))
    support_threshold = float(min_support) if min_support is not None else profile_min_support
    tx_profile = tx_antenna_profile or resolve_voacap_antenna_profile(None, DEFAULT_TX_ANTENNA_PROFILE_ID)
    rx_profile = rx_antenna_profile or resolve_voacap_antenna_profile(None, DEFAULT_RX_ANTENNA_PROFILE_ID)

    dataset_cache: Dict[str, Optional[EndpointKernelArrays]] = {}
    for band, reports in reports_by_band.items():
        dataset_cache[band.lower()] = build_endpoint_kernel_arrays(reports)

    for spot in spot_paths:
        band_key = (spot.band or "").lower()
        dataset = dataset_cache.get(band_key)
        if not dataset:
            predictions[spot.spot_id] = PropagationEstimate(None, None, None)
            continue
        if spot.target_lat is None or spot.target_lon is None:
            predictions[spot.spot_id] = PropagationEstimate(None, None, None)
            continue
        if not spot.activator_grid:
            predictions[spot.spot_id] = PropagationEstimate(None, None, None)
            continue

        freq_mhz = _resolve_frequency_mhz(spot.frequency_mhz, band_key)
        mode_category = categorize_operation_mode(spot.mode)
        threshold_db = mode_thresholds.get(mode_category, MODE_THRESHOLDS_DB['digital'])
        threshold_dbhz = snr_threshold_to_voacap_required_snr_dbhz(threshold_db, reference_bw_hz)

        kernel_snr = None
        kernel_prob = None
        support = {"n_eff": 0.0, "used_points": 0}
        try:
            kernel_snr, kernel_prob, support = predict_endpoint_kernel(
                dataset,
                user_lat=user_lat,
                user_lon=user_lon,
                target_lat=spot.target_lat,
                target_lon=spot.target_lon,
                user_power_dbm=user_power_dbm,
                r_tx_km=r_tx_km,
                r_rx_km=r_rx_km,
                min_weight=min_weight,
                top_k=KERNEL_EA_TOP_K,
                sharpen_gamma=KERNEL_EA_GAMMA,
                threshold_db=threshold_db,
            )
        except Exception as exc:
            logging.debug("[KERNEL EA] Error predicting SNR for spot %s: %s", spot.spot_id, exc)

        voacap_snr = None
        voacap_prob = None
        try:
            voacap_snr, voacap_prob = predict_voacap_for_path(
                hunter_grid=hunter_grid,
                target_grid=spot.activator_grid,
                freq_mhz=freq_mhz,
                ssn=ssn,
                tx_power_watts=user_power_watts,
                reference_bw_hz=reference_bw_hz,
                snr_threshold_dbhz=threshold_dbhz,
                tx_antenna=tx_profile,
                rx_antenna=rx_profile,
            )
        except Exception as exc:
            logging.debug("[VOACAP] Prediction failure for spot %s: %s", spot.spot_id, exc)

        n_eff = float(support.get('n_eff') or 0.0)
        voacap_unreachable = voacap_snr is not None and voacap_snr < MODE_THRESHOLDS_DB['digital']

        probability: Optional[float] = None
        if kernel_prob is not None and math.isfinite(kernel_prob) and n_eff >= support_threshold:
            k0_prob = K0_UNREACHABLE if voacap_unreachable else K0_DEFAULT
            probability = shrink_with_voacap_prior(kernel_prob, n_eff, voacap_prob, k0_prob)
        elif voacap_prob is not None:
            probability = clamp_probability(voacap_prob)
        elif kernel_prob is not None:
            probability = clamp_probability(kernel_prob)

        snr_final: Optional[float]
        if n_eff < min_support and voacap_snr is not None:
            snr_final = voacap_snr
        else:
            k0_snr = K0_UNREACHABLE if voacap_unreachable else K0_DEFAULT
            snr_final = shrink_snr_with_voacap_prior(kernel_snr, n_eff, voacap_snr, k0_snr)

        support_value = n_eff if n_eff > 0 else None

        predictions[spot.spot_id] = PropagationEstimate(
            snr=snr_final,
            probability=probability,
            support=support_value,
            kernel_probability=kernel_prob,
            voacap_probability=voacap_prob,
            voacap_snr=voacap_snr,
        )

    return predictions
