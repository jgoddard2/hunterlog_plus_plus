#!/usr/bin/env python3
"""
Offline GUI tool to validate VOACAP SNR predictions against stored WSPR data.

This fork loads WSPR.Rocks exports that include TX/RX endpoints and the
reported transmitter power.  We split the most recent spots into train/test
groups, keep the VOACAP median SNR check, retain the distance/azimuth kernel,
and add an endpoint-aware, power-aware kernel that predicts SNR and the
probability of exceeding an SSB threshold for each test spot.
"""

import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox

from dvoacap.path_geometry import GeoPoint
from dvoacap.prediction_engine import PredictionEngine


WSPR_ROCKS_FILE = Path("WSPRrocks_20m_20251215_150733.txt")
WSPR_LOOKBACK_MINUTES = 15
MAX_WSPR_REPORTS = 5000
TRAIN_FRACTION = 0.8

PSKREPORTER_REFERENCE_BW_HZ = 2500.0  # WSJT-X/PSKReporter SNR reference bandwidth

EFFECTIVE_TX_POWER_WATTS = 18.0  # 30 W with ~2 dB loss
MIN_TAKEOFF_DEG = 3.0
REQUIRED_SNR_DBHZ = 13.0
REQUIRED_RELIABILITY = 0.9
RESIDENTIAL_NOISE_DB = 145.0
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

D0_KM = 200.
T0_DEG = 5.0
MIN_KERNEL_WEIGHT = 0.01
SSB_MIN_THRESHOLD_DB = 6.0
KERNEL_EA_TX_RADIUS_KM = 200.0
KERNEL_EA_RX_RADIUS_KM = 200.0
KERNEL_EA_MIN_WEIGHT = 1e-4
KERNEL_EA_TOP_K = 300
KERNEL_EA_GAMMA = 2.0
KERNEL_EA_MIN_N_EFF = 5.0
BASE_TX_FILTER_GRID: str | None = None
BASE_TX_FILTER_RADIUS_KM = 50.0
SSB_REQUIRED_SNR_DBHZ = None  # initialized after helper definitions

SNR_CATEGORIES = (
    (20.0, "SSB High"),
    (13.0, "SSB Medium"),
    (6.0, "SSB Low"),
    (-15.0, "Digital Only"),
    (-float("inf"), "Unreachable"),
)

VOACAP_DET_THRESHOLD_DB = 0.0

def is_valid_maidenhead(text: str) -> bool:
    """Return True if the string looks like a Maidenhead grid (4-8 chars)."""
    text = text.strip().upper()
    if len(text) not in (4, 6, 8):
        return False
    pattern = re.compile(r"^[A-R]{2}\d{2}([A-X]{2}(\d{2})?)?$")
    return bool(pattern.match(text))


WSPR_LINE_RE = re.compile(
    r"^(?P<timestamp>\S+)\s+(?P<freq>[\d\.]+)\s+MHz\s+SNR\s+(?P<snr>[-+]?\d+)\s+dB\s+TX\s+(?P<tx_call>[A-Za-z0-9/]+)\s+\((?P<tx_grid>[A-Za-z0-9]{4,8})\)\s+->\s+RX\s+(?P<rx_call>[A-Za-z0-9/]+)\s+\((?P<rx_grid>[A-Za-z0-9]{4,8})\)\s+TX\s+PWR\s+(?P<tx_pwr>[-+]?\d+(?:\.\d+)?)\s+dBm"
)


# ---------------------------------------------------------------------------
# Maidenhead helper
# ---------------------------------------------------------------------------

def maidenhead_to_latlon(locator: str) -> tuple[float, float]:
    """
    Convert a Maidenhead locator (4- or 6-character) to (lat, lon) in degrees,
    using the *center* of the grid square.

    Very standard implementation; good enough for VOACAP path geometry.
    """
    loc = locator.strip()
    if len(loc) < 4:
        raise ValueError(f"Grid '{locator}' is too short; need at least 4 chars")

    loc = loc.strip()
    # Normalize case: fields/squares are upper, subsquares can be lower
    loc = loc[:6]  # ignore extra precision if present
    # Pad to even length: 2, 4 or 6 characters
    if len(loc) in (3, 5):
        raise ValueError(f"Grid '{locator}' has odd length; expected 4 or 6")

    loc = loc.upper()

    # Base coordinates
    lon = -180.0
    lat = -90.0

    # 1) Field (AA–RR)
    lon += (ord(loc[0]) - ord('A')) * 20.0
    lat += (ord(loc[1]) - ord('A')) * 10.0

    # 2) Square (00–99)
    if len(loc) >= 4:
        lon += int(loc[2]) * 2.0
        lat += int(loc[3]) * 1.0

    # 3) Subsquare (AA–XX)
    if len(loc) >= 6:
        lon += (ord(loc[4]) - ord('A')) * (2.0 / 24.0)
        lat += (ord(loc[5]) - ord('A')) * (1.0 / 24.0)

    # Now shift to center of the smallest cell we used
    if len(loc) == 2:
        lon += 10.0      # half of 20°
        lat += 5.0       # half of 10°
    elif len(loc) == 4:
        lon += 1.0       # half of 2°
        lat += 0.5       # half of 1°
    elif len(loc) >= 6:
        lon += (2.0 / 24.0) / 2.0   # half of 2/24°
        lat += (1.0 / 24.0) / 2.0   # half of 1/24°

    return lat, lon


def great_circle_distance_km(grid_a: str, grid_b: str) -> float:
    """Return great-circle distance between two Maidenhead locators in km."""
    lat_a, lon_a = maidenhead_to_latlon(grid_a)
    lat_b, lon_b = maidenhead_to_latlon(grid_b)

    lat_a_rad = math.radians(lat_a)
    lat_b_rad = math.radians(lat_b)
    d_lat = math.radians(lat_b - lat_a)
    d_lon = math.radians(lon_b - lon_a)

    sin_dlat = math.sin(d_lat / 2)
    sin_dlon = math.sin(d_lon / 2)
    a = sin_dlat ** 2 + math.cos(lat_a_rad) * math.cos(lat_b_rad) * sin_dlon ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return PredictionEngine.EARTH_RADIUS * c


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)

    y = math.sin(dlambda) * math.cos(phi2)
    x = (
        math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    )

    bearing = math.degrees(math.atan2(y, x))
    return (bearing + 360.0) % 360.0


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


def dbm_to_watts(dbm: float) -> float:
    """Convert power from dBm to watts."""
    return 10 ** ((dbm - 30.0) / 10.0)


def ssb_threshold_to_voacap_required_snr_dbhz(
    threshold_db_2p5k: float, reference_bw_hz: float
) -> float:
    """Convert 2.5 kHz SNR threshold to VOACAP-required SNR in dB-Hz."""
    return threshold_db_2p5k + 10.0 * math.log10(max(1.0, reference_bw_hz))


def clamp_probability(p: float, eps: float = 1e-4) -> float:
    return max(eps, min(1.0 - eps, float(p)))


def shrink_with_voacap_prior(
    p_kernel: float,
    n_eff: float,
    p_voacap: float,
    k0: float = 20.0,
) -> float:
    p_kernel = clamp_probability(p_kernel)
    p_voacap = clamp_probability(p_voacap)
    n_eff = max(0.0, float(n_eff))
    return (p_kernel * n_eff + p_voacap * k0) / (n_eff + k0)


def shrink_snr_with_voacap_prior(
    snr_kernel: float | None,
    n_eff: float,
    snr_voacap: float | None,
    k0: float = 20.0,
) -> float | None:
    if snr_kernel is None and snr_voacap is None:
        return None
    if snr_kernel is None:
        return snr_voacap
    if snr_voacap is None:
        return snr_kernel

    n_eff = max(0.0, float(n_eff))
    w = n_eff / (n_eff + max(k0, 1e-6))
    return w * float(snr_kernel) + (1.0 - w) * float(snr_voacap)


SSB_REQUIRED_SNR_DBHZ = ssb_threshold_to_voacap_required_snr_dbhz(
    SSB_MIN_THRESHOLD_DB, PSKREPORTER_REFERENCE_BW_HZ
)


def categorize_snr(snr_db: float) -> str:
    for threshold, label in SNR_CATEGORIES:
        if snr_db >= threshold:
            return label
    return "Digital only"


