"""
Programmatic TI CCS 12.5 EnergyTrace capture driver & binary IVE decoder for
mspgame on the TI MSP-EXP430FR5994 LaunchPad (via 64-bit MSP430.dll).

Captures energy-only analog EnergyTrace streams (ET_PROFILING_ANALOG = 0,
event_id = 8, 18-byte IVE records) synchronized with tools/power_bench.py,
and exports raw CSVs matching CCS 12.5's schema:
    Time (ns),Current (nA),Voltage (mV),Energy (uJ)
(supports both ',' and ';' delimiters compatible with tools/power_plots.py).
"""
from __future__ import annotations

import argparse
import ctypes
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
POWER_BENCH_PY = REPO_ROOT / "tools" / "power_bench.py"
DEFAULT_DATA_DIR = REPO_ROOT / "docs" / "power" / "data"
DEFAULT_CCS_BASE = Path(r"C:\ti\ccs1250\ccs\ccs_base")

PROFILE_TO_CSV: Dict[str, str] = {
    "idle": "optimized_idle.csv",
    "play": "optimized_play.csv",
    "ai-heuristic": "optimized_ai_heuristic.csv",
    "ai-mlp": "optimized_ai_mlp.csv",
    "low-hp": "optimized_low_hp.csv",
}

ALL_PROFILES: Tuple[str, ...] = ("idle", "ai-heuristic", "ai-mlp", "low-hp", "play")

# MSP430_EnergyTrace.h / MSP430.h constants
ET_PROFILING_ANALOG = 0
ET_PROFILING_10K = 4
ET_ALL = 2
ET_EVENT_WINDOW_100 = 2
ET_CALLBACKS_CONTINUOUS = 0
ET_CALLBACKS_ONLY_DURING_RUN = 1

ET_EVENT_STATE_VOLT_CURR = 7   # 26-byte SVIE record
ET_EVENT_CURR_VOLT_ENERGY = 8  # 18-byte IVE record

CONFIG_DEBUG_LPM_X = 14
CONFIG_ET_CURRENTDRIVE_FINE = 17
RUN_FREE_RUN = 1
RESET_FORCE_RST = 0x0A


class EnergyTraceSetup(ctypes.Structure):
    _fields_ = [
        ("ETMode", ctypes.c_int32),
        ("ETFreq", ctypes.c_int32),
        ("ETFormat", ctypes.c_int32),
        ("ETSampleWindow", ctypes.c_int32),
        ("ETCallback", ctypes.c_int32),
    ]


PUSH_DATA_FN = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32
)
ERROR_OCCURRED_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p)


class EnergyTraceCallbacks(ctypes.Structure):
    _fields_ = [
        ("pContext", ctypes.c_void_p),
        ("pPushDataFn", PUSH_DATA_FN),
        ("pErrorOccurredFn", ERROR_OCCURRED_FN),
    ]


@dataclass
class EnergyTraceSample:
    time_ns: int
    current_nA: int
    voltage_mV: int
    energy_uJ: float


@dataclass
class DecodedTrace:
    samples: List[EnergyTraceSample]
    event_ids: List[int]
    trailing_bytes: int


