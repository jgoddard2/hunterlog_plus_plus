from __future__ import annotations

import math
import random
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Dict, List, Optional, Tuple

import requests
import xml.etree.ElementTree as ET

import tkinter as tk
from tkinter import ttk, messagebox


# ==============================
# Logging setup
# ==============================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("prop_kernel_tester")


# ==============================
# PSKReporter configuration
# ==============================

PSKREPORTER_URL = "https://retrieve.pskreporter.info/query"

# How far back to request data (seconds); 3600 = last hour
FLOW_START_SECONDS = -3600

# Mode to request from PSKReporter; FT8 has lots of data
PSK_MODE = "FT8"

# Maximum number of reports to retrieve
RPT_LIMIT = 5000

# ==============================
# Band definitions (Hz)
# ==============================

BAND_FREQ_RANGES_HZ: Dict[int, Tuple[float, float]] = {
    160: (1.8e6, 2.0e6),
    80:  (3.5e6, 4.0e6),
    60:  (5.3e6, 5.4e6),
    40:  (7.0e6, 7.3e6),
    30:  (10.1e6, 10.15e6),
    20:  (14.0e6, 14.35e6),
    17:  (18.068e6, 18.168e6),
    15:  (21.0e6, 21.45e6),
    12:  (24.89e6, 24.99e6),
    10:  (28.0e6, 29.7e6),
    6:   (50.0e6, 54.0e6),
}


def band_label(band_m: int) -> str:
    return f"{band_m}m"


# ==============================
# Kernel configuration
# ==============================

# Distance scale (km) – similarity in distance
D0_KM = 3000.0

# Azimuth scale (degrees) – similarity in bearing
T0_DEG = 60.0

# Minimum kernel weight to keep a report
MIN_WEIGHT = 0.01


# ==============================
# Data structures
# ==============================

@dataclass
class PropagationReport:
    """A single PSKReporter reception report with path geometry."""
    timestamp_utc: datetime
    band: str
    snr_db: float

    tx_call: str
    tx_grid: str
    tx_lat: float
    tx_lon: float

    rx_call: str
    rx_grid: str
    rx_lat: float
    rx_lon: float

    distance_km: float   # from origin to TX station
    azimuth_deg: float   # from origin to TX station


@dataclass
class SpotPath:
    """Path geometry from origin to a remote station (validation target)."""
    spot_id: int
    activator_call: str
    activator_grid: str

    distance_km: float
    azimuth_deg: float
    band: str


# ==============================
# Maidenhead locator → lat/lon
# ==============================

def locator_to_latlon(grid: str) -> Tuple[float, float]:
    """
    Convert a 2/4/6-character Maidenhead locator to (lat, lon) in degrees.
    Returns the approximate center of the square.
    """
    grid = grid.strip()
    if len(grid) < 2:
        raise ValueError(f"Grid locator too short: {grid!r}")

    grid = grid.upper()

    # Field (first 2 letters)
    lon = (ord(grid[0]) - ord("A")) * 20 - 180
    lat = (ord(grid[1]) - ord("A")) * 10 - 90

    # Square (2 digits)
    if len(grid) >= 4:
        lon += int(grid[2]) * 2
        lat += int(grid[3]) * 1
        # center of the 2x1 degree square
        lon += 1.0
        lat += 0.5
    else:
        # center of the 20x10 degree field
        lon += 10.0
        lat += 5.0

    # Subsquare (2 letters)
    if len(grid) >= 6:
        sub_lon = grid[4]
        sub_lat = grid[5]
        # 2° x 1° square subdivided into 24x24
        lon += (ord(sub_lon) - ord("A")) * (2.0 / 24.0)
        lat += (ord(sub_lat) - ord("A")) * (1.0 / 24.0)
        lon += (2.0 / 24.0) / 2.0
        lat += (1.0 / 24.0) / 2.0

    return lat, lon


# ==============================
# Geometry helpers
# ==============================

EARTH_RADIUS_KM = 6371.0