def rankdata(values: list[float]) -> list[float]:
    n = len(values)
    if n == 0:
        return []
    indexed = sorted(enumerate(values), key=lambda iv: iv[1])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        _, val = indexed[i]
        while j < n and indexed[j][1] == val:
            j += 1
        avg_rank = (i + j - 1) / 2 + 1  # 1-based rank
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def build_kernel_reports(reports: list[dict], band: str) -> list[dict]:
    kernel_data = []
    for rpt in reports:
        try:
            tx_lat, tx_lon = maidenhead_to_latlon(rpt["tx_grid"])
            rx_lat, rx_lon = maidenhead_to_latlon(rpt["rx_grid"])
        except Exception:
            continue

        dist_km = great_circle_distance_km(rpt["tx_grid"], rpt["rx_grid"])
        az = calculate_bearing(tx_lat, tx_lon, rx_lat, rx_lon)
        kernel_data.append(
            {
                "band": band,
                "snr_db": rpt["snr_db"],
                "distance_km": dist_km,
                "azimuth_deg": az,
            }
        )
    return kernel_data


def predict_kernel_snr(
    tx_grid: str,
    rx_grid: str,
    kernel_reports: list[dict],
    distance_scale_km: float = D0_KM,
    azimuth_scale_deg: float = T0_DEG,
) -> float | None:
    if not kernel_reports:
        return None

    target_dist = great_circle_distance_km(tx_grid, rx_grid)
    tx_lat, tx_lon = maidenhead_to_latlon(tx_grid)
    rx_lat, rx_lon = maidenhead_to_latlon(rx_grid)
    target_az = calculate_bearing(tx_lat, tx_lon, rx_lat, rx_lon)

    weighted = 0.0
    total = 0.0
    for rpt in kernel_reports:
        delta_d = abs(rpt["distance_km"] - target_dist)
        w_d = math.exp(-((delta_d / max(1.0, distance_scale_km)) ** 2))

        delta_theta_raw = abs(rpt["azimuth_deg"] - target_az)
        delta_theta = min(delta_theta_raw, 360.0 - delta_theta_raw)
        w_t = math.exp(-((delta_theta / max(1.0, azimuth_scale_deg)) ** 2))

        w = w_d * w_t
        if w < MIN_KERNEL_WEIGHT:
            continue
        weighted += w * rpt["snr_db"]
        total += w

    if total == 0.0:
        return None
    return weighted / total


def build_endpoint_kernel_arrays(reports: list[dict]) -> dict[str, np.ndarray] | None:
    if not reports:
        return None
    return {
        "tx_lat": np.array([r["tx_lat"] for r in reports], dtype=float),
        "tx_lon": np.array([r["tx_lon"] for r in reports], dtype=float),
        "rx_lat": np.array([r["rx_lat"] for r in reports], dtype=float),
        "rx_lon": np.array([r["rx_lon"] for r in reports], dtype=float),
        "snr_db": np.array([r["snr_db"] for r in reports], dtype=float),
        "tx_pwr_dbm": np.array([r["tx_power_dbm"] for r in reports], dtype=float),
    }


def predict_endpoint_kernel(
    kernel_arrays: dict[str, np.ndarray] | None,
    my_lat: float,
    my_lon: float,
    target_lat: float,
    target_lon: float,
    user_power_dbm: float,
    r_tx_km: float = KERNEL_EA_TX_RADIUS_KM,
    r_rx_km: float = KERNEL_EA_RX_RADIUS_KM,
    min_weight: float = KERNEL_EA_MIN_WEIGHT,
    ssb_threshold_db: float = SSB_MIN_THRESHOLD_DB,
    top_k: int | None = KERNEL_EA_TOP_K,
    sharpen_gamma: float = KERNEL_EA_GAMMA,
) -> tuple[float | None, float | None, dict[str, float]]:
    """Endpoint-aware kernel using TX/RX proximity and explicit TX power."""
    if not kernel_arrays or kernel_arrays["tx_lat"].size == 0:
        return None, None, {"sum_w": 0.0, "n_eff": 0.0, "used_points": 0}

    tx_lat = kernel_arrays["tx_lat"]
    tx_lon = kernel_arrays["tx_lon"]
    rx_lat = kernel_arrays["rx_lat"]
    rx_lon = kernel_arrays["rx_lon"]
    snr_db = kernel_arrays["snr_db"]
    tx_pwr_dbm = kernel_arrays["tx_pwr_dbm"]

    channel_score = snr_db - tx_pwr_dbm

    d_tx = np.array(
        [haversine_km(lat, lon, my_lat, my_lon) for lat, lon in zip(tx_lat, tx_lon)],
        dtype=float,
    )
    d_rx = np.array(
        [haversine_km(lat, lon, target_lat, target_lon) for lat, lon in zip(rx_lat, rx_lon)],
        dtype=float,
    )

    w_tx = np.exp(-((d_tx / max(1.0, r_tx_km)) ** 2))
    w_rx = np.exp(-((d_rx / max(1.0, r_rx_km)) ** 2))
    weights = w_tx * w_rx

    if sharpen_gamma and sharpen_gamma != 1.0:
        weights = weights ** sharpen_gamma

    if top_k is not None and weights.size > top_k:
        idx = np.argpartition(weights, -top_k)[-top_k:]
        tx_lat = tx_lat[idx]
        tx_lon = tx_lon[idx]
        rx_lat = rx_lat[idx]
        rx_lon = rx_lon[idx]
        snr_db = snr_db[idx]
        tx_pwr_dbm = tx_pwr_dbm[idx]
        channel_score = channel_score[idx]
        d_tx = d_tx[idx]
        d_rx = d_rx[idx]
        weights = weights[idx]

    mask = (weights >= min_weight) & np.isfinite(channel_score)
    if not np.any(mask):
        return None, None, {"sum_w": 0.0, "n_eff": 0.0, "used_points": 0}

    w = weights[mask]
    c_use = channel_score[mask]
    sum_w = float(np.sum(w))
    sum_w2 = float(np.sum(w * w))
    n_eff = (sum_w * sum_w / sum_w2) if sum_w2 > 0 else 0.0

    c_hat = float(np.sum(w * c_use) / sum_w)
    snr_hat = c_hat + user_power_dbm
    p_ssb = float(np.sum(w * (c_use + user_power_dbm >= ssb_threshold_db)) / sum_w)
    support = {"sum_w": sum_w, "n_eff": n_eff, "used_points": int(mask.sum())}
    return snr_hat, p_ssb, support


# ---------------------------------------------------------------------------
# Stage 1: Simple VOACAP SNR wrapper
# ---------------------------------------------------------------------------

MAX_SHORT_PATH_KM = PredictionEngine.RAD_7000_KM * PredictionEngine.EARTH_RADIUS