def decode_et_stream(
    raw_bytes: bytes,
    max_duration_ns: Optional[int] = None,
) -> DecodedTrace:
    """
    Decode raw EnergyTrace callback stream (18-byte IVE event_id=8 or 26-byte SVIE event_id=7)
    into zero-referenced EnergyTraceSample rows with 32-bit energy wraparound handling.
    """
    samples: List[EnergyTraceSample] = []
    event_ids_seen: List[int] = []
    seen_set = set()

    offset = 0
    total_len = len(raw_bytes)
    ts0_us: Optional[int] = None
    e0_raw: Optional[int] = None
    prev_e_raw: Optional[int] = None
    overflow_energy = 0

    while offset + 8 <= total_len:
        hdr = struct.unpack_from("<Q", raw_bytes, offset)[0]
        event_id = hdr & 0xFF
        ts_us = (hdr >> 8) & 0x00FFFFFFFFFFFFFF

        if event_id == ET_EVENT_CURR_VOLT_ENERGY:
            rec_size = 18
            if offset + rec_size > total_len:
                break
            _, curr_nA, volt_mV, ener_raw = struct.unpack_from("<QIHI", raw_bytes, offset)
        elif event_id == ET_EVENT_STATE_VOLT_CURR:
            rec_size = 26
            if offset + rec_size > total_len:
                break
            _, _dev_state, curr_nA, volt_mV, ener_raw = struct.unpack_from("<QQIHI", raw_bytes, offset)
        else:
            raise ValueError(f"Unsupported EnergyTrace event_id={event_id} at byte offset {offset}")

        if event_id not in seen_set:
            seen_set.add(event_id)
            event_ids_seen.append(event_id)

        if ts0_us is None:
            ts0_us = ts_us
            e0_raw = ener_raw
            prev_e_raw = ener_raw
        else:
            assert prev_e_raw is not None
            if ener_raw < prev_e_raw and (prev_e_raw - ener_raw) > 0x80000000:
                overflow_energy += 1 << 32
            prev_e_raw = ener_raw

        assert ts0_us is not None and e0_raw is not None
        time_ns = (ts_us - ts0_us) * 1000
        if max_duration_ns is not None and time_ns > max_duration_ns and len(samples) > 0:
            offset += rec_size
            break

        energy_uJ = ((ener_raw + overflow_energy) - e0_raw) / 10.0
        samples.append(
            EnergyTraceSample(
                time_ns=time_ns,
                current_nA=int(curr_nA),
                voltage_mV=int(volt_mV),
                energy_uJ=float(energy_uJ),
            )
        )
        offset += rec_size

    trailing = total_len - offset if (max_duration_ns is None or offset == total_len) else 0
    return DecodedTrace(samples=samples, event_ids=event_ids_seen, trailing_bytes=trailing)


def write_energytrace_csv(
    out_csv: Path,
    samples: Sequence[EnergyTraceSample],
    delimiter: str = ",",
) -> None:
    """Write samples in CCS 12.5 Raw Data CSV format."""
    if delimiter not in (",", ";"):
        raise ValueError(f"Unsupported delimiter: {delimiter!r}")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        f.write(f"Time (ns){delimiter}Current (nA){delimiter}Voltage (mV){delimiter}Energy (uJ)\n")
        if delimiter == ";":
            for s in samples:
                f.write(f'"{s.time_ns}";"{s.current_nA}";"{s.voltage_mV}";"{s.energy_uJ:.1f}"\n')
        else:
            for s in samples:
                f.write(f"{s.time_ns},{s.current_nA},{s.voltage_mV},{s.energy_uJ:.1f}\n")


