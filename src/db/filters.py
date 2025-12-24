import logging as L
import sqlalchemy as sa
from typing import Optional
from bands import Bands
from db.models.spots import Spot
from db.qso_query import QsoQuery

logging = L.getLogger(__name__)


class Filters:
    def __init__(self):
        self.band_filter = Bands.NOBAND
        self.region_filter = list[str]()
        self.location_filter = None
        self.qrt_filter_on = True  # filter out QRT spots by default
        self.hunted_filter_on = False  # filter out spots you hunted
        self.only_new_on = False  # filter out parks you have never worked
        self.cont_filter = list[str]()
        self.sig_filter = ''
        self.probability_range: Optional[tuple[float, float]] = None

    def set_band_filter(self, band: Bands):
        logging.debug(f"setting band filter to {band}")
        self.band_filter = band

    def set_region_filter(self, region: list[str]):
        logging.debug(f"setting region filter to {region}")
        self.region_filter = region

    def set_continent_filter(self, continents: list[str]):
        logging.debug(f"setting continent filter to {continents}")
        self.cont_filter = continents

    def set_location_filter(self, location: str):
        logging.debug(f"setting location filter to {location}")
        self.location_filter = location

    def set_qrt_filter(self, is_on: bool):
        logging.debug(f"setting QRT filter to {is_on}")
        self.qrt_filter_on = is_on

    def set_hunted_filter(self, is_on: bool):
        logging.debug(f"setting hunted filter to {is_on}")
        self.hunted_filter_on = is_on

    def set_only_new_filter(self, is_on: bool):
        logging.debug(f"setting ATNO filter to {is_on}")
        self.only_new_on = is_on

    def set_sig_filter(self, sig: str):
        self.sig_filter = sig

    def set_probability_filter(self, min_probability: Optional[float], max_probability: Optional[float]):
        logging.debug(f"setting probability filter to {min_probability} - {max_probability}")
        if min_probability is None or max_probability is None:
            self.probability_range = None
            return
        low = max(0.0, min(min_probability, max_probability))
        high = min(1.0, max(min_probability, max_probability))
        self.probability_range = (low, high)

    def set_snr_filter(self, snr_threshold: Optional[float]):
        """
        Backward compatible setter that maps an SNR threshold to a probability
        filter (>= threshold). Threshold is assumed to be provided as 0-1.
        """
        if snr_threshold is None:
            self.set_probability_filter(None, None)
        else:
            self.set_probability_filter(float(snr_threshold), 1.0)

    def get_and_filters(self) -> list[sa.ColumnElement[bool]]:
        '''
        Gets all the search terms that should be boolean and-ed together when
        filtering the spots.
        '''
        return self._get_band_filters() + \
            self._get_location_filters() + \
            self._get_qrt_filter() + \
            self._get_hunted_filter() + \
            self._get_only_new_filter() + \
            self._get_sig_filter() + \
            self._get_probability_filter()

    def get_or_filters(self) -> list[sa.ColumnElement[bool]]:
        '''
        Gets all the search terms that should be boolean or-ed together when
        filtering the spots.
        '''
        return self._get_region_filters() + \
            self._get_continent_filters()

    def _get_band_filters(self) -> list[sa.ColumnElement[bool]]:
        band = Bands(self.band_filter)  # not sure why cast is needed
        if band == Bands.NOBAND:
            return []
        terms = QsoQuery.get_band_lmt_terms(band, Spot.frequency)
        return terms

    def _get_region_filters(self) -> list[sa.ColumnElement[bool]]:
        region = self.region_filter
        if (region is None or len(region) == 0):
            return []

        terms = []
        for r in region:
            if len(r) > 0:
                terms.append(Spot.locationDesc.startswith(r))
        return terms

    def _get_continent_filters(self) -> list[sa.ColumnElement[bool]]:
        conts = self.cont_filter
        if (conts is None or len(conts) == 0):
            return []

        terms = []
        for r in conts:
            if len(r) > 0:
                terms.append(Spot.continent == r)
        return terms

    def _get_location_filters(self) -> list[sa.ColumnElement[bool]]:
        loc = self.location_filter
        if (loc is None or loc == ''):
            return []
        terms = [Spot.locationDesc.contains(loc)]
        return terms

    def _get_qrt_filter(self) -> list[sa.ColumnElement[bool]]:
        qrt = self.qrt_filter_on
        if qrt:
            return [Spot.is_qrt == False]  # noqa E712
        terms = []
        return terms

    def _get_hunted_filter(self) -> list[sa.ColumnElement[bool]]:
        hunt_filter = self.hunted_filter_on
        if hunt_filter:
            return [Spot.hunted == False]  # noqa E712
        terms = []
        return terms

    def _get_only_new_filter(self) -> list[sa.ColumnElement[bool]]:
        new_filter = self.only_new_on
        logging.debug(f'newfilter is {new_filter}')
        if new_filter:
            return [Spot.park_hunts == 0]  # noqa E712
        terms = []
        return terms

    def _get_sig_filter(self) -> list[sa.ColumnElement[bool]]:
        sig = self.sig_filter
        if (sig is None or sig == ''):
            return []
        terms = [Spot.spot_source == sig]
        return terms

    def _get_probability_filter(self) -> list[sa.ColumnElement[bool]]:
        if self.probability_range is None:
            return []
        min_prob, max_prob = self.probability_range
        return [
            sa.and_(
                Spot.propagation_probability.isnot(None),
                Spot.propagation_probability >= float(min_prob),
                Spot.propagation_probability <= float(max_prob) + 1e-9
            )
        ]