def predict_median_snr_for_path(
    hunter_grid: str,
    target_grid: str,
    freq_mhz: float,
    ssn: float = 100.0,
    utc_datetime: datetime | None = None,
    reference_bw_hz: float = PSKREPORTER_REFERENCE_BW_HZ,
    tx_power_watts: float | None = None,
    rx_noise_db: float | None = None,
    min_takeoff_deg: float | None = None,
    return_path_probability: bool = False,
    probability_thresholds: dict[str, float] | None = None,
) -> float | tuple[float, float] | tuple[float, dict[str, float]]:
    """
    Minimal Stage 1 function: return median SNR at RX for a single path.

    Inputs:
        hunter_grid : your QTH Maidenhead (TX)
        target_grid : remote station grid (RX)
        freq_mhz    : frequency in MHz
        ssn         : sunspot number to use
        utc_datetime: datetime in UTC (if None, use now)

    Returns:
        median SNR at RX in dB (float)
    """
    if utc_datetime is None:
        utc_datetime = datetime.now(timezone.utc)
    else:
        # Ensure it's UTC-aware
        if utc_datetime.tzinfo is None:
            utc_datetime = utc_datetime.replace(tzinfo=timezone.utc)
        else:
            utc_datetime = utc_datetime.astimezone(timezone.utc)

    # Convert to VOACAP utc_time fraction of the day
    seconds = (
        utc_datetime.hour * 3600
        + utc_datetime.minute * 60
        + utc_datetime.second
        + utc_datetime.microsecond / 1_000_000.0
    )
    utc_fraction = seconds / 86400.0

    # Convert grids to lat/lon
    hunter_lat, hunter_lon = maidenhead_to_latlon(hunter_grid)
    target_lat, target_lon = maidenhead_to_latlon(target_grid)

    tx_point = GeoPoint.from_degrees(hunter_lat, hunter_lon)
    rx_point = GeoPoint.from_degrees(target_lat, target_lon)

    # Create a fresh engine for this call (simpler; you can optimize later)
    engine = PredictionEngine()

    # Basic parameter setup
    params = engine.params
    params.ssn = float(ssn)
    params.month = utc_datetime.month
    params.tx_power = float(tx_power_watts) if tx_power_watts else EFFECTIVE_TX_POWER_WATTS
    params.tx_location = tx_point
    params.min_angle = (
        np.deg2rad(min_takeoff_deg)
        if min_takeoff_deg is not None
        else np.deg2rad(MIN_TAKEOFF_DEG)
    )
    # Residential man-made noise at 3 MHz (same as example)
    params.man_made_noise_at_3mhz = float(rx_noise_db) if rx_noise_db else RESIDENTIAL_NOISE_DB
    # Required SNR is not critical for just getting median SNR
    params.required_snr = REQUIRED_SNR_DBHZ
    params.required_reliability = REQUIRED_RELIABILITY

    # Run prediction for a single frequency
    engine.predict(
        rx_location=rx_point,
        utc_time=utc_fraction,
        frequencies=[float(freq_mhz)],
    )

    if not engine.predictions:
        raise RuntimeError("VOACAP returned no predictions for this path")

    pred = engine.predictions[0]
    snr_db = float(pred.signal.snr_db)

    if engine.path.dist >= PredictionEngine.RAD_7000_KM:
        best_mode = getattr(engine, "_best_mode", None)
        if best_mode and getattr(best_mode, "signal", None):
            snr_db = float(best_mode.signal.snr_db)

    path_prob = float(pred.service_prob)

    probability_payload: dict[str, float] | None = None
    if probability_thresholds:
        probability_payload = {}
        for label, threshold in probability_thresholds.items():
            params.required_snr = float(threshold)
            probability_payload[label] = float(engine._calc_service_prob())

    # VOACAP produces SNR referenced to 1 Hz noise bandwidth. Convert to the
    # 2.5 kHz reference used by WSJT-X/WSPR spots so predictions and observations
    # are directly comparable.
    snr_db -= 10.0 * math.log10(max(1.0, reference_bw_hz))

    if return_path_probability:
        if probability_payload is not None:
            return snr_db, probability_payload
        return snr_db, path_prob

    if probability_payload is not None:
        return snr_db, probability_payload
    return snr_db


# ---------------------------------------------------------------------------
# WSPR rocks loading helpers
# ---------------------------------------------------------------------------

BAND_FRANGES_HZ = {
    "160m": (1_800_000, 2_000_000),
    "80m":  (3_500_000, 4_000_000),
    "60m":  (5_330_500, 5_403_500),
    "40m":  (7_000_000, 7_300_000),
    "30m":  (10_100_000, 10_150_000),
    "20m":  (14_000_000, 14_350_000),
    "17m":  (18_068_000, 18_168_000),
    "15m":  (21_000_000, 21_450_000),
    "12m":  (24_890_000, 24_990_000),
    "10m":  (28_000_000, 29_700_000),
    "6m":   (50_000_000, 54_000_000),
}


def load_wspr_rocks_reports(
    log_path: Path,
    band: str,
    lookback_minutes: int,
    max_reports: int = MAX_WSPR_REPORTS,
) -> list[dict]:
    """Parse WSPR.Rocks text export containing TX/RX endpoints and TX power."""
    if band not in BAND_FRANGES_HZ:
        raise ValueError(f"Unsupported band: {band}")
    if not log_path.exists():
        raise FileNotFoundError(f"WSPR log not found: {log_path}")

    low_hz, high_hz = BAND_FRANGES_HZ[band]
    reports: list[dict] = []

    with log_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or not line[0].isdigit():
                continue

            match = WSPR_LINE_RE.match(line)
            if not match:
                continue

            freq_mhz = float(match.group("freq"))
            freq_hz = freq_mhz * 1_000_000
            if not (low_hz <= freq_hz <= high_hz):
                continue

            tx_grid = match.group("tx_grid").upper()
            rx_grid = match.group("rx_grid").upper()
            if not (is_valid_maidenhead(tx_grid) and is_valid_maidenhead(rx_grid)):
                continue

            timestamp = datetime.fromisoformat(match.group("timestamp").replace("Z", "+00:00"))
            tx_lat, tx_lon = maidenhead_to_latlon(tx_grid)
            rx_lat, rx_lon = maidenhead_to_latlon(rx_grid)
            dist_km = great_circle_distance_km(tx_grid, rx_grid)
            azimuth = calculate_bearing(tx_lat, tx_lon, rx_lat, rx_lon)

            reports.append(
                {
                    "timestamp": timestamp,
                    "tx_call": match.group("tx_call").upper(),
                    "tx_grid": tx_grid,
                    "tx_lat": tx_lat,
                    "tx_lon": tx_lon,
                    "rx_call": match.group("rx_call").upper(),
                    "rx_grid": rx_grid,
                    "rx_lat": rx_lat,
                    "rx_lon": rx_lon,
                    "freq_mhz": freq_mhz,
                    "snr_db": float(match.group("snr")),
                    "distance_km": dist_km,
                    "azimuth_deg": azimuth,
                    "tx_power_dbm": float(match.group("tx_pwr")),
                    "synthetic": False,
                }
            )

    if not reports:
        return []

    reports.sort(key=lambda r: r["timestamp"], reverse=True)
    if lookback_minutes > 0:
        newest = reports[0]["timestamp"]
        cutoff = newest - timedelta(minutes=lookback_minutes)
        reports = [r for r in reports if r["timestamp"] >= cutoff]

    return reports[:max_reports]


