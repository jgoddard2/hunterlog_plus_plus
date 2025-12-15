import datetime
import time
import sqlalchemy as sa
from sqlalchemy.orm import scoped_session
import re
import logging as L

from db.filters import Filters
from db.models.spot_comments import SpotComment
from db.models.spots import Spot

logging = L.getLogger(__name__)


class SpotQuery:
    def __init__(self,
                 session: scoped_session,
                 filters: Filters):
        '''
        Ctor for SpotQuery
        :param scoped_session session: the db session object
        :param filters: the Filters object. provides db filtering terms for
                        returning the list of spots
        '''
        self.session = session
        self._flts = filters

    def delete_all_spots(self):
        self.session.execute(sa.text('DELETE FROM spots;'))
        self.session.commit()

    def get_spots(self):
        '''
        Get all the spots after applying the current filters: band, region, and
        QRT filters
        '''
        and_flts = []
        or_flts = []
        and_flts = self._flts.get_and_filters()
        or_flts = self._flts.get_or_filters()

        # logging.debug(f"get_spots filter {and_flts} {or_flts}")

        x = self.session.query(Spot) \
            .filter(sa.and_(*and_flts)) \
            .filter(sa.or_(*or_flts)) \
            .all()
        return x

    def get_spot(self, id: int) -> Spot:
        return self.session.query(Spot).get(id)

    def get_spot_by_actx(self, activator: str, park: str) -> Spot:
        return self.session.query(Spot) \
            .filter(
                sa.and_(Spot.activator == activator,
                        Spot.reference == park)) \
            .first()

    def insert_test_spot(self):
        # test data
        test = Spot()
        test.spotId = int(time.time())
        test.spot_source = 'POTA'
        test.activator = "N9FZ"
        test.reference = "K-TEST"
        test.grid4 = "FL31"
        test.grid6 = "FL31vt"
        test.spotTime = datetime.datetime.utcnow()
        test.spotter = "HUNTER-LOG"
        test.mode = "CW"
        test.locationDesc = "TC-TC"
        test.latitude = "21.8022"
        test.longitude = "-72.17"
        test.name = "TEST"
        test.parkName = "TEST"
        test.comments = "A TEST SPOT FROM HL"
        test.frequency = "7200"
        test.hunted_bands = ""
        test.is_qrt = False
        test.hunted = False
        self.session.add(test)
        self.session.commit()

        test_cmt = SpotComment()
        test_cmt.activator = 'N9FZ'
        test_cmt.spotId = test.spotId
        test_cmt.spotter = 'W1AW'
        test_cmt.frequency = '7200'
        test_cmt.mode = 'CW'
        test_cmt.park = 'K-TEST'
        test_cmt.comments = "{this is a test} {With: N0CALL,W1AW} {Also: US-9798}"  # NOQA
        test_cmt.source = "test"
        test_cmt.band = "40m"
        test_cmt.spotTime = datetime.datetime.now()
        self.session.add(test_cmt)
        self.session.commit()

    def _update_comment_metadata(self, activator: str, park: str):
        # logging.debug(f"_update_comment_metadata: {activator} at {park}")
        wpm = r'^RBN \d+ dB (\d+) WPM.*'
        spot = self.get_spot_by_actx(activator, park)
        if spot is None:
            return

        act_comments = []
        comments = self.session.query(SpotComment) \
            .filter(
                sa.and_(SpotComment.activator == activator,
                        SpotComment.park == park))\
            .all()

        for c in comments:
            if c.source == "RBN" and c.mode == "CW":
                m = re.match(wpm, c.comments)
                if m and spot.cw_wpm is None:
                    # logging.debug(f"got wpm {m.group(1)}")
                    spot.cw_wpm = m.group(1)
            if c.spotter == activator:
                # logging.debug(f"appending activator cmt {c.comments}")
                if c.comments is not None:
                    act_comments.append(c.comments)

        spot.act_cmts = "|".join(act_comments)
        self.session.commit()

    # NOTE: This method is deprecated in favor of kernel smoothing approach
    # Propagation updates are now handled directly in api.py using kernel smoothing
    # Keeping this for reference in case we need to revert
    """
    def update_propagation_data(self, reports: list[dict]):
        '''
        Update spots with propagation data from WSPR reports.
        '''
        if not reports:
            return

        logging.info(f"[PROP UPDATE] Processing {len(reports)} propagation reports")

        # Create a map of callsign -> best report (highest SNR)
        best_reports = {}
        for r in reports:
            call = r['tx_call']
            snr = r['snr']
            # If we already have a report for this call, keep the one with higher SNR
            if call not in best_reports or snr > best_reports[call]['snr']:
                best_reports[call] = r

        logging.info(f"[PROP UPDATE] Consolidated to {len(best_reports)} unique callsigns")

        if not best_reports:
            return

        # Find spots that match these activators
        # We only care about spots that are not expired/invalid? 
        # Or just update any matching spot.
        # Let's update all matching spots to be safe.
        activators = list(best_reports.keys())
        
        logging.info(f"[PROP UPDATE] Looking for spots matching {len(activators)} activators")
        
        # Process in chunks to avoid SQLite limits if too many
        chunk_size = 500
        total_updated = 0
        
        for i in range(0, len(activators), chunk_size):
            chunk = activators[i:i + chunk_size]
            
            spots = self.session.query(Spot).filter(Spot.activator.in_(chunk)).all()
            
            logging.info(f"[PROP UPDATE] Chunk {i//chunk_size + 1}: Found {len(spots)} spots to update")
            
            for spot in spots:
                report = best_reports.get(spot.activator)
                if report:
                    spot.propagation_snr = report['snr']
                    # Determine status based on SNR (simple logic for now, UI handles color)
                    # We could store 'Good', 'Fair', 'Poor' here if needed
                    spot.propagation_status = 'Active' 
                    spot.propagation_updated = datetime.datetime.utcnow()
                    
                    total_updated += 1
                    
                    # Log first few updates
                    if total_updated <= 5:
                        logging.info(f"[PROP UPDATE] Updated spot: {spot.activator} at {spot.reference} with SNR={report['snr']}dB")
            
            self.session.commit()
        
        logging.info(f"[PROP UPDATE] Successfully updated propagation data for {total_updated} spots")
    """
