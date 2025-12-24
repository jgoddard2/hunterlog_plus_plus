import json
import time
import webview
import logging as L
import datetime
from typing import Optional, List, Tuple
import threading
from datetime import timedelta

from propagation_fetcher import PropagationDataFetcher
from propagation_kernel import (
    DEFAULT_SSN,
    DEFAULT_KERNEL_PROFILE_ID,
    DEFAULT_TX_ANTENNA_PROFILE_ID,
    DEFAULT_RX_ANTENNA_PROFILE_ID,
    KERNEL_EA_MIN_N_EFF,
    KERNEL_EA_MIN_WEIGHT,
    KERNEL_EA_TOP_K,
    KERNEL_EA_GAMMA,
    KERNEL_EA_TX_RADIUS_KM,
    KERNEL_EA_RX_RADIUS_KM,
    MODE_THRESHOLDS_DB,
    PSKREPORTER_REFERENCE_BW_HZ,
    PropagationEstimate,
    PropagationReport,
    SpotPath,
    build_endpoint_kernel_arrays,
    batch_predict_kernel_ea,
    categorize_operation_mode,
    predict_endpoint_kernel,
    resolve_kernel_profile,
    resolve_voacap_antenna_profile,
    watts_to_dbm,
)
from solar_ssn import resolve_noaa_ssn
from propagation_utils import calculate_bearing, calculate_distance, determine_propagation_status, grid_to_latlon
from bands import get_band, get_name_of_band
from db.db import DataBase
from db.models.activators import Activator, ActivatorSchema
from db.models.alerts import AlertsSchema
from db.models.parks import ParkSchema
from db.models.qsos import QsoSchema
from db.models.spot_comments import SpotCommentSchema
from db.models.spots import Spot, SpotSchema
from loggers import LoggerInterface
from loggers.logger_interface import LoggerParams
from pota import PotaApi, PotaStats
from programs import Program, SotaProgram, WwffProgram, PotaProgram, NoProgram
from sota import SotaApi
from wwff import WwffApi
from utils.adif import AdifLog
from version import __version__

from cat import CAT

logging = L.getLogger(__name__)

PROPAGATION_CONFIG_KEYS = [
    'prop_enabled',
    'prop_refresh_minutes',
    'prop_default_ssn',
    'prop_history_minutes',
    'prop_ssb_threshold',
    'prop_digital_threshold',
    'prop_kernel_profile_id',
    'prop_tx_antenna_profile_id',
    'prop_rx_antenna_profile_id',
    'prop_distance_scale_km',
    'prop_azimuth_scale_deg',
    'default_pwr',
    'my_grid6',
]

PROPAGATION_MAP_GRID_STEP_DEG = 10.0
PROPAGATION_MAP_CACHE_LIMIT = 6