def great_circle_distance_km(lat1: float, lon1: float,
                             lat2: float, lon2: float) -> float:
    """Haversine distance in km."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (math.sin(dphi / 2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_KM * c


def calculate_bearing(lat1: float, lon1: float,
                      lat2: float, lon2: float) -> float:
    """
    Calculate initial great-circle bearing from point 1 to point 2.
    Returns bearing in degrees (0–360).
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)

    y = math.sin(dlambda) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2) -
         math.sin(phi1) * math.cos(phi2) * math.cos(dlambda))

    brng = math.degrees(math.atan2(y, x))
    return (brng + 360.0) % 360.0


# ==============================
# PSKReporter retrieval
# ==============================

def fetch_reports_for_band(origin_grid: str,
                           band_m: int,
                           window_seconds: int = 3600) -> List[PropagationReport]:
    """
    Fetch recent PSKReporter reports for a given band and map them into
    PropagationReport objects relative to the given origin grid.

    NOTE: This does a single HTTP request and may take several seconds.
    Do not run it more frequently than every ~5 minutes.
    """
    if band_m not in BAND_FREQ_RANGES_HZ:
        raise ValueError(f"Unsupported band {band_m}m")

    origin_lat, origin_lon = locator_to_latlon(origin_grid)

    f_low, f_high = BAND_FREQ_RANGES_HZ[band_m]
    frange = f"{int(f_low)}-{int(f_high)}"

    params = {
        "flowStartSeconds": -abs(window_seconds),
        "mode": PSK_MODE,
        "rptlimit": RPT_LIMIT,
        "rronly": 1,
        "noactive": 1,
        "frange": frange,
    }

    log.info("Requesting PSKReporter data: band=%sm, window=%ss", band_m, window_seconds)
    resp = requests.get(PSKREPORTER_URL, params=params, timeout=90)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)

    reports: List[PropagationReport] = []
    now = datetime.now(timezone.utc)
    start_time = now - timedelta(seconds=window_seconds)

    for rr in root.findall("receptionReport"):
        try:
            tx_call = rr.attrib.get("senderCallsign", "").upper()
            rx_call = rr.attrib.get("receiverCallsign", "").upper()
            tx_grid = rr.attrib.get("senderLocator", "")
            rx_grid = rr.attrib.get("receiverLocator", "")
            freq_str = rr.attrib.get("frequency")
            snr_str = rr.attrib.get("sNR")
            flow_str = rr.attrib.get("flowStartSeconds")

            # Require basic fields
            if not (tx_call and tx_grid and freq_str and snr_str and flow_str):
                continue

            freq_hz = float(freq_str)
            # Sanity check frequency is truly in our band
            if not (f_low <= freq_hz <= f_high):
                continue

            snr_db = float(snr_str)
            ts = datetime.fromtimestamp(int(flow_str), tz=timezone.utc)
            if ts < start_time or ts > now:
                continue

            tx_lat, tx_lon = locator_to_latlon(tx_grid)
            if rx_grid:
                rx_lat, rx_lon = locator_to_latlon(rx_grid)
            else:
                rx_lat, rx_lon = math.nan, math.nan

            distance_km = great_circle_distance_km(origin_lat, origin_lon, tx_lat, tx_lon)
            azimuth_deg = calculate_bearing(origin_lat, origin_lon, tx_lat, tx_lon)

            report = PropagationReport(
                timestamp_utc=ts,
                band=band_label(band_m),
                snr_db=snr_db,
                tx_call=tx_call,
                tx_grid=tx_grid,
                tx_lat=tx_lat,
                tx_lon=tx_lon,
                rx_call=rx_call,
                rx_grid=rx_grid,
                rx_lat=rx_lat,
                rx_lon=rx_lon,
                distance_km=distance_km,
                azimuth_deg=azimuth_deg,
            )
            reports.append(report)
        except Exception as e:
            # Ignore malformed records
            log.debug("Skipping malformed record: %s", e)
            continue

    log.info("Fetched %d usable reports for band %sm", len(reports), band_m)
    return reports


# ==============================
# Kernel smoothing algorithm
# ==============================

