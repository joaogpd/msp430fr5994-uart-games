"""
Unit-aware CCS 12.5 EnergyTrace CSV parser, time-weighted statistics engine,
battery/supercapacitor lifetime projector, and 300 DPI IEEEtran figure generator
for mspgame power benchmarking.

Dependencies: Python stdlib + numpy + matplotlib (Agg backend, no scipy).
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import re
import tempfile
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# NumPy 1.x (trapz) / 2.x (trapezoid) compatibility
_trapz = getattr(np, "trapezoid", getattr(np, "trapz", None))

# Hardware & energy storage constants (SLASE54 / SLAU678)
CR2032_CAPACITY_MAH = 220.0
CR2032_NOMINAL_V = 3.0
CR2032_ENERGY_J = (CR2032_CAPACITY_MAH * 1e-3) * CR2032_NOMINAL_V * 3600.0  # 2376.0 J

C1_CAPACITANCE_F = 0.22
C1_V_MAX = 3.3
C1_V_MIN = 1.8
C1_TOTAL_ENERGY_J = 0.5 * C1_CAPACITANCE_F * (C1_V_MAX ** 2)  # 1.1979 J
C1_USABLE_ENERGY_J = 0.5 * C1_CAPACITANCE_F * (C1_V_MAX ** 2 - C1_V_MIN ** 2)  # 0.8415 J
C1_USABLE_CHARGE_C = C1_CAPACITANCE_F * (C1_V_MAX - C1_V_MIN)  # 0.33 C

CORE_PROFILES: Tuple[Tuple[str, str], ...] = (
    ("idle", "Idle\n(Connected)"),
    ("play", "Active Play\n(1 key/s)"),
    ("ai_heuristic", "AI Demo:\nHeuristic A"),
    ("ai_mlp", "AI Demo:\nTiny-MLP B"),
    ("low_hp", "Low-HP\nAlert"),
)

OPTIONAL_PROFILES: Tuple[Tuple[str, str], ...] = (
    ("disconnected", "Disconnected\nStandby"),
)

_COL_RE = re.compile(r"^(?P<name>[a-zA-Z_ ]+?)\s*(?:\((?P<unit>[a-zA-Z]+)\))?$")

_TIME_SCALE_TO_S = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0}
_CURR_SCALE_TO_UA = {"na": 1e-3, "ua": 1.0, "ma": 1e3, "a": 1e6}
_VOLT_SCALE_TO_V = {"mv": 1e-3, "v": 1.0}
_ENER_SCALE_TO_MJ = {"nj": 1e-6, "uj": 1e-3, "mj": 1.0, "j": 1e3}
_PWR_SCALE_TO_UW = {"nw": 1e-3, "uw": 1.0, "mw": 1e3, "w": 1e6}


@dataclass
class EnergyTraceData:
    path: str
    time_s: np.ndarray
    current_uA: np.ndarray
    voltage_V: np.ndarray
    energy_mJ: np.ndarray
    has_energy_col: bool
    has_dsr_col: bool
    dropped_non_monotonic: int
    raw_headers: List[str]


@dataclass
class TraceStats:
    file_path: str
    sample_count: int
    duration_s: float
    total_energy_mJ: float
    mean_power_uW: float
    vi_mean_power_uW: float
    power_discrepancy_pct: float
    mean_voltage_V: float
    tw_mean_current_uA: float
    ev_mean_current_uA: float
    sample_unweighted_mean_uA: float
    tw_median_current_uA: float
    tw_p95_current_uA: float
    min_current_uA: float
    max_current_uA: float
    has_dsr_col: bool
    cr2032_hours_current: float
    cr2032_days_current: float
    cr2032_hours_energy: float
    cr2032_days_energy: float
    c1_seconds_power: float
    c1_minutes_power: float
    c1_hours_power: float
    c1_seconds_current: float
    c1_minutes_current: float
    c1_hours_current: float


def _parse_header_col(raw_col: str) -> Tuple[str, str]:
    cleaned = raw_col.strip().lstrip("\ufeff")
    m = _COL_RE.match(cleaned)
    if not m:
        return cleaned.lower(), ""
    name = m.group("name").strip().lower()
    unit = (m.group("unit") or "").strip().lower()
    return name, unit


def _detect_csv_delimiter(sample_lines: List[str]) -> str:
    """Auto-detect ',' vs ';' CSV delimiter from header/sample lines."""
    cleaned_lines = [ln.lstrip("\ufeff") for ln in sample_lines if ln.strip()]
    if not cleaned_lines:
        return ","

    header = cleaned_lines[0].strip()
    cols_comma = next(csv.reader([header], delimiter=","))
    cols_semi = next(csv.reader([header], delimiter=";"))
    if len(cols_semi) > len(cols_comma):
        return ";"
    if len(cols_comma) > len(cols_semi):
        return ","

    try:
        dialect = csv.Sniffer().sniff("\n".join(cleaned_lines), delimiters=",;")
        if dialect.delimiter in (",", ";"):
            return dialect.delimiter
    except csv.Error:
        pass
    return ","


def parse_energytrace_csv(csv_path: Path) -> EnergyTraceData:
    """Parse a CCS 12.5 EnergyTrace Raw Data CSV with unit normalization and validation."""
    if not csv_path.exists():
        raise FileNotFoundError(f"EnergyTrace CSV not found: {csv_path}")

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        sample_lines: List[str] = []
        for _ in range(5):
            ln = f.readline()
            if not ln:
                break
            sample_lines.append(ln)
        if not any(ln.strip() for ln in sample_lines):
            raise ValueError(f"Empty CSV file: {csv_path}")

        delimiter = _detect_csv_delimiter(sample_lines)
        reader = csv.reader(itertools.chain(sample_lines, f), delimiter=delimiter)
        try:
            headers = next(reader)
            while not headers or not any(cell.strip() for cell in headers):
                headers = next(reader)
        except StopIteration as exc:
            raise ValueError(f"Empty CSV file: {csv_path}") from exc

        parsed_cols = [_parse_header_col(h) for h in headers]
        col_names = [c[0] for c in parsed_cols]

        # Explicitly reject EnergyTrace summary exports (exportProfileToCSV)
        if "item" in col_names and "runtime" in col_names:
            raise ValueError(
                f"{csv_path}: Detected 3-column EnergyTrace summary export ({headers}). "
                "Export **Raw Data** CSV ('Time (ns),Current (nA),Voltage (mV),Energy (uJ)') instead."
            )

        idx_time = idx_curr = idx_volt = idx_ener = idx_pwr = idx_dsr = None
        time_scale = 1e-9
        curr_scale = 1e-3
        volt_scale = 1e-3
        ener_scale = 1e-3
        pwr_scale = 1.0
        has_dsr = False

        for i, (name, unit) in enumerate(parsed_cols):
            if name == "dsr":
                has_dsr = True
                if idx_dsr is None:
                    idx_dsr = i
            elif name == "time" and idx_time is None:
                idx_time = i
                if unit:
                    if unit not in _TIME_SCALE_TO_S:
                        raise ValueError(f"{csv_path}: Unsupported Time unit '{unit}'")
                    time_scale = _TIME_SCALE_TO_S[unit]
                else:
                    time_scale = 1.0
            elif name == "current" and idx_curr is None:
                idx_curr = i
                if unit:
                    if unit not in _CURR_SCALE_TO_UA:
                        raise ValueError(f"{csv_path}: Unsupported Current unit '{unit}'")
                    curr_scale = _CURR_SCALE_TO_UA[unit]
                else:
                    curr_scale = 1.0
            elif name == "voltage" and idx_volt is None:
                idx_volt = i
                if unit:
                    if unit not in _VOLT_SCALE_TO_V:
                        raise ValueError(f"{csv_path}: Unsupported Voltage unit '{unit}'")
                    volt_scale = _VOLT_SCALE_TO_V[unit]
                else:
                    volt_scale = 1.0
            elif name == "energy" and idx_ener is None:
                idx_ener = i
                if unit:
                    if unit not in _ENER_SCALE_TO_MJ:
                        raise ValueError(f"{csv_path}: Unsupported Energy unit '{unit}'")
                    ener_scale = _ENER_SCALE_TO_MJ[unit]
                else:
                    ener_scale = 1.0
            elif name == "power" and idx_pwr is None:
                idx_pwr = i
                if unit:
                    if unit not in _PWR_SCALE_TO_UW:
                        raise ValueError(f"{csv_path}: Unsupported Power unit '{unit}'")
                    pwr_scale = _PWR_SCALE_TO_UW[unit]
                else:
                    pwr_scale = 1.0

        if idx_time is None or idx_curr is None:
            raise ValueError(
                f"{csv_path}: Missing required Time/Current columns in header {headers}"
            )

        if has_dsr:
            warnings.warn(
                f"WARNING: DSR column detected in {csv_path.name} -- capture appears to be "
                "EnergyTrace++, which adds ~150 uA JTAG overhead!",
                UserWarning,
                stacklevel=2,
            )

        present_indices = [
            idx for idx in (idx_time, idx_curr, idx_volt, idx_ener, idx_pwr, idx_dsr) if idx is not None
        ]
        min_row_len = max(present_indices) + 1

        t_vals: List[float] = []
        i_vals: List[float] = []
        v_vals: List[float] = []
        e_vals: List[float] = []
        p_vals: List[float] = []
        e_valid_vals: List[bool] = []
        p_valid_vals: List[bool] = []
        skipped_incomplete = 0

        for row in reader:
            if not row:
                continue
            if len(row) < min_row_len:
                skipped_incomplete += 1
                continue
            try:
                t_s = float(row[idx_time]) * time_scale
                i_ua = float(row[idx_curr]) * curr_scale
                v_v = (
                    float(row[idx_volt]) * volt_scale
                    if (idx_volt is not None and idx_volt < len(row) and row[idx_volt].strip())
                    else 3.3
                )
                if idx_ener is not None and idx_ener < len(row) and row[idx_ener].strip():
                    e_mj = float(row[idx_ener]) * ener_scale
                    has_row_e = True
                else:
                    e_mj = 0.0
                    has_row_e = False

                if idx_pwr is not None and idx_pwr < len(row) and row[idx_pwr].strip():
                    p_uw = float(row[idx_pwr]) * pwr_scale
                    has_row_p = True
                else:
                    p_uw = 0.0
                    has_row_p = False
            except ValueError:
                continue
            t_vals.append(t_s)
            i_vals.append(i_ua)
            v_vals.append(v_v)
            e_vals.append(e_mj)
            p_vals.append(p_uw)
            e_valid_vals.append(has_row_e)
            p_valid_vals.append(has_row_p)

    if skipped_incomplete > 0:
        warnings.warn(
            f"{csv_path.name}: Skipped {skipped_incomplete} incomplete/ragged CSV row(s).",
            UserWarning,
            stacklevel=2,
        )

    if len(t_vals) < 2:
        raise ValueError(f"{csv_path}: Need at least 2 valid data rows, found {len(t_vals)}")

    t_arr = np.asarray(t_vals, dtype=np.float64)
    i_arr = np.asarray(i_vals, dtype=np.float64)
    v_arr = np.asarray(v_vals, dtype=np.float64)
    e_arr = np.asarray(e_vals, dtype=np.float64)
    p_arr = np.asarray(p_vals, dtype=np.float64)
    e_valid_arr = np.asarray(e_valid_vals, dtype=bool)
    p_valid_arr = np.asarray(p_valid_vals, dtype=bool)

    # Filter strictly monotonic increasing timestamps and shift t[0] -> 0
    keep = np.ones(len(t_arr), dtype=bool)
    last_t = t_arr[0]
    for k in range(1, len(t_arr)):
        if t_arr[k] <= last_t:
            keep[k] = False
        else:
            last_t = t_arr[k]
    dropped = int(np.size(keep) - np.count_nonzero(keep))
    t_arr = t_arr[keep] - t_arr[keep][0]
    i_arr = i_arr[keep]
    v_arr = v_arr[keep]
    e_arr = e_arr[keep]
    p_arr = p_arr[keep]
    e_valid_arr = e_valid_arr[keep]
    p_valid_arr = p_valid_arr[keep]

    if len(t_arr) < 2 or t_arr[-1] <= 0.0:
        raise ValueError(f"{csv_path}: Non-positive capture duration after monotonic filtering")

    has_energy = (idx_ener is not None) and bool(np.any(e_valid_arr))
    if not has_energy:
        if idx_pwr is not None and bool(np.all(p_valid_arr)):
            # Cumulative trapezoidal integral of Power(t) -> mJ (total equals _trapz(p_uw, t_s) * 1e-3)
            p_mw = p_arr * 1e-3
        else:
            # Cumulative trapezoidal integral of V(t)*I(t) -> mJ
            p_mw = v_arr * (i_arr * 1e-3)
        dt = np.diff(t_arr)
        de = 0.5 * (p_mw[:-1] + p_mw[1:]) * dt
        e_arr = np.concatenate(([0.0], np.cumsum(de)))

    dur = float(t_arr[-1])
    if dur < 25.0 or dur > 35.0:
        warnings.warn(
            f"{csv_path.name}: Capture duration is {dur:.2f} s (expected ~30.0 s).",
            UserWarning,
            stacklevel=2,
        )

    return EnergyTraceData(
        path=str(csv_path),
        time_s=t_arr,
        current_uA=i_arr,
        voltage_V=v_arr,
        energy_mJ=e_arr,
        has_energy_col=has_energy,
        has_dsr_col=has_dsr,
        dropped_non_monotonic=dropped,
        raw_headers=[h.strip().lstrip("\ufeff") for h in headers],
    )


def _time_weighted_quantile(values: np.ndarray, dt: np.ndarray, q: float) -> float:
    """Compute time-weighted quantile (0 <= q <= 1) over interval durations dt."""
    if len(values) == 0:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    # Pair each interval dt[k] with interval midpoint current
    mid_vals = 0.5 * (values[:-1] + values[1:])
    order = np.argsort(mid_vals, kind="mergesort")
    sorted_vals = mid_vals[order]
    sorted_dt = dt[order]
    total_dt = float(np.sum(sorted_dt))
    if total_dt <= 0.0:
        return float(np.quantile(values, q))
    cdf = np.cumsum(sorted_dt) / total_dt
    idx = int(np.searchsorted(cdf, q, side="left"))
    idx = min(max(idx, 0), len(sorted_vals) - 1)
    return float(sorted_vals[idx])


def compute_trace_stats(trace: EnergyTraceData) -> TraceStats:
    """Compute time-weighted statistics and CR2032 / C1 lifetime projections."""
    t = trace.time_s
    i_ua = trace.current_uA
    v = trace.voltage_V
    e_mj = trace.energy_mJ

    duration_s = float(t[-1] - t[0])
    dt = np.diff(t)

    total_energy_mJ = float(e_mj[-1] - e_mj[0])
    mean_power_uW = (total_energy_mJ * 1000.0) / duration_s

    p_inst_uW = v * i_ua
    vi_mean_power_uW = float(_trapz(p_inst_uW, t)) / duration_s
    if abs(mean_power_uW) > 1e-12:
        discrepancy_pct = abs(mean_power_uW - vi_mean_power_uW) / abs(mean_power_uW) * 100.0
    elif abs(vi_mean_power_uW) > 1e-12:
        discrepancy_pct = 100.0
    else:
        discrepancy_pct = 0.0

    mean_voltage_V = float(_trapz(v, t)) / duration_s
    tw_mean_current_uA = float(_trapz(i_ua, t)) / duration_s
    ev_mean_current_uA = mean_power_uW / mean_voltage_V if mean_voltage_V > 0 else tw_mean_current_uA
    sample_unweighted_mean_uA = float(np.mean(i_ua))

    tw_median_uA = _time_weighted_quantile(i_ua, dt, 0.50)
    tw_p95_uA = _time_weighted_quantile(i_ua, dt, 0.95)

    # CR2032 (220 mAh, 3.0 V nominal = 2376 J)
    i_ma = max(tw_mean_current_uA / 1000.0, 1e-9)
    p_w = max(mean_power_uW * 1e-6, 1e-12)
    i_a = max(tw_mean_current_uA * 1e-6, 1e-12)

    cr_h_curr = CR2032_CAPACITY_MAH / i_ma
    cr_h_ener = CR2032_ENERGY_J / (p_w * 3600.0)

    # C1 Supercapacitor (0.22 F, 3.3 V -> 1.8 V = 0.8415 J, 0.33 C)
    c1_s_pwr = C1_USABLE_ENERGY_J / p_w
    c1_s_curr = C1_USABLE_CHARGE_C / i_a

    return TraceStats(
        file_path=trace.path,
        sample_count=int(len(t)),
        duration_s=duration_s,
        total_energy_mJ=total_energy_mJ,
        mean_power_uW=mean_power_uW,
        vi_mean_power_uW=vi_mean_power_uW,
        power_discrepancy_pct=discrepancy_pct,
        mean_voltage_V=mean_voltage_V,
        tw_mean_current_uA=tw_mean_current_uA,
        ev_mean_current_uA=ev_mean_current_uA,
        sample_unweighted_mean_uA=sample_unweighted_mean_uA,
        tw_median_current_uA=tw_median_uA,
        tw_p95_current_uA=tw_p95_uA,
        min_current_uA=float(np.min(i_ua)),
        max_current_uA=float(np.max(i_ua)),
        has_dsr_col=trace.has_dsr_col,
        cr2032_hours_current=cr_h_curr,
        cr2032_days_current=cr_h_curr / 24.0,
        cr2032_hours_energy=cr_h_ener,
        cr2032_days_energy=cr_h_ener / 24.0,
        c1_seconds_power=c1_s_pwr,
        c1_minutes_power=c1_s_pwr / 60.0,
        c1_hours_power=c1_s_pwr / 3600.0,
        c1_seconds_current=c1_s_curr,
        c1_minutes_current=c1_s_curr / 60.0,
        c1_hours_current=c1_s_curr / 3600.0,
    )


def compute_deltas(base: TraceStats, opt: TraceStats) -> Dict[str, float]:
    def pct(b: float, o: float) -> float:
        return ((b - o) / b * 100.0) if abs(b) > 1e-12 else 0.0

    return {
        "delta_tw_current_uA": base.tw_mean_current_uA - opt.tw_mean_current_uA,
        "delta_tw_current_pct": pct(base.tw_mean_current_uA, opt.tw_mean_current_uA),
        "delta_mean_power_uW": base.mean_power_uW - opt.mean_power_uW,
        "delta_mean_power_pct": pct(base.mean_power_uW, opt.mean_power_uW),
        "delta_energy_mJ": base.total_energy_mJ - opt.total_energy_mJ,
        "delta_energy_pct": pct(base.total_energy_mJ, opt.total_energy_mJ),
        "power_reduction_factor": (base.mean_power_uW / opt.mean_power_uW) if opt.mean_power_uW > 0 else 0.0,
    }


def _configure_matplotlib_style() -> None:
    plt.rcParams.update({
        "font.size": 8.0,
        "axes.labelsize": 8.5,
        "axes.titlesize": 9.0,
        "xtick.labelsize": 7.5,
        "ytick.labelsize": 7.5,
        "legend.fontsize": 7.5,
        "axes.spines.top": False,
        "axes.spines.right": False,
    })


def _fmt_power_label(uw: float) -> str:
    if uw >= 1000.0:
        return f"{uw / 1000.0:.2f} mW"
    if uw >= 10.0:
        return f"{uw:.0f} uW"
    return f"{uw:.1f} uW"


def _plot_power_comparison(
    stats_map: Dict[str, Dict[str, TraceStats]],
    deltas_map: Dict[str, Dict[str, float]],
    out_path: Path,
) -> None:
    _configure_matplotlib_style()
    profiles = [p for p, _ in CORE_PROFILES if p in stats_map]
    if not profiles:
        return
    labels = [lbl for p, lbl in CORE_PROFILES if p in stats_map]
    x = np.arange(len(profiles))
    width = 0.36

    base_vals = [stats_map[p]["baseline"].mean_power_uW if "baseline" in stats_map[p] else np.nan for p in profiles]
    opt_vals = [stats_map[p]["optimized"].mean_power_uW if "optimized" in stats_map[p] else np.nan for p in profiles]

    fig, ax = plt.subplots(figsize=(7.16, 3.1), dpi=300)
    has_base = any(not np.isnan(v) for v in base_vals)
    has_opt = any(not np.isnan(v) for v in opt_vals)

    if has_base:
        bars_b = ax.bar(
            x - (width / 2 if has_opt else 0.0),
            [v if not np.isnan(v) else 0.0 for v in base_vals],
            width=width,
            label="Baseline (5cf488d)",
            color="#dc2626",
            alpha=0.9,
        )
        for idx, b in enumerate(bars_b):
            if not np.isnan(base_vals[idx]) and base_vals[idx] > 0:
                ax.text(
                    b.get_x() + b.get_width() / 2,
                    base_vals[idx] * 1.12,
                    _fmt_power_label(base_vals[idx]),
                    ha="center",
                    va="bottom",
                    fontsize=6.5,
                    color="#991b1b",
                )

    if has_opt:
        bars_o = ax.bar(
            x + (width / 2 if has_base else 0.0),
            [v if not np.isnan(v) else 0.0 for v in opt_vals],
            width=width,
            label="Optimized (Tier 1)",
            color="#2563eb",
            alpha=0.9,
        )
        for idx, b in enumerate(bars_o):
            p_key = profiles[idx]
            if not np.isnan(opt_vals[idx]) and opt_vals[idx] > 0:
                lbl = _fmt_power_label(opt_vals[idx])
                if p_key in deltas_map:
                    pct = deltas_map[p_key]["delta_mean_power_pct"]
                    lbl = f"{lbl}\n({-pct:+.1f}%)"
                ax.text(
                    b.get_x() + b.get_width() / 2,
                    opt_vals[idx] * 1.12,
                    lbl,
                    ha="center",
                    va="bottom",
                    fontsize=6.5,
                    color="#1e3a8a",
                )

    ax.set_yscale("log")
    all_pos = [v for v in base_vals + opt_vals if not np.isnan(v) and v > 0]
    if all_pos:
        ax.set_ylim(min(all_pos) * 0.5, max(all_pos) * 3.2)
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("Mean Power (uW, log scale)")
    ax.set_title("MSP430FR5994 Power Comparison: Baseline vs. Optimized (30 s EnergyTrace)")
    ax.grid(True, axis="y", which="both", alpha=0.25)
    ax.legend(frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(out_path, dpi=300, bbox_inches="tight", pad_inches=0.04)
    plt.close(fig)


def _plot_waveforms_idle_ai(
    traces_map: Dict[str, Dict[str, EnergyTraceData]],
    out_path: Path,
) -> None:
    _configure_matplotlib_style()
    fig, axes = plt.subplots(2, 1, figsize=(7.16, 3.8), dpi=300)

    panels = [
        ("idle", 10.0, "(a) Profile 1: Connected Terminal Idle (0-10 s Slice)", axes[0]),
        ("ai_heuristic", 5.0, "(b) Profile 3a: On-Chip AI Demo - Heuristic A (0-5 s Slice)", axes[1]),
    ]

    for p_key, t_win, title, ax in panels:
        series = traces_map.get(p_key, {})
        for variant, label, color, lw in (
            ("baseline", "Baseline (5cf488d)", "#dc2626", 1.1),
            ("optimized", "Optimized (Tier 1)", "#2563eb", 1.1),
        ):
            tr = series.get(variant)
            if tr is None:
                continue
            mask = tr.time_s <= t_win
            t_s = tr.time_s[mask]
            i_ua = tr.current_uA[mask]
            if len(t_s) > 0:
                ax.step(t_s, i_ua, where="post", label=label, color=color, linewidth=lw, alpha=0.9)
        ax.set_title(title)
        ax.set_ylabel("Current (uA)")
        ax.set_xlim(0.0, t_win)
        ax.grid(True, alpha=0.25)
        if series:
            ax.legend(frameon=False, loc="upper right")
        else:
            ax.text(0.5, 0.5, "No capture data", transform=ax.transAxes, ha="center", va="center")

    axes[1].set_xlabel("Time (s)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=300, bbox_inches="tight", pad_inches=0.04)
    plt.close(fig)


def _plot_waveform_low_hp(
    traces_map: Dict[str, Dict[str, EnergyTraceData]],
    out_path: Path,
) -> None:
    _configure_matplotlib_style()
    fig, ax = plt.subplots(figsize=(3.5, 2.5), dpi=300)
    series = traces_map.get("low_hp", {})
    t_win = 4.0
    for variant, label, color in (
        ("baseline", "Baseline (50% LED1)", "#dc2626"),
        ("optimized", "Optimized (Pulse LED1)", "#2563eb"),
    ):
        tr = series.get(variant)
        if tr is None:
            continue
        mask = tr.time_s <= t_win
        ax.step(
            tr.time_s[mask],
            tr.current_uA[mask] / 1000.0,
            where="post",
            label=label,
            color=color,
            linewidth=1.1,
            alpha=0.9,
        )
    ax.set_title("Low-HP Alert Current (0-4 s)")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Current (mA)")
    ax.set_xlim(0.0, t_win)
    ax.grid(True, alpha=0.25)
    if series:
        ax.legend(frameon=False, loc="upper right", fontsize=6.8)
    else:
        ax.text(0.5, 0.5, "No capture data", transform=ax.transAxes, ha="center", va="center")
    fig.tight_layout()
    fig.savefig(out_path, dpi=300, bbox_inches="tight", pad_inches=0.04)
    plt.close(fig)


def _plot_lifetime_projections(
    stats_map: Dict[str, Dict[str, TraceStats]],
    out_path: Path,
) -> None:
    _configure_matplotlib_style()
    profiles = [p for p, _ in CORE_PROFILES if p in stats_map]
    if not profiles:
        return
    labels = [lbl for p, lbl in CORE_PROFILES if p in stats_map]
    x = np.arange(len(profiles))
    width = 0.36

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.16, 3.0), dpi=300)

    for ax, metric_attr, ylabel, title in (
        (ax1, "cr2032_days_energy", "Projected Runtime (days, log)", "(a) CR2032 Coin Cell (220 mAh, 3.0 V = 2,376 J)"),
        (ax2, "c1_minutes_power", "Projected Runtime (min, log)", "(b) LaunchPad C1 Supercap (0.22 F, 3.3->1.8 V = 0.8415 J)"),
    ):
        b_vals = [getattr(stats_map[p]["baseline"], metric_attr) if "baseline" in stats_map[p] else np.nan for p in profiles]
        o_vals = [getattr(stats_map[p]["optimized"], metric_attr) if "optimized" in stats_map[p] else np.nan for p in profiles]
        has_b = any(not np.isnan(v) for v in b_vals)
        has_o = any(not np.isnan(v) for v in o_vals)
        if has_b:
            ax.bar(
                x - (width / 2 if has_o else 0.0),
                [v if not np.isnan(v) else 0.0 for v in b_vals],
                width=width,
                label="Baseline",
                color="#dc2626",
                alpha=0.9,
            )
        if has_o:
            ax.bar(
                x + (width / 2 if has_b else 0.0),
                [v if not np.isnan(v) else 0.0 for v in o_vals],
                width=width,
                label="Optimized",
                color="#10b981",
                alpha=0.9,
            )
        ax.set_yscale("log")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=6.8)
        ax.set_ylabel(ylabel)
        ax.set_title(title, fontsize=8.2)
        ax.grid(True, axis="y", which="both", alpha=0.25)
        ax.legend(frameon=False, loc="upper left", fontsize=7.0)

    fig.tight_layout()
    fig.savefig(out_path, dpi=300, bbox_inches="tight", pad_inches=0.04)
    plt.close(fig)


def render_summary_markdown(
    stats_map: Dict[str, Dict[str, TraceStats]],
    deltas_map: Dict[str, Dict[str, float]],
) -> str:
    lines = [
        "# EnergyTrace Summary Table",
        "",
        "| Profile | Variant | $T$ (s) | $N$ | $E_{\\text{tot}}$ (mJ) | $P_{\\text{mean}}$ (uW) | $I_{\\text{tw}}$ (uA) | $I_{p50}$ (uA) | $I_{p95}$ (uA) | $V$–$I$ Diff (%) | CR2032 (days) | C1 Cap (min) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    all_profiles = list(CORE_PROFILES) + list(OPTIONAL_PROFILES)
    for p_key, p_lbl in all_profiles:
        if p_key not in stats_map:
            continue
        clean_lbl = p_lbl.replace("\n", " ")
        for var in ("baseline", "optimized"):
            st = stats_map[p_key].get(var)
            if st is None:
                continue
            lines.append(
                f"| {clean_lbl} | {var} | {st.duration_s:.2f} | {st.sample_count} | "
                f"{st.total_energy_mJ:.3f} | {st.mean_power_uW:.2f} | {st.tw_mean_current_uA:.2f} | "
                f"{st.tw_median_current_uA:.2f} | {st.tw_p95_current_uA:.2f} | "
                f"{st.power_discrepancy_pct:.2f} | {st.cr2032_days_energy:.2f} | {st.c1_minutes_power:.2f} |"
            )

    if deltas_map:
        lines.extend([
            "",
            "## Baseline vs. Optimized Deltas",
            "",
            "| Profile | $\\Delta I_{\\text{tw}}$ (uA) | $\\Delta P_{\\text{mean}}$ (uW) | Power Reduction (%) | Factor |",
            "|---|---:|---:|---:|---:|",
        ])
        for p_key, p_lbl in all_profiles:
            if p_key not in deltas_map:
                continue
            d = deltas_map[p_key]
            clean_lbl = p_lbl.replace("\n", " ")
            lines.append(
                f"| {clean_lbl} | {d['delta_tw_current_uA']:.2f} | {d['delta_mean_power_uW']:.2f} | "
                f"{d['delta_mean_power_pct']:.2f}% | {d['power_reduction_factor']:.2f}x |"
            )
    lines.append("")
    return "\n".join(lines)


def run_pipeline(
    data_dir: Path,
    out_dir: Path,
    summary_json: Path,
    summary_md: Path,
    allow_partial: bool = False,
) -> Dict[str, object]:
    """Ingest EnergyTrace CSVs from data_dir, compute stats/deltas, write JSON/MD and 4 PNG figures."""
    traces_map: Dict[str, Dict[str, EnergyTraceData]] = {}
    stats_map: Dict[str, Dict[str, TraceStats]] = {}
    deltas_map: Dict[str, Dict[str, float]] = {}
    missing: List[str] = []

    all_keys = [p for p, _ in CORE_PROFILES] + [p for p, _ in OPTIONAL_PROFILES]
    core_keys = {p for p, _ in CORE_PROFILES}

    for p_key in all_keys:
        for var in ("baseline", "optimized"):
            fpath = data_dir / f"{var}_{p_key}.csv"
            if fpath.exists():
                tr = parse_energytrace_csv(fpath)
                st = compute_trace_stats(tr)
                traces_map.setdefault(p_key, {})[var] = tr
                stats_map.setdefault(p_key, {})[var] = st
            elif p_key in core_keys:
                missing.append(str(fpath))

    if missing and not allow_partial:
        raise FileNotFoundError(
            "Missing required EnergyTrace CSV files (use --allow-partial to process existing subset): "
            + ", ".join(missing)
        )

    if not stats_map:
        raise FileNotFoundError(f"No EnergyTrace CSV files found in {data_dir}")

    for p_key, pair in stats_map.items():
        if "baseline" in pair and "optimized" in pair:
            deltas_map[p_key] = compute_deltas(pair["baseline"], pair["optimized"])

    out_dir.mkdir(parents=True, exist_ok=True)
    fig_paths = {
        "power_comparison": out_dir / "power_comparison.png",
        "waveforms_idle_ai": out_dir / "waveforms_idle_ai.png",
        "waveform_low_hp": out_dir / "waveform_low_hp.png",
        "lifetime_projections": out_dir / "lifetime_projections.png",
    }
    _plot_power_comparison(stats_map, deltas_map, fig_paths["power_comparison"])
    _plot_waveforms_idle_ai(traces_map, fig_paths["waveforms_idle_ai"])
    _plot_waveform_low_hp(traces_map, fig_paths["waveform_low_hp"])
    _plot_lifetime_projections(stats_map, fig_paths["lifetime_projections"])

    payload = {
        "constants": {
            "cr2032_capacity_mAh": CR2032_CAPACITY_MAH,
            "cr2032_nominal_V": CR2032_NOMINAL_V,
            "cr2032_energy_J": CR2032_ENERGY_J,
            "c1_capacitance_F": C1_CAPACITANCE_F,
            "c1_v_max_V": C1_V_MAX,
            "c1_v_min_V": C1_V_MIN,
            "c1_usable_energy_J": C1_USABLE_ENERGY_J,
            "c1_usable_charge_C": C1_USABLE_CHARGE_C,
        },
        "profiles": {
            p_key: {var: asdict(st) for var, st in pair.items()}
            for p_key, pair in stats_map.items()
        },
        "deltas": deltas_map,
        "missing_core_files": missing,
        "figures": {k: str(v) for k, v in fig_paths.items() if v.exists()},
    }

    summary_json.parent.mkdir(parents=True, exist_ok=True)
    summary_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    summary_md.parent.mkdir(parents=True, exist_ok=True)
    summary_md.write_text(render_summary_markdown(stats_map, deltas_map), encoding="utf-8")
    return payload


# ---------------------------------------------------------------------------
# Isolated Self-Test (--self-test)
# ---------------------------------------------------------------------------

def _write_synthetic_ccs_csv(
    path: Path,
    duration_s: float,
    base_ua: float,
    pulse_ua: float,
    pulse_period_s: float,
    pulse_width_s: float,
    irregular_sleep_dt: bool = True,
    include_dsr: bool = False,
    unit_variant: bool = False,
    delimiter: str = ",",
    quote_rows: bool = False,
    utf8_bom: bool = False,
) -> None:
    """Create a realistic synthetic CCS EnergyTrace CSV with variable DC-DC pulse intervals."""
    t = 0.0
    rows: List[Tuple[float, float, float, float]] = []
    energy_uj = 0.0
    v_mv = 3300.0
    prev_i_na = base_ua * 1000.0

    while t <= duration_s + 1e-9:
        in_pulse = (pulse_period_s > 0.0) and ((t % pulse_period_s) < pulse_width_s)
        curr_ua = pulse_ua if in_pulse else base_ua
        curr_na = curr_ua * 1000.0
        dt = 0.002 if in_pulse else (0.5 if (irregular_sleep_dt and base_ua < 5.0) else 0.02)
        if rows:
            dt_actual = t - rows[-1][0]
            avg_i_a = 0.5 * (prev_i_na + curr_na) * 1e-9
            energy_uj += (v_mv * 1e-3) * avg_i_a * dt_actual * 1e6
        rows.append((t, curr_na, v_mv, energy_uj))
        prev_i_na = curr_na
        t += dt

    enc = "utf-8-sig" if utf8_bom else "utf-8"
    with open(path, "w", encoding=enc, newline="") as f:
        w_hdr = csv.writer(f, delimiter=delimiter)
        w_row = csv.writer(f, delimiter=delimiter, quoting=csv.QUOTE_ALL if quote_rows else csv.QUOTE_MINIMAL)
        if unit_variant:
            hdr = ["Time (ms)", "Current (uA)", "Voltage (V)", "Energy (mJ)"]
        else:
            hdr = ["Time (ns)", "Current (nA)", "Voltage (mV)", "Energy (uJ)"]
        if include_dsr:
            hdr.append("DSR")
        w_hdr.writerow(hdr)
        for t_s, i_na, v_m, e_u in rows:
            if unit_variant:
                r = [f"{t_s * 1e3:.3f}", f"{i_na * 1e-3:.3f}", f"{v_m * 1e-3:.4f}", f"{e_u * 1e-3:.6f}"]
            else:
                r = [f"{int(round(t_s * 1e9))}", f"{int(round(i_na))}", f"{int(round(v_m))}", f"{e_u:.4f}"]
            if include_dsr:
                r.append("0x00")
            w_row.writerow(r)


def run_self_test() -> None:
    print("[SELF-TEST] Running offline tools/power_plots.py test suite...", flush=True)
    assert abs(C1_USABLE_ENERGY_J - 0.8415) < 1e-9
    assert abs(CR2032_ENERGY_J - 2376.0) < 1e-9

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        data_dir = tmp_path / "data"
        out_dir = tmp_path / "figures"
        data_dir.mkdir()

        # 1. Negative test: reject 3-column summary CSV (exportProfileToCSV)
        bad_summary = tmp_path / "bad_summary.csv"
        bad_summary.write_text("Item,Runtime (nS),Energy (nJ)\nmain,1000000,5000\n", encoding="utf-8")
        try:
            parse_energytrace_csv(bad_summary)
            raise AssertionError("Expected ValueError for 3-column summary CSV")
        except ValueError as exc:
            assert "Raw Data" in str(exc)

        # 2. DSR column EnergyTrace++ warning test + unit variant (ms/uA/V/mJ)
        dsr_csv = tmp_path / "dsr_test.csv"
        _write_synthetic_ccs_csv(dsr_csv, 30.0, 200.0, 200.0, 0.0, 0.0, include_dsr=True, unit_variant=True)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            tr_dsr = parse_energytrace_csv(dsr_csv)
        assert tr_dsr.has_dsr_col is True
        assert any("EnergyTrace++" in str(w.message) for w in caught)
        st_dsr = compute_trace_stats(tr_dsr)
        assert abs(st_dsr.tw_mean_current_uA - 200.0) < 1.0
        assert st_dsr.power_discrepancy_pct < 0.5

        # 3. Time-weighted vs sample-weighted accuracy test (irregular DC-DC intervals)
        irreg_csv = tmp_path / "irreg.csv"
        _write_synthetic_ccs_csv(
            irreg_csv,
            duration_s=30.0,
            base_ua=1.2,       # 0.5 s intervals during sleep
            pulse_ua=1500.0,   # 0.002 s intervals during short burst
            pulse_period_s=5.0,
            pulse_width_s=0.05,
            irregular_sleep_dt=True,
        )
        st_irreg = compute_trace_stats(parse_energytrace_csv(irreg_csv))
        # Time-weighted mean is ~ 1.2 + 0.01*1500 = ~16 uA, whereas unweighted sample mean heavily overweights 2 ms bursts (> 150 uA)
        assert st_irreg.tw_mean_current_uA < 40.0, st_irreg
        assert st_irreg.sample_unweighted_mean_uA > st_irreg.tw_mean_current_uA * 3.0, st_irreg

        # 4. Generate all 5 Baseline + 5 Optimized CSVs and run full pipeline in temp dir
        profiles_cfg = {
            "idle": ((290.0, 650.0, 0.5, 0.02), (1.8, 40.0, 0.0, 0.0)),
            "play": ((320.0, 1400.0, 1.0, 0.04), (45.0, 1200.0, 1.0, 0.04)),
            "ai_heuristic": ((300.0, 1500.0, 0.5, 0.05), (40.0, 1400.0, 0.5, 0.05)),
            "ai_mlp": ((310.0, 1650.0, 0.5, 0.06), (48.0, 1550.0, 0.5, 0.06)),
            "low_hp": ((1800.0, 3200.0, 1.0, 0.5), (80.0, 2900.0, 0.5, 0.025)),
        }
        for p_key, (b_cfg, o_cfg) in profiles_cfg.items():
            _write_synthetic_ccs_csv(data_dir / f"baseline_{p_key}.csv", 30.0, *b_cfg)
            _write_synthetic_ccs_csv(data_dir / f"optimized_{p_key}.csv", 30.0, *o_cfg)

        sum_json = tmp_path / "summary.json"
        sum_md = tmp_path / "summary_table.md"
        payload = run_pipeline(data_dir, out_dir, sum_json, sum_md, allow_partial=False)

        assert sum_json.exists() and sum_md.exists()
        assert len(payload["deltas"]) == 5
        for fig_name in ("power_comparison.png", "waveforms_idle_ai.png", "waveform_low_hp.png", "lifetime_projections.png"):
            fp = out_dir / fig_name
            assert fp.exists(), f"Missing figure {fp}"
            raw = fp.read_bytes()
            assert len(raw) > 5000 and raw[:8] == b"\x89PNG\r\n\x1a\n", f"Invalid PNG {fp}"

        # 5. Ragged CSV row regression test (must skip incomplete row safely without IndexError)
        ragged_csv = tmp_path / "ragged.csv"
        ragged_csv.write_text(
            "Time (s),Current (uA),Voltage (V),Energy (mJ)\n"
            "0.0,100.0,3.3,0.0\n"
            "15.0,100.0\n"
            "30.0,100.0,3.3,9.9\n",
            encoding="utf-8",
        )
        with warnings.catch_warnings(record=True) as caught_rag:
            warnings.simplefilter("always")
            tr_rag = parse_energytrace_csv(ragged_csv)
        assert len(tr_rag.time_s) == 2
        assert any("ragged" in str(w.message).lower() or "incomplete" in str(w.message).lower() for w in caught_rag)
        st_rag = compute_trace_stats(tr_rag)
        assert abs(st_rag.total_energy_mJ - 9.9) < 1e-6

        # 6. Power (uW)/(mW) integration when Voltage & Energy are absent or Energy is empty
        pwr_uw_csv = tmp_path / "pwr_uw.csv"
        pwr_uw_csv.write_text(
            "Time (s),Current (uA),Power (uW)\n"
            "0.0,100.0,300.0\n"
            "30.0,100.0,300.0\n",
            encoding="utf-8",
        )
        st_pwr_uw = compute_trace_stats(parse_energytrace_csv(pwr_uw_csv))
        assert abs(st_pwr_uw.mean_power_uW - 300.0) < 1e-6, st_pwr_uw
        assert abs(st_pwr_uw.total_energy_mJ - 9.0) < 1e-6, st_pwr_uw

        pwr_mw_empty_e_csv = tmp_path / "pwr_mw_empty_e.csv"
        pwr_mw_empty_e_csv.write_text(
            "Time (s),Current (uA),Energy (mJ),Power (mW)\n"
            "0.0,100.0,,0.3\n"
            "30.0,100.0,,0.3\n",
            encoding="utf-8",
        )
        st_pwr_mw = compute_trace_stats(parse_energytrace_csv(pwr_mw_empty_e_csv))
        assert abs(st_pwr_mw.mean_power_uW - 300.0) < 1e-6, st_pwr_mw

        # 7. Zero-energy nonzero-VI discrepancy regression test
        zero_e_csv = tmp_path / "zero_e_nonzero_vi.csv"
        zero_e_csv.write_text(
            "Time (s),Current (uA),Voltage (V),Energy (mJ)\n"
            "0.0,100.0,3.3,0.0\n"
            "15.0,100.0,3.3,4.95\n"
            "30.0,100.0,3.3,\n",
            encoding="utf-8",
        )
        st_zero_e = compute_trace_stats(parse_energytrace_csv(zero_e_csv))
        assert abs(st_zero_e.mean_power_uW) <= 1e-12
        assert abs(st_zero_e.vi_mean_power_uW - 330.0) < 1e-6
        assert abs(st_zero_e.power_discrepancy_pct - 100.0) < 1e-6, st_zero_e

        both_zero_csv = tmp_path / "both_zero.csv"
        both_zero_csv.write_text(
            "Time (s),Current (uA),Voltage (V),Energy (mJ)\n"
            "0.0,0.0,0.0,0.0\n"
            "30.0,0.0,0.0,0.0\n",
            encoding="utf-8",
        )
        st_both_zero = compute_trace_stats(parse_energytrace_csv(both_zero_csv))
        assert st_both_zero.power_discrepancy_pct == 0.0, st_both_zero

        # 8. Windows-locale semicolon-delimited + quoted dot-decimal CCS 12.5 CSVs (+ UTF-8 BOM)
        semi_ccs_csv = tmp_path / "semi_ccs.csv"
        semi_ccs_csv.write_text(
            "\ufeffTime (ns);Current (nA);Voltage (mV);Energy (uJ)\n"
            '"0";"0";"3278";"0.6"\n'
            '"15000000000";"200000";"3290";"9885.6"\n'
            '"30000000000";"200000";"3290";"19755.6"\n',
            encoding="utf-8",
        )
        tr_semi = parse_energytrace_csv(semi_ccs_csv)
        assert tr_semi.raw_headers == ["Time (ns)", "Current (nA)", "Voltage (mV)", "Energy (uJ)"]
        assert abs(tr_semi.time_s[-1] - 30.0) < 1e-9
        assert abs(tr_semi.voltage_V[0] - 3.278) < 1e-6
        assert abs(tr_semi.energy_mJ[0] - 0.0006) < 1e-9
        st_semi = compute_trace_stats(tr_semi)
        assert abs(st_semi.total_energy_mJ - 19.755) < 1e-6, st_semi

        # 9. Semicolon-delimited 3-column summary export rejection
        bad_semi_summary = tmp_path / "bad_semi_summary.csv"
        bad_semi_summary.write_text(
            'Item;Runtime (nS);Energy (nJ)\n"main";"1000000";"5000"\n',
            encoding="utf-8",
        )
        try:
            parse_energytrace_csv(bad_semi_summary)
            raise AssertionError("Expected ValueError for semicolon 3-column summary CSV")
        except ValueError as exc:
            assert "Raw Data" in str(exc)

        # 10. Semicolon-delimited header variant + DSR warning
        semi_dsr_csv = tmp_path / "semi_dsr.csv"
        _write_synthetic_ccs_csv(
            semi_dsr_csv,
            duration_s=30.0,
            base_ua=250.0,
            pulse_ua=250.0,
            pulse_period_s=0.0,
            pulse_width_s=0.0,
            include_dsr=True,
            unit_variant=True,
            delimiter=";",
            quote_rows=True,
            utf8_bom=True,
        )
        with warnings.catch_warnings(record=True) as caught_semi_dsr:
            warnings.simplefilter("always")
            tr_semi_dsr = parse_energytrace_csv(semi_dsr_csv)
        assert tr_semi_dsr.has_dsr_col is True
        assert any("EnergyTrace++" in str(w.message) for w in caught_semi_dsr)
        st_semi_dsr = compute_trace_stats(tr_semi_dsr)
        assert abs(st_semi_dsr.tw_mean_current_uA - 250.0) < 1.0, st_semi_dsr

        # 11. Semicolon-delimited ragged row regression test
        semi_ragged_csv = tmp_path / "semi_ragged.csv"
        semi_ragged_csv.write_text(
            'Time (ns);Current (nA);Voltage (mV);Energy (uJ)\n'
            '"0";"100000";"3300";"0.0"\n'
            '"15000000000";"100000"\n'
            '"30000000000";"100000";"3300";"9900.0"\n',
            encoding="utf-8",
        )
        with warnings.catch_warnings(record=True) as caught_semi_rag:
            warnings.simplefilter("always")
            tr_semi_rag = parse_energytrace_csv(semi_ragged_csv)
        assert len(tr_semi_rag.time_s) == 2
        assert any("ragged" in str(w.message).lower() or "incomplete" in str(w.message).lower() for w in caught_semi_rag)
        assert abs(compute_trace_stats(tr_semi_rag).total_energy_mJ - 9.9) < 1e-6

        # 12. Full pipeline run with semicolon-delimited + quoted dot-decimal CSVs
        semi_data_dir = tmp_path / "semi_data"
        semi_out_dir = tmp_path / "semi_figures"
        semi_data_dir.mkdir()
        for p_key, (b_cfg, o_cfg) in profiles_cfg.items():
            _write_synthetic_ccs_csv(
                semi_data_dir / f"baseline_{p_key}.csv", 30.0, *b_cfg,
                delimiter=";", quote_rows=True, utf8_bom=True,
            )
            _write_synthetic_ccs_csv(
                semi_data_dir / f"optimized_{p_key}.csv", 30.0, *o_cfg,
                delimiter=";", quote_rows=True, utf8_bom=True,
            )
        semi_payload = run_pipeline(
            semi_data_dir,
            semi_out_dir,
            tmp_path / "semi_summary.json",
            tmp_path / "semi_summary.md",
            allow_partial=False,
        )
        assert len(semi_payload["deltas"]) == 5

    print("[SELF-TEST] All power_plots.py offline checks PASSED.", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Parse CCS 12.5 EnergyTrace Raw CSVs and generate summary + 300 DPI matplotlib figures."
    )
    ap.add_argument("--data-dir", default="docs/power/data", help="Directory containing EnergyTrace CSV files")
    ap.add_argument("--out-dir", default="docs/power/figures", help="Output directory for 300 DPI PNG figures")
    ap.add_argument("--summary-json", default="docs/power/summary.json", help="Output JSON summary path")
    ap.add_argument("--summary-md", default="docs/power/summary_table.md", help="Output Markdown table path")
    ap.add_argument(
        "--allow-partial",
        action="store_true",
        help="Process whatever CSV subset currently exists without failing on missing pairs",
    )
    ap.add_argument("--self-test", action="store_true", help="Run isolated offline tests in TemporaryDirectory")
    args = ap.parse_args()

    if args.self_test:
        run_self_test()
        return 0

    payload = run_pipeline(
        data_dir=Path(args.data_dir),
        out_dir=Path(args.out_dir),
        summary_json=Path(args.summary_json),
        summary_md=Path(args.summary_md),
        allow_partial=args.allow_partial,
    )
    print(json.dumps({"figures": payload["figures"], "deltas": payload["deltas"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
