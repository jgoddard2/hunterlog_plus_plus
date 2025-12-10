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

# How far back to request data (seconds); 3600 = "last hour"
# (We still may only get the last ~15 min on a busy band due to rptlimit.)
DEFAULT_WINDOW_SECONDS = 3600

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
# Kernel default parameters
# ==============================

# Distance scale (km)
DEFAULT_D0_KM = 3000.0

# Azimuth scale (degrees)
DEFAULT_T0_DEG = 60.0

# Minimum kernel weight
DEFAULT_MIN_WEIGHT = 0.01


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
                           window_seconds: int = DEFAULT_WINDOW_SECONDS
                           ) -> List[PropagationReport]:
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
            # Optional clipping to reduce crazy outliers
            snr_db = max(min(snr_db, 40.0), -30.0)

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
                target_az_deg: float,
                d0_km: float,
                t0_deg: float) -> float:
    """
    Calculate similarity weight for a report relative to a target path.
    Uses Gaussian kernels in distance and azimuth.
    """
    # Distance similarity
    delta_d = abs(report.distance_km - target_dist_km)
    w_d = math.exp(-(delta_d / d0_km) ** 2) if d0_km > 0 else 0.0

    # Azimuth similarity (handle wrap-around at 0/360)
    delta_theta_raw = abs(report.azimuth_deg - target_az_deg)
    delta_theta = min(delta_theta_raw, 360.0 - delta_theta_raw)
    w_t = math.exp(-(delta_theta / t0_deg) ** 2) if t0_deg > 0 else 0.0

    return w_d * w_t


def predict_snr_for_spot(spot_path: SpotPath,
                         reports: List[PropagationReport],
                         d0_km: float,
                         t0_deg: float,
                         min_weight: float) -> Optional[float]:
    """
    Predict SNR for a single spot using kernel smoothing over all reports.
    """
    if not reports:
        return None

    weighted_sum = 0.0
    weight_total = 0.0
    strong_neighbors = 0

    for report in reports:
        w = spot_weight(report, spot_path.distance_km, spot_path.azimuth_deg,
                        d0_km, t0_deg)
        if w < min_weight:
            continue
        strong_neighbors += 1
        weighted_sum += w * report.snr_db
        weight_total += w

    # Require at least a few decent neighbors
    if strong_neighbors < 5 or weight_total <= 0.0:
        return None

    return weighted_sum / weight_total


# ==============================
# Metrics
# ==============================

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
# Evaluation helpers
# ==============================

def evaluate_params_with_new_split(
    reports: List[PropagationReport],
    d0_km: float,
    t0_deg: float,
    min_weight: float,
    train_fraction: float = 0.7,
    sample_max: Optional[int] = None,
    need_details: bool = False,
) -> Tuple[Dict[str, Optional[float]],
           Optional[List[Tuple[PropagationReport, Optional[float], float, Optional[float], Optional[float]]]]]:
    """
    Use a fresh random 70/30 split of the provided reports to evaluate parameters.

    Returns:
        metrics dict,
        and (if need_details=True) a list of tuples
        (report, pred, actual, abs_diff, rel_diff).
    """
    if not reports:
        return compute_metrics([]), [] if need_details else None

    data = list(reports)
    if sample_max is not None and len(data) > sample_max:
        data = random.sample(data, sample_max)

    random.shuffle(data)
    n = len(data)
    if n < 10:
        return compute_metrics([]), [] if need_details else None

    n_train = max(1, int(n * train_fraction))
    n_val = max(1, n - n_train)
    if n_train + n_val > n:
        n_val = n - n_train

    train_reports = data[:n_train]
    val_reports = data[n_train:n_train + n_val]

    pred_actual_pairs: List[Tuple[Optional[float], float]] = []
    details: List[Tuple[PropagationReport, Optional[float], float, Optional[float], Optional[float]]] = []

    for j, val_r in enumerate(val_reports):
        spot_path = SpotPath(
            spot_id=j,
            activator_call=val_r.tx_call,
            activator_grid=val_r.tx_grid,
            distance_km=val_r.distance_km,
            azimuth_deg=val_r.azimuth_deg,
            band=val_r.band,
        )
        pred = predict_snr_for_spot(spot_path, train_reports, d0_km, t0_deg, min_weight)
        actual = val_r.snr_db
        pred_actual_pairs.append((pred, actual))

        if need_details:
            if pred is None:
                abs_diff = None
                rel_diff = None
            else:
                abs_diff = abs(pred - actual)
                if abs(actual) >= 1.0:
                    rel_diff = abs_diff / abs(actual)
                else:
                    rel_diff = None
            details.append((val_r, pred, actual, abs_diff, rel_diff))

    metrics = compute_metrics(pred_actual_pairs)
    return metrics, details if need_details else None