def spot_weight(report: PropagationReport,
                target_dist_km: float,
                target_az_deg: float) -> float:
    """
    Calculate similarity weight for a report relative to a target path.
    Uses Gaussian kernels in distance and azimuth.
    """
    # Distance similarity
    delta_d = abs(report.distance_km - target_dist_km)
    w_d = math.exp(-(delta_d / D0_KM) ** 2)

    # Azimuth similarity (handle wrap-around at 0/360)
    delta_theta_raw = abs(report.azimuth_deg - target_az_deg)
    delta_theta = min(delta_theta_raw, 360.0 - delta_theta_raw)
    w_t = math.exp(-(delta_theta / T0_DEG) ** 2)

    return w_d * w_t


def predict_snr_for_spot(spot_path: SpotPath,
                         reports: List[PropagationReport]) -> Optional[float]:
    """
    Predict SNR for a single spot using kernel smoothing over all reports.
    """
    if not reports:
        return None

    weighted_sum = 0.0
    weight_total = 0.0

    for report in reports:
        # >>> this is the line that was previously truncated <<<
        w = spot_weight(report, spot_path.distance_km, spot_path.azimuth_deg)

        if w < MIN_WEIGHT:
            continue
        weighted_sum += w * report.snr_db
        weight_total += w

    if weight_total == 0.0:
        return None

    return weighted_sum / weight_total


def classify_snr(snr: Optional[float],
                 ssb_threshold: float = 10.0,
                 digital_threshold: float = -15.0) -> str:
    """
    Classify SNR into capability categories.
    """
    if snr is None:
        return "no_data"
    if snr >= ssb_threshold:
        return "ssb"
    elif snr >= digital_threshold:
        return "digital"
    else:
        return "not_reachable"


# ==============================
# Evaluation helpers
# ==============================

