import json
import time
import webview
import logging as L
import datetime
import threading
from datetime import timedelta

from propagation_fetcher import PropagationDataFetcher
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


class JsApi:
    def __init__(self):
        self.lock = threading.Lock()
        self.db = DataBase()
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

    def trigger_propagation_fetch(self, band_id: int):
        """
        Trigger a fetch of propagation data for the specified band.
        This is called when the user changes the band filter.
        """
        try:
            # Check if feature is enabled
            if self.db.config.get_value('prop_enabled') != 'True':
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
            from propagation_kernel import PropagationReport, SpotPath, batch_predict_snr, classify_snr
            from propagation_utils import grid_to_latlon, calculate_distance, calculate_bearing
            
            logging.info(f"[PROP FETCH] Starting kernel smoothing propagation fetch for band={band_name}, rx_grid={my_grid}")
            
            # Fetch global reports (not filtered by user grid)
            reports = self.prop_fetcher.fetch_reception_reports(
                rx_grid=my_grid,  # Kept for API compatibility, not used for filtering
                band=band_name,
                minutes=15
            )
            
            logging.info(f"[PROP FETCH] Received {len(reports)} global propagation reports")
            
            if not reports:
                logging.info("[PROP FETCH] No propagation reports found")
                return

            # Log first few reports for debugging
            for i, r in enumerate(reports[:3]):
                logging.info(f"[PROP FETCH] Report {i+1}: {r.get('tx_call')} ({r.get('tx_grid')}) -> {r.get('rx_call')} ({r.get('rx_grid')}), snr={r.get('snr')}dB, distance={r.get('distance_km'):.0f}km, azimuth={r.get('azimuth_deg', 0):.0f}°")
            
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
                    
                    prop_report = PropagationReport(
                        timestamp_utc=r.get('timestamp', ''),
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
                        azimuth_deg=azimuth
                    )
                    prop_reports.append(prop_report)
                except Exception as ex:
                    logging.debug(f"[PROP FETCH] Error converting report: {ex}")
                    continue
            
            logging.info(f"[PROP FETCH] Converted {len(prop_reports)} reports for kernel smoothing")
            
            # Get current spots from database (only active, non-QRT)
            try:
                logging.debug('getting lock for propagation update')
                if not self.lock.acquire(timeout=4.00):
                    logging.warning("_fetch_and_update_propagation: lock not acquired")
                    return
                    
                spots = self.db.spots.get_spots()
                logging.info(f"[PROP FETCH] Processing {len(spots)} spots for predictions")
                
                # Build spot paths for kernel smoothing
                spot_paths = []
                my_lat, my_lon = grid_to_latlon(my_grid)
                
                for spot in spots:
                    # Skip spots without grid square data
                    if not spot.grid4 and not spot.grid6:
                        continue
                    
                    spot_grid = spot.grid6 if spot.grid6 else spot.grid4
                    
                    try:
                        # Calculate path geometry from user to activator
                        distance_km = calculate_distance(my_grid, spot_grid)
                        azimuth_deg = calculate_bearing(my_grid, spot_grid)
                        
                        spot_path = SpotPath(
                            spot_id=spot.spotId,
                            activator_call=spot.activator,
                            activator_grid=spot_grid,
                            distance_km=distance_km,
                            azimuth_deg=azimuth_deg,
                            band=band_name
                        )
                        spot_paths.append(spot_path)
                    except Exception as ex:
                        logging.debug(f"[PROP FETCH] Error calculating path for spot {spot.spotId}: {ex}")
                        continue
                
                logging.info(f"[PROP FETCH] Calculated path geometry for {len(spot_paths)} spots")
                
                # Use kernel smoothing to predict SNR for each spot
                reports_by_band = {band_name: prop_reports}
                predictions = batch_predict_snr(spot_paths, reports_by_band)
                
                logging.info(f"[PROP FETCH] Generated {sum(1 for v in predictions.values() if v is not None)} SNR predictions")
                
                # Get SNR thresholds from config
                ssb_threshold = self.db.config.get_value('prop_ssb_threshold')
                digital_threshold = self.db.config.get_value('prop_digital_threshold')
                
                # Update spots with predictions
                update_count = 0
                for spot_id, predicted_snr in predictions.items():
                    spot = self.db.spots.get_spot(spot_id)
                    if spot:
                        if predicted_snr is not None:
                            spot.propagation_snr = predicted_snr
                            spot.propagation_status = classify_snr(predicted_snr, ssb_threshold, digital_threshold)
                            spot.propagation_updated = datetime.datetime.utcnow()
                            update_count += 1
                            
                            # Log first few predictions
                            if update_count <= 3:
                                logging.info(f"[PROP UPDATE] Predicted SNR for {spot.activator} at {spot.reference}: {predicted_snr:.1f}dB ({spot.propagation_status})")
                        else:
                            # No prediction available
                            spot.propagation_status = 'no_data'
                
                self.db.commit_session()
                
                logging.info(f"[PROP FETCH] Successfully updated {update_count} spots with predicted SNR")
                
            finally:
                if self.lock.locked():
                    self.lock.release()
            
        except Exception as e:
            logging.error(f"[PROP FETCH] Error in propagation background worker: {e}", exc_info=True)


    def get_spot(self, spot_id: int):
        logging.debug('py get_spot')
        spot = self.db.spots.get_spot(spot_id)
        ss = SpotSchema()
        return ss.dumps(spot)

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
        self.cat.set_mode(mode)
        self.cat.set_vfo(hrz)

        return self._response(True, "")

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
        Test connection to the configured propagation data source.
        
        :returns: API response with connection test result
        '''
        try:
            from propagation_fetcher import PropagationDataFetcher
            
            data_source = self.db.config.get_value('prop_data_source')
            fetcher = PropagationDataFetcher(data_source)
            
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