def split_train_test_reports(
    reports: list[dict],
    train_fraction: float = TRAIN_FRACTION,
) -> tuple[list[dict], list[dict]]:
    """Split reports chronologically into train/test subsets."""
    if not reports:
        return [], []
    n_train = max(1, int(len(reports) * train_fraction))
    if n_train >= len(reports):
        n_train = max(1, len(reports) - 1)
    train = reports[:n_train]
    test = reports[n_train:]
    if not test:
        test = train[-1:]
        train = train[:-1]
    return train, test


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class VoacapTestApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("VOACAP vs WSPR Endpoint Analysis")
        self.geometry("900x600")

        # ---- Inputs ----
        self.band_var = tk.StringVar(value="20m")
        self.mode_var = tk.StringVar(value="WSPR")
        self.ssn_var = tk.DoubleVar(value=100.0)

        input_frame = ttk.Frame(self)
        input_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        ttk.Label(input_frame, text="Band:").grid(row=0, column=0, sticky="e")
        band_combo = ttk.Combobox(
            input_frame,
            textvariable=self.band_var,
            values=sorted(BAND_FRANGES_HZ.keys()),
            width=6,
            state="readonly",
        )
        band_combo.grid(row=0, column=1, sticky="w", padx=5)

        ttk.Label(input_frame, text="Mode:").grid(row=0, column=2, sticky="e")
        mode_combo = ttk.Combobox(
            input_frame,
            textvariable=self.mode_var,
            values=["WSPR", "FT8", "FT4", "RTTY", "PSK31"],
            width=6,
            state="readonly",
        )
        mode_combo.grid(row=0, column=3, sticky="w", padx=5)

        ttk.Label(input_frame, text="SSN:").grid(row=0, column=4, sticky="e")
        ttk.Entry(input_frame, textvariable=self.ssn_var, width=6).grid(row=0, column=5, sticky="w", padx=5)

        run_button = ttk.Button(input_frame, text="Run Test", command=self.run_test)
        run_button.grid(row=0, column=6, sticky="w", padx=10)

        # Progress bar below the controls
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(
            self,
            variable=self.progress_var,
            maximum=1.0,
            mode="determinate",
        )
        self.progress_bar.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))

        # ---- Results table ----
        columns = [
            "tx_call",
            "tx_grid",
            "rx_call",
            "rx_grid",
            "actual_snr",
            "actual_cat",
            "pred_raw",
            "pred_cal",
        ]
        columns.extend(
            [
                "kernel_raw",
                "kernel_cal",
                "kernel_ea_snr",
                "kernel_ea_snr_a",
                "kernel_ea_prob",
                "kernel_ea_prob_a",
                "kernel_ea_n_eff",
            ]
        )
        widths = [
            70,
            70,
            70,
            70,
            70,
            90,
            110,
            110,
        ]
        widths.extend([130, 100, 130, 130, 120, 120, 90])
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=15)
        self._tree_sort_reverse: dict[str, bool] = {}
        for col, width in zip(columns, widths):
            heading = col
            if col == "kernel_ea_snr":
                heading = "Kernel EA SNR"
            elif col == "kernel_ea_snr_a":
                heading = "Kernel EA SNR A"
            elif col == "kernel_ea_prob":
                heading = "Kernel EA Prob"
            elif col == "kernel_ea_prob_a":
                heading = "Kernel EA Prob A"
            elif col == "kernel_ea_n_eff":
                heading = "Kernel EA n_eff"
            self.tree.heading(
                col,
                text=heading,
                command=lambda c=col: self._sort_treeview_column(c),
            )
            self.tree.column(col, width=width, anchor="center")

        self.tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=5)

        # ---- Stats ----
        self.stats_var = tk.StringVar(value="Stats: (no run yet)")
        ttk.Label(self, textvariable=self.stats_var, justify="left").pack(
            side=tk.TOP, fill=tk.X, padx=10, pady=(5, 2)
        )

        self.stats_tables_container = ttk.Frame(self)
        self.stats_tables_container.pack(side=tk.TOP, fill=tk.X, padx=10, pady=5)

        self.overall_stats_table = ttk.Treeview(
            self.stats_tables_container,
            columns=["metric", "voacap_raw", "voacap_cal", "kernel_raw", "kernel_cal", "kernel_ea", "kernel_ea_snr_a"],
            show="headings",
            height=5,
        )
        for col, heading, width in [
            ("metric", "Metric", 140),
            ("voacap_raw", "VOACAP Raw", 110),
            ("voacap_cal", "VOACAP Cal", 110),
            ("kernel_raw", "Kernel Raw", 110),
            ("kernel_cal", "Kernel Cal", 110),
            ("kernel_ea", "Kernel EA", 110),
            ("kernel_ea_snr_a", "Kernel EA SNR A", 130),
        ]:
            self.overall_stats_table.heading(col, text=heading)
            self.overall_stats_table.column(col, width=width, anchor="center")
        self.overall_stats_table.pack(side=tk.TOP, expand=True, fill=tk.BOTH, padx=5, pady=(0,5))

        self.kernel_ea_prob_var = tk.StringVar(value="Kernel EA Prob: --")
        self.unreachable_stats_var = tk.StringVar(value="VOACAP Unreachable vs SSB heard: --")
        self.kernel_ea_unreach_var = tk.StringVar(value="Kernel EA Unreachable vs SSB heard: --")
        self.kernel_ea_prob_hi_var = tk.StringVar(
            value="Kernel EA Prob>50% but not SSB heard: --\nKernel EA Prob A>50% but not SSB heard: --"
        )
        self.kernel_ea_prob_lo_var = tk.StringVar(
            value="Kernel EA Prob<25% but not SSB heard: --\nKernel EA Prob A<25% but not SSB heard: --"
        )

        info_frame = ttk.Frame(self.stats_tables_container)
        info_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=(0,5))

        for var in [
            self.kernel_ea_prob_var,
            self.unreachable_stats_var,
            self.kernel_ea_unreach_var,
            self.kernel_ea_prob_hi_var,
            self.kernel_ea_prob_lo_var,
        ]:
            ttk.Label(
                info_frame,
                textvariable=var,
                anchor="w",
                justify="left",
            ).pack(side=tk.TOP, fill=tk.X, pady=1)

    def _sort_treeview_column(self, column: str) -> None:
        reverse = self._tree_sort_reverse.get(column, False)
        items = list(self.tree.get_children(""))
        keyed: list[tuple[tuple[int, object], str]] = []
        for iid in items:
            value = self.tree.set(iid, column)
            keyed.append((self._extract_tree_value_key(value), iid))
        keyed.sort(reverse=reverse)
        for index, (_, iid) in enumerate(keyed):
            self.tree.move(iid, "", index)
        self._tree_sort_reverse[column] = not reverse

    @staticmethod
    def _extract_tree_value_key(value: object) -> tuple[int, object]:
        if value is None:
            return (1, "")
        if isinstance(value, (int, float)):
            return (0, float(value))
        text = str(value).strip()
        if not text or text == "--":
            return (1, "")
        parts = text.split()
        token = parts[-1] if parts else text
        token = token.rstrip("%")
        try:
            number = float(token)
            return (0, number)
        except ValueError:
            return (1, text.lower())

    def run_test(self):
        band = self.band_var.get().strip()
        mode = self.mode_var.get().strip() or "FT8"
        try:
            ssn = float(self.ssn_var.get())
        except ValueError:
            messagebox.showerror("Input error", "SSN must be a number")
            return

        if band not in BAND_FRANGES_HZ:
            messagebox.showerror("Input error", f"Band '{band}' is not supported")
            return

        # Clear previous results
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.stats_var.set("Stats: running...")
        self._reset_progress()

        try:
            reports = load_wspr_rocks_reports(
                log_path=WSPR_ROCKS_FILE,
                band=band,
                lookback_minutes=WSPR_LOOKBACK_MINUTES,
                max_reports=MAX_WSPR_REPORTS,
            )
        except Exception as e:
            messagebox.showerror("Log error", f"Error loading WSPR data:\n{e}")
            self.stats_var.set("Stats: error loading WSPR data")
            self._reset_progress()
            return

        if not reports:
            messagebox.showwarning("No data", "No WSPR spots found in the selected file.")
            self.stats_var.set("Stats: no reports found")
            self._reset_progress()
            return

        short_path_reports = [
            rpt
            for rpt in reports
            if great_circle_distance_km(rpt["tx_grid"], rpt["rx_grid"]) <= MAX_SHORT_PATH_KM
        ]

        if not short_path_reports:
            messagebox.showinfo(
                "Long-path only",
                "All available WSPR spots exceed the current VOACAP short-path limit (~7000 km)."
                " Try a different band/mode or wait for nearer activity.",
            )
            self.stats_var.set("Stats: filtered out long-path spots")
            self._reset_progress()
            return

        train_reports, test_reports = split_train_test_reports(short_path_reports)
        if not train_reports or not test_reports:
            messagebox.showwarning("No data", "Unable to create train/test split from WSPR dataset.")
            self.stats_var.set("Stats: no reports selected")
            self._reset_progress()
            return

        if BASE_TX_FILTER_GRID:
            base_lat, base_lon = maidenhead_to_latlon(BASE_TX_FILTER_GRID)
            train_reports = [
                rpt
                for rpt in train_reports
                if haversine_km(rpt["tx_lat"], rpt["tx_lon"], base_lat, base_lon)
                <= BASE_TX_FILTER_RADIUS_KM
            ]
            if not train_reports:
                messagebox.showwarning(
                    "No training data",
                    "TX filter removed all training spots; adjust BASE_TX_FILTER settings.",
                )
                self.stats_var.set("Stats: no training spots after TX filter")
                self._reset_progress()
                return

        kernel_reports = build_kernel_reports(train_reports, band)
        kernel_ea_arrays = build_endpoint_kernel_arrays(train_reports)

        selected_reports = test_reports

        results = []

        total_reports = len(selected_reports)
        self.progress_bar["maximum"] = max(1, total_reports)
        self.progress_var.set(0.0)

        for idx, rpt in enumerate(selected_reports, start=1):
            tx_call = rpt["tx_call"]
            tx_grid = rpt["tx_grid"]
            rx_call = rpt["rx_call"]
            rx_grid = rpt["rx_grid"]
            freq_mhz = rpt["freq_mhz"]
            report_time = rpt.get("timestamp") or datetime.now(timezone.utc)
            heard_flag = 1
            actual_snr = rpt["snr_db"]
            tx_power_dbm = rpt.get("tx_power_dbm", 0.0)
            tx_power_watts = dbm_to_watts(tx_power_dbm)

            dist_km = great_circle_distance_km(tx_grid, rx_grid)
            tx_lat, tx_lon = maidenhead_to_latlon(tx_grid)
            rx_lat, rx_lon = maidenhead_to_latlon(rx_grid)
            azimuth = calculate_bearing(tx_lat, tx_lon, rx_lat, rx_lon)

            prediction = self._run_single_prediction(
                tx_grid=tx_grid,
                rx_grid=rx_grid,
                freq_mhz=freq_mhz,
                ssn=ssn,
                reference_bw_hz=PSKREPORTER_REFERENCE_BW_HZ,
                report_time=report_time,
                tx_power_watts=tx_power_watts,
            )

            if not prediction:
                self.progress_var.set(idx)
                self.progress_bar.update_idletasks()
                continue
            probabilities = prediction.get("probabilities") or {}
            p_voacap = probabilities.get("ssb")
            if p_voacap is None:
                p_voacap = probabilities.get("path", 0.05)
            try:
                p_voacap = float(p_voacap)
            except (TypeError, ValueError):
                p_voacap = 0.05

            kernel_snr = None
            if heard_flag:
                kernel_snr = predict_kernel_snr(
                    tx_grid=tx_grid,
                    rx_grid=rx_grid,
                    kernel_reports=kernel_reports,
                )

            pred_mean = prediction.get("mean")
            pred_cat_raw = categorize_snr(pred_mean) if pred_mean is not None else "No data"

            kernel_ea_snr = None
            kernel_ea_snr_a = None
            kernel_ea_prob = None
            kernel_ea_cat = None
            kernel_ea_cat_a = None
            kernel_ea_support = None
            kernel_ea_prob_a = None
            kernel_ea_n_eff = None
            if kernel_ea_arrays:
                kernel_ea_snr, kernel_ea_prob, kernel_ea_support = predict_endpoint_kernel(
                    kernel_arrays=kernel_ea_arrays,
                    my_lat=tx_lat,
                    my_lon=tx_lon,
                    target_lat=rx_lat,
                    target_lon=rx_lon,
                    user_power_dbm=rpt["tx_power_dbm"],
                    r_tx_km=KERNEL_EA_TX_RADIUS_KM,
                    r_rx_km=KERNEL_EA_RX_RADIUS_KM,
                    min_weight=KERNEL_EA_MIN_WEIGHT,
                    top_k=KERNEL_EA_TOP_K,
                    sharpen_gamma=KERNEL_EA_GAMMA,
                )
                if kernel_ea_snr is not None:
                    kernel_ea_cat = categorize_snr(kernel_ea_snr)
                kernel_ea_n_eff = (kernel_ea_support or {}).get("n_eff")

            n_eff = float(kernel_ea_n_eff or 0.0)
            snr_voacap_prior = pred_mean

            if kernel_ea_prob is not None:
                try:
                    kernel_ea_prob = float(kernel_ea_prob)
                except (TypeError, ValueError):
                    kernel_ea_prob = None
            if kernel_ea_prob is not None and not math.isfinite(kernel_ea_prob):
                kernel_ea_prob = None

            if kernel_ea_prob is None or n_eff < KERNEL_EA_MIN_N_EFF:
                kernel_ea_prob = p_voacap
                kernel_ea_prob_a = p_voacap
            else:
                k0_prob = 20.0
                p_prior = p_voacap
                if pred_cat_raw == "Unreachable":
                    k0_prob = 80.0
                    p_prior = min(p_prior, 0.05)
                kernel_ea_prob_a = shrink_with_voacap_prior(
                    p_kernel=kernel_ea_prob,
                    n_eff=n_eff,
                    p_voacap=p_prior,
                    k0=k0_prob,
                )

            k0_snr = 20.0
            if pred_cat_raw == "Unreachable":
                k0_snr = 80.0

            if n_eff < KERNEL_EA_MIN_N_EFF and snr_voacap_prior is not None:
                kernel_ea_snr_a = snr_voacap_prior
            else:
                kernel_ea_snr_a = shrink_snr_with_voacap_prior(
                    snr_kernel=kernel_ea_snr,
                    n_eff=n_eff,
                    snr_voacap=snr_voacap_prior,
                    k0=k0_snr,
                )
            if kernel_ea_snr_a is not None:
                kernel_ea_cat_a = categorize_snr(kernel_ea_snr_a)

            sample_index = len(results)
            results.append(
                {
                    "sample_index": sample_index,
                    "tx_call": tx_call,
                    "tx_grid": tx_grid,
                    "rx_call": rx_call,
                    "rx_grid": rx_grid,
                    "pred_raw": prediction["mean"],
                    "pred_snr": prediction["mean"],
                    "pred_cat_raw": pred_cat_raw,
                    "pred_min": prediction["min"],
                    "pred_max": prediction["max"],
                    "actual_snr": actual_snr,
                    "actual_cat": categorize_snr(actual_snr) if heard_flag else "Not heard",
                    "kernel_snr": kernel_snr,
                    "kernel_cat": categorize_snr(kernel_snr) if kernel_snr is not None else ("No data" if heard_flag else "Not heard"),
                    "kernel_ea_snr": kernel_ea_snr,
                    "kernel_ea_cat": kernel_ea_cat if kernel_ea_snr is not None else "No data",
                    "kernel_ea_snr_a": kernel_ea_snr_a,
                    "kernel_ea_cat_a": kernel_ea_cat_a if kernel_ea_snr_a is not None else "No data",
                    "kernel_ea_prob": kernel_ea_prob,
                    "kernel_ea_prob_a": kernel_ea_prob_a,
                    "kernel_ea_n_eff": kernel_ea_n_eff,
                    "pred_cat": None,
                    "distance_km": dist_km,
                    "azimuth_deg": azimuth,
                    "heard_flag": heard_flag,
                    "snr_measured": actual_snr if heard_flag else float("nan"),
                    "synthetic": bool(rpt.get("synthetic")),
                    "tx_power_dbm": rpt["tx_power_dbm"],
                }
            )

            self.progress_var.set(idx)
            self.progress_bar.update_idletasks()

        if not results:
            self._reset_progress()
            self.stats_var.set("Stats: no successful VOACAP predictions for selected reports")
            return

        heard_results = [r for r in results if r["heard_flag"]]

        kernel_stats_raw = self._compute_kernel_stats(heard_results, calibrated=False)
        slope, intercept = self._calibrate_predictions(results)
        kernel_slope, kernel_intercept = self._calibrate_kernel_predictions(heard_results)
        kernel_stats_cal = self._compute_kernel_stats(heard_results, calibrated=True)
        kernel_ea_stats = self._compute_kernel_ea_stats(heard_results)
        kernel_ea_stats_a = self._compute_kernel_ea_stats(heard_results, value_key="kernel_ea_snr_a")
        kernel_ea_prob_stats_raw = self._compute_kernel_ea_probability_stats(
            heard_results, prob_key="kernel_ea_prob"
        )
        kernel_ea_prob_stats_adj = self._compute_kernel_ea_probability_stats(
            heard_results, prob_key="kernel_ea_prob_a"
        )

        # Populate table (sorted by Kernel EA probability descending)
        sorted_results = sorted(
            results,
            key=lambda r: (r.get("kernel_ea_prob") if r.get("kernel_ea_prob") is not None else -1.0),
            reverse=True,
        )
        unreachable_miss = 0
        unreachable_total = 0
        kernel_ea_unreach_miss = 0
        kernel_ea_unreach_total = 0
        kernel_ea_prob_hi_miss = 0
        kernel_ea_prob_hi_total = 0
        kernel_ea_prob_lo_hit = 0
        kernel_ea_prob_lo_total = 0
        kernel_ea_prob_a_hi_miss = 0
        kernel_ea_prob_a_hi_total = 0
        kernel_ea_prob_a_lo_hit = 0
        kernel_ea_prob_a_lo_total = 0
        for r in sorted_results:
            pred_cat_raw = r.get("pred_cat_raw")
            actual_cat = r.get("actual_cat")
            if pred_cat_raw == "Unreachable":
                unreachable_total += 1
                if actual_cat in ("SSB High", "SSB Medium", "SSB Low"):
                    unreachable_miss += 1
            kernel_ea_cat = r.get("kernel_ea_cat")
            kernel_ea_prob = r.get("kernel_ea_prob")
            kernel_ea_prob_a = r.get("kernel_ea_prob_a")
            if kernel_ea_cat == "Unreachable":
                kernel_ea_unreach_total += 1
                if actual_cat in ("SSB High", "SSB Medium", "SSB Low"):
                    kernel_ea_unreach_miss += 1
            if kernel_ea_prob is not None:
                if kernel_ea_prob > 0.5:
                    kernel_ea_prob_hi_total += 1
                    if actual_cat not in ("SSB High", "SSB Medium", "SSB Low"):
                        kernel_ea_prob_hi_miss += 1
                if kernel_ea_prob < 0.25:
                    kernel_ea_prob_lo_total += 1
                    if actual_cat not in ("SSB High", "SSB Medium", "SSB Low"):
                        kernel_ea_prob_lo_hit += 1
            if kernel_ea_prob_a is not None:
                if kernel_ea_prob_a > 0.5:
                    kernel_ea_prob_a_hi_total += 1
                    if actual_cat not in ("SSB High", "SSB Medium", "SSB Low"):
                        kernel_ea_prob_a_hi_miss += 1
                if kernel_ea_prob_a < 0.25:
                    kernel_ea_prob_a_lo_total += 1
                    if actual_cat not in ("SSB High", "SSB Medium", "SSB Low"):
                        kernel_ea_prob_a_lo_hit += 1
            self.tree.insert(
                "",
                tk.END,
                values=(
                    r["tx_call"],
                    r["tx_grid"],
                    r["rx_call"],
                    r["rx_grid"],
                    self._format_actual_cell(r["actual_snr"], r["heard_flag"]),
                    r.get("actual_cat", "?"),
                    self._format_prediction_cell(r.get("pred_raw"), r.get("pred_cat_raw")),
                    self._format_prediction_cell(r.get("pred_snr"), r.get("pred_cat")),
                    self._format_prediction_cell(r.get("kernel_snr"), r.get("kernel_cat")),
                    self._format_prediction_cell(r.get("kernel_cal"), r.get("kernel_cat_cal")),
                    self._format_prediction_cell(r.get("kernel_ea_snr"), r.get("kernel_ea_cat")),
                    self._format_prediction_cell(r.get("kernel_ea_snr_a"), r.get("kernel_ea_cat_a")),
                    self._format_probability_cell(r.get("kernel_ea_prob")),
                    self._format_probability_cell(r.get("kernel_ea_prob_a")),
                    f"{r.get('kernel_ea_n_eff'):.1f}" if r.get("kernel_ea_n_eff") is not None else "--",
                ),
            )

        # Compute stats
        n = len(results)
        voacap_raw_stats = self._compute_voacap_stats(heard_results, calibrated=False)
        voacap_cal_stats = self._compute_voacap_stats(heard_results, calibrated=True)
        voacap_text_raw = self._format_voacap_stats(voacap_raw_stats, "VOACAP raw")
        voacap_text_cal = self._format_voacap_stats(
            voacap_cal_stats,
            f"VOACAP calibrated (pred'={slope:.2f}*pred+{intercept:.2f})",
        )

        kernel_text_raw = self._format_kernel_stats(kernel_stats_raw, label="Kernel raw")
        kernel_text_cal = self._format_kernel_stats(
            kernel_stats_cal,
            label="Kernel calibrated" + (f" (pred'={kernel_slope:.2f}*pred+{kernel_intercept:.2f})"),
        )

        self._update_stats_tables(
            voacap_raw_stats,
            voacap_cal_stats,
            kernel_stats_raw,
            kernel_stats_cal,
            kernel_ea_stats,
            kernel_ea_stats_a,
            kernel_ea_prob_stats_raw,
            kernel_ea_prob_stats_adj,
        )
        status = (
            f"Stats: computed for {n} test reports "
            f"(train={len(train_reports)}, test={len(test_reports)})"
        )
        self.stats_var.set(status)
        self.progress_var.set(total_reports)
        self.progress_bar.update_idletasks()
        if unreachable_total > 0:
            pct = 100.0 * unreachable_miss / unreachable_total
            self.unreachable_stats_var.set(
                f"VOACAP Unreachable but SSB heard: {unreachable_miss}/{unreachable_total} ({pct:.1f}%)"
            )
        else:
            self.unreachable_stats_var.set("VOACAP Unreachable but SSB heard: n/a")
        if kernel_ea_unreach_total > 0:
            pct = 100.0 * kernel_ea_unreach_miss / kernel_ea_unreach_total
            self.kernel_ea_unreach_var.set(
                f"Kernel EA Unreachable but SSB heard: {kernel_ea_unreach_miss}/{kernel_ea_unreach_total} ({pct:.1f}%)"
            )
        else:
            self.kernel_ea_unreach_var.set("Kernel EA Unreachable but SSB heard: n/a")
        def _format_ratio(label: str, hits: int, total: int) -> str:
            if total > 0:
                pct = 100.0 * hits / total
                return f"{label}: {hits}/{total} ({pct:.1f}%)"
            return f"{label}: n/a"

        hi_raw = _format_ratio(
            "Kernel EA Prob>50% but not SSB heard",
            kernel_ea_prob_hi_miss,
            kernel_ea_prob_hi_total,
        )
        hi_adj = _format_ratio(
            "Kernel EA Prob A>50% but not SSB heard",
            kernel_ea_prob_a_hi_miss,
            kernel_ea_prob_a_hi_total,
        )
        self.kernel_ea_prob_hi_var.set(f"{hi_raw}\n{hi_adj}")

        lo_raw = _format_ratio(
            "Kernel EA Prob<25% but not SSB heard",
            kernel_ea_prob_lo_hit,
            kernel_ea_prob_lo_total,
        )
        lo_adj = _format_ratio(
            "Kernel EA Prob A<25% but not SSB heard",
            kernel_ea_prob_a_lo_hit,
            kernel_ea_prob_a_lo_total,
        )
        self.kernel_ea_prob_lo_var.set(f"{lo_raw}\n{lo_adj}")

    def _run_single_prediction(
        self,
        tx_grid: str,
        rx_grid: str,
        freq_mhz: float,
        ssn: float,
        reference_bw_hz: float,
        report_time: datetime,
        tx_power_watts: float | None = None,
    ) -> dict[str, float | None] | None:
        try:
            snr_payload = predict_median_snr_for_path(
                hunter_grid=tx_grid,
                target_grid=rx_grid,
                freq_mhz=freq_mhz,
                ssn=ssn,
                utc_datetime=report_time,
                reference_bw_hz=reference_bw_hz,
                tx_power_watts=tx_power_watts or EFFECTIVE_TX_POWER_WATTS,
                rx_noise_db=RESIDENTIAL_NOISE_DB,
                min_takeoff_deg=MIN_TAKEOFF_DEG,
                return_path_probability=True,
                probability_thresholds={"ssb": SSB_REQUIRED_SNR_DBHZ},
            )
        except Exception as exc:
            print(f"Prediction failed for {tx_grid}->{rx_grid}: {exc}")
            return None

        if isinstance(snr_payload, tuple) and len(snr_payload) == 2:
            snr, probabilities = snr_payload
        else:
            snr = float(snr_payload) if snr_payload is not None else None  # type: ignore[arg-type]
            probabilities = {}

        if not isinstance(probabilities, dict):
            try:
                probabilities = {"path": float(probabilities)}
            except Exception:
                probabilities = {}

        return {
            "mean": snr,
            "std": 0.0,
            "min": snr,
            "max": snr,
            "probabilities": probabilities,
        }

    @staticmethod
    def _format_actual_cell(value: float | None, heard_flag: int) -> str:
        try:
            if not heard_flag or value is None or math.isnan(value):
                return "--"
        except TypeError:
            return "--"
        return f"{value:.1f}"

    @staticmethod
    def _calibrate_predictions(results: list[dict[str, float]]) -> tuple[float, float]:
        valid = [
            r for r in results if r["heard_flag"] and r["actual_snr"] is not None and not math.isnan(r["actual_snr"])
        ]
        if len(valid) < 2:
            for r in results:
                if r["heard_flag"] and r["actual_snr"] is not None and not math.isnan(r["actual_snr"]):
                    rel = r["pred_snr"] - r["actual_snr"]
                    r["error_db"] = rel
                    r["abs_diff"] = abs(rel)
                    denom = max(1.0, abs(r["actual_snr"]))
                    r["rel_pct"] = (rel / denom) * 100.0
                else:
                    r["error_db"] = None
                    r["abs_diff"] = None
                    r["rel_pct"] = None
                r["pred_raw"] = r["pred_snr"]
                r["pred_cat"] = categorize_snr(r["pred_snr"])
            return 1.0, 0.0

        preds = [r["pred_snr"] for r in valid]
        actuals = [r["actual_snr"] for r in valid]
        mean_pred = statistics.mean(preds)
        mean_act = statistics.mean(actuals)
        var_pred = sum((p - mean_pred) ** 2 for p in preds)
        if var_pred <= 1e-6:
            slope = 1.0
        else:
            cov = sum((p - mean_pred) * (a - mean_act) for p, a in zip(preds, actuals))
            slope = cov / var_pred
        intercept = mean_act - slope * mean_pred

        for r in results:
            calibrated = slope * r["pred_snr"] + intercept
            r["pred_raw"] = r["pred_snr"]
            r["pred_snr"] = calibrated
            r["pred_min"] = slope * r.get("pred_min", calibrated) + intercept
            r["pred_max"] = slope * r.get("pred_max", calibrated) + intercept
            if r["heard_flag"] and r["actual_snr"] is not None and not math.isnan(r["actual_snr"]):
                rel = calibrated - r["actual_snr"]
                r["error_db"] = rel
                r["abs_diff"] = abs(rel)
                denom = max(1.0, abs(r["actual_snr"]))
                r["rel_pct"] = (rel / denom) * 100.0
            else:
                r["error_db"] = None
                r["abs_diff"] = None
                r["rel_pct"] = None
            r["pred_cat"] = categorize_snr(calibrated)

        return slope, intercept

    @staticmethod
    def _calibrate_kernel_predictions(results: list[dict[str, float]]) -> tuple[float, float]:
        kernel_entries = [
            r
            for r in results
            if r.get("kernel_snr") is not None
            and r["actual_snr"] is not None
            and not math.isnan(r["actual_snr"])
        ]
        if len(kernel_entries) < 2:
            for r in kernel_entries:
                r["kernel_cal"] = r["kernel_snr"]
                r["kernel_cat_cal"] = r.get("kernel_cat")
            return 1.0, 0.0

        preds = [r["kernel_snr"] for r in kernel_entries]
        actuals = [r["actual_snr"] for r in kernel_entries]
        mean_pred = statistics.mean(preds)
        mean_act = statistics.mean(actuals)
        var_pred = sum((p - mean_pred) ** 2 for p in preds)
        if var_pred <= 1e-6:
            slope = 1.0
        else:
            cov = sum((p - mean_pred) * (a - mean_act) for p, a in zip(preds, actuals))
            slope = cov / var_pred
        intercept = mean_act - slope * mean_pred

        for r in kernel_entries:
            calibrated = slope * r["kernel_snr"] + intercept
            r["kernel_cal"] = calibrated
            r["kernel_cat_cal"] = categorize_snr(calibrated)

        for r in results:
            if r.get("kernel_snr") is None and r.get("kernel_cal") is None:
                r["kernel_cal"] = None
                r["kernel_cat_cal"] = "No data"

        return slope, intercept

    @staticmethod
    def _compute_kernel_stats(results: list[dict[str, float]], calibrated: bool) -> dict | None:
        key = "kernel_cal" if calibrated else "kernel_snr"
        cat_key = "kernel_cat_cal" if calibrated else "kernel_cat"
        errors = []
        preds = []
        actuals = []
        cat_hits = 0

        for r in results:
            val = r.get(key)
            if val is None or r["actual_snr"] is None or math.isnan(r["actual_snr"]):
                continue
            err = val - r["actual_snr"]
            errors.append(err)
            preds.append(val)
            actuals.append(r["actual_snr"])
            if r.get(cat_key) == r.get("actual_cat"):
                cat_hits += 1

        if not errors:
            return None

        mean_err = sum(errors) / len(errors)
        mae = sum(abs(e) for e in errors) / len(errors)
        rmse = math.sqrt(sum(e * e for e in errors) / len(errors))
        cat_acc = 100.0 * cat_hits / len(errors)

        if len(preds) > 1:
            mean_pred = statistics.mean(preds)
            mean_act = statistics.mean(actuals)
            var_pred = sum((p - mean_pred) ** 2 for p in preds)
            var_act = sum((a - mean_act) ** 2 for a in actuals)
            if var_pred > 1e-6 and var_act > 1e-6:
                cov = sum((p - mean_pred) * (a - mean_act) for p, a in zip(preds, actuals))
                corr = cov / math.sqrt(var_pred * var_act)
            else:
                corr = float('nan')

            # Spearman rank correlation
            ranks_pred = rankdata(preds)
            ranks_act = rankdata(actuals)
            mean_rank_pred = statistics.mean(ranks_pred)
            mean_rank_act = statistics.mean(ranks_act)
            var_rank_pred = sum((rp - mean_rank_pred) ** 2 for rp in ranks_pred)
            var_rank_act = sum((ra - mean_rank_act) ** 2 for ra in ranks_act)
            if var_rank_pred > 0 and var_rank_act > 0:
                cov_rank = sum(
                    (rp - mean_rank_pred) * (ra - mean_rank_act)
                    for rp, ra in zip(ranks_pred, ranks_act)
                )
                spearman = cov_rank / math.sqrt(var_rank_pred * var_rank_act)
            else:
                spearman = float('nan')

            # Top-quantile accuracy (top 25%)
            quantile_count = max(1, len(preds) // 4)
            top_pred_indices = sorted(range(len(preds)), key=lambda i: preds[i], reverse=True)[:quantile_count]
            top_act_indices = set(
                sorted(range(len(actuals)), key=lambda i: actuals[i], reverse=True)[:quantile_count]
            )
            top_hits = sum(1 for i in top_pred_indices if i in top_act_indices)
            top_quantile_acc = 100.0 * top_hits / quantile_count
        else:
            corr = spearman = float('nan')
            top_quantile_acc = float('nan')

        return {
            "count": len(errors),
            "mean_err": mean_err,
            "mae": mae,
            "rmse": rmse,
            "cat_acc": cat_acc,
            "corr": corr,
            "spearman": spearman,
            "top_quantile_acc": top_quantile_acc,
        }

    @staticmethod
    def _compute_kernel_ea_stats(
        results: list[dict[str, float]],
        value_key: str = "kernel_ea_snr",
    ) -> dict | None:
        entries = [
            r
            for r in results
            if r.get(value_key) is not None
            and r["actual_snr"] is not None
            and not math.isnan(r["actual_snr"])
        ]
        if not entries:
            return None

        preds = [r[value_key] for r in entries]
        actuals = [r["actual_snr"] for r in entries]
        errors = [p - a for p, a in zip(preds, actuals)]
        mean_err = sum(errors) / len(errors)
        mae = sum(abs(e) for e in errors) / len(errors)
        rmse = math.sqrt(sum(e * e for e in errors) / len(errors))
        cat_hits = sum(
            1 for r, pred in zip(entries, preds) if categorize_snr(pred) == r["actual_cat"]
        )
        cat_acc = 100.0 * cat_hits / len(entries)

        if len(preds) > 1:
            mean_pred = statistics.mean(preds)
            mean_act = statistics.mean(actuals)
            var_pred = sum((p - mean_pred) ** 2 for p in preds)
            var_act = sum((a - mean_act) ** 2 for a in actuals)
            if var_pred > 1e-6 and var_act > 1e-6:
                cov = sum((p - mean_pred) * (a - mean_act) for p, a in zip(preds, actuals))
                corr = cov / math.sqrt(var_pred * var_act)
            else:
                corr = float("nan")
        else:
            corr = float("nan")

        return {
            "mean_err": mean_err,
            "mae": mae,
            "rmse": rmse,
            "cat_acc": cat_acc,
            "corr": corr,
            "spearman": float("nan"),
            "top_quantile_acc": float("nan"),
        }

    @staticmethod
    def _compute_kernel_ea_probability_stats(
        results: list[dict[str, float]],
        prob_key: str,
    ) -> dict | None:
        y_true: list[float] = []
        y_pred: list[float] = []
        for r in results:
            actual = r["actual_snr"]
            prob = r.get(prob_key)
            if actual is None or math.isnan(actual) or prob is None:
                continue
            y_true.append(1.0 if actual >= SSB_MIN_THRESHOLD_DB else 0.0)
            y_pred.append(prob)
        if not y_true:
            return None
        errors = [(p - y) ** 2 for p, y in zip(y_pred, y_true)]
        brier = float(sum(errors) / len(errors))
        accuracy = float(
            sum((p >= 0.5) == (y == 1.0) for p, y in zip(y_pred, y_true)) / len(y_true)
            * 100.0
        )
        return {"brier": brier, "accuracy": accuracy}

    @staticmethod
    def _format_kernel_stats(stats: dict | None, label: str) -> str:
        if not stats:
            return f"{label}: no data"
        corr = stats.get("corr")
        spearman = stats.get("spearman")
        top_q = stats.get("top_quantile_acc")
        corr_text = f"{corr:.2f}" if corr is not None and not math.isnan(corr) else "n/a"
        spear_text = (
            f"{spearman:.2f}" if spearman is not None and not math.isnan(spearman) else "n/a"
        )
        topq_text = (
            f"{top_q:.1f}%" if top_q is not None and not math.isnan(top_q) else "n/a"
        )
        return (
            f"{label}\n"
            f"  Mean error: {stats['mean_err']:.2f} dB   MAE: {stats['mae']:.2f} dB   RMSE: {stats['rmse']:.2f} dB\n"
            f"  Category accuracy: {stats['cat_acc']:.1f}%   Corr: {corr_text}   Spearman: {spear_text}   Top-Quartile hit: {topq_text}"
        )

    def _update_stats_tables(
        self,
        voacap_raw_stats: dict,
        voacap_cal_stats: dict,
        kernel_stats_raw: dict | None,
        kernel_stats_cal: dict | None,
        kernel_ea_stats: dict | None,
        kernel_ea_snr_a_stats: dict | None,
        kernel_ea_prob_stats_raw: dict | None,
        kernel_ea_prob_stats_adj: dict | None,
    ) -> None:
        for item in self.overall_stats_table.get_children():
            self.overall_stats_table.delete(item)

        def fmt(stat: dict | None, key: str, suffix: str = "", precision: int = 2) -> str:
            if not stat or key not in stat or stat[key] is None or math.isnan(stat[key]):
                return "--"
            return f"{stat[key]:.{precision}f}{suffix}"

        overall_rows = [
            (
                "Mean error",
                fmt(voacap_raw_stats, "mean_err"),
                fmt(voacap_cal_stats, "mean_err"),
                fmt(kernel_stats_raw, "mean_err"),
                fmt(kernel_stats_cal, "mean_err"),
                fmt(kernel_ea_stats, "mean_err"),
                fmt(kernel_ea_snr_a_stats, "mean_err"),
            ),
            (
                "MAE",
                fmt(voacap_raw_stats, "mae"),
                fmt(voacap_cal_stats, "mae"),
                fmt(kernel_stats_raw, "mae"),
                fmt(kernel_stats_cal, "mae"),
                fmt(kernel_ea_stats, "mae"),
                fmt(kernel_ea_snr_a_stats, "mae"),
            ),
            (
                "RMSE",
                fmt(voacap_raw_stats, "rmse"),
                fmt(voacap_cal_stats, "rmse"),
                fmt(kernel_stats_raw, "rmse"),
                fmt(kernel_stats_cal, "rmse"),
                fmt(kernel_ea_stats, "rmse"),
                fmt(kernel_ea_snr_a_stats, "rmse"),
            ),
            (
                "Cat accuracy (%)",
                fmt(voacap_raw_stats, "cat_acc"),
                fmt(voacap_cal_stats, "cat_acc"),
                fmt(kernel_stats_raw, "cat_acc"),
                fmt(kernel_stats_cal, "cat_acc"),
                fmt(kernel_ea_stats, "cat_acc"),
                fmt(kernel_ea_snr_a_stats, "cat_acc"),
            ),
        ]
        for row in overall_rows:
            self.overall_stats_table.insert("", tk.END, values=row)

        prob_lines: list[str] = []
        if kernel_ea_prob_stats_raw:
            prob_lines.append(
                f"Kernel EA Prob: Brier={kernel_ea_prob_stats_raw['brier']:.3f}, "
                f"Accuracy={kernel_ea_prob_stats_raw['accuracy']:.1f}%"
            )
        else:
            prob_lines.append("Kernel EA Prob: --")

        if kernel_ea_prob_stats_adj:
            prob_lines.append(
                f"Kernel EA Prob A: Brier={kernel_ea_prob_stats_adj['brier']:.3f}, "
                f"Accuracy={kernel_ea_prob_stats_adj['accuracy']:.1f}%"
            )
        else:
            prob_lines.append("Kernel EA Prob A: --")

        self.kernel_ea_prob_var.set("\n".join(prob_lines))

    @staticmethod
    def _compute_voacap_stats(results: list[dict[str, float]], calibrated: bool) -> dict:
        valid = [
            r
            for r in results
            if r["actual_snr"] is not None and not math.isnan(r["actual_snr"])
        ]
        if not valid:
            return {
                "mean_err": 0.0,
                "mae": 0.0,
                "rmse": 0.0,
                "max_abs": 0.0,
                "cat_acc": 0.0,
                "corr": float("nan"),
                "spearman": float("nan"),
                "top_quantile_acc": float("nan"),
            }
        preds = [r["pred_snr"] if calibrated else r["pred_raw"] for r in valid]
        actuals = [r["actual_snr"] for r in valid]
        errors = [p - a for p, a in zip(preds, actuals)]
        mae = sum(abs(e) for e in errors) / len(errors)
        rmse = math.sqrt(sum(e * e for e in errors) / len(errors))
        max_abs = max(abs(e) for e in errors)
        cat_acc = 100.0 * sum(
            1
            for pred, r in zip(preds, valid)
            if categorize_snr(pred) == r["actual_cat"]
        ) / len(valid)
        if len(preds) > 1:
            mean_pred = statistics.mean(preds)
            mean_act = statistics.mean(actuals)
            var_pred = sum((p - mean_pred) ** 2 for p in preds)
            var_act = sum((a - mean_act) ** 2 for a in actuals)
            if var_pred > 1e-6 and var_act > 1e-6:
                cov = sum((p - mean_pred) * (a - mean_act) for p, a in zip(preds, actuals))
                corr = cov / math.sqrt(var_pred * var_act)
            else:
                corr = float('nan')

            ranks_pred = rankdata(preds)
            ranks_act = rankdata(actuals)
            mean_rank_pred = statistics.mean(ranks_pred)
            mean_rank_act = statistics.mean(ranks_act)
            var_rank_pred = sum((rp - mean_rank_pred) ** 2 for rp in ranks_pred)
            var_rank_act = sum((ra - mean_rank_act) ** 2 for ra in ranks_act)
            if var_rank_pred > 0 and var_rank_act > 0:
                cov_rank = sum(
                    (rp - mean_rank_pred) * (ra - mean_rank_act)
                    for rp, ra in zip(ranks_pred, ranks_act)
                )
                spearman = cov_rank / math.sqrt(var_rank_pred * var_rank_act)
            else:
                spearman = float('nan')

            quantile_count = max(1, len(preds) // 4)
            top_pred_indices = sorted(range(len(preds)), key=lambda i: preds[i], reverse=True)[:quantile_count]
            top_act_indices = set(
                sorted(range(len(actuals)), key=lambda i: actuals[i], reverse=True)[:quantile_count]
            )
            top_hits = sum(1 for i in top_pred_indices if i in top_act_indices)
            top_quantile_acc = 100.0 * top_hits / quantile_count
        else:
            corr = spearman = float('nan')
            top_quantile_acc = float('nan')

        return {
            "mean_err": sum(errors) / len(errors),
            "mae": mae,
            "rmse": rmse,
            "max_abs": max_abs,
            "cat_acc": cat_acc,
            "corr": corr,
            "spearman": spearman,
            "top_quantile_acc": top_quantile_acc,
        }

    @staticmethod
    def _format_voacap_stats(stats: dict, label: str) -> str:
        corr = stats.get("corr")
        spearman = stats.get("spearman")
        top_q = stats.get("top_quantile_acc")
        corr_text = f"{corr:.2f}" if corr is not None and not math.isnan(corr) else "n/a"
        spear_text = (
            f"{spearman:.2f}" if spearman is not None and not math.isnan(spearman) else "n/a"
        )
        topq_text = (
            f"{top_q:.1f}%" if top_q is not None and not math.isnan(top_q) else "n/a"
        )
        return (
            f"{label}\n"
            f"  Mean error: {stats['mean_err']:.2f} dB   MAE: {stats['mae']:.2f} dB   RMSE: {stats['rmse']:.2f} dB\n"
            f"  Max |error|: {stats['max_abs']:.2f} dB   Category accuracy: {stats['cat_acc']:.1f}%\n"
            f"  Corr: {corr_text}   Spearman: {spear_text}   Top-Quartile hit: {topq_text}"
        )

    @staticmethod
    def _format_prediction_cell(value: float | None, category: str | None) -> str:
        if value is None:
            return "--"
        try:
            if math.isnan(value):  # type: ignore[arg-type]
                return "--"
        except (TypeError, ValueError):
            pass
        label = category or "?"
        return f"[{label}] {value:.1f}"

    @staticmethod
    def _format_probability_cell(probability: float | None) -> str:
        if probability is None:
            return "--"
        try:
            if math.isnan(probability):  # type: ignore[arg-type]
                return "--"
        except (TypeError, ValueError):
            pass
        return f"{probability * 100.0:.1f}%"

    def _reset_progress(self) -> None:
        if hasattr(self, "progress_bar"):
            self.progress_bar["maximum"] = 1.0
            self.progress_var.set(0.0)
            self.progress_bar.update_idletasks()


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = VoacapTestApp()
    app.mainloop()