def chunk_reports_into_15min(reports: List[PropagationReport],
                             window_seconds: int = 3600
                             ) -> List[Tuple[List[PropagationReport], datetime, datetime]]:
    """
    Split reports into four 15-minute chunks over the last `window_seconds`.
    Returns list of (chunk_reports, chunk_start, chunk_end) for each chunk.
    """
    if not reports:
        return []

    now = datetime.now(timezone.utc)
    window = timedelta(seconds=window_seconds)
    start_time = now - window
    chunk_len = window / 4

    chunks: List[List[PropagationReport]] = [[] for _ in range(4)]

    for r in reports:
        if r.timestamp_utc < start_time or r.timestamp_utc > now:
            continue
        offset = (r.timestamp_utc - start_time).total_seconds()
        idx = int(offset // (window_seconds / 4))
        if 0 <= idx < 4:
            chunks[idx].append(r)

    result = []
    for i in range(4):
        c_start = start_time + i * chunk_len
        c_end = c_start + chunk_len
        result.append((chunks[i], c_start, c_end))
    return result


def compute_metrics(pred_actual: List[Tuple[Optional[float], float]]) -> Dict[str, Optional[float]]:
    """
    Compute basic error metrics from list of (predicted, actual) SNR pairs.
    """
    eff = [(p, a) for (p, a) in pred_actual if p is not None]
    if not eff:
        return {
            "count": 0,
            "mae": None,
            "rmse": None,
            "bias": None,
            "max_abs": None,
        }
    errors = [p - a for (p, a) in eff]
    abs_errors = [abs(e) for e in errors]
    sq_errors = [e * e for e in errors]

    mae = mean(abs_errors)
    rmse = math.sqrt(mean(sq_errors))
    bias = mean(errors)
    max_abs = max(abs_errors)

    return {
        "count": len(eff),
        "mae": mae,
        "rmse": rmse,
        "bias": bias,
        "max_abs": max_abs,
    }


# ==============================
# GUI application
# ==============================

class PropagationKernelTesterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Hunter Log++ Propagation Tester")
        root.geometry("1000x700")

        # --- Top controls ---
        top_frame = ttk.Frame(root, padding=10)
        top_frame.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top_frame, text="Origin grid (e.g. EM73):").grid(
            row=0, column=0, sticky=tk.W, padx=5, pady=2
        )
        self.origin_entry = ttk.Entry(top_frame, width=10)
        self.origin_entry.grid(row=0, column=1, sticky=tk.W, padx=5, pady=2)
        self.origin_entry.insert(0, "EM73")

        ttk.Label(top_frame, text="Band (m):").grid(
            row=0, column=2, sticky=tk.W, padx=5, pady=2
        )
        self.band_var = tk.StringVar(value="20")
        band_values = ["160", "80", "60", "40", "30", "20", "17", "15", "12", "10", "6"]
        self.band_combo = ttk.Combobox(
            top_frame,
            textvariable=self.band_var,
            values=band_values,
            width=5,
            state="readonly",
        )
        self.band_combo.grid(row=0, column=3, sticky=tk.W, padx=5, pady=2)
        self.band_combo.current(band_values.index("20"))

        self.test_button = ttk.Button(top_frame, text="Run Test", command=self.on_run_test)
        self.test_button.grid(row=0, column=4, sticky=tk.W, padx=10, pady=2)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(top_frame, textvariable=self.status_var).grid(
            row=1, column=0, columnspan=5, sticky=tk.W, padx=5, pady=2
        )

        # --- Results table ---
        table_frame = ttk.Frame(root, padding=(10, 0, 10, 5))
        table_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        cols = ("chunk", "grid", "pred_snr", "actual_snr", "abs_diff", "rel_diff")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=15)
        for col in cols:
            self.tree.heading(col, text=col)
        self.tree.column("chunk", width=70, anchor=tk.CENTER)
        self.tree.column("grid", width=80, anchor=tk.CENTER)
        self.tree.column("pred_snr", width=100, anchor=tk.E)
        self.tree.column("actual_snr", width=100, anchor=tk.E)
        self.tree.column("abs_diff", width=100, anchor=tk.E)
        self.tree.column("rel_diff", width=100, anchor=tk.E)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # --- Metrics text ---
        metrics_frame = ttk.Frame(root, padding=(10, 0, 10, 10))
        metrics_frame.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=False)

        ttk.Label(metrics_frame, text="Chunk / Overall metrics:").pack(anchor=tk.W)
        self.metrics_text = tk.Text(metrics_frame, height=10, wrap="word")
        self.metrics_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        metrics_scroll = ttk.Scrollbar(
            metrics_frame, orient="vertical", command=self.metrics_text.yview
        )
        self.metrics_text.configure(yscrollcommand=metrics_scroll.set)
        metrics_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    # ------------------------------
    # Test runner
    # ------------------------------
    def on_run_test(self):
        origin = self.origin_entry.get().strip().upper()
        if not origin:
            messagebox.showerror("Error", "Please enter an origin grid square.")
            return
        band_str = self.band_var.get().strip()
        try:
            band_m = int(band_str)
        except ValueError:
            messagebox.showerror("Error", f"Invalid band: {band_str}")
            return

        if band_m not in BAND_FREQ_RANGES_HZ:
            messagebox.showerror("Error", f"Unsupported band: {band_m}m")
            return

        self.status_var.set("Fetching PSKReporter data (this may take up to a minute)...")
        self.root.update_idletasks()
        self.test_button.configure(state=tk.DISABLED)

        try:
            reports = fetch_reports_for_band(origin, band_m, window_seconds=3600)
        except Exception as e:
            log.exception("Error fetching PSKReporter data")
            messagebox.showerror("Error", f"Failed to fetch PSKReporter data:\n{e}")
            self.status_var.set("Error fetching data.")
            self.test_button.configure(state=tk.NORMAL)
            return

        if not reports:
            messagebox.showinfo(
                "No Data",
                "No usable PSKReporter reports found for that band and time window.",
            )
            self.status_var.set("No data.")
            self.test_button.configure(state=tk.NORMAL)
            return

        # Clear previous results
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)
        self.metrics_text.delete("1.0", tk.END)

        # Chunk into 4×15min
        chunks = chunk_reports_into_15min(reports, window_seconds=3600)

        all_pred_actual: List[Tuple[Optional[float], float]] = []
        chunk_metrics_text_lines: List[str] = []

        train_fraction = 0.7  # 70% train, 30% validation

        for idx, (chunk_reports, c_start, c_end) in enumerate(chunks, start=1):
            if not chunk_reports:
                chunk_metrics_text_lines.append(
                    f"Chunk {idx}: {c_start.strftime('%H:%M')}–{c_end.strftime('%H:%M')} UTC: no reports."
                )
                continue

            # Shuffle and split into train/validation
            chunk_reports_sorted = sorted(chunk_reports, key=lambda r: r.timestamp_utc)
            random.shuffle(chunk_reports_sorted)
            n = len(chunk_reports_sorted)
            n_train = max(1, int(n * train_fraction))
            n_val = max(0, n - n_train)

            train_reports = chunk_reports_sorted[:n_train]
            val_reports = chunk_reports_sorted[n_train:]

            if n_val == 0:
                chunk_metrics_text_lines.append(
                    f"Chunk {idx}: {c_start.strftime('%H:%M')}–{c_end.strftime('%H:%M')} UTC: "
                    f"{n} reports, but none left for validation after split."
                )
                continue

            pred_actual_pairs: List[Tuple[Optional[float], float]] = []

            for j, val_r in enumerate(val_reports):
                spot_path = SpotPath(
                    spot_id=j,
                    activator_call=val_r.tx_call,
                    activator_grid=val_r.tx_grid,
                    distance_km=val_r.distance_km,
                    azimuth_deg=val_r.azimuth_deg,
                    band=val_r.band,
                )
                pred = predict_snr_for_spot(spot_path, train_reports)
                actual = val_r.snr_db
                pred_actual_pairs.append((pred, actual))
                all_pred_actual.append((pred, actual))

                # Compute diffs for display
                if pred is None:
                    abs_diff = None
                    rel_diff = None
                else:
                    abs_diff = abs(pred - actual)
                    if abs(actual) >= 1.0:
                        rel_diff = abs_diff / abs(actual)
                    else:
                        rel_diff = None

                # Insert row in table: show chunk index, station grid, predicted, actual, abs, rel
                self.tree.insert(
                    "",
                    "end",
                    values=(
                        f"C{idx}",
                        val_r.tx_grid,
                        "" if pred is None else f"{pred:6.1f}",
                        f"{actual:6.1f}",
                        "" if abs_diff is None else f"{abs_diff:6.1f}",
                        "" if rel_diff is None else f"{rel_diff*100:6.1f}%",
                    ),
                )

            # Metrics for this chunk
            m = compute_metrics(pred_actual_pairs)
            if m["count"] == 0:
                chunk_metrics_text_lines.append(
                    f"Chunk {idx}: {c_start.strftime('%H:%M')}–{c_end.strftime('%H:%M')} UTC: "
                    f"{len(chunk_reports)} reports, 0 with predictions."
                )
            else:
                chunk_metrics_text_lines.append(
                    f"Chunk {idx}: {c_start.strftime('%H:%M')}–{c_end.strftime('%H:%M')} UTC: "
                    f"N_val={m['count']}, MAE={m['mae']:.2f} dB, RMSE={m['rmse']:.2f} dB, "
                    f"Bias={m['bias']:.2f} dB, Max|err|={m['max_abs']:.2f} dB."
                )

        # Overall metrics
        overall = compute_metrics(all_pred_actual)
        self.metrics_text.insert(
            tk.END,
            "Per-chunk metrics:\n" + "\n".join(chunk_metrics_text_lines) + "\n\n",
        )

        if overall["count"] == 0:
            self.metrics_text.insert(tk.END, "Overall: no predictions computed.\n")
        else:
            self.metrics_text.insert(
                tk.END,
                (
                    f"Overall (all chunks combined): N_val={overall['count']}, "
                    f"MAE={overall['mae']:.2f} dB, RMSE={overall['rmse']:.2f} dB, "
                    f"Bias={overall['bias']:.2f} dB, Max|err|={overall['max_abs']:.2f} dB.\n"
                ),
            )

        self.metrics_text.see(tk.END)
        self.status_var.set("Done.")
        self.test_button.configure(state=tk.NORMAL)


def main():
    root = tk.Tk()
    app = PropagationKernelTesterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
