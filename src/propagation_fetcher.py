"""
PSKReporter API client for fetching propagation data.
Documentation: https://pskreporter.info/pskdev.html
"""

import requests
import logging as L
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import time

logging = L.getLogger(__name__)


class PSKReporterClient:
    """
    Client for interacting with the PSKReporter API.
    PSKReporter collects reception reports from various digital modes including
    FT8, FT4, WSPR, and others.
    """
    
    BASE_URL = "https://retrieve.pskreporter.info/query"
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize PSKReporter client.
        
        Args:
            api_key: Optional API key for authenticated requests (not required for basic queries)
        """
        self.api_key = api_key
        self.last_request_time = 0
        self.min_request_interval = 10  # seconds between requests (rate limiting)
        
    def _rate_limit(self):
        """Enforce rate limiting between API requests."""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)
        self.last_request_time = time.time()
    
    def fetch_recent_reports(
        self,
        minutes: int = 15,
        mode: Optional[str] = None,
        band: Optional[str] = None
    ) -> List[Dict]:
        """
        Fetch recent reception reports from PSKReporter.
        
        Args:
            minutes: Number of minutes to look back (default: 15)
            mode: Optional mode filter (e.g., "FT8", "WSPR")
            band: Optional band filter (e.g., "20m")
            
        Returns:
            List of reception report dictionaries
        """
        self._rate_limit()
        
        params = {
            # PSKReporter expects a negative offset relative to "now"
            'flowStartSeconds': -int(minutes * 60),
            'noactive': '1',  # Exclude senders who are currently active
            'nolocator': '0',  # Include records with locators
            'rronly': '1',    # Reception reports only
        }
        
        if mode:
            params['mode'] = mode
        
        try:
            response = requests.get(self.BASE_URL, params=params, timeout=30)
            response.raise_for_status()
            
            # Parse XML response
            reports = self._parse_xml_response(response.text, band)
            
            logging.info(f"Fetched {len(reports)} reports from PSKReporter")
            return reports
            
        except requests.exceptions.RequestException as ex:
            logging.error(f"Error fetching PSKReporter data: {ex}")
            return []
    
    def fetch_reception_reports(
        self,
        rx_grid: str,
        band: Optional[str] = None,
        minutes: int = 15,
        mode: Optional[str] = None
    ) -> List[Dict]:
        """
        Fetch recent global reception reports for kernel smoothing.
        
        NOTE: This fetches broadly (not filtered by receiver grid) to enable
        kernel smoothing predictions based on path geometry.
        
        Args:
            rx_grid: User's grid square (not used for filtering, kept for API compatibility)
            band: Optional band filter
            minutes: Minutes to look back
            mode: Optional mode filter (leave None to include all)
            
        Returns:
            List of reception reports with path geometry
        """
        self._rate_limit()
        
        params = {
            # PSKReporter expects negative seconds relative to now
            'flowStartSeconds': -int(minutes * 60),
            'rptlimit': 2000,  # Limit to prevent overload
            'rronly': 1,
            'noactive': 1,
            # NOTE: NOT filtering by receiverLocator to get global data
        }
        if mode:
            params['mode'] = mode
        
        logging.info(f"[PROP FETCH] PSKReporter API query: GLOBAL (no grid filter), band={band}, mode={mode or 'any'}, minutes={minutes}")
        logging.info(f"[PROP FETCH] PSKReporter params: {params}")
        
        try:
            response = requests.get(self.BASE_URL, params=params, timeout=30)
            response.raise_for_status()
            
            logging.info(f"[PROP FETCH] PSKReporter response status: {response.status_code}, content length: {len(response.text)} bytes")
            
            reports = self._parse_xml_response(response.text, band)
            
            logging.info(f"[PROP FETCH] Fetched {len(reports)} global reception reports (band filter: {band or 'all bands'})")
            
            # Log sample of parsed reports
            if reports:
                sample = reports[0]
                logging.info(f"[PROP FETCH] Sample report: {sample}")
            
            return reports
            
        except requests.exceptions.RequestException as ex:
            logging.error(f"[PROP FETCH] Error fetching PSKReporter reception data: {ex}")
            return []


    def query_path_reports(
        self,
        tx_grid: str,
        rx_grid: str,
        band: Optional[str] = None,
        hours: int = 2
    ) -> List[Dict]:
        """
        Query reports for a specific transmitter to receiver path.
        
        Args:
            tx_grid: Transmitter grid square (4 or 6 chars)
            rx_grid: Receiver grid square (4 or 6 chars)
            band: Optional band filter
            hours: Hours to look back
            
        Returns:
            List of matching reception reports
        """
        self._rate_limit()
        
        flow_start = datetime.utcnow() - timedelta(hours=hours)
        
        # Truncate to 4-char grids for broader matching
        tx_prefix = tx_grid[:4].upper()
        rx_prefix = rx_grid[:4].upper()
        
        params = {
            'flowStartSeconds': int(flow_start.timestamp()),
            'senderLocator': tx_prefix,
            'receiverLocator': rx_prefix,
            'rronly': '1',
        }
        
        try:
            response = requests.get(self.BASE_URL, params=params, timeout=30)
            response.raise_for_status()
            
            reports = self._parse_xml_response(response.text, band)
            
            logging.debug(f"Found {len(reports)} reports for path {tx_grid} -> {rx_grid}")
            return reports
            
        except requests.exceptions.RequestException as ex:
            logging.error(f"Error querying PSKReporter path: {ex}")
            return []
    
    def _parse_xml_response(self, xml_text: str, band_filter: Optional[str] = None) -> List[Dict]:
        """
        Parse XML response from PSKReporter API.
        
        Args:
            xml_text: Raw XML response text
            band_filter: Optional band to filter results
            
        Returns:
            List of parsed report dictionaries
        """
        import xml.etree.ElementTree as ET
        from propagation_utils import get_band_from_frequency, grid_to_latlon, calculate_distance
        
        reports = []
        
        try:
            root = ET.fromstring(xml_text)
            
            # Find all reception reports
            for rr in root.findall('.//receptionReport'):
                try:
                    # Extract data from XML
                    tx_call = rr.get('senderCallsign', '')
                    tx_grid = rr.get('senderLocator', '')
                    rx_call = rr.get('receiverCallsign', '')
                    rx_grid = rr.get('receiverLocator', '')
                    freq_str = rr.get('frequency', '0')
                    mode = rr.get('mode', '')
                    snr_str = rr.get('sNR', '0')
                    time_str = rr.get('flowStartSeconds', '')
                    
                    # Convert frequency from Hz to kHz
                    frequency = float(freq_str) / 1000.0 if freq_str else 0.0
                    band = get_band_from_frequency(frequency)
                    
                    # Apply band filter if specified
                    if band_filter and band != band_filter:
                        continue
                    
                    # Parse SNR
                    snr = float(snr_str) if snr_str else 0.0
                    
                    # Parse timestamp
                    timestamp = datetime.utcfromtimestamp(int(time_str)) if time_str else datetime.utcnow()
                    
                    # Calculate lat/lon and distance
                    tx_lat, tx_lon = (0.0, 0.0)
                    rx_lat, rx_lon = (0.0, 0.0)
                    distance_km = 0.0
                    
                    try:
                        if tx_grid and rx_grid:
                            tx_lat, tx_lon = grid_to_latlon(tx_grid)
                            rx_lat, rx_lon = grid_to_latlon(rx_grid)
                            distance_km = calculate_distance(tx_grid, rx_grid)
                    except Exception:
                        pass
                    
                    report = {
                        'timestamp': timestamp,
                        'tx_call': tx_call,
                        'tx_grid': tx_grid,
                        'tx_lat': tx_lat,
                        'tx_lon': tx_lon,
                        'rx_call': rx_call,
                        'rx_grid': rx_grid,
                        'rx_lat': rx_lat,
                        'rx_lon': rx_lon,
                        'frequency': frequency,
                        'band': band,
                        'mode': mode,
                        'snr': snr,
                        'source': 'pskreporter',
                        'distance_km': distance_km,
                    }
                    
                    reports.append(report)
                    
                except (ValueError, AttributeError) as ex:
                    logging.debug(f"Error parsing reception report: {ex}")
                    continue
            
        except ET.ParseError as ex:
            logging.error(f"Error parsing PSKReporter XML: {ex}")
        
        return reports
    
    def test_connection(self) -> bool:
        """
        Test connection to PSKReporter API.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            reports = self.fetch_recent_reports(minutes=5)
            logging.info(f"PSKReporter connection test: OK ({len(reports)} reports)")
            return len(reports) >= 0  # Success even if no reports
        except Exception as ex:
            logging.error(f"PSKReporter connection test failed: {ex}")
            return False


