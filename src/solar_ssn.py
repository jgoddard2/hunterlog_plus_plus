"""
Helpers for retrieving the current observed sunspot number (SSN).

The NOAA SWPC JSON feed publishes both smoothed and observed values. We want
the observed (raw monthly) number when available, but must gracefully fall
back to a configured default when offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from typing import Iterable, Tuple

import logging as L
import requests

logging = L.getLogger(__name__)

NOAA_SWPC_SSN_URL = "https://services.swpc.noaa.gov/json/solar-cycle/observed-solar-cycle-indices.json"
NOAA_SWPC_TIMEOUT_SEC = 8


def _parse_noaa_time_tag_to_month_start_utc(time_tag: str) -> datetime:
    """Convert a NOAA 'time-tag' string (YYYY-MM) into a UTC datetime."""
    dt = datetime.strptime(time_tag, "%Y-%m")
    return dt.replace(tzinfo=timezone.utc)


@lru_cache(maxsize=1)
def _fetch_noaa_payload() -> Iterable[dict]:
    """Fetch and cache the NOAA solar-cycle JSON payload."""
    resp = requests.get(NOAA_SWPC_SSN_URL, timeout=NOAA_SWPC_TIMEOUT_SEC)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError("Unexpected NOAA SWPC payload format")
    return tuple(data)


def resolve_noaa_ssn(
    *,
    fallback: float,
    use_smoothed: bool = False,
) -> Tuple[float, bool]:
    """
    Return the latest SSN available from NOAA along with whether it was observed.

    Args:
        fallback: Value to return when NOAA data cannot be used.
        use_smoothed: If True, prefer the smoothed SSN column instead of raw.

    Returns:
        Tuple of (ssn_value, is_observed).
    """
    safe_fallback = float(fallback if fallback and fallback > 0 else 0.0)
    key = "smoothed_ssn" if use_smoothed else "ssn"
    try:
        rows = _fetch_noaa_payload()
        if not rows:
            return safe_fallback, False

        now_month = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )

        best_val = None
        best_dt = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            tt = row.get("time-tag") or row.get("time_tag")
            val = row.get(key)
            if not tt or val is None:
                continue
            try:
                row_dt = _parse_noaa_time_tag_to_month_start_utc(str(tt))
                val_f = float(val)
            except Exception:
                continue

            if row_dt <= now_month and (best_dt is None or row_dt > best_dt):
                best_dt = row_dt
                best_val = val_f

        if best_val is not None and best_val >= 0:
            return float(best_val), True

        for row in reversed(rows):
            if isinstance(row, dict) and row.get(key) is not None:
                try:
                    val_f = float(row[key])
                    if val_f >= 0:
                        return val_f, True
                except Exception:
                    continue

        logging.debug("[SSN] NOAA payload lacked usable %s values; using fallback", key)
        return safe_fallback, False
    except Exception as exc:
        logging.debug("[SSN] NOAA fetch failed: %s", exc)
        return safe_fallback, False


def clear_noaa_cache() -> None:
    """Reset the cached NOAA payload, mainly for tests."""
    _fetch_noaa_payload.cache_clear()