class JsApi:
    def __init__(self):
        self.lock = threading.Lock()
        self.db = DataBase()
        self._ensure_propagation_default_enabled()
        self.pota = PotaApi()
        self.sota = SotaApi()
        self.wwff = WwffApi()
        self.prop_fetcher = PropagationDataFetcher()
        self.programs: dict[str, Program] = {
            "POTA": PotaProgram(self.db),
            "SOTA": SotaProgram(self.db),
            "WWFF": WwffProgram(self.db),
            '': NoProgram(self.db)
        }
        self.seen_regions = [""]
        
        # Propagation tracking
        self.last_prop_update = None
        self.current_band_id = 0
        self.prop_model_cache: dict[str, tuple[datetime.datetime, int, list]] = {}
        self.propagation_snapshot: dict[str, dict[str, dict[str, object]]] = {}
        self.propagation_history: dict[str, list[dict[str, object]]] = {}
        self.propagation_map_cache: dict[str, list[tuple[datetime.datetime, dict[str, object]]]] = {}
        self.current_ssn_value: Optional[float] = None
        self.current_ssn_source: str = 'fallback'
        self.current_ssn_updated: Optional[datetime.datetime] = None
        self.ssn_override_enabled: bool = False
        self.ssn_override_value: Optional[float] = None
        self._prime_ssn_defaults()

        logging.debug("init CAT...")
        lp = LoggerParams(
            self.db.config.get_value('logger_type'),
            self.db.config.get_value('my_call'),
            self.db.config.get_value('my_grid6'),
            self.db.config.get_value('adif_host'),
            self.db.config.get_value('adif_port'),
        )
        self.adif_log = LoggerInterface.get_logger(lp, __version__)
        logging.debug(f"got logger {self.adif_log}")
        try:
            rig_if = self.db.config.get_value('rig_if_type')
            ip = self.db.config.get_value('flr_host')
            port = self.db.config.get_value('flr_port')
            self.cat = CAT.get_interface(rig_if)
            self.cat.init_cat(host=ip, port=port)
        except Exception:
            logging.error("Error creating CAT object: ", exc_info=True)
            self.cat = None
        self.pw = None

    def _ensure_propagation_default_enabled(self) -> None:
        """
        Ensure the propagation feature is seeded to enabled at least once.
        """
        try:
            seeded = self.db.config.get_value('prop_enabled_seeded')
        except Exception:
            logging.debug("[PROP CFG] Unable to read propagation default flag; skipping seeding")
            return

        if seeded:
            return

        try:
            self.db.config.set_value('prop_enabled', True)
            self.db.config.set_value('prop_enabled_seeded', True, commit=True)
            logging.info("[PROP CFG] Propagation feature enabled by default for this install")
        except Exception as exc:
            logging.error("[PROP CFG] Failed to seed propagation defaults", exc_info=exc)

    def _prime_ssn_defaults(self) -> None:
        """Seed the current SSN info with the configured fallback at startup."""
        fallback = self._get_configured_default_ssn()
        self.current_ssn_value = fallback
        self.current_ssn_source = 'fallback'
        self.current_ssn_updated = datetime.datetime.utcnow()

    def _capture_propagation_settings(self) -> dict[str, object]:
        """Snapshot current propagation-related config values."""
        snapshot: dict[str, object] = {}
        for key in PROPAGATION_CONFIG_KEYS:
            try:
                snapshot[key] = self.db.config.get_value(key)
            except Exception:
                snapshot[key] = None
        return snapshot

    def _clear_propagation_caches(self, force: bool = False) -> bool:
        """Reset cached propagation landscapes/history."""
        acquired = self.lock.acquire(timeout=4.0)
        if not acquired and force:
            logging.warning("[PROP CFG] Cache clear waiting for propagation worker to finish")
            self.lock.acquire()
            acquired = True
        if not acquired:
            logging.warning("[PROP CFG] Unable to acquire lock to clear propagation caches")
            return False
        try:
            self.prop_model_cache.clear()
            self.propagation_snapshot.clear()
            self.propagation_history.clear()
            self.propagation_map_cache.clear()
            logging.info("[PROP CFG] Cleared cached propagation datasets due to config change")
            return True
        finally:
            self.lock.release()

    def _reset_propagation_state(self) -> None:
        """Clear propagation caches and reset runtime state."""
        self._clear_propagation_caches(force=True)
        self.last_prop_update = None
        self.current_band_id = 0
        self._prime_ssn_defaults()
        logging.info("[PROP CFG] Propagation runtime state reset")

    def _handle_propagation_settings_change(self) -> None:
        """Clear caches and rerun predictions for the current band."""
        self._clear_propagation_caches()
        try:
            prop_enabled = bool(self.db.config.get_value('prop_enabled'))
        except Exception:
            prop_enabled = False

        if not prop_enabled:
            logging.info("[PROP CFG] Propagation settings changed while feature disabled; caches cleared only")
            return

        band_id = self.current_band_id or self.db.filters.band_filter
        if band_id and band_id != 0:
            logging.info("[PROP CFG] Propagation settings changed; refreshing predictions for band %s", band_id)
            self.trigger_propagation_fetch(band_id)
        else:
            logging.info("[PROP CFG] Propagation settings changed but no band is selected; waiting for next trigger")

    def _get_configured_default_ssn(self) -> float:
        """Read the user-configured fallback SSN, with validation."""
        try:
            raw_val = self.db.config.get_value('prop_default_ssn')
            value = float(raw_val) if raw_val is not None else DEFAULT_SSN
        except (TypeError, ValueError):
            value = DEFAULT_SSN
        if value <= 0:
            return DEFAULT_SSN
        return value

    def _refresh_current_ssn(self) -> float:
        """Fetch the observed NOAA SSN, falling back to the configured default."""
        fallback = self._get_configured_default_ssn()
        source = 'fallback'
        if self.ssn_override_enabled:
            value = self.ssn_override_value if self.ssn_override_value is not None else fallback
            source = 'override'
        else:
            try:
                value, observed = resolve_noaa_ssn(fallback=fallback, use_smoothed=False)
                source = 'observed' if observed else 'fallback'
            except Exception as exc:
                logging.debug("[SSN] NOAA fetch failed: %s", exc)
                value = fallback
                source = 'fallback'
        self.current_ssn_value = value
        self.current_ssn_source = source
        self.current_ssn_updated = datetime.datetime.utcnow()
        return value

    def _current_ssn_payload(self) -> dict[str, object]:
        """Serialize the current SSN status for the frontend."""
        payload: dict[str, object] = {
            'ssn': self.current_ssn_value,
            'ssn_source': self.current_ssn_source,
        }
        if self.current_ssn_updated:
            payload['ssn_updated'] = self.current_ssn_updated.isoformat() + 'Z'
        return payload

    def _get_propagation_reports_for_band(self, my_grid: str, band_name: str, fetch_minutes: int) -> list[PropagationReport]:
        cache_key = band_name.lower()
        now = datetime.datetime.utcnow()
        refresh_minutes = self.db.config.get_value('prop_refresh_minutes') or 3
        cached_entry = self.prop_model_cache.get(cache_key)
        if cached_entry:
            cached_time, cached_window, cached_reports = cached_entry
            age_minutes = (now - cached_time).total_seconds() / 60.0
            if age_minutes < refresh_minutes and cached_window >= fetch_minutes:
                logging.info("[PROP MAP] Using cached propagation dataset for %s", band_name)
                return cached_reports

        reports = self.prop_fetcher.fetch_reception_reports(
            rx_grid=my_grid,
            band=band_name,
            minutes=fetch_minutes
        )
        logging.info("[PROP MAP] Received %s reports for %s", len(reports), band_name)
        if not reports:
            self.prop_model_cache.pop(cache_key, None)
            return []

        prop_reports: list[PropagationReport] = []
        for r in reports:
            try:
                azimuth = r.get('azimuth_deg', 0)
                if azimuth == 0 and r.get('tx_grid') and r.get('rx_grid'):
                    azimuth = calculate_bearing(r.get('tx_grid'), r.get('rx_grid'))

                timestamp_obj = r.get('timestamp', datetime.datetime.utcnow())
                if isinstance(timestamp_obj, datetime.datetime):
                    timestamp_str = timestamp_obj.isoformat()
                else:
                    timestamp_str = str(timestamp_obj)

                prop_report = PropagationReport(
                    timestamp_utc=timestamp_str,
                    band=r.get('band', band_name),
                    snr_db=r.get('snr', 0.0),
                    tx_call=r.get('tx_call', ''),
                    tx_grid=r.get('tx_grid', ''),
                    tx_lat=r.get('tx_lat', 0.0),
                    tx_lon=r.get('tx_lon', 0.0),
                    rx_call=r.get('rx_call', ''),
                    rx_grid=r.get('rx_grid', ''),
                    rx_lat=r.get('rx_lat', 0.0),
                    rx_lon=r.get('rx_lon', 0.0),
                    distance_km=r.get('distance_km', 0.0),
                    azimuth_deg=azimuth,
                    tx_power_dbm=r.get('tx_power_dbm')
                )
                prop_reports.append(prop_report)
            except Exception as ex:
                logging.debug("[PROP MAP] Error converting report: %s", ex)
                continue

        self.prop_model_cache[cache_key] = (now, fetch_minutes, prop_reports)
        return prop_reports

    def _build_probability_grid(
        self,
        dataset: object,
        user_lat: float,
        user_lon: float,
        user_power_dbm: float,
        threshold_db: float,
        profile_settings: dict[str, float],
        grid_step_deg: float,
    ) -> list[dict[str, float]]:
        r_tx_km = float(profile_settings.get("r_tx_km", KERNEL_EA_TX_RADIUS_KM))
        r_rx_km = float(profile_settings.get("r_rx_km", KERNEL_EA_RX_RADIUS_KM))
        min_weight = float(profile_settings.get("min_weight", KERNEL_EA_MIN_WEIGHT))
        min_support = float(profile_settings.get("min_n_eff", KERNEL_EA_MIN_N_EFF))

        half_step = grid_step_deg / 2.0
        min_lat = -60.0
        max_lat = 80.0
        min_lon = -180.0
        max_lon = 180.0

        cells: list[dict[str, float]] = []
        lat = min_lat + half_step
        while lat <= max_lat - half_step + 1e-6:
            lon = min_lon + half_step
            while lon <= max_lon - half_step + 1e-6:
                snr_hat, prob, support = predict_endpoint_kernel(
                    dataset,
                    user_lat=user_lat,
                    user_lon=user_lon,
                    target_lat=lat,
                    target_lon=lon,
                    user_power_dbm=user_power_dbm,
                    r_tx_km=r_tx_km,
                    r_rx_km=r_rx_km,
                    min_weight=min_weight,
                    top_k=KERNEL_EA_TOP_K,
                    sharpen_gamma=KERNEL_EA_GAMMA,
                    threshold_db=threshold_db,
                )
                if prob is not None and float(support.get("n_eff", 0.0)) >= min_support:
                    cells.append({
                        "lat": float(lat),
                        "lon": float(lon),
                        "probability": float(prob),
                    })
                lon += grid_step_deg
            lat += grid_step_deg

        return cells

    def check_and_update_propagation(self):
        """
        Check if propagation data needs updating based on configured interval.
        Called periodically from update_ticker.
        """
        try:
            # Check if feature is enabled
            prop_enabled = self.db.config.get_value('prop_enabled')
            logging.info("prop_enabled: %s", prop_enabled)
            if not prop_enabled:
                logging.info("Propagation is disabled")
                return
            
            # Get refresh interval in minutes
            refresh_minutes = self.db.config.get_value('prop_refresh_minutes') or 3
            
            # Check if enough time has passed since last update
            now = datetime.datetime.now()
            if self.last_prop_update is not None:
                elapsed = (now - self.last_prop_update).total_seconds() / 60.0
                if elapsed < refresh_minutes:
                    # Not time yet
                    return
            
            # Get current band filter
            band_id = self.db.filters.band_filter
            if band_id == 0 or band_id is None:
                # No band selected, skip
                return
            
            logging.info(f"[PROP TIMER] Auto-refresh triggered (interval: {refresh_minutes} min)")
            
            # Trigger update
            self.last_prop_update = now
            self.trigger_propagation_fetch(band_id)
            
        except Exception as e:
            logging.error(f"Error in check_and_update_propagation: {e}", exc_info=True)
    
    def trigger_propagation_fetch(self, band_id: int):
        """
        Trigger a fetch of propagation data for the specified band.
        This is called when the user changes the band filter or periodically.
        """
        try:
            # Check if feature is enabled
            prop_enabled = self.db.config.get_value('prop_enabled')
            logging.info("prop_enabled: %s", prop_enabled)
            if not prop_enabled:
                return

            # Get user grid
            my_grid = self.db.config.get_value('my_grid6')
            if not my_grid or len(my_grid) < 4:
                logging.warning("Cannot fetch propagation: Invalid grid square")
                return

            # Convert band ID to string (e.g. 20 -> "20m")
            # Note: get_name_of_band returns "20m" format
            band_name = get_name_of_band(band_id)
            if not band_name:
                logging.warning(f"Cannot fetch propagation: Invalid band ID {band_id}")
                return

            logging.info(f"Triggering propagation fetch for {band_name} at {my_grid}")
            
            # Update tracking when manually triggered
            self.last_prop_update = datetime.datetime.now()
            self.current_band_id = band_id
            
            # Run in a separate thread to avoid blocking UI
            threading.Thread(
                target=self._fetch_and_update_propagation,
                args=(my_grid, band_name)
            ).start()
            
        except Exception as e:
            logging.error(f"Error triggering propagation fetch: {e}")

    def _fetch_and_update_propagation(self, my_grid: str, band_name: str):
        """Background worker to fetch and update propagation data using kernel smoothing."""
        try:
            logging.info(f"[PROP FETCH] Starting kernel smoothing propagation fetch for band={band_name}, rx_grid={my_grid}")
            
            refresh_minutes = self.db.config.get_value('prop_refresh_minutes') or 3
            history_minutes = self.db.config.get_value('prop_history_minutes') or 0
            try:
                history_minutes = int(history_minutes)
            except (TypeError, ValueError):
                history_minutes = 0
            history_minutes = max(0, min(60, history_minutes))
            if history_minutes > 0 and history_minutes < refresh_minutes:
                history_minutes = refresh_minutes
            chunk_mode = history_minutes > 0
            fetch_minutes = history_minutes if chunk_mode else refresh_minutes
            if fetch_minutes <= 0:
                fetch_minutes = refresh_minutes

            try:
                user_power_watts = float(self.db.config.get_value('default_pwr') or 10.0)
            except (TypeError, ValueError):
                user_power_watts = 10.0
            if user_power_watts <= 0:
                user_power_watts = 10.0
            user_power_dbm = watts_to_dbm(user_power_watts)

            cache_key = band_name.lower()
            now = datetime.datetime.utcnow()
            cached_entry = self.prop_model_cache.get(cache_key)
            prop_reports = None
            reports_with_time: List[Tuple[object, datetime.datetime]] = []
            
            if cached_entry:
                cached_time, cached_window, cached_reports = cached_entry
                age_minutes = (now - cached_time).total_seconds() / 60.0
                if age_minutes < refresh_minutes and cached_window >= fetch_minutes:
                    prop_reports = cached_reports
                    logging.info(f"[PROP FETCH] Using cached propagation dataset for {band_name} (age {age_minutes:.1f} min < {refresh_minutes}, window {cached_window}min)")
            
            if prop_reports is None:
                # Fetch global reports (not filtered by user grid)
                reports = self.prop_fetcher.fetch_reception_reports(
                    rx_grid=my_grid,  # Kept for API compatibility, not used for filtering
                    band=band_name,
                    minutes=fetch_minutes
                )
                
                logging.info(f"[PROP FETCH] Received {len(reports)} global propagation reports")
                
                if not reports:
                    logging.info("[PROP FETCH] No propagation reports found")
                    self.prop_model_cache.pop(cache_key, None)
                    self.propagation_snapshot.pop(cache_key, None)
                    return

                # Log first few reports for debugging
                for i, r in enumerate(reports[:3]):
                    logging.info(f"[PROP FETCH] Report {i+1}: {r.get('tx_call')} ({r.get('tx_grid')}) -> {r.get('rx_call')} ({r.get('rx_grid')}), snr={r.get('snr')}dB, distance={r.get('distance_km'):.0f}km, azimuth={r.get('azimuth_deg', 0):.0f}deg")
                
                if len(reports) > 3:
                    logging.info(f"[PROP FETCH] ... and {len(reports) - 3} more reports")

                # Convert reports to PropagationReport objects
                prop_reports = []
                for r in reports:
                    try:
                        # Calculate azimuth if not present
                        azimuth = r.get('azimuth_deg', 0)
                        if azimuth == 0 and r.get('tx_grid') and r.get('rx_grid'):
                            azimuth = calculate_bearing(r.get('tx_grid'), r.get('rx_grid'))

                        timestamp_obj = r.get('timestamp', datetime.datetime.utcnow())
                        if isinstance(timestamp_obj, datetime.datetime):
                            timestamp_str = timestamp_obj.isoformat()
                        else:
                            timestamp_str = str(timestamp_obj)
                        
                        prop_report = PropagationReport(
                            timestamp_utc=timestamp_str,
                            band=r.get('band', band_name),
                            snr_db=r.get('snr', 0.0),
                            tx_call=r.get('tx_call', ''),
                            tx_grid=r.get('tx_grid', ''),
                            tx_lat=r.get('tx_lat', 0.0),
                            tx_lon=r.get('tx_lon', 0.0),
                            rx_call=r.get('rx_call', ''),
                            rx_grid=r.get('rx_grid', ''),
                            rx_lat=r.get('rx_lat', 0.0),
                            rx_lon=r.get('rx_lon', 0.0),
                            distance_km=r.get('distance_km', 0.0),
                            azimuth_deg=azimuth,
                            tx_power_dbm=r.get('tx_power_dbm')
                        )
                        prop_reports.append(prop_report)
                        reports_with_time.append((prop_report, self._normalize_report_timestamp(timestamp_str)))
                    except Exception as ex:
                        logging.debug(f"[PROP FETCH] Error converting report: {ex}")
                        continue
                
                logging.info(f"[PROP FETCH] Converted {len(prop_reports)} reports for kernel smoothing (prediction dataset ready)")
                self.prop_model_cache[cache_key] = (now, fetch_minutes, prop_reports)
            else:
                logging.info(f"[PROP FETCH] Reusing {len(prop_reports)} cached propagation reports for kernel smoothing")

            if not reports_with_time:
                reports_with_time = [
                    (report, self._normalize_report_timestamp(report.timestamp_utc))
                    for report in prop_reports
                ]
            reports_with_time.sort(key=lambda item: item[1])
            
            # Get current spots from database (only active, non-QRT)
            try:
                logging.debug('getting lock for propagation update')
                if not self.lock.acquire(timeout=4.00):
                    logging.warning("_fetch_and_update_propagation: lock not acquired")
                    return
                    
                spots = self.db.spots.get_spots()
                logging.info(f"[PROP FETCH] Processing {len(spots)} spots for predictions")
                spot_lookup = {spot.spotId: spot for spot in spots}
                
                # Build spot paths for kernel smoothing
                spot_paths: list[SpotPath] = []
                my_lat, my_lon = grid_to_latlon(my_grid)
                
                missing_grid_spots: list[int] = []
                for spot in spots:
                    # Skip spots without grid square data
                    if not spot.grid4 and not spot.grid6:
                        missing_grid_spots.append(spot.spotId)
                        continue
                    
                    spot_grid = spot.grid6 if spot.grid6 else spot.grid4
                    
                    try:
                        distance_km = calculate_distance(my_grid, spot_grid)
                        azimuth_deg = calculate_bearing(my_grid, spot_grid)
                        target_lat, target_lon = grid_to_latlon(spot_grid)

                        frequency_mhz: Optional[float] = None
                        try:
                            freq_val = float(spot.frequency)
                            if freq_val > 0:
                                frequency_mhz = freq_val / 1000.0 if freq_val > 1000 else freq_val
                        except (TypeError, ValueError):
                            frequency_mhz = None
                        
                        spot_path = SpotPath(
                            spot_id=spot.spotId,
                            activator_call=spot.activator,
                            activator_grid=spot_grid,
                            distance_km=distance_km,
                            azimuth_deg=azimuth_deg,
                            band=band_name,
                            mode=spot.mode,
                            frequency_mhz=frequency_mhz,
                            target_lat=target_lat,
                            target_lon=target_lon
                        )
                        spot_paths.append(spot_path)
                    except Exception as ex:
                        logging.debug(f"[PROP FETCH] Error calculating path for spot {spot.spotId}: {ex}")
                        continue
                
                logging.info(f"[PROP FETCH] Calculated path geometry for {len(spot_paths)} spots")
                if missing_grid_spots:
                    logging.warning(f"[PROP FETCH] Skipped {len(missing_grid_spots)} spots with no grid square data (examples: {missing_grid_spots[:3]})")
                if not spot_paths:
                    logging.info("[PROP FETCH] No spots with valid geometry; skipping propagation update")
                    return
                
                # Build prediction inputs
                try:
                    ssb_threshold = float(self.db.config.get_value('prop_ssb_threshold') or MODE_THRESHOLDS_DB['ssb'])
                except (TypeError, ValueError):
                    ssb_threshold = MODE_THRESHOLDS_DB['ssb']
                try:
                    digital_threshold = float(self.db.config.get_value('prop_digital_threshold') or MODE_THRESHOLDS_DB['digital'])
                except (TypeError, ValueError):
                    digital_threshold = MODE_THRESHOLDS_DB['digital']
                mode_thresholds = {'ssb': ssb_threshold, 'digital': digital_threshold}
                kernel_profile_id = str(self.db.config.get_value('prop_kernel_profile_id') or DEFAULT_KERNEL_PROFILE_ID).lower()
                kernel_profile = resolve_kernel_profile(kernel_profile_id)
                tx_antenna_id = self.db.config.get_value('prop_tx_antenna_profile_id') or DEFAULT_TX_ANTENNA_PROFILE_ID
                rx_antenna_id = self.db.config.get_value('prop_rx_antenna_profile_id') or DEFAULT_RX_ANTENNA_PROFILE_ID
                tx_antenna_profile = resolve_voacap_antenna_profile(tx_antenna_id, DEFAULT_TX_ANTENNA_PROFILE_ID)
                rx_antenna_profile = resolve_voacap_antenna_profile(rx_antenna_id, DEFAULT_RX_ANTENNA_PROFILE_ID)
                min_support_override = float(kernel_profile.get('min_n_eff', KERNEL_EA_MIN_N_EFF))
                logging.info(f"[PROP MODEL] Building prediction model for {len(spot_paths)} active spots on {band_name}")

                predictor_kwargs = {
                    'hunter_grid': my_grid,
                    'user_lat': my_lat,
                    'user_lon': my_lon,
                    'user_power_dbm': user_power_dbm,
                    'user_power_watts': user_power_watts,
                    'ssn': self._refresh_current_ssn(),
                    'reference_bw_hz': PSKREPORTER_REFERENCE_BW_HZ,
                    'mode_thresholds': mode_thresholds,
                    'min_support': min_support_override,
                    'kernel_profile': kernel_profile,
                    'tx_antenna_profile': tx_antenna_profile,
                    'rx_antenna_profile': rx_antenna_profile,
                }

                prediction_series: List[Tuple[datetime.datetime, dict[int, PropagationEstimate]]] = []
                if chunk_mode:
                    prediction_series = self._build_chunk_prediction_series(
                        spot_paths,
                        reports_with_time,
                        band_name,
                        refresh_minutes,
                        history_minutes,
                        batch_predict_kernel_ea,
                        predictor_kwargs=predictor_kwargs
                    )
                else:
                    predictions = batch_predict_kernel_ea(
                        spot_paths,
                        {band_name: prop_reports},
                        **predictor_kwargs
                    )
                    prediction_series = [(datetime.datetime.utcnow(), predictions)]

                if not prediction_series:
                    logging.info("[PROP FETCH] No prediction series generated; falling back to single snapshot")
                    predictions = batch_predict_kernel_ea(
                        spot_paths,
                        {band_name: prop_reports},
                        **predictor_kwargs
                    )
                    prediction_series = [(datetime.datetime.utcnow(), predictions)]
                    chunk_mode = False
                
                if chunk_mode:
                    for chunk_time, chunk_predictions in prediction_series:
                        self._record_history_for_chunk(chunk_predictions, spot_lookup, chunk_time)
                
                # Update spots with predictions
                update_count = 0
                band_snapshot: dict[str, dict[str, object]] = {}
                latest_time, latest_predictions = prediction_series[-1]
                predicted_count = sum(1 for v in latest_predictions.values() if isinstance(v, PropagationEstimate) and v.snr is not None)
                logging.info(f"[PROP FETCH] Generated {predicted_count} SNR predictions for latest chunk")

                update_count = self._apply_predictions_to_spots(
                    latest_predictions,
                    spot_lookup,
                    ssb_threshold,
                    digital_threshold,
                    band_snapshot,
                    prediction_time=latest_time,
                    record_history=not chunk_mode
                )
                
                self.db.commit_session()
                
                logging.info(f"[PROP FETCH] Successfully updated {update_count} spots with predicted SNR; committing propagation landscape")
                if band_snapshot:
                    self.propagation_snapshot[cache_key] = band_snapshot
                else:
                    self.propagation_snapshot.pop(cache_key, None)
                
                self._refresh_spots_frontend()
                
            finally:
                if self.lock.locked():
                    self.lock.release()
            
        except Exception as e:
            logging.error(f"[PROP FETCH] Error in propagation background worker: {e}", exc_info=True)

    def ensure_propagation_landscape(self) -> str:
        """
        Ensure propagation predictions exist for the currently selected band.
        If a cached landscape exists, apply it to any spots that are still missing
        data; otherwise kick off a background fetch immediately.
        """
        try:
            if not self.db.config.get_value('prop_enabled'):
                return self._response(False, "Propagation is disabled")

            band_id = self.db.filters.band_filter
            if band_id is None or band_id == 0:
                return self._response(False, "No band selected")

            band_name = get_name_of_band(band_id)
            cache_key = band_name.lower()
            snapshot = self.propagation_snapshot.get(cache_key)

            if snapshot:
                updated = self._apply_landscape_to_missing_spots(band_name)
                if updated:
                    self._refresh_spots_frontend()
                    return self._response(True, "", action="restored", updated=updated)

                rebuilt = self._recompute_predictions_from_cache(band_name)
                if rebuilt:
                    self._refresh_spots_frontend()
                    return self._response(True, "", action="recomputed", updated=rebuilt)
                return self._response(True, "No cached landscape entries for this spot", action="none", updated=0)

            logging.info(f"[PROP FETCH] No cached landscape for {band_name}; triggering fresh propagation build")
            self.trigger_propagation_fetch(band_id)
            return self._response(True, "", action="fetching", updated=0)
        except Exception as ex:
            logging.error("Error ensuring propagation landscape", exc_info=ex)
            return self._response(False, "Unable to ensure propagation landscape")

    def _normalize_report_timestamp(self, value) -> datetime.datetime:
        """Convert various timestamp formats into a naive UTC datetime."""
        if isinstance(value, datetime.datetime):
            if value.tzinfo is not None:
                return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            return value

        if isinstance(value, str):
            try:
                iso_value = value.replace('Z', '+00:00') if value.endswith('Z') else value
                parsed = datetime.datetime.fromisoformat(iso_value)
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
                return parsed
            except ValueError:
                pass

        if isinstance(value, (int, float)):
            try:
                return datetime.datetime.utcfromtimestamp(float(value))
            except Exception:
                pass

        return datetime.datetime.utcnow()

    def _record_history_for_chunk(self, predictions: dict[int, PropagationEstimate], spot_lookup: dict[int, Spot], timestamp: datetime.datetime):
        """Record propagation history samples for a specific time chunk."""
        if timestamp is None:
            return

        for spot_id, estimate in predictions.items():
            if not isinstance(estimate, PropagationEstimate):
                continue
            if estimate.snr is None and estimate.probability is None:
                continue
            spot = spot_lookup.get(spot_id)
            if not spot:
                continue
            spot_key = self._propagation_snapshot_key(spot.activator, spot.reference)
            self._append_propagation_history(spot_key, estimate.snr, timestamp, estimate.probability)

    def _build_chunk_prediction_series(
        self,
        spot_paths,
        reports_with_time: List[Tuple[object, datetime.datetime]],
        band_name: str,
        refresh_minutes: int,
        history_minutes: int,
        predictor,
        predictor_kwargs: Optional[dict] = None,
    ) -> List[Tuple[datetime.datetime, dict[int, PropagationEstimate]]]:
        """Build predictions for multiple time chunks to backfill history."""
        series: List[Tuple[datetime.datetime, dict[int, PropagationEstimate]]] = []
        if not reports_with_time or not spot_paths:
            return series

        reports_with_time.sort(key=lambda item: item[1])
        latest_ts = reports_with_time[-1][1]
        chunk_minutes = max(1, int(refresh_minutes))
        history_minutes = max(chunk_minutes, min(60, int(history_minutes)))
        num_chunks = max(1, (history_minutes + chunk_minutes - 1) // chunk_minutes)
        earliest_limit = latest_ts - datetime.timedelta(minutes=history_minutes)

        windows_desc: List[Tuple[datetime.datetime, datetime.datetime]] = []
        window_end = latest_ts
        while len(windows_desc) < num_chunks and window_end > earliest_limit:
            window_start = window_end - datetime.timedelta(minutes=chunk_minutes)
            windows_desc.append((window_start, window_end))
            window_end = window_start

        generated = 0
        for window_start, window_end in windows_desc:
            chunk_reports = [
                report for report, ts in reports_with_time
                if window_start < ts <= window_end
            ]
            if not chunk_reports:
                logging.debug(f"[PROP FETCH] No WSPR data for chunk ending {window_end.isoformat()} - skipping")
                continue

            kwargs = predictor_kwargs or {}
            predictions = predictor(
                spot_paths,
                {band_name: chunk_reports},
                **kwargs
            )
            series.append((window_end, dict(predictions)))
            generated += 1

        if not series:
            logging.warning("[PROP FETCH] Unable to build any chunked predictions from fetched WSPR data")
        else:
            series.sort(key=lambda item: item[0])
            logging.info(f"[PROP FETCH] Built {generated} chunked prediction windows spanning {len(series)} samples")

        return series

    def _apply_predictions_to_spots(
        self,
        predictions: dict[int, PropagationEstimate],
        spot_lookup: dict[int, Spot],
        ssb_threshold: float,
        digital_threshold: float,
        band_snapshot: dict[str, dict[str, object]],
        prediction_time: datetime.datetime,
        record_history: bool = True
    ) -> int:
        """Apply prediction values to the database spots and optionally record history."""
        if prediction_time is None:
            prediction_time = datetime.datetime.utcnow()

        update_count = 0
        for spot_id, estimate in predictions.items():
            if not isinstance(estimate, PropagationEstimate):
                continue
            spot = spot_lookup.get(spot_id)
            if not spot:
                continue

            if estimate.snr is not None:
                spot.propagation_snr = estimate.snr
                status = determine_propagation_status(estimate.snr, ssb_threshold, digital_threshold)
                spot.propagation_status = status
                spot.propagation_probability = estimate.probability
                spot.propagation_support = estimate.support
                spot.propagation_updated = prediction_time
                update_count += 1

                spot_key = self._propagation_snapshot_key(spot.activator, spot.reference)
                band_snapshot[spot_key] = {
                    'snr': estimate.snr,
                    'status': status,
                    'probability': estimate.probability,
                    'support': estimate.support,
                    'updated': prediction_time
                }

                if record_history:
                    self._append_propagation_history(spot_key, estimate.snr, prediction_time, estimate.probability)

                if update_count <= 3:
                    prob_text = f"{estimate.probability * 100:.0f}%" if estimate.probability is not None else "n/a"
                    logging.info(f"[PROP UPDATE] Predicted SNR for {spot.activator} at {spot.reference}: {estimate.snr:.1f}dB ({status}, Prob={prob_text})")
            else:
                spot.propagation_snr = None
                spot.propagation_probability = None
                spot.propagation_support = None
                spot.propagation_status = 'no_data'
                logging.debug(f"[PROP FETCH] No propagation estimate for spot {spot.spotId} ({spot.activator}/{spot.reference}); landscape did not produce a prediction")

        return update_count

    def _recompute_predictions_from_cache(self, band_name: str) -> int:
        """Rebuild predictions for the current spots using cached propagation reports."""
        cache_key = band_name.lower()
        cache_entry = self.prop_model_cache.get(cache_key)
        if not cache_entry:
            logging.info(f"[PROP FETCH] Cannot recompute cached landscape for {band_name}: no cached dataset")
            return 0

        cached_time, cached_window, prop_reports = cache_entry
        if not prop_reports:
            logging.info(f"[PROP FETCH] Cannot recompute cached landscape for {band_name}: cached dataset empty")
            return 0

        my_grid = self.db.config.get_value('my_grid6')
        if not my_grid or len(my_grid) < 4:
            logging.warning("[PROP FETCH] Cannot recompute cached landscape: invalid grid square")
            return 0


        refresh_minutes = self.db.config.get_value('prop_refresh_minutes') or 3
        history_minutes = self.db.config.get_value('prop_history_minutes') or 0
        try:
            refresh_minutes = int(refresh_minutes)
        except (TypeError, ValueError):
            refresh_minutes = 10
        try:
            history_minutes = int(history_minutes)
        except (TypeError, ValueError):
            history_minutes = 0
        history_minutes = max(0, min(60, history_minutes))
        chunk_mode = history_minutes > 0
        fetch_minutes = cached_window or refresh_minutes

        try:
            user_power_watts = float(self.db.config.get_value('default_pwr') or 10.0)
        except (TypeError, ValueError):
            user_power_watts = 10.0
        if user_power_watts <= 0:
            user_power_watts = 10.0
        user_power_dbm = watts_to_dbm(user_power_watts)

        logging.info(f"[PROP FETCH] Recomputing propagation landscape for {band_name} using cached {len(prop_reports)} reports (window {fetch_minutes} min)")

        reports_with_time = [
            (report, self._normalize_report_timestamp(report.timestamp_utc))
            for report in prop_reports
        ]
        reports_with_time.sort(key=lambda item: item[1])

        acquired = self.lock.acquire(timeout=4.0)
        if not acquired:
            logging.warning("[PROP FETCH] Could not acquire lock to recompute cached landscape")
            return 0

        try:
            spots = self.db.spots.get_spots()
            if not spots:
                return 0

            spot_lookup = {spot.spotId: spot for spot in spots}
            spot_paths: list[SpotPath] = []
            my_lat, my_lon = grid_to_latlon(my_grid)

            for spot in spots:
                if not spot.grid4 and not spot.grid6:
                    continue
                spot_grid = spot.grid6 if spot.grid6 else spot.grid4
                try:
                    distance_km = calculate_distance(my_grid, spot_grid)
                    azimuth_deg = calculate_bearing(my_grid, spot_grid)
                    target_lat, target_lon = grid_to_latlon(spot_grid)
                    frequency_mhz: Optional[float] = None
                    try:
                        freq_val = float(spot.frequency)
                        if freq_val > 0:
                            frequency_mhz = freq_val / 1000.0 if freq_val > 1000 else freq_val
                    except (TypeError, ValueError):
                        frequency_mhz = None
                    spot_paths.append(SpotPath(
                        spot_id=spot.spotId,
                        activator_call=spot.activator,
                        activator_grid=spot_grid,
                        distance_km=distance_km,
                        azimuth_deg=azimuth_deg,
                        band=band_name,
                        mode=spot.mode,
                        frequency_mhz=frequency_mhz,
                        target_lat=target_lat,
                        target_lon=target_lon
                    ))
                except Exception as ex:
                    logging.debug(f"[PROP FETCH] Error calculating path during recompute for spot {spot.spotId}: {ex}")
                    continue

            if not spot_paths:
                logging.info("[PROP FETCH] No valid spot paths during cached recompute; aborting")
                return 0

            try:
                ssb_threshold = float(self.db.config.get_value('prop_ssb_threshold') or MODE_THRESHOLDS_DB['ssb'])
            except (TypeError, ValueError):
                ssb_threshold = MODE_THRESHOLDS_DB['ssb']
            try:
                digital_threshold = float(self.db.config.get_value('prop_digital_threshold') or MODE_THRESHOLDS_DB['digital'])
            except (TypeError, ValueError):
                digital_threshold = MODE_THRESHOLDS_DB['digital']
            mode_thresholds = {'ssb': ssb_threshold, 'digital': digital_threshold}
            kernel_profile_id = str(self.db.config.get_value('prop_kernel_profile_id') or DEFAULT_KERNEL_PROFILE_ID).lower()
            kernel_profile = resolve_kernel_profile(kernel_profile_id)
            tx_antenna_id = self.db.config.get_value('prop_tx_antenna_profile_id') or DEFAULT_TX_ANTENNA_PROFILE_ID
            rx_antenna_id = self.db.config.get_value('prop_rx_antenna_profile_id') or DEFAULT_RX_ANTENNA_PROFILE_ID
            tx_antenna_profile = resolve_voacap_antenna_profile(tx_antenna_id, DEFAULT_TX_ANTENNA_PROFILE_ID)
            rx_antenna_profile = resolve_voacap_antenna_profile(rx_antenna_id, DEFAULT_RX_ANTENNA_PROFILE_ID)
            min_support_override = float(kernel_profile.get('min_n_eff', KERNEL_EA_MIN_N_EFF))

            predictor_kwargs = {
                'hunter_grid': my_grid,
                'user_lat': my_lat,
                'user_lon': my_lon,
                'user_power_dbm': user_power_dbm,
                'user_power_watts': user_power_watts,
                'ssn': self._refresh_current_ssn(),
                'reference_bw_hz': PSKREPORTER_REFERENCE_BW_HZ,
                'mode_thresholds': mode_thresholds,
                'min_support': min_support_override,
                'kernel_profile': kernel_profile,
                'tx_antenna_profile': tx_antenna_profile,
                'rx_antenna_profile': rx_antenna_profile,
            }

            prediction_series: List[Tuple[datetime.datetime, dict[int, PropagationEstimate]]] = []
            if chunk_mode:
                prediction_series = self._build_chunk_prediction_series(
                    spot_paths,
                    reports_with_time,
                    band_name,
                    refresh_minutes,
                    history_minutes,
                    batch_predict_kernel_ea,
                    predictor_kwargs=predictor_kwargs
                )
            else:
                predictions = batch_predict_kernel_ea(
                    spot_paths,
                    {band_name: prop_reports},
                    **predictor_kwargs
                )
                prediction_series = [(datetime.datetime.utcnow(), predictions)]

            if not prediction_series:
                logging.info("[PROP FETCH] Cached recompute did not produce predictions")
                return 0

            if chunk_mode:
                for chunk_time, chunk_predictions in prediction_series:
                    self._record_history_for_chunk(chunk_predictions, spot_lookup, chunk_time)

            latest_time, latest_predictions = prediction_series[-1]
            if chunk_mode:
                self._record_history_for_chunk(latest_predictions, spot_lookup, datetime.datetime.utcnow())
            band_snapshot: dict[str, dict[str, object]] = {}
            update_count = self._apply_predictions_to_spots(
                latest_predictions,
                spot_lookup,
                ssb_threshold,
                digital_threshold,
                band_snapshot,
                prediction_time=latest_time,
                record_history=not chunk_mode
            )

            if update_count == 0:
                return 0

            self.db.commit_session()
            self.propagation_snapshot[cache_key] = band_snapshot
            logging.info(f"[PROP FETCH] Recomputed cached landscape for {band_name}; updated {update_count} spots")
            return update_count
        finally:
            if acquired:
                self.lock.release()

    def get_spot(self, spot_id: int):
        logging.debug('py get_spot')
        spot = self.db.spots.get_spot(spot_id)
        ss = SpotSchema()
        return ss.dumps(spot)

    def get_propagation_history(self, spot_id: int):
        """Return up to one hour of propagation predictions for a spot."""
        spot = self.db.spots.get_spot(spot_id)
        if spot is None:
            return self._response(False, "spot not found", history=[])

        key = self._propagation_snapshot_key(spot.activator, spot.reference)
        history = self.propagation_history.get(key, [])
        serialised = [
            {
                'timestamp': entry['timestamp'].isoformat() + 'Z',
                'snr': entry.get('snr'),
                'probability': entry.get('probability'),
            }
            for entry in history
        ]
        ssn_payload = self._current_ssn_payload()
        return self._response(True, "", history=serialised, **ssn_payload)

    def get_propagation_map(self, mode: Optional[str] = None, cache_index: int = 0) -> str:
        """
        Build a world grid of propagation probabilities using the endpoint-aware kernel.
        """
        try:
            prop_enabled = self.db.config.get_value('prop_enabled')
            if not prop_enabled:
                return self._response(False, "Propagation is disabled")

            band_id = self.current_band_id or self.db.filters.band_filter
            if band_id is None or band_id == 0:
                return self._response(False, "Select a band to view the propagation map")

            band_name = get_name_of_band(band_id)
            if not band_name:
                return self._response(False, "Invalid band selection")

            my_grid = self.db.config.get_value('my_grid6')
            if not my_grid or len(str(my_grid)) < 4:
                return self._response(False, "Invalid grid square")

            mode_text = str(mode or "").strip().upper()
            if not mode_text:
                mode_text = "FT8"
            mode_category = categorize_operation_mode(mode_text)
            threshold_db = MODE_THRESHOLDS_DB.get(mode_category, MODE_THRESHOLDS_DB['digital'])

            profile_id = self.db.config.get_value('prop_kernel_profile_id') or DEFAULT_KERNEL_PROFILE_ID
            profile_settings = resolve_kernel_profile(profile_id)
            try:
                grid_step = float(self.db.config.get_value('prop_map_grid_step_deg') or PROPAGATION_MAP_GRID_STEP_DEG)
            except (TypeError, ValueError):
                grid_step = PROPAGATION_MAP_GRID_STEP_DEG
            if grid_step <= 0:
                grid_step = PROPAGATION_MAP_GRID_STEP_DEG

            try:
                user_power_watts = float(self.db.config.get_value('default_pwr') or 10.0)
            except (TypeError, ValueError):
                user_power_watts = 10.0
            if user_power_watts <= 0:
                user_power_watts = 10.0
            user_power_dbm = watts_to_dbm(user_power_watts)

            refresh_minutes = self.db.config.get_value('prop_refresh_minutes') or 3
            cache_key = f"{band_name.lower()}|{mode_text}|{my_grid}|{profile_id}|{user_power_watts:.1f}|{grid_step:.2f}"
            cache_list = self.propagation_map_cache.get(cache_key, [])
            if cache_list:
                cache_list = list(cache_list)
            grid_payload: Optional[dict[str, object]] = None
            cache_index = max(0, int(cache_index))
            if cache_list and cache_index < len(cache_list):
                cached_time, cached_payload = cache_list[cache_index]
                age_minutes = (datetime.datetime.utcnow() - cached_time).total_seconds() / 60.0
                if cache_index > 0 or age_minutes < refresh_minutes:
                    grid_payload = cached_payload

            if grid_payload is None:
                prop_reports = self._get_propagation_reports_for_band(my_grid, band_name, refresh_minutes)
                if not prop_reports:
                    return self._response(False, "No propagation data available yet")

                dataset = build_endpoint_kernel_arrays(prop_reports)
                if dataset is None:
                    return self._response(False, "Unable to build kernel dataset")

                try:
                    user_lat, user_lon = grid_to_latlon(my_grid)
                except Exception as exc:
                    logging.error("[PROP MAP] Invalid grid: %s", exc)
                    return self._response(False, "Invalid grid square")

                grid_cells = self._build_probability_grid(
                    dataset,
                    user_lat=user_lat,
                    user_lon=user_lon,
                    user_power_dbm=user_power_dbm,
                    threshold_db=threshold_db,
                    profile_settings=profile_settings,
                    grid_step_deg=grid_step,
                )
                created_at = datetime.datetime.utcnow().isoformat() + 'Z'
                grid_payload = {
                    "grid_step_deg": grid_step,
                    "grid": grid_cells,
                    "user": {"lat": user_lat, "lon": user_lon, "grid": my_grid},
                    "band_name": band_name,
                    "mode": mode_text,
                    "created_at": created_at,
                }
                cache_list.insert(0, (datetime.datetime.utcnow(), grid_payload))
                if len(cache_list) > PROPAGATION_MAP_CACHE_LIMIT:
                    cache_list = cache_list[:PROPAGATION_MAP_CACHE_LIMIT]
                self.propagation_map_cache[cache_key] = cache_list
                cache_index = 0
            else:
                cache_index = min(cache_index, max(0, len(cache_list) - 1))
                cache_list = self.propagation_map_cache.get(cache_key, cache_list)

            spots = self.db.spots.get_spots()
            spot_markers: list[dict[str, object]] = []
            for spot in spots:
                lat = spot.latitude
                lon = spot.longitude
                if lat is None or lon is None or (lat == 0 and lon == 0):
                    grid = spot.grid6 or spot.grid4
                    if grid:
                        try:
                            lat, lon = grid_to_latlon(grid)
                        except Exception:
                            continue
                if lat is None or lon is None:
                    continue

                spot_markers.append({
                    "spot_id": spot.spotId,
                    "lat": float(lat),
                    "lon": float(lon),
                    "source": spot.spot_source,
                    "activator": spot.activator,
                    "reference": spot.reference,
                    "mode": spot.mode,
                    "probability": spot.propagation_probability,
                })

            cache_total = len(cache_list)
            payload = {
                **grid_payload,
                "spots": spot_markers,
                "cache_index": cache_index,
                "cache_total": cache_total,
            }
            return self._response(True, "", **payload)
        except Exception as exc:
            logging.error("[PROP MAP] Failed to build map", exc_info=exc)
            return self._response(False, "Unable to build propagation map")

    def get_spots(self):
        logging.debug('py get_spots')
        spots = self.db.spots.get_spots()
        ss = SpotSchema(many=True)
        return ss.dumps(spots)

    def get_spot_comments(self, spot_id: int):
        spot = self.db.spots.get_spot(spot_id)

        x = self.db.get_spot_comments(spot.activator, spot.reference)
        ss = SpotCommentSchema(many=True)
        return ss.dumps(x)

    def insert_spot_comments(self, spot_id: int):
        '''
        Pulls the spot comments from the POTA api and inserts them into our
        database.

        :param int spot_id: spot id. pk in db
        '''
        spot = self.db.spots.get_spot(spot_id)
        if spot is None:
            return

        # other program dont have this (AFAIK) so unlock and return
        if spot.spot_source != 'POTA':
            return

        comms = self.pota.get_spot_comments(spot.activator, spot.reference)
        try:
            logging.debug('getting lock insert spot comments')
            if not self.lock.acquire(timeout=4.00):
                # self.db.session.rollback()
                logging.warning("insert_spot_comments: lock not acquired")
                return
            self.db.insert_spot_comments(spot.activator, spot.reference, comms)
        finally:
            if self.lock.locked():
                self.lock.release()

    def get_qso_from_spot(self, id: int):
        # cfg = self.db.get_user_config()
        # my_grid = self.db.config.get_value("my_grid6")

        # if we cant get a lock return null
        logging.debug('getting lock for qso from spot')
        if not self.lock.acquire(timeout=4.00):
            self.db.session.rollback()
            logging.warning("timed out lock acquisition. session rollback")
            return self._response(False, "failed to get db lock. timed out.")

        spot = self.db.spots.get_spot(id)
        if spot is None:
            logging.warning(f"spot not found {id}")
            if self.lock.locked():
                self.lock.release()
            return self._response(False, "failed to get spot.")

        prog = spot.spot_source
        q = self.programs[prog].build_qso(spot)

        if q is None:
            logging.error(f"failed to build qso from spot {spot}")
            if self.lock.locked():
                self.lock.release()
            return self._response(False, "failed to build qso from spot.")

        # if q.gridsquare:
        #     dist = Distance.distance(my_grid, q.gridsquare)
        #     bearing = Distance.bearing(my_grid, q.gridsquare)
        #     q.distance = dist
        #     q.bearing = bearing
        qs = QsoSchema()
        result = qs.dumps(q)

        if self.lock.locked():
            self.lock.release()
        return self._response(True, "", qso=result)

    def get_activator_stats(self, callsign):
        logging.debug("getting activator stats...")
        ac = self._get_activator(callsign)
        if ac is None:
            return self._response(False, f"Activator {callsign} not found")
        return ActivatorSchema().dumps(ac)

    def get_activator_hunts(self, callsign):
        logging.debug("getting hunt count stats...")
        return self.db.qsos.get_activator_hunts(callsign)

    def get_reference(
            self,
            sig: str,
            ref: str,
            pull_from_api: bool = True) -> str:
        '''
        Returns the JSON for the location reference if found in the db. If not
        it can be downloaded from the program's API

        :param str sig: the SIG id of the program
        :param str ref: the programs reference designator string
        :param bool pull_from_pota: True (default) to try to download data when
            a reference is not in the db.

        :returns API response containing the JSON of park object. Or None if
            not found and not downloaded. the park JSON is in
            result.park_data field
        '''
        try:
            prog = self.programs[sig]
            ref = prog.get_reference(ref, pull_from_api)
            ps = ParkSchema()
            json = ps.dumps(ref)
            return self._response(True, "", park_data=json)
        except Exception as ex:
            logging.error("error getting ref", exc_info=ex)
            return self._response(False, f"Error getting reference: {ref}")

    def get_park_hunts(self, ref: str) -> str:
        '''
        Returns a JSON object containing the number of QSOs with activators at
        the given park reference.

        :param str ref: the POTA park reference designator string

        :returns JSON of park object in db or None if not found
        '''
        if ref is None:
            logging.error("get_park: ref param was None")
            return self._response(False, "park references invalid")

        park = self.db.parks.get_park(ref)

        if park is None:
            return self._response(True, "", count=0)
        else:
            return self._response(True, "", count=park.hunts)

    def get_park_hunted_bands(self, freq: str, ref: str) -> str:
        '''
        Gets data about a references hunted bands.

        :param str freq: current freq in MHz. used to test for previously
                         hunted bands
        :param str ref: the park/summit reference designator string

        :returns JSON with two fields, bands (string) and new_band (bool)
        '''
        if ref is None:
            logging.error("get_park_hunted_bands: ref param was None")
            return self._response(False, "park references invalid")

        hunted_bands = self.db.qsos.get_ref_hunted_bands(ref)

        current_band = get_band(freq)
        new_band = True
        if current_band is not None and current_band.value in hunted_bands:
            new_band = False

        if hunted_bands is None:
            return self._response(True, "", bands='unknown qso data',
                                  new_band=True)
        else:
            txt = ",".join(map(get_name_of_band, hunted_bands))
            return self._response(True, "", bands=txt,
                                  new_band=new_band)

    # def get_user_config(self):
    #     '''
    #     Returns the JSON for the user configuration record in the db
    #     '''
    #     cfg = self.db.get_user_config()
    #     return UserConfigSchema().dumps(cfg)

    def get_user_config2(self):
        '''
        Returns the JSON for the user configuration record in the db
        '''
        x = self.db.config.get_editable_json()
        return x

    def get_user_config_val(self, k: str):
        '''
        Returns API response with the value of a given config setting in the
        `val` property.

        If config key is not found, returns error API response.
        '''
        try:
            x = self.db.config.get_value(k)
        except KeyError as ke:
            logging.error('get_user_config_val caught KeyError', exc_info=ke)
            return self._response(False, f"Config Key {k} not found")

        return self._response(True, "", val=x)

    def get_version_num(self):
        return self._response(
            True,
            "",
            app_ver=__version__,
            db_ver=self.db.get_version())

    def spot_activator(self, qso_data, park: str) -> str:
        '''
        Spots the activator at the given park. The QSO data needs to be filled
        out for this to work properly. Needs freq, call, and mode

        :param any qso_data: dict of qso data from the UI
        :param string spot_comment: the comment to add to the spot.
        '''
        f = qso_data['freq']
        a = qso_data['call']
        m = qso_data['mode']
        r = qso_data['rst_sent']
        c = str(qso_data['comment'])

        logging.debug(f"sending spot for {a} on {f}")

        # cfg = self.db.get_user_config()

        # if spot+log is used the comment is modified before coming here.
        # remove boilerplate fluff and get the users comments for spot
        if c.startswith("["):
            x = c.index("]") + 1
            c = c[x:]

        qth = self.db.config.get_value("qth_string")
        my_call = self.db.config.get_value("my_call")

        if qth is not None:
            spot_comment = f"[{r} {qth}] {c}"
        else:
            spot_comment = f"[{r}] {c}"

        try:
            PotaApi.post_spot(activator_call=a,
                              park_ref=park,
                              freq=f,
                              mode=m,
                              spotter_call=my_call,
                              spotter_comments=spot_comment)
        except Exception as ex:
            msg = "Error posting spot to pota api!"
            logging.error(msg)
            logging.exception(ex)
            return self._response(False, msg)

        return self._response(True, "spot posted")

    def import_adif(self) -> str:
        '''
        Opens a Open File Dialog to allow the user to select a ADIF file
        containing POTA QSOs to be imported into the app's database.
        '''
        ft = ('ADIF files (*.adi;*.adif)', 'All files (*.*)')
        filename = webview.windows[0] \
            .create_file_dialog(
                webview.OPEN_DIALOG,
            file_types=ft)
        if not filename:
            return self._response(True, "")

        logging.info("starting import of ADIF file...")

        try:
            AdifLog.import_from_log(filename[0], self.db)
        except Exception as ex:
            logging.error('error importing log', exc_info=ex)
            return self._response(False, "Error with ADIF import.")

        return self._response(True, "Completed ADIF import", persist=True)

    def log_qso(self, qso_data):
        '''
        Logs the QSO to the database, adif file, and updates stats. Will force
        a reload of the currently displayed spots.

        :param any qso_data: dict of qso data from the UI
        '''
        logging.info('acquiring lock to log qso')
        self.lock.acquire()

        def_pwr = self.db.config.get_value('default_pwr')

        try:
            program = qso_data['sig']
            ref = qso_data['sig_info']
            pota_ref = qso_data['pota_ref'] if 'pota_ref' in qso_data else ''

            self.programs[program].inc_ref_hunt(ref, pota_ref)

            qso_data['tx_pwr'] = def_pwr
            logging.debug(f"logging qso: {qso_data}")
            id = self.db.qsos.insert_new_qso(qso_data)
        except Exception as ex:
            logging.error("Error logging QSO to db:")
            logging.exception(ex)
            self.lock.release()
            return self._response(False, f"Error logging QSO: {ex}")

        # db written so commit & release lock
        self.db.commit_session()
        self.lock.release()

        # get the data to log to the adif file and remote adif host
        qso = self.db.qsos.get_qso(id)
        act = self.db.get_activator_name(qso_data['call'])
        qso.name = act if act is not None else 'ERROR NO NAME'

        try:
            # self.adif_log.log_qso_and_send(qso, cfg)
            self.adif_log.log_qso(qso)
        except Exception as log_ex:
            logging.exception(
                msg="Error logging QSO to as adif (local/remote):",
                exc_info=log_ex)
            self.lock.release()
            return self._response(False, f"Error logging as ADIF: {log_ex}")

        return self._response(True, "QSO logged successfully")

    def refresh_spot(self, spot_id: int, call: str, ref: str):
        '''
        Refreshes the data for a given spot. If the spot_id is out of date from
        a refresh, this will lookup the new spot by call and ref.

        :param int spot_id: valid id of spot in db (this id from endpoints)
        :param str call: callsign of activator
        :param str ref:  sig_info ie. park reference

        :return: see API._response(). `False` if a bad id was given
        '''
        logging.debug(f"spot id = {spot_id}")

        if spot_id <= 0:
            logging.warning('bad spot id passed to refresh_spot')
            return self._response(False, "")

        logging.info(f"doing single spot update {spot_id}")

        to_mod: Spot = self.db.spots.get_spot(spot_id)

        if to_mod is None:
            logging.warning("refresh_spot: didn't find a spot for this id")
            spot = self.db.spots.get_spot_by_actx(call, ref)
            if spot is None:
                logging.warning("refresh_spot: didn't find a spot for actx")
                return self._response(False, "")

            spot_id = spot.spotId
            to_mod = self.db.spots.get_spot(spot_id)
            if to_mod is None:
                self.db.session.commit()
                return self._response(False, "")

        x = to_mod.spot_source
        self.programs[x].update_spot_metadata(to_mod)
        self.db.session.commit()
        return self._response(True, "")

    def export_qsos(self):
        '''
        Exports the QSOs logged with this logger app into a file.
        '''
        try:
            qs = self.db.qsos.get_qsos_from_app()
            my_call = self.db.config.get_value('my_call')
            my_grid6 = self.db.config.get_value('my_grid6')

            dt = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            log = AdifLog(filename=f"{dt}_export.adi")
            for q in qs:
                log.log_qso(q, my_call, my_grid6)

            return self._response(True, "QSOs exported successfully")
        except Exception as ex:
            logging.exception("Error exporting the DB")
            return self._response(
                False, "Error exporting QSOs from DB", ext=str(ex))

    # def set_user_config(self, config_json: any):
    #     logging.debug(f"setting config {config_json}")
    #     self.db.update_user_config(config_json)

    #     lp = LoggerParams(
    #         self.db.config.get_value('logger_type'),
    #         self.db.config.get_value('my_call'),
    #         self.db.config.get_value('my_grid6'),
    #         self.db.config.get_value('adif_host'),
    #         self.db.config.get_value('adif_port'),
    #     )
    #     self.adif_log = LoggerInterface.get_logger(lp, __version__)
    #     logging.debug(f"updating logger {self.adif_log}")

    def set_user_config2(self, config2_json: any):
        logging.debug(f"setting config2 {config2_json}")
        # self.db.update_user_config(config2_json)
        prev_prop_settings = self._capture_propagation_settings()
        prev_prop_enabled = bool(prev_prop_settings.get('prop_enabled'))
        self.db.config.set_editable_json(config2_json)

        lp = LoggerParams(
            self.db.config.get_value('logger_type'),
            self.db.config.get_value('my_call'),
            self.db.config.get_value('my_grid6'),
            self.db.config.get_value('adif_host'),
            self.db.config.get_value('adif_port'),
        )
        self.adif_log = LoggerInterface.get_logger(lp, __version__)
        logging.debug(f"updating logger {self.adif_log}")

        new_prop_settings = self._capture_propagation_settings()
        new_prop_enabled = bool(new_prop_settings.get('prop_enabled'))
        settings_changed = any(
            str(prev_prop_settings.get(key)) != str(new_prop_settings.get(key))
            for key in PROPAGATION_CONFIG_KEYS
            if key != 'prop_enabled'
        )

        try:
            if not prev_prop_enabled and new_prop_enabled:
                logging.info("[PROP FETCH] Propagation enabled via config; refreshing predictions for active band")
                self._reset_propagation_state()
                self._handle_propagation_settings_change()
            elif prev_prop_enabled and not new_prop_enabled:
                logging.info("[PROP FETCH] Propagation disabled via config; clearing cached predictions")
                self._reset_propagation_state()
            elif new_prop_enabled and settings_changed:
                logging.info("[PROP CFG] Propagation settings updated; re-running predictions")
                self._handle_propagation_settings_change()
        except Exception as ex:
            logging.error("Error triggering propagation fetch after enabling propagation", exc_info=ex)

    def set_ssn_override(self, enabled: bool, override_value: Optional[float] = None) -> str:
        """
        Allow the UI to temporarily override NOAA SSN readings with the configured fallback.
        The override only persists for the current runtime session.
        """
        self.ssn_override_enabled = bool(enabled)
        if self.ssn_override_enabled:
            try:
                numeric_value = float(override_value) if override_value is not None else None
            except (TypeError, ValueError):
                numeric_value = None
            if numeric_value is None or numeric_value <= 0:
                numeric_value = self._get_configured_default_ssn()
            self.ssn_override_value = numeric_value
            logging.info("[SSN] Override enabled with value %.1f", numeric_value)
        else:
            logging.info("[SSN] Override disabled; reverting to NOAA feed")
            self.ssn_override_value = None

        self._refresh_current_ssn()
        try:
            self._handle_propagation_settings_change()
        except Exception as exc:
            logging.error("[SSN] Failed to refresh propagation after override toggle", exc_info=exc)
        return self._response(True, "", override=self.ssn_override_enabled, ssn=self.current_ssn_value)

    def set_band_filter(self, band: int):
        logging.debug(f"api setting band filter to: {band}")
        self.db.filters.set_band_filter(band)

    def set_region_filter(self, region: list[str]):
        logging.debug(f"api setting region filter to: {region}")
        self.db.filters.set_region_filter(region)

    def set_continent_filter(self, continents: list[str]):
        logging.debug(f"api setting cont filter to: {continents}")
        self.db.filters.set_continent_filter(continents)

    def set_location_filter(self, location: str):
        logging.debug(f"setting region filter to {location}")
        self.db.filters.set_location_filter(location)

    def set_qrt_filter(self, is_qrt: bool):
        logging.debug(f"api setting qrt filter to: {is_qrt}")
        self.db.filters.set_qrt_filter(is_qrt)

    def set_hunted_filter(self, filter_hunted: bool):
        logging.debug(f"api setting qrt filter to: {filter_hunted}")
        self.db.filters.set_hunted_filter(filter_hunted)

    def set_only_new_filter(self, filter_only_new: bool):
        logging.debug(f"api setting ATNO filter to: {filter_only_new}")
        self.db.filters.set_only_new_filter(filter_only_new)

    def set_sig_filter(self, sig_filter: str):
        '''
        Set the Special Interest Group (sig) filter.

        :param string sig_filter: only POTA or SOTA or WWFF
        '''
        logging.debug(f"api setting SIG filter to: {sig_filter}")
        self.db.filters.set_sig_filter(sig_filter)

    def set_probability_filter(self, min_probability: Optional[float], max_probability: Optional[float]):
        """
        Set the propagation probability range (0.0-1.0) required for spots.
        Pass None values to clear the filter.
        """
        logging.debug(f"api setting probability filter to: {min_probability}-{max_probability}")
        self.db.filters.set_probability_filter(min_probability, max_probability)

    def set_snr_filter(self, snr_threshold: Optional[float]):
        """
        Legacy compatibility wrapper that maps an SNR threshold to a probability range.
        """
        logging.debug(f"api setting legacy SNR filter to: {snr_threshold}")
        if snr_threshold is None:
            self.db.filters.set_probability_filter(None, None)
        else:
            self.db.filters.set_probability_filter(float(snr_threshold), 1.0)

    def update_activator_stats(self, callsign: str) -> int:
        j = self.pota.get_activator_stats(callsign)

        if j is not None:
            # the json will be none if say the call doesn't return success
            # from api. probably they dont have an account
            return self.db.update_activator_stat(j)
        else:
            logging.warn(f"activator callsign {callsign} not found")
            return -1

    def launch_pota_window(self):
        self.pw = webview.create_window(
            title='POTA APP', url='https://pota.app/#/user/stats')

    def load_location_data(self):
        logging.debug("downloading location data...")
        locations = PotaApi.get_locations()
        self.db.locations.load_location_data(locations)
        return self._response(True, "Downloaded location data successfully")

    def qsy_to(self, freq, mode: str):
        '''Use CAT control to QSY'''
        logging.debug(f"qsy_to {freq} {mode}")

        if self.cat is None:
            logging.warn("CAT is None. not qsy-ing")
            return self._response(False, "CAT control failure.")

        hrz = float(freq) * 1000.0
        logging.debug(f"adjusted freq {hrz}")
        if mode == "SSB" and hrz >= 10000000:
            mode = "USB"
        elif mode == "SSB" and hrz < 10000000:
            mode = "LSB"
            if hrz > 5330000 and hrz < 5404000:  # 60m SSB is USB
                mode = "USB"
        elif mode == "CW":
            mode = self.db.config.get_value('cw_mode')
        elif mode.startswith("FT"):
            mode = self.db.config.get_value('ftx_mode')
        logging.debug(f"adjusted mode {mode}")
        bandwidth = self._preferred_filter_width(mode)
        self.cat.set_mode(mode, bandwidth)
        self.cat.set_vfo(hrz)

        return self._response(True, "")

    def _preferred_filter_width(self, mode: str) -> Optional[int]:
        """
        Determine an appropriate passband width (Hz) for the requested mode.
        Returns None if we should let the rig pick its default.
        """
        normalized = (mode or "").upper()
        if normalized.startswith("CW"):
            return 500
        if normalized in ("USB", "LSB", "USB-D", "LSB-D"):
            return 2400
        if normalized in ("DIGU", "DIGL", "DATA-U", "DATA-L"):
            return 3000
        return None

    def get_ptt(self):
        '''Returns the PTT state from CAT control'''
        if self.cat is None:
            return self._response(False, "CAT control failure.")
        
        ptt = self.cat.get_ptt()
        return self._response(True, "", ptt=ptt)

    def update_park_hunts_from_csv(self) -> str:
        '''
        Will use the current pota stats from hunter.csv to update the db with
        new park hunt numbers. It will then update all the parks with data from
        the POTA API. This method will run a while depending on how many parks
        are in the csv file.
        '''
        ft = ('CSV files (*.csv;*.txt)', 'All files (*.*)')
        filename = webview.windows[0] \
            .create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=ft)
        if not filename:
            return self._response(True, "user cancelled")

        logging.info(f"updating park hunts from {filename[0]}")
        stats = PotaStats(filename[0])
        hunts = stats.get_all_hunts()

        for park in hunts:
            count = stats.get_park_hunt_count(park)
            j = {'reference': park, 'hunts': count}
            self.db.parks.update_park_hunts(j, count)

        self.db.commit_session()

        return self._update_all_parks()

    def export_park_data(self) -> str:
        '''
        Dumps the entire parks table into a file named 'park_export.json'.

        This can then be later used to import. This is useful to avoid having
        to download park info from the POTA endpoints.
        '''
        logging.debug("export_park_data: dumping parks table...")
        parks = self.db.parks.get_parks()
        schema = ParkSchema()
        data = schema.dumps(parks, many=True)

        with open("park_export.json", "w") as out:
            out.write(data)

        return self._response(
            True, "Park data exported successfully")

    def import_park_data(self) -> str:
        '''
        Loads previously exported park data from a file into the parks table.

        The opposite of :meth:`export_park_data`
        '''
        logging.debug("import_park_data: loading table...")

        ft = ('JSON files (*.json)', 'All files (*.*)')
        filename = webview.windows[0] \
            .create_file_dialog(
                webview.OPEN_DIALOG,
            file_types=ft)

        if not filename:
            # user cancelled
            return self._response(True, "")

        with open(filename[0], "r") as input:
            text = input.read()
            obj = json.loads(text)
            self.db.parks.import_park_data(obj)

        logging.debug("import_park_data: import finished")

        return self._response(
            True, "Park data imported successfully", persist=True)

    def get_seen_regions(self) -> str:
        '''
        Gets a sorted list of distinct regions (POTA) and associations (SOTA)
        that are in the current set of spots.
        '''
        x = self.seen_regions
        # logging.debug(f"return seen regions: {x}")
        return self._response(True, '', seen_regions=x)

    def get_alerts(self):
        '''
        Gets the list of user configured alert filters.
        '''
        logging.debug('py get_alerts')
        alerts = self.db.alerts.get_alerts()
        schema = AlertsSchema(many=True)
        return schema.dumps(alerts)

    def set_alerts(self, alerts: str):
        '''
        Sets the list of user configured alert filters.
        '''
        logging.debug('py set_alerts ' + alerts)
        new_alerts = json.loads(alerts)
        logging.debug(new_alerts)
        schema = AlertsSchema(many=True)
        to_load = schema.load(new_alerts, session=self.db.session,
                              many=True, partial=True)
        self.db.session.add_all(to_load)
        self.db.commit_session()

    def delete_alert(self, alert_id: int):
        '''
        Delete the given alert.
        '''
        logging.debug(f'py delete_alerts {alert_id}')
        self.db.alerts.delete_alert(alert_id)
        self.db.commit_session()

    def snooze_alert(self, alert_id: int) -> str:
        '''
        Snooze the given alert for a period of time.
        '''
        logging.debug(f'py snooze_alert {alert_id}')
        self.db.alerts.snooze_alert(alert_id)
        self.db.commit_session()

        return self._response(True, "Alert snoozed!")

    def get_pota_locations(self) -> str:
        locs = self.db.locations.get_all_locations()
        return self._response(True, '', locations=locs)

    def get_hamalert_text(self, location: str) -> str:
        hunted = self.db.parks.get_hunted_parks(location)
        self.pota.check_and_download_parks(location)
        with open(f"data\\parks-{location}.json", 'r', encoding='utf-8') as r:
            text = r.read()
            obj = json.loads(text)
            all_parks: list[str] = list(map(lambda x: x['reference'], obj))
            # logging.debug(all_parks)

            hunted_set = set(hunted)
            all_set = set(all_parks)
            unhunted = list(all_set - hunted_set)
            return self._response(True, '',
                                  hunted_refs=hunted,
                                  unhunted_refs=unhunted)

        return self._response(False, 'Error getting hamalert text')

    def _do_update(self, pota: any, sota: any, wwff: any):
        '''
        The main update method. Called on a timer

        First will delete all previous spots, then read the ones passed in
        and perform the logic to update meta info about the spots

        :param dict pota: the dict from the pota api
        :param dict sota: the dict from the sota api
        :param dict wwff: the dict from the wwff api. wwff['RCD']
        '''
        logging.debug('updating db')

        try:
            # json = self.pota.get_spots()
            # sota = self.sota.get_spots()
            # wwff = self.wwff.get_spots()

            logging.info("acquiring lock for update")
            if not self.lock.acquire(timeout=4.0):
                logging.error('no lock aquired')
                return
            self.db.delete_spots()
            self.programs["POTA"].update_spots(pota)
            self.programs["SOTA"].update_spots(sota)
            self.programs["WWFF"].update_spots(wwff)
            self.db.session.commit()
            logging.info("spots updated for programs")
            self._restore_cached_propagation()
            self.lock.release()
            logging.info("update lock released")

            self.seen_regions.clear()

            for p in self.programs.values():
                unique_reg = list(set(p.seen_regions))
                self.seen_regions += unique_reg

            self._handle_alerts()
        except ConnectionError as con_ex:
            logging.warning("Connection error in do_update: ")
            logging.exception(con_ex)
        except Exception as ex:
            logging.error("Unhandled error caught in do_update: ")
            logging.error(type(ex).__name__)
            logging.exception(ex)
        finally:
            if self.lock.locked():
                self.lock.release()

    def _update_all_parks(self) -> str:
        logging.info("updating all parks in db")

        parks = self.db.parks.get_parks()
        for park in parks:
            if park.name is not None:
                continue

            api_res = self.pota.get_park(park.reference)
            self.db.parks.update_park_data(api_res)  # delay_commit=True

            time.sleep(0.001)  # dont want to hurt POTA

        return self._response(
            True, "Park Data updated successfully", persist=True)

    def _get_activator(self, callsign: str) -> Activator:
        ''''
        Gets the activator model from the db or pulls the data to create a
        new one or update and old one.
        '''
        def update():
            logging.info("activator needs update from POTA API...")
            id = self.update_activator_stats(callsign)
            if id > 0:
                activator = self.db.get_activator_by_id(id)
                return activator
            return None

        ac = self.db.get_activator(callsign)
        if (ac is None):
            # not found pull new data
            return update()
        else:
            # check timestamp
            if (datetime.datetime.utcnow() - ac.updated > timedelta(days=1)):
                return update()

        return ac

    def test_propagation_connection(self) -> str:
        '''
        Test connectivity to the WSPR Rocks propagation data source.
        
        :returns: API response with connection test result
        '''
        try:
            from propagation_fetcher import PropagationDataFetcher

            fetcher = PropagationDataFetcher()
            logging.info("Testing WSPR Rocks connection...")
            success = fetcher.test_connection()
            
            return self._response(True, "", connected=success)
        except Exception as ex:
            logging.error("Error testing propagation connection", exc_info=ex)
            return self._response(False, f"Connection test failed: {ex}")


    def _response(self, success: bool, message: str, **kwargs) -> str:
        '''
        Returns a dumped json string from the given inputs.

        :param bool success: indicates if response is pass or fail
        :param str message: default message to return
        :param any kwargs: any keyword arguments are included in the json
        '''
        return json.dumps({
            'success': success,
            'message': message,
            **kwargs
        })

    def _get_win_size(self) -> tuple[int, int]:
        '''
        Get the stored windows size.
        '''
        x = self.db.config.get_value('size_x')
        y = self.db.config.get_value('size_y')
        return (x, y)

    def _get_win_pos(self) -> tuple[int, int]:
        '''
        Get the stored windows position.
        '''
        x = self.db.config.get_value('pos_x')
        y = self.db.config.get_value('pos_y')
        return (x, y)

    def _get_win_maximized(self) -> bool:
        '''
        Get the stored window maximized state.
        '''
        return self.db.config.get_value('is_max')


    def _store_win_size(self, size: tuple[int, int]):
        '''
        Save the window size to the database
        '''
        self.db.config.set_value('size_x', size[0])
        self.db.config.set_value('size_y', size[1], commit=True)

    def _store_win_pos(self, position: tuple[int, int]):
        '''
        Save the window position to the database
        '''
        self.db.config.set_value('pos_x', position[0])
        self.db.config.set_value('pos_y', position[1], commit=True)

    def _store_win_maxi(self, is_max: bool):
        self.db.config.set_value('is_max', 1 if is_max else 0, commit=True)

    def _handle_alerts(self):
        def get_str(spot: Spot) -> str:
            obj = {
                'location': spot.locationDesc,
                'activator': spot.activator,
                'reference': spot.reference,
                'freq': spot.frequency,
                'mode': spot.mode,
                'spotId': spot.spotId
            }
            return obj

        to_alert = self.db.check_alerts()

        # this is obj to get send to JS side via showSpotAlert()
        res: dict[str, list[str]] = {}

        for key in to_alert:
            spots = to_alert[key]
            res[key] = list(map(get_str, spots))

        # logging.debug(f"dict to send {res}")
        # logging.debug(f"dict to send {json.dumps(res)}")

        if len(webview.windows) > 0 and len(res) > 0:
            js = """if (window.pywebview.state !== undefined && 
                        window.pywebview.state.showSpotAlert !== undefined)  {{  // # noqa
                            window.pywebview.state.showSpotAlert('{obj}'); // # noqa
                    }}
                """.format(obj=json.dumps(res))
            # logging.debug(f"alerting w this {js}")
            webview.windows[0].evaluate_js(js)

    def _propagation_snapshot_key(self, activator: str, reference: str) -> str:
        """Build a stable key for caching propagation data."""
        act = (activator or '').upper()
        ref = (reference or '').upper()
        return f"{act}::{ref}"

    def _apply_landscape_to_missing_spots(self, band_name: str) -> int:
        """
        Apply cached propagation values for the provided band to spots that
        currently show "No Reports".
        """
        cache_key = band_name.lower()
        snapshot = self.propagation_snapshot.get(cache_key)
        if not snapshot:
            return 0

        logging.debug(f"[PROP RESTORE] Attempting to apply cached landscape for {band_name}")

        acquired = self.lock.acquire(timeout=4.0)
        if not acquired:
            logging.warning("[PROP RESTORE] Could not acquire lock to apply cached landscape")
            return 0

        try:

            spots = self.db.spots.get_spots()
            if not spots:
                return 0

            updated = 0
            for spot in spots:
                status = getattr(spot, 'propagation_status', None)
                if status not in (None, '', 'no_data'):
                    continue

                key = self._propagation_snapshot_key(spot.activator, spot.reference)
                cached = snapshot.get(key)
                if not cached:
                    continue

                spot.propagation_snr = cached.get('snr')
                spot.propagation_status = cached.get('status')
                spot.propagation_probability = cached.get('probability')
                spot.propagation_support = cached.get('support')
                spot.propagation_updated = cached.get('updated')
                updated += 1

            if updated:
                self.db.commit_session()
                logging.info(f"[PROP RESTORE] Populated propagation data for {updated} spots on {band_name} from cached landscape")
            return updated
        finally:
            if acquired:
                self.lock.release()

    def _append_propagation_history(
        self,
        spot_key: str,
        snr: Optional[float],
        timestamp: datetime.datetime,
        probability: Optional[float],
    ):
        """Append a propagation sample and prune history older than one hour."""
        if snr is None and probability is None:
            return
        history = self.propagation_history.setdefault(spot_key, [])
        history.append({'timestamp': timestamp, 'snr': snr, 'probability': probability})
        cutoff = timestamp - datetime.timedelta(hours=1)
        self.propagation_history[spot_key] = [
            entry for entry in history if entry['timestamp'] >= cutoff
        ]

    def _restore_cached_propagation(self) -> int:
        """
        Reapply cached propagation predictions to freshly updated spots.
        Returns number of spots updated.
        """
        if not self.propagation_snapshot:
            return 0

        spots = self.db.spots.get_spots()
        if not spots:
            return 0

        applied = 0
        for spot in spots:
            key = self._propagation_snapshot_key(spot.activator, spot.reference)
            restored = False
            for band_name, band_cache in self.propagation_snapshot.items():
                snapshot = band_cache.get(key)
                if not snapshot:
                    continue
                spot.propagation_snr = snapshot.get('snr')
                spot.propagation_status = snapshot.get('status')
                spot.propagation_probability = snapshot.get('probability')
                spot.propagation_support = snapshot.get('support')
                spot.propagation_updated = snapshot.get('updated')
                applied += 1
                restored = True
                break
            if not restored:
                continue

        if applied:
            self.db.commit_session()
            logging.info(f"[PROP RESTORE] Restored cached propagation data for {applied} spots across {len(self.propagation_snapshot)} cached bands")
        return applied

    def _refresh_spots_frontend(self):
        """Trigger a spot refresh in the frontend after propagation updates."""
        try:
            if len(webview.windows) == 0:
                return
            js = """if (window.pywebview.state && window.pywebview.state.getSpots) { window.pywebview.state.getSpots(); }"""
            webview.windows[0].evaluate_js(js)
        except Exception as ex:
            logging.error("error refreshing frontend after propagation update", exc_info=ex)