def evaluate_params_fixed_split(
    train_reports: List[PropagationReport],
    val_reports: List[PropagationReport],
    d0_km: float,
    t0_deg: float,
    min_weight: float,
) -> Dict[str, Optional[float]]:
    """Evaluate parameters using a fixed train/validation split."""
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
        pred = predict_snr_for_spot(spot_path, train_reports, d0_km, t0_deg, min_weight)
        pred_actual_pairs.append((pred, val_r.snr_db))

    return compute_metrics(pred_actual_pairs)


# ==============================
# GUI application
# ==============================

class PropagationKernelTesterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Hunter Log++ Propagation Tester")
        root.geometry("1100x750")

        self.last_reports: Optional[List[PropagationReport]] = None

        # --- Top controls ---
        top_frame = ttk.Frame(root, padding=10)
        top_frame.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top_frame, text="Origin grid (e.g. EM73):").grid(
            row=0, column=0, sticky=tk.W, padx=5, pady=2
        )
        self.origin_entry = ttk.Entry(top_frame, width=10)
        self.origin_entry.grid(row=0, column=1, sticky=tk.W, padx=5, pady=2)
        self.origin_entry.insert(0, "EM62")

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

        self.optimize_button = ttk.Button(
            top_frame, text="Optimize Params", command=self.on_optimize_params
        )
        self.optimize_button.grid(row=0, column=5, sticky=tk.W, padx=10, pady=2)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(top_frame, textvariable=self.status_var).grid(
            row=1, column=0, columnspan=6, sticky=tk.W, padx=5, pady=2
        )

        # --- Kernel parameter sliders ---
        param_frame = ttk.LabelFrame(root, text="Kernel Parameters", padding=10)
        param_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 5))

        self.d0_var = tk.DoubleVar(value=DEFAULT_D0_KM)
        self.t0_var = tk.DoubleVar(value=DEFAULT_T0_DEG)
        self.minw_var = tk.DoubleVar(value=DEFAULT_MIN_WEIGHT)

        # Use tk.Scale for simplicity
        self.d0_scale = tk.Scale(
            param_frame,
            from_=500,
            to=6000,
            resolution=100,
            orient=tk.HORIZONTAL,
            label="Distance scale D0 (km)",
            variable=self.d0_var,
            length=400,
        )
        self.d0_scale.grid(row=0, column=0, columnspan=3, sticky="we", padx=5)

        self.t0_scale = tk.Scale(
            param_frame,
            from_=10,
            to=120,
            resolution=5,
            orient=tk.HORIZONTAL,
            label="Azimuth scale T0 (degrees)",
            variable=self.t0_var,
            length=400,
        )
        self.t0_scale.grid(row=1, column=0, columnspan=3, sticky="we", padx=5)

        self.minw_scale = tk.Scale(
            param_frame,
            from_=0.0,
            to=0.1,
            resolution=0.005,
            orient=tk.HORIZONTAL,
            label="Min weight",
            variable=self.minw_var,
            length=400,
        )
        self.minw_scale.grid(row=2, column=0, columnspan=3, sticky="we", padx=5)

        # --- Results table ---
        table_frame = ttk.Frame(root, padding=(10, 0, 10, 5))
        table_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        cols = ("call", "grid", "pred_snr", "actual_snr", "abs_diff", "rel_diff")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=14)
        for col in cols:
            self.tree.heading(col, text=col)
        self.tree.column("call", width=100, anchor=tk.CENTER)
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

        ttk.Label(metrics_frame, text="Metrics / Optimization log:").pack(anchor=tk.W)
        self.metrics_text = tk.Text(metrics_frame, height=10, wrap="word")
        self.metrics_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        metrics_scroll = ttk.Scrollbar(
            metrics_frame, orient="vertical", command=self.metrics_text.yview
        )
        self.metrics_text.configure(yscrollcommand=metrics_scroll.set)
        metrics_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    # ------------------------------
    # Common helpers
    # ------------------------------
    def _get_origin_and_band(self) -> Optional[Tuple[str, int]]:
        origin = self.origin_entry.get().strip().upper()
        if not origin:
            messagebox.showerror("Error", "Please enter an origin grid square.")
            return None
        band_str = self.band_var.get().strip()
        try:
            band_m = int(band_str)
        except ValueError:
            messagebox.showerror("Error", f"Invalid band: {band_str}")
            return None
        if band_m not in BAND_FREQ_RANGES_HZ:
            messagebox.showerror("Error", f"Unsupported band: {band_m}m")
            return None
        return origin, band_m

    def _fetch_reports(self, origin: str, band_m: int) -> Optional[List[PropagationReport]]:
        self.status_var.set("Fetching PSKReporter data (may take up to a minute)...")
        self.root.update_idletasks()
        try:
            reports = fetch_reports_for_band(origin, band_m, window_seconds=DEFAULT_WINDOW_SECONDS)
        except Exception as e:
            log.exception("Error fetching PSKReporter data")
            messagebox.showerror("Error", f"Failed to fetch PSKReporter data:\n{e}")
            self.status_var.set("Error fetching data.")
            return None
        if not reports:
            messagebox.showinfo(
                "No Data",
                "No usable PSKReporter reports found for that band and time window.",
            )
            self.status_var.set("No data.")
            return None
        self.last_reports = reports
        return reports

    # ------------------------------
    # Run Test button
    # ------------------------------
    def on_run_test(self):
        res = self._get_origin_and_band()
        if res is None:
            return
        origin, band_m = res

        reports = self._fetch_reports(origin, band_m)
        if reports is None:
            return

        # Clear previous results
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)
        self.metrics_text.delete("1.0", tk.END)

        d0 = float(self.d0_var.get())
        t0 = float(self.t0_var.get())
        minw = float(self.minw_var.get())

        self.status_var.set(
            f"Evaluating kernel (D0={d0:.0f} km, T0={t0:.0f}°, minw={minw:.3f})..."
        )
        self.root.update_idletasks()

        metrics, details = evaluate_params_with_new_split(
            reports, d0, t0, minw,
            train_fraction=0.7,
            sample_max=None,         # use all reports for the test run
            need_details=True,
        )

        # Fill table with validation entries
        if details:
            for (r, pred, actual, abs_diff, rel_diff) in details:
                self.tree.insert(
                    "",
                    "end",
                    values=(
                        r.tx_call,
                        r.tx_grid,
                        "" if pred is None else f"{pred:6.1f}",
                        f"{actual:6.1f}",
                        "" if abs_diff is None else f"{abs_diff:6.1f}",
                        "" if rel_diff is None else f"{rel_diff*100:6.1f}%",
                    ),
                )

        # Show metrics
        if metrics["count"] == 0:
            self.metrics_text.insert(
                tk.END,
                "No predictions could be made with the current parameters.\n"
            )
        else:
            self.metrics_text.insert(
                tk.END,
                (
                    f"Kernel evaluation (D0={d0:.0f} km, T0={t0:.0f}°, minw={minw:.3f})\n"
                    f"  N_val={metrics['count']}, "
                    f"MAE={metrics['mae']:.2f} dB, RMSE={metrics['rmse']:.2f} dB,\n"
                    f"  Bias={metrics['bias']:.2f} dB, Max|err|={metrics['max_abs']:.2f} dB.\n"
                ),
            )
        self.metrics_text.see(tk.END)
        self.status_var.set("Done.")

    # ------------------------------
    # Optimize Params button
    # ------------------------------
    def on_optimize_params(self):
        res = self._get_origin_and_band()
        if res is None:
            return
        origin, band_m = res

        # Use already-fetched reports if available and same band/origin.
        # For simplicity we just refetch here.
        reports = self._fetch_reports(origin, band_m)
        if reports is None:
            return

        # Limit sample size for optimization to keep it fast.
        data = list(reports)
        max_opt_samples = 2000
        if len(data) > max_opt_samples:
            data = random.sample(data, max_opt_samples)

        random.shuffle(data)
        n = len(data)
        if n < 50:
            messagebox.showinfo(
                "Not enough data",
                "Not enough reports to perform parameter optimization."
            )
            return

        train_fraction = 0.7
        n_train = max(1, int(n * train_fraction))
        n_val = max(1, n - n_train)
        if n_train + n_val > n:
            n_val = n - n_train
        train_reports = data[:n_train]
        val_reports = data[n_train:n_train + n_val]

        current_minw = float(self.minw_var.get())

        # Baseline: always predict mean training SNR
        baseline_mean = mean(r.snr_db for r in train_reports)
        baseline_pairs = [(baseline_mean, r.snr_db) for r in val_reports]
        baseline_metrics = compute_metrics(baseline_pairs)

        # Default parameters metrics (using same fixed split)
        default_metrics = evaluate_params_fixed_split(
            train_reports, val_reports,
            DEFAULT_D0_KM, DEFAULT_T0_DEG, current_minw
        )

        # Grid search over D0 & T0; keep min_weight fixed
        D0_candidates = [1000.0, 2000.0, 3000.0, 5000.0]
        T0_candidates = [20.0, 40.0, 60.0, 90.0, 120.0]

        best_rmse = float("inf")
        best_d0 = DEFAULT_D0_KM
        best_t0 = DEFAULT_T0_DEG
        best_metrics: Optional[Dict[str, Optional[float]]] = None

        total_combos = len(D0_candidates) * len(T0_candidates)
        combo_idx = 0

        self.metrics_text.insert(
            tk.END,
            "Starting parameter optimization (fixed train/validation split)...\n"
        )
        self.metrics_text.see(tk.END)

        for d0 in D0_candidates:
            for t0 in T0_candidates:
                combo_idx += 1
                self.status_var.set(
                    f"Optimizing ({combo_idx}/{total_combos}) "
                    f"D0={d0:.0f}, T0={t0:.0f}..."
                )
                self.root.update_idletasks()

                m = evaluate_params_fixed_split(
                    train_reports, val_reports,
                    d0, t0, current_minw
                )
                if m["rmse"] is not None and m["rmse"] < best_rmse:
                    best_rmse = m["rmse"]
                    best_d0 = d0
                    best_t0 = t0
                    best_metrics = m

        self.status_var.set("Optimization complete.")
        self.metrics_text.insert(
            tk.END,
            "\nOptimization results (same train/validation split used for all):\n"
        )

        self.metrics_text.insert(
            tk.END,
            (
                f"Baseline (predict mean SNR): N_val={baseline_metrics['count']}, "
                f"MAE={baseline_metrics['mae']:.2f}, RMSE={baseline_metrics['rmse']:.2f}\n"
            )
        )
        self.metrics_text.insert(
            tk.END,
            (
                f"Default kernel (D0={DEFAULT_D0_KM:.0f}, T0={DEFAULT_T0_DEG:.0f}): "
                f"N_val={default_metrics['count']}, "
                f"MAE={default_metrics['mae']:.2f}, RMSE={default_metrics['rmse']:.2f}, "
                f"Bias={default_metrics['bias']:.2f}, "
                f"Max|err|={default_metrics['max_abs']:.2f}\n"
            )
        )

        if best_metrics is None:
            self.metrics_text.insert(
                tk.END,
                "No improved parameters were found.\n"
            )
        else:
            self.metrics_text.insert(
                tk.END,
                (
                    f"Best kernel (D0={best_d0:.0f}, T0={best_t0:.0f}, "
                    f"minw={current_minw:.3f}): "
                    f"N_val={best_metrics['count']}, "
                    f"MAE={best_metrics['mae']:.2f}, RMSE={best_metrics['rmse']:.2f}, "
                    f"Bias={best_metrics['bias']:.2f}, "
                    f"Max|err|={best_metrics['max_abs']:.2f}\n"
                )
            )

            # Update sliders to best values
            self.d0_var.set(best_d0)
            self.t0_var.set(best_t0)

        self.metrics_text.see(tk.END)


def main():
    root = tk.Tk()
    app = PropagationKernelTesterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