def load_msp430_dll(ccs_base: Path = DEFAULT_CCS_BASE) -> ctypes.WinDLL:
    """Load 64-bit MSP430.dll from CCS 12.5 and configure ctypes signatures."""
    drivers_dir = ccs_base / "DebugServer" / "drivers"
    bin_dir = ccs_base / "DebugServer" / "bin"
    common_bin = ccs_base.parent / "common" / "bin"
    for d in (drivers_dir, bin_dir, common_bin):
        if d.exists():
            os.add_dll_directory(str(d))

    dll_path = drivers_dir / "MSP430.dll"
    if not dll_path.exists():
        raise FileNotFoundError(f"MSP430.dll not found at {dll_path}")

    dll = ctypes.WinDLL(str(dll_path))

    dll.MSP430_Initialize.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int32)]
    dll.MSP430_Initialize.restype = ctypes.c_int32

    dll.MSP430_VCC.argtypes = [ctypes.c_int32]
    dll.MSP430_VCC.restype = ctypes.c_int32

    dll.MSP430_Configure.argtypes = [ctypes.c_int32, ctypes.c_int32]
    dll.MSP430_Configure.restype = ctypes.c_int32

    dll.MSP430_OpenDevice.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int32,
        ctypes.c_int32,
        ctypes.c_int32,
    ]
    dll.MSP430_OpenDevice.restype = ctypes.c_int32

    dll.MSP430_EnableEnergyTrace.argtypes = [
        ctypes.POINTER(EnergyTraceSetup),
        ctypes.POINTER(EnergyTraceCallbacks),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    dll.MSP430_EnableEnergyTrace.restype = ctypes.c_int32

    dll.MSP430_ResetEnergyTrace.argtypes = [ctypes.c_void_p]
    dll.MSP430_ResetEnergyTrace.restype = ctypes.c_int32

    dll.MSP430_DisableEnergyTrace.argtypes = [ctypes.c_void_p]
    dll.MSP430_DisableEnergyTrace.restype = ctypes.c_int32

    dll.MSP430_Run.argtypes = [ctypes.c_int32, ctypes.c_int32]
    dll.MSP430_Run.restype = ctypes.c_int32

    dll.MSP430_Reset.argtypes = [ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
    dll.MSP430_Reset.restype = ctypes.c_int32

    dll.MSP430_Close.argtypes = [ctypes.c_int32]
    dll.MSP430_Close.restype = ctypes.c_int32

    dll.MSP430_Error_Number.argtypes = []
    dll.MSP430_Error_Number.restype = ctypes.c_int32

    dll.MSP430_Error_String.argtypes = [ctypes.c_int32]
    dll.MSP430_Error_String.restype = ctypes.c_char_p

    return dll


def _dll_err(dll: ctypes.WinDLL) -> str:
    err_no = dll.MSP430_Error_Number()
    err_s = dll.MSP430_Error_String(err_no)
    msg = err_s.decode("utf-8", errors="replace") if err_s else ""
    return f"err={err_no} ({msg})"


class _BenchSyncProcess:
    """Context manager driving tools/power_bench.py --wait-start for synchronized capture."""

    def __init__(self, port: str, baud: int, profile: str, duration_s: float) -> None:
        self.port = port
        self.baud = baud
        self.profile = profile
        self.duration_s = duration_s
        self.proc: Optional[subprocess.Popen[str]] = None
        self._stdout_lines: List[str] = []
        self._ready_event = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None

    def stage(self, timeout_s: float = 45.0) -> None:
        cmd = [
            sys.executable,
            str(POWER_BENCH_PY),
            "--port",
            self.port,
            "--baud",
            str(self.baud),
            "--profile",
            self.profile,
            "--duration",
            f"{self.duration_s:.2f}",
            "--wait-start",
            "--countdown",
            "0",
        ]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        def _pump_stdout() -> None:
            assert self.proc is not None and self.proc.stdout is not None
            for line in self.proc.stdout:
                self._stdout_lines.append(line)
                if "press ENTER" in line or "Verify CCS" in line:
                    self._ready_event.set()

        self._reader_thread = threading.Thread(target=_pump_stdout, daemon=True)
        self._reader_thread.start()

        deadline = time.perf_counter() + timeout_s
        while not self._ready_event.is_set():
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"power_bench.py exited early ({self.proc.returncode}) while staging '{self.profile}':\n"
                    + "".join(self._stdout_lines)
                )
            if time.perf_counter() > deadline:
                raise TimeoutError(
                    f"Timed out waiting for power_bench.py to stage '{self.profile}':\n"
                    + "".join(self._stdout_lines)
                )
            time.sleep(0.05)

    def release_start(self) -> None:
        if self.proc is not None and self.proc.stdin is not None:
            self.proc.stdin.write("\n")
            self.proc.stdin.flush()

    def finish(self, timeout_s: float = 20.0) -> Tuple[int, str]:
        if self.proc is None:
            return 0, ""
        try:
            rc = self.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            rc = self.proc.wait(timeout=5.0)
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
        return rc, "".join(self._stdout_lines)


def capture_profile(
    profile: str,
    out_csv: Path,
    duration_s: float = 30.0,
    port: str = "COM4",
    baud: int = 115200,
    delimiter: str = ",",
    ccs_base: Path = DEFAULT_CCS_BASE,
    mode: str = "auto",
    standalone: bool = False,
    fine_current: bool = False,
) -> Dict[str, object]:
    """Capture one EnergyTrace CSV synchronized with power_bench.py."""
    dll = load_msp430_dll(ccs_base)
    bench: Optional[_BenchSyncProcess] = None
    handle = ctypes.c_void_p(0)
    initialized = False
    et_enabled = False

    chunks: List[bytes] = []
    et_errors: List[str] = []
    lock = threading.Lock()
    state = {"first_ts_us": None, "last_ts_us": None, "bytes": 0}

    @PUSH_DATA_FN
    def _on_push_data(_ctx, p_buf, n_size) -> None:
        if n_size <= 0:
            return
        raw = ctypes.string_at(p_buf, int(n_size))
        ts_first = None
        ts_last = None
        if len(raw) >= 8:
            hdr0 = struct.unpack_from("<Q", raw, 0)[0]
            ev0 = hdr0 & 0xFF
            ts_first = (hdr0 >> 8) & 0x00FFFFFFFFFFFFFF
            rec_sz = 18 if ev0 == ET_EVENT_CURR_VOLT_ENERGY else (26 if ev0 == ET_EVENT_STATE_VOLT_CURR else 0)
            if rec_sz and len(raw) >= rec_sz:
                last_off = ((len(raw) // rec_sz) - 1) * rec_sz
                hdr_l = struct.unpack_from("<Q", raw, last_off)[0]
                ts_last = (hdr_l >> 8) & 0x00FFFFFFFFFFFFFF
        with lock:
            chunks.append(raw)
            state["bytes"] += len(raw)
            if ts_first is not None and state["first_ts_us"] is None:
                state["first_ts_us"] = ts_first
            if ts_last is not None:
                state["last_ts_us"] = ts_last

    @ERROR_OCCURRED_FN
    def _on_error(_ctx, psz_err) -> None:
        msg = psz_err.decode("utf-8", errors="replace") if psz_err else "unknown error"
        with lock:
            et_errors.append(msg)

    callbacks = EnergyTraceCallbacks(
        pContext=None,
        pPushDataFn=_on_push_data,
        pErrorOccurredFn=_on_error,
    )

    try:
        if not standalone:
            # Keep power_bench active 3 s longer than EnergyTrace window so workload never stops early
            bench = _BenchSyncProcess(port=port, baud=baud, profile=profile, duration_s=duration_s + 3.0)
            print(f"[ET] Staging profile '{profile}' on {port}...", flush=True)
            bench.stage(timeout_s=45.0)
            print(f"[ET] Profile '{profile}' staged.", flush=True)

        ver = ctypes.c_int32(0)
        rc = dll.MSP430_Initialize(b"TIUSB", ctypes.byref(ver))
        if rc != 0:
            raise RuntimeError(f"MSP430_Initialize failed: {_dll_err(dll)}")
        initialized = True

        if fine_current:
            dll.MSP430_Configure(CONFIG_ET_CURRENTDRIVE_FINE, 1)

        rc = dll.MSP430_VCC(3300)
        if rc != 0:
            raise RuntimeError(f"MSP430_VCC(3300) failed: {_dll_err(dll)}")

        used_mode = "continuous"
        if mode in ("auto", "continuous"):
            setup = EnergyTraceSetup(
                ETMode=ET_PROFILING_ANALOG,
                ETFreq=ET_PROFILING_10K,
                ETFormat=ET_ALL,
                ETSampleWindow=ET_EVENT_WINDOW_100,
                ETCallback=ET_CALLBACKS_CONTINUOUS,
            )
            rc = dll.MSP430_EnableEnergyTrace(
                ctypes.byref(setup), ctypes.byref(callbacks), ctypes.byref(handle)
            )
            if rc != 0 and mode == "continuous":
                raise RuntimeError(f"MSP430_EnableEnergyTrace(CONTINUOUS) failed: {_dll_err(dll)}")
            if rc != 0:
                print(f"[ET] Primary continuous mode returned {rc} ({_dll_err(dll)}); trying Fallback B...", flush=True)
                used_mode = "fallback_b"
        else:
            used_mode = "fallback_b"

        if used_mode == "fallback_b":
            dll.MSP430_Configure(CONFIG_DEBUG_LPM_X, 1)
            rc = dll.MSP430_OpenDevice(b"MSP430FR5994", b"", 0, 0, 0)
            if rc != 0:
                raise RuntimeError(f"MSP430_OpenDevice failed: {_dll_err(dll)}")
            setup_fb = EnergyTraceSetup(
                ETMode=ET_PROFILING_ANALOG,
                ETFreq=ET_PROFILING_10K,
                ETFormat=ET_ALL,
                ETSampleWindow=ET_EVENT_WINDOW_100,
                ETCallback=ET_CALLBACKS_ONLY_DURING_RUN,
            )
            rc = dll.MSP430_EnableEnergyTrace(
                ctypes.byref(setup_fb), ctypes.byref(callbacks), ctypes.byref(handle)
            )
            if rc != 0:
                raise RuntimeError(f"MSP430_EnableEnergyTrace(Fallback B) failed: {_dll_err(dll)}")
            rc = dll.MSP430_Run(RUN_FREE_RUN, 1)
            if rc != 0:
                raise RuntimeError(f"MSP430_Run(FREE_RUN, releaseJTAG=1) failed: {_dll_err(dll)}")

        et_enabled = True

        # Wait briefly for first callback chunk, then reset buffer & start synchronized window
        t_wait0 = time.perf_counter()
        while time.perf_counter() - t_wait0 < 2.0:
            with lock:
                if state["bytes"] > 0:
                    break
            time.sleep(0.01)

        with lock:
            chunks.clear()
            state["first_ts_us"] = None
            state["last_ts_us"] = None
            state["bytes"] = 0
            et_errors.clear()

        dll.MSP430_ResetEnergyTrace(handle)
        if bench is not None:
            bench.release_start()

        target_us = int(round(duration_s * 1_000_000.0))
        wall_start = time.perf_counter()
        max_wall = duration_s + 5.0
        while True:
            time.sleep(0.02)
            with lock:
                f_ts = state["first_ts_us"]
                l_ts = state["last_ts_us"]
                errs = list(et_errors)
            if errs:
                raise RuntimeError(f"EnergyTrace callback error: {errs}")
            if f_ts is not None and l_ts is not None and (l_ts - f_ts) >= target_us:
                break
            if time.perf_counter() - wall_start >= max_wall:
                break

        dll.MSP430_DisableEnergyTrace(handle)
        et_enabled = False
        dll.MSP430_Close(0)
        initialized = False

        if bench is not None:
            b_rc, b_out = bench.finish(timeout_s=20.0)
            if b_rc != 0:
                raise RuntimeError(f"power_bench.py failed (rc={b_rc}):\n{b_out}")

        with lock:
            raw_stream = b"".join(chunks)

        if not raw_stream:
            raise RuntimeError("No EnergyTrace binary data captured from MSP430.dll")

        decoded = decode_et_stream(raw_stream, max_duration_ns=int(round(duration_s * 1_000_000_000.0)))
        if len(decoded.samples) < 10:
            raise RuntimeError(f"Too few EnergyTrace samples decoded: {len(decoded.samples)}")

        write_energytrace_csv(out_csv, decoded.samples, delimiter=delimiter)
        dur_actual_s = decoded.samples[-1].time_ns * 1e-9
        mean_v = sum(s.voltage_mV for s in decoded.samples) / (len(decoded.samples) * 1000.0)
        summary = {
            "profile": profile,
            "out_csv": str(out_csv),
            "mode": used_mode,
            "sample_count": len(decoded.samples),
            "duration_s": round(dur_actual_s, 4),
            "event_ids": decoded.event_ids,
            "mean_voltage_V": round(mean_v, 4),
            "total_energy_mJ": round(decoded.samples[-1].energy_uJ * 1e-3, 4),
            "trailing_bytes": decoded.trailing_bytes,
        }
        print(f"[ET] Saved {out_csv.name}: {summary}", flush=True)
        return summary
    finally:
        if et_enabled and handle.value:
            try:
                dll.MSP430_DisableEnergyTrace(handle)
            except Exception:
                pass
        if initialized:
            try:
                dll.MSP430_Close(0)
            except Exception:
                pass
        if bench is not None and bench.proc is not None and bench.proc.poll() is None:
            try:
                bench.proc.kill()
                bench.proc.wait(timeout=3.0)
            except Exception:
                pass


def run_self_test() -> None:
    """Offline unit tests for 18-byte IVE / 26-byte SVIE decoding, wraparound, and power_plots compatibility."""
    tools_dir = Path(__file__).resolve().parent
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    import power_plots  # noqa: E402

    # 1. 18-byte IVE (event_id = 8) decoding + 32-bit energy wraparound
    t0_us = 1_000_000
    e0 = 0xFFFFFFF0
    recs = [
        struct.pack("<QIHI", ((t0_us + 0) << 8) | 8, 70_000, 3290, e0),
        struct.pack("<QIHI", ((t0_us + 250) << 8) | 8, 72_000, 3291, 0xFFFFFFFC),
        struct.pack("<QIHI", ((t0_us + 500) << 8) | 8, 68_000, 3289, 10),  # wrapped past 2^32!
    ]
    dec = decode_et_stream(b"".join(recs))
    assert dec.event_ids == [8]
    assert dec.trailing_bytes == 0
    assert len(dec.samples) == 3
    assert dec.samples[0].time_ns == 0 and dec.samples[0].energy_uJ == 0.0
    assert dec.samples[1].time_ns == 250_000 and abs(dec.samples[1].energy_uJ - 1.2) < 1e-9
    # Wrapped delta = (2^32 + 10) - 0xFFFFFFF0 = 16 + 10 = 26 -> 2.6 uJ
    assert dec.samples[2].time_ns == 500_000 and abs(dec.samples[2].energy_uJ - 2.6) < 1e-9

    # 2. 26-byte SVIE (event_id = 7) decoding
    svie = b"".join([
        struct.pack("<QQIHI", ((t0_us + 0) << 8) | 7, 0x11223344, 120_000, 3300, 100),
        struct.pack("<QQIHI", ((t0_us + 1000) << 8) | 7, 0x11223344, 125_000, 3300, 104),
    ])
    dec7 = decode_et_stream(svie)
    assert dec7.event_ids == [7] and len(dec7.samples) == 2
    assert abs(dec7.samples[1].energy_uJ - 0.4) < 1e-9

    # 3. Max-duration cutoff
    dec_cut = decode_et_stream(b"".join(recs), max_duration_ns=300_000)
    assert len(dec_cut.samples) == 2 and dec_cut.samples[-1].time_ns == 250_000

    # 4. CSV export + round-trip parse with power_plots.parse_energytrace_csv (both ',' and ';')
    with tempfile.TemporaryDirectory() as td:
        for idx, delim in enumerate((",", ";")):
            p = Path(td) / f"test_{idx}.csv"
            write_energytrace_csv(p, dec.samples, delimiter=delim)
            tr = power_plots.parse_energytrace_csv(p)
            assert tr.has_dsr_col is False
            assert len(tr.time_s) == 3
            assert abs(float(tr.time_s[-1]) - 0.0005) < 1e-9
            assert abs(float(tr.voltage_V.mean()) - 3.290) < 1e-3

    print("[SELF-TEST] All energytrace_capture offline checks passed.", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Programmatic CCS 12.5 EnergyTrace capture via MSP430.dll synchronized with power_bench.py."
    )
    ap.add_argument(
        "--profile",
        choices=["idle", "play", "ai-heuristic", "ai-mlp", "low-hp", "all"],
        default="idle",
        help="Workload profile to capture (default: idle)",
    )
    ap.add_argument("--duration", type=float, default=30.0, help="Capture duration in seconds (default: 30.0)")
    ap.add_argument("--port", default="COM4", help="Serial port for power_bench.py (default: COM4)")
    ap.add_argument("--baud", type=int, default=115200, help="UART baud rate (default: 115200)")
    ap.add_argument("--out-csv", default=None, help="Output CSV path (single-profile mode)")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Directory for --profile all outputs")
    ap.add_argument("--delimiter", choices=[",", ";"], default=",", help="CSV delimiter (default: ',')")
    ap.add_argument("--ccs-base", default=str(DEFAULT_CCS_BASE), help="TI CCS ccs_base directory")
    ap.add_argument(
        "--mode",
        choices=["auto", "continuous", "fallback_b"],
        default="auto",
        help="EnergyTrace capture mode (default: auto)",
    )
    ap.add_argument("--standalone", action="store_true", help="Capture EnergyTrace without launching power_bench.py")
    ap.add_argument("--fine-current", action="store_true", help="Enable ET_CURRENTDRIVE_FINE (17)")
    ap.add_argument("--self-test", action="store_true", help="Run offline decoder and CSV tests")
    args = ap.parse_args()

    if args.self_test:
        run_self_test()
        return 0

    ccs_base = Path(args.ccs_base)
    if args.profile == "all":
        data_dir = Path(args.data_dir)
        for prof in ALL_PROFILES:
            out_csv = data_dir / PROFILE_TO_CSV[prof]
            capture_profile(
                profile=prof,
                out_csv=out_csv,
                duration_s=args.duration,
                port=args.port,
                baud=args.baud,
                delimiter=args.delimiter,
                ccs_base=ccs_base,
                mode=args.mode,
                standalone=args.standalone,
                fine_current=args.fine_current,
            )
    else:
        out_csv = (
            Path(args.out_csv)
            if args.out_csv
            else Path(args.data_dir) / PROFILE_TO_CSV[args.profile]
        )
        capture_profile(
            profile=args.profile,
            out_csv=out_csv,
            duration_s=args.duration,
            port=args.port,
            baud=args.baud,
            delimiter=args.delimiter,
            ccs_base=ccs_base,
            mode=args.mode,
            standalone=args.standalone,
            fine_current=args.fine_current,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
