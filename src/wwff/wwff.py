import requests
import logging as L
from cachetools.func import ttl_cache
# import urllib.parse
# from utils.callsigns import get_basecall

logging = L.getLogger(__name__)

# -1 gets last hour of spots
SPOT_URL = "https://www.cqgma.org/api/spots/wwff/"
WWFF_INFO__URL = "https://www.cqgma.org/api/wwff/?"


class WwffApi():
    '''Class that calls the GMA WWFF endpoints and returns their results'''

    def _safe_get(self, url: str, timeout: float = 10.0):
        """
        Wrapper around requests.get with timeouts and exception handling so a
        transient network failure doesn't crash the downloader thread.
        """
        try:
            response = requests.get(url, timeout=timeout)
            logging.debug("WWFF API %s -> %s", url, response.status_code)
            response.raise_for_status()
            return response
        except requests.Timeout:
            logging.warning("WWFF request to %s timed out after %.1fs", url, timeout)
        except requests.RequestException as ex:
            logging.warning("WWFF request to %s failed: %s", url, ex, exc_info=True)
        return None

    def get_spots(self):
        '''Return all current spots from GMA WWFF API'''
        response = self._safe_get(SPOT_URL)
        if not response:
            return None

        try:
            # this threw on JSON decode before. even w/ 200 status
            # the error indicated an empty response json
            json = response.json()
        except requests.JSONDecodeError as ex:
            logging.warning("bad json in get_spots", exc_info=ex)
            return None
        return json

    @ttl_cache(ttl=24*60*60)  # 24 hours of cache
    def get_wwff_info(self, wwff_ref: str):
        '''Return all current spots from GMA WWFF API'''
        response = self._safe_get(WWFF_INFO__URL + wwff_ref)
        if response is None:
            return None
        try:
            return response.json()
        except requests.JSONDecodeError as ex:
            logging.warning("bad json in get_wwff_info for %s", wwff_ref, exc_info=ex)
            return None