class PropagationDataFetcher:
    """
    Main service for fetching and managing propagation data from various sources.
    """
    
    def __init__(self, data_source: str = 'pskreporter'):
        """
        Initialize propagation data fetcher.
        
        Args:
            data_source: Data source to use ('pskreporter', 'wspr', 'rbn')
        """
        self.data_source = data_source
        
        if data_source == 'pskreporter':
            self.client = PSKReporterClient()
        else:
            logging.warning(f"Data source '{data_source}' not yet implemented, using PSKReporter")
            self.client = PSKReporterClient()
    
    def fetch_propagation_data(self, minutes: int = 15) -> List[Dict]:
        """
        Fetch recent propagation data.
        
        Args:
            minutes: Minutes to look back
            
        Returns:
            List of propagation reports
        """
        return self.client.fetch_recent_reports(minutes=minutes)
    
    def fetch_reception_reports(
        self,
        rx_grid: str,
        band: Optional[str] = None,
        minutes: int = 15,
        mode: Optional[str] = None
    ) -> List[Dict]:
        """
        Fetch recent reception reports for global kernel smoothing.
        
        Args:
            rx_grid: User's grid square (kept for API compatibility)
            band: Optional band filter (e.g., "20m")
            minutes: Look-back window in minutes
            mode: Optional PSKReporter mode parameter (None = include all)
        
        Returns:
            List of reception reports with geometry metadata.
        """
        return self.client.fetch_reception_reports(rx_grid, band, minutes, mode)
    
    def query_path(
        self,
        tx_grid: str,
        rx_grid: str,
        band: Optional[str] = None,
        hours: int = 2
    ) -> List[Dict]:
        """
        Query propagation for a specific path.
        
        Args:
            tx_grid: Transmitter grid
            rx_grid: Receiver grid
            band: Optional band filter
            hours: Hours to look back
            
        Returns:
            List of reports for that path
        """
        return self.client.query_path_reports(tx_grid, rx_grid, band, hours)
    
    def test_connection(self) -> bool:
        """Test connection to data source."""
        return self.client.test_connection()
