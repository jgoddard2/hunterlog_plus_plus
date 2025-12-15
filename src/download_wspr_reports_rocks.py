#!/usr/bin/env python3
"""
Download WSPR spots directly from wspr.rocks / wspr.live ClickHouse backend.

This script fetches the last hour of spots for a hard-coded amateur band
and writes up to 5000 spots to a text file in a format similar to the other
PSK/WSPR downloaders in this project.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

BASE_URL = "https://db1.wspr.live"
LOOKBACK_MINUTES = 60
MAX_SPOTS = 5000

BAND_LABEL = "20m"
# Mapping pulled from wspr.rocks reference (see SQL tab description)
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


@dataclass
class WsprReport:
    timestamp: str
    sort_key: datetime
    tx_call: str
    tx_grid: str
    rx_call: str
    rx_grid: str
    freq_mhz: str
    snr: str
    tx_power_dbm: str
    distance_km: str
    azimuth_deg: str


def band_code(label: str) -> int:
    try:
        return BAND_CODES[label]
    except KeyError as exc:
        raise SystemExit(f"Unsupported BAND_LABEL {label!r}. Edit BAND_CODES if needed.") from exc


def _build_query(band: int, minutes: int, limit: int) -> str:
    return (
        "SELECT time, tx_sign, tx_loc, rx_sign, rx_loc, band, frequency, snr, power, "
        "distance, azimuth "
        "FROM wspr.rx "
        f"WHERE band = {band} AND time > subtractMinutes(now(), {minutes}) "
        "ORDER BY time DESC "
        f"LIMIT {limit} "
        "FORMAT JSONCompact"
    )


def _parse_timestamp(ts: str) -> Optional[datetime]:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def fetch_reports() -> List[WsprReport]:
    code = band_code(BAND_LABEL)
    query = _build_query(code, LOOKBACK_MINUTES, MAX_SPOTS)
    try:
        resp = requests.get(BASE_URL, params={"query": query}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SystemExit(f"Failed to query wspr.live backend: {exc}") from exc

    try:
        payload: Dict[str, Any] = resp.json()
    except ValueError as exc:
        snippet = resp.text[:200]
        raise SystemExit(f"wspr.live returned invalid JSON: {exc}\nSnippet: {snippet}") from exc

    data_rows = payload.get("data", [])
    reports: List[WsprReport] = []
    for row in data_rows:
        try:
            (
                ts_raw,
                tx_call,
                tx_loc,
                rx_call,
                rx_loc,
                _band,
                freq_hz,
                snr,
                power_dbm,
                distance_km,
                azimuth_deg,
            ) = row
        except ValueError:
            # Skip malformed rows
            continue

        ts_obj = _parse_timestamp(ts_raw)
        ts_iso = (
            ts_obj.isoformat().replace("+00:00", "Z")
            if ts_obj
            else (ts_raw if isinstance(ts_raw, str) else "Unknown")
        )
        try:
            freq_mhz = f"{float(freq_hz) / 1_000_000:.3f}"
        except (TypeError, ValueError):
            freq_mhz = "N/A"

        report = WsprReport(
            timestamp=ts_iso,
            sort_key=ts_obj or datetime.min.replace(tzinfo=timezone.utc),
            tx_call=tx_call or "Unknown",
            tx_grid=tx_loc or "Unknown",
            rx_call=rx_call or "Unknown",
            rx_grid=rx_loc or "Unknown",
            freq_mhz=freq_mhz,
            snr=str(snr if snr is not None else "N/A"),
            tx_power_dbm=str(power_dbm if power_dbm is not None else "N/A"),
            distance_km=str(distance_km if distance_km is not None else "N/A"),
            azimuth_deg=str(azimuth_deg if azimuth_deg is not None else "N/A"),
        )
        reports.append(report)

    return reports


def summarize_span(reports: List[WsprReport]) -> Dict[str, str]:
    if not reports:
        return {"oldest": "Unknown", "newest": "Unknown", "span": "0 minutes"}
    sorted_reports = sorted(reports, key=lambda r: r.sort_key)
    oldest = sorted_reports[0].sort_key
    newest = sorted_reports[-1].sort_key
    span_minutes = (newest - oldest).total_seconds() / 60.0 if newest and oldest else 0.0
    return {
        "oldest": sorted_reports[0].timestamp,
        "newest": sorted_reports[-1].timestamp,
        "span": f"{span_minutes:.1f} minutes",
    }


def write_reports(reports: List[WsprReport], output_path: Path) -> None:
    stats = summarize_span(reports)
    header = (
        f"WSPR Rocks spots for {BAND_LABEL}\n"
        f"Requested window: last {LOOKBACK_MINUTES} minutes\n"
        f"Total reports: {len(reports)} (max {MAX_SPOTS})\n"
        f"Generated at: {datetime.now(timezone.utc).isoformat()}\n"
        f"Oldest spot: {stats['oldest']}\n"
        f"Newest spot: {stats['newest']}\n"
        f"Actual span: {stats['span']}\n"
        f"{'-'*70}"
    )

    lines: List[str] = [header]
    for rpt in sorted(reports, key=lambda r: r.sort_key):
        lines.append(
            f"{rpt.timestamp}  {rpt.freq_mhz} MHz  SNR {rpt.snr:>4} dB  "
            f"TX {rpt.tx_call:>10} ({rpt.tx_grid}) -> RX {rpt.rx_call:>10} ({rpt.rx_grid})  "
            f"TX PWR {rpt.tx_power_dbm:>4} dBm  Dist {rpt.distance_km:>4} km  Azi {rpt.azimuth_deg:>3} deg"
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    reports = fetch_reports()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_file = Path(f"WSPRrocks_{BAND_LABEL}_{timestamp}.txt")
    write_reports(reports, output_file)
    print(
        f"Wrote {len(reports)} WSPR spots (band {BAND_LABEL}) "
        f"from the last {LOOKBACK_MINUTES} minutes to {output_file}"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("\nCancelled by user.")
