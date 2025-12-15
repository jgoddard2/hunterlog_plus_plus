"""
WSPR Rocks (wspr.live) client for fetching propagation data.
"""

import logging as L
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from propagation_utils import calculate_bearing, calculate_distance, grid_to_latlon

logging = L.getLogger(__name__)


class WsprRocksClient:
    """
    Lightweight client for the public wspr.rocks/wspr.live ClickHouse backend.
    """

    BASE_URL = "https://db1.wspr.live"
    MAX_RECORDS = 5000
    BAND_CODES: Dict[str, int] = {
        "160m": 1,
        "80m": 3,
        "60m": 5,
        "40m": 7,
        "30m": 10,
        "20m": 14,
        "17m": 18,
        "15m": 21,
        "12m": 24,
        "10m": 28,
        "6m": 50,
        "4m": 70,
        "2m": 144,
        "70cm": 432,
        "23cm": 1296,
    }

    def __init__(self) -> None:
        self.last_request_time = 0.0
        self.min_request_interval = 5.0
        self._reverse_band_codes = {v: k for k, v in self.BAND_CODES.items()}

    def _rate_limit(self) -> None:
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_request_interval:
            time.sleep(self.min_request_interval - elapsed)
        self.last_request_time = time.time()

    def _band_code(self, band: Optional[str]) -> Optional[int]:
        if not band:
            return None
        return self.BAND_CODES.get(band.lower())

    def _band_label(self, code: Optional[int], fallback: Optional[str]) -> str:
        if code and code in self._reverse_band_codes:
            return self._reverse_band_codes[code]
        return fallback or "unknown"

    def _build_query(self, where_clause: str, limit: int) -> str:
        return (
            "SELECT time, tx_sign, tx_loc, rx_sign, rx_loc, band, frequency, snr, power, "
            "distance, azimuth "
            "FROM wspr.rx "
            f"WHERE {where_clause} "
            "ORDER BY time DESC "
            f"LIMIT {limit} "
            "FORMAT JSONCompact"
        )

    @staticmethod
    def _parse_timestamp(raw: str) -> datetime:
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return datetime.utcnow().replace(tzinfo=timezone.utc)

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _execute_query(self, query: str) -> List[List]:
        self._rate_limit()
        response = requests.get(self.BASE_URL, params={"query": query}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        return payload.get("data", [])

    def _parse_rows(self, rows: List[List], band_hint: Optional[str]) -> List[Dict]:
        reports: List[Dict] = []
        for row in rows:
            if len(row) < 11:
                continue

            (
                time_raw,
                tx_call,
                tx_grid,
                rx_call,
                rx_grid,
                band_code,
                frequency_hz,
                snr,
                power_dbm,
                distance_km,
                azimuth_deg,
            ) = row

            timestamp = self._parse_timestamp(time_raw)
            band_numeric = self._safe_float(band_code)
            band_label = self._band_label(
                int(band_numeric) if band_numeric is not None else None,
                band_hint,
            )
            frequency = self._safe_float(frequency_hz)
            frequency_khz = frequency / 1000.0 if frequency is not None else 0.0
            snr_value = self._safe_float(snr) or 0.0
            tx_grid = (tx_grid or "").strip().upper()
            rx_grid = (rx_grid or "").strip().upper()
            tx_lat = tx_lon = rx_lat = rx_lon = 0.0

            if tx_grid:
                try:
                    tx_lat, tx_lon = grid_to_latlon(tx_grid)
                except Exception:
                    tx_lat = tx_lon = 0.0
            if rx_grid:
                try:
                    rx_lat, rx_lon = grid_to_latlon(rx_grid)
                except Exception:
                    rx_lat = rx_lon = 0.0

            distance_value = self._safe_float(distance_km)
            if (distance_value is None or distance_value <= 0) and tx_grid and rx_grid:
                try:
                    distance_value = calculate_distance(tx_grid, rx_grid)
                except Exception:
                    distance_value = 0.0

            azimuth_value = self._safe_float(azimuth_deg)
            if (azimuth_value is None or azimuth_value <= 0) and tx_grid and rx_grid:
                try:
                    azimuth_value = calculate_bearing(tx_grid, rx_grid)
                except Exception:
                    azimuth_value = 0.0

            report = {
                "timestamp": timestamp,
                "tx_call": tx_call or "",
                "tx_grid": tx_grid,
                "tx_lat": tx_lat,
                "tx_lon": tx_lon,
                "rx_call": rx_call or "",
                "rx_grid": rx_grid,
                "rx_lat": rx_lat,
                "rx_lon": rx_lon,
                "frequency": frequency_khz,
                "band": band_label,
                "mode": "WSPR",
                "snr": snr_value,
                "source": "wsprrocks",
                "distance_km": distance_value or 0.0,
                "azimuth_deg": azimuth_value or 0.0,
                "tx_power_dbm": self._safe_float(power_dbm),
            }
            reports.append(report)

        return reports

    def fetch_recent_reports(
        self,
        minutes: int = 15,
        band: Optional[str] = None,
    ) -> List[Dict]:
        """
        Fetch recent WSPR reports for the requested band.
        """
        target_band = band or "20m"
        return self._fetch_band_reports(target_band, minutes)

    def fetch_reception_reports(
        self,
        rx_grid: str,
        band: Optional[str] = None,
        minutes: int = 15,
        mode: Optional[str] = None,  # compatibility parameter, ignored
    ) -> List[Dict]:
        """
        Fetch global WSPR reports for kernel smoothing.
        """
        target_band = band or "20m"
        return self._fetch_band_reports(target_band, minutes)

    def _fetch_band_reports(self, band: str, minutes: int) -> List[Dict]:
        band_code = self._band_code(band)
        if band_code is None:
            logging.error(f"[PROP FETCH] Unknown WSPR band: {band}")
            return []

        minutes = max(1, min(60, int(minutes)))
        where_clause = (
            f"band = {band_code} AND time > subtractMinutes(now(), {minutes})"
        )
        query = self._build_query(where_clause, self.MAX_RECORDS)

        try:
            rows = self._execute_query(query)
            reports = self._parse_rows(rows, band)
            logging.info(
                f"[PROP FETCH] Fetched {len(reports)} WSPR reports for band={band}, window={minutes}m"
            )
            if reports:
                logging.debug(f"[PROP FETCH] Sample WSPR report: {reports[0]}")
            return reports
        except requests.exceptions.RequestException as ex:
            logging.error(f"[PROP FETCH] Error querying wspr.live backend: {ex}")
        except Exception as ex:
            logging.error(f"[PROP FETCH] Error parsing WSPR data: {ex}")
        return []

    def query_path_reports(
        self,
        tx_grid: str,
        rx_grid: str,
        band: Optional[str] = None,
        hours: int = 2,
    ) -> List[Dict]:
        band_code = self._band_code(band) if band else None
        hours = max(1, int(hours))
        conditions = [f"time > subtractHours(now(), {hours})"]
        if band_code is not None:
            conditions.append(f"band = {band_code}")

        tx_prefix = (tx_grid or "")[:4].upper()
        rx_prefix = (rx_grid or "")[:4].upper()
        if tx_prefix:
            conditions.append(f"tx_loc LIKE '{tx_prefix}%'")
        if rx_prefix:
            conditions.append(f"rx_loc LIKE '{rx_prefix}%'")

        where_clause = " AND ".join(conditions)
        query = self._build_query(where_clause, self.MAX_RECORDS)

        try:
            rows = self._execute_query(query)
            return self._parse_rows(rows, band)
        except Exception as ex:
            logging.error(f"[PROP FETCH] Error querying WSPR path: {ex}")
            return []

    def test_connection(self) -> bool:
        try:
            reports = self.fetch_recent_reports(minutes=5, band="20m")
            logging.info(
                f"WSPR Rocks connection test: OK ({len(reports)} reports retrieved)"
            )
            return True
        except Exception as ex:
            logging.error(f"WSPR Rocks connection test failed: {ex}")
            return False


class PropagationDataFetcher:
    """
    Main service for fetching and managing propagation data from WSPR Rocks.
    """

    def __init__(self) -> None:
        # Always use the wspr.rocks ClickHouse backend
        self.client = WsprRocksClient()

    def fetch_propagation_data(self, minutes: int = 15, band: Optional[str] = None) -> List[Dict]:
        return self.client.fetch_recent_reports(minutes=minutes, band=band)

    def fetch_reception_reports(
        self,
        rx_grid: str,
        band: Optional[str] = None,
        minutes: int = 15,
        mode: Optional[str] = None,
    ) -> List[Dict]:
        return self.client.fetch_reception_reports(rx_grid, band, minutes, mode)

    def query_path(
        self,
        tx_grid: str,
        rx_grid: str,
        band: Optional[str] = None,
        hours: int = 2,
    ) -> List[Dict]:
        return self.client.query_path_reports(tx_grid, rx_grid, band, hours)

    def test_connection(self) -> bool:
        return self.client.test_connection()
