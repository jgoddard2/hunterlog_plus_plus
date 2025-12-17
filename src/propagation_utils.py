"""
Utility functions for propagation calculations.
Includes grid square conversions, distance calculations, and SNR status determination.
"""

import math
import logging as L

logging = L.getLogger(__name__)


def grid_to_latlon(grid: str) -> tuple[float, float]:
    """
    Convert Maidenhead grid square to latitude/longitude.
    
    Args:
        grid: 4 or 6 character Maidenhead grid square (e.g., "FN31pr")
        
    Returns:
        Tuple of (latitude, longitude) in degrees
    """
    grid = grid.upper().strip()
    
    if len(grid) < 4:
        raise ValueError(f"Grid square must be at least 4 characters: {grid}")
    
    # Field (first 2 characters)
    lon = (ord(grid[0]) - ord('A')) * 20 - 180
    lat = (ord(grid[1]) - ord('A')) * 10 - 90
    
    # Square (next 2 digits)
    lon += int(grid[2]) * 2
    lat += int(grid[3]) * 1
    
    # Subsquare (optional, next 2 characters)
    if len(grid) >= 6:
        lon += (ord(grid[4]) - ord('A')) * (2.0 / 24.0)
        lat += (ord(grid[5]) - ord('A')) * (1.0 / 24.0)
        # Center of subsquare
        lon += 1.0 / 24.0
        lat += 0.5 / 24.0
    else:
        # Center of square
        lon += 1.0
        lat += 0.5
    
    return (lat, lon)


def calculate_distance(grid1: str, grid2: str) -> float:
    """
    Calculate great-circle distance between two grid squares.
    
    Args:
        grid1: First grid square
        grid2: Second grid square
        
    Returns:
        Distance in kilometers
    """
    try:
        lat1, lon1 = grid_to_latlon(grid1)
        lat2, lon2 = grid_to_latlon(grid2)
        
        # Haversine formula
        R = 6371  # Earth radius in km
        
        lat1_rad = math.radians(lat1)
        lat2_rad = math.radians(lat2)
        delta_lat = math.radians(lat2 - lat1)
        delta_lon = math.radians(lon2 - lon1)
        
        a = (math.sin(delta_lat / 2) ** 2 +
             math.cos(lat1_rad) * math.cos(lat2_rad) *
             math.sin(delta_lon / 2) ** 2)
        
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        distance = R * c
        
        return distance
    except Exception as ex:
        logging.error(f"Error calculating distance between {grid1} and {grid2}: {ex}")
        return 0.0


def calculate_bearing(grid1: str, grid2: str) -> float:
    """
    Calculate great-circle bearing from grid1 to grid2.
    
    Args:
        grid1: Starting grid square
        grid2: Destination grid square
        
    Returns:
        Bearing in degrees (0-360), where 0 = North, 90 = East
    """
    try:
        lat1, lon1 = grid_to_latlon(grid1)
        lat2, lon2 = grid_to_latlon(grid2)
        
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dlambda = math.radians(lon2 - lon1)
        
        y = math.sin(dlambda) * math.cos(phi2)
        x = (math.cos(phi1) * math.sin(phi2) -
             math.sin(phi1) * math.cos(phi2) * math.cos(dlambda))
        
        brng = math.degrees(math.atan2(y, x))
        return (brng + 360.0) % 360.0
    except Exception as ex:
        logging.error(f"Error calculating bearing between {grid1} and {grid2}: {ex}")
        return 0.0


def determine_propagation_status(
    snr: float,
    ssb_threshold: float = 6.0,
    digital_threshold: float = -15.0
) -> str:
    """
    Determine propagation status based on SNR and thresholds.
    
    Args:
        snr: Signal-to-Noise Ratio in dB
        ssb_threshold: SNR threshold for SSB mode (default: 10dB)
        digital_threshold: SNR threshold for digital modes (default: -15dB)
        
    Returns:
        Status string: 'ssb', 'digital', 'not_reachable'
    """
    if snr >= ssb_threshold:
        return 'ssb'
    elif snr >= digital_threshold:
        return 'digital'
    else:
        return 'not_reachable'


def get_band_from_frequency(freq_khz: float) -> str:
    """
    Determine amateur radio band from frequency in kHz.
    
    Args:
        freq_khz: Frequency in kilohertz
        
    Returns:
        Band name (e.g., "20m", "40m") or "unknown"
    """
    freq_mhz = freq_khz / 1000.0
    
    # Amateur radio band ranges
    bands = [
        (1.8, 2.0, "160m"),
        (3.5, 4.0, "80m"),
        (7.0, 7.3, "40m"),
        (10.1, 10.15, "30m"),
        (14.0, 14.35, "20m"),
        (18.068, 18.168, "17m"),
        (21.0, 21.45, "15m"),
        (24.89, 24.99, "12m"),
        (28.0, 29.7, "10m"),
        (50.0, 54.0, "6m"),
    ]
    
    for low, high, band_name in bands:
        if low <= freq_mhz <= high:
            return band_name
    
    return "unknown"


def format_snr_display(snr: float, status: str) -> str:
    """
    Format SNR value for display based on status.
    
    Args:
        snr: SNR value in dB
        status: Propagation status ('ssb', 'digital', 'not_reachable', 'no_data')
        
    Returns:
        Formatted string for display
    """
    if status == 'no_data':
        return "No Reports"
    elif status == 'ssb':
        return f"SSB: {snr:+.0f}dB"
    elif status == 'digital':
        return f"Digital: {snr:+.0f}dB"
    elif status == 'not_reachable':
        return f"N/R: {snr:+.0f}dB"
    else:
        return "Unknown"


def get_status_color(status: str) -> str:
    """
    Get color code for propagation status.
    
    Args:
        status: Propagation status
        
    Returns:
        Color name for UI display
    """
    colors = {
        'ssb': 'green',
        'digital': 'orange',
        'not_reachable': 'red',
        'no_data': 'black',
    }
    return colors.get(status, 'black')
