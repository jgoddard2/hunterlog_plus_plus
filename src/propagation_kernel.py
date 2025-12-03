"""
Kernel smoothing algorithm for propagation SNR prediction.

This module implements a sophisticated propagation prediction system that uses
kernel smoothing to predict SNR values based on path geometry similarity rather
than direct callsign matching.

The algorithm:
1. For each spot, calculates the path geometry (distance, azimuth) from user to activator
2. Compares this path to all available PSKReporter reports
3. Uses a weighted average where weights depend on path similarity
4. Higher weight for reports with similar distance AND azimuth

Based on the approach described in the PSKReporter kernel smoothing example.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional
import math
import logging as L

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


# ==============================
# Data structures
# ==============================

@dataclass
class PropagationReport:
    """A single PSKReporter reception report with path geometry."""
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


@dataclass
class SpotPath:
    """Path geometry for a spot (user -> activator)."""
    spot_id: int
    activator_call: str
    activator_grid: str
    
    distance_km: float
    azimuth_deg: float
    band: str


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


# ==============================
# Kernel smoothing algorithm
# ==============================

def spot_weight(report: PropagationReport, 
                target_dist_km: float,
                target_az_deg: float) -> float:
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
    # Distance similarity
    delta_d = abs(report.distance_km - target_dist_km)
    w_d = math.exp(-(delta_d / D0_KM) ** 2)
    
    # Azimuth similarity (handle wrap-around at 0/360)
    delta_theta_raw = abs(report.azimuth_deg - target_az_deg)
    delta_theta = min(delta_theta_raw, 360.0 - delta_theta_raw)
    w_t = math.exp(-(delta_theta / T0_DEG) ** 2)
    
    # Combined weight
    return w_d * w_t


def predict_snr_for_spot(spot_path: SpotPath,
                         reports: List[PropagationReport]) -> Optional[float]:
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
        w = spot_weight(report, spot_path.distance_km, spot_path.azimuth_deg)
        
        # Skip reports with negligible weight
        if w < MIN_WEIGHT:
            continue
            
        weighted_sum += w * report.snr_db
        weight_total += w
    
    if weight_total == 0.0:
        return None
    
    return weighted_sum / weight_total


def batch_predict_snr(spot_paths: List[SpotPath],
                      reports_by_band: Dict[str, List[PropagationReport]]) -> Dict[int, float]:
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
        snr = predict_snr_for_spot(spot_path, reports)
        predictions[spot_path.spot_id] = snr
    
    logging.debug(f"[KERNEL] Predicted SNR for {len(spot_paths)} spots, "
                 f"{sum(1 for v in predictions.values() if v is not None)} have predictions")
    
    return predictions


# ==============================
# SNR classification
# ==============================

def classify_snr(snr: Optional[float],
                ssb_threshold: float = 10.0,
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
