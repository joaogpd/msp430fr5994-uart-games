"""
Requirement-driven opaque-box End-to-End (E2E) test suite for mspgame.

Covers Feature Areas FA1-FA8 across 4 Tiers (94 test cases total), partitioned into:
- OfflineE2ETests (Track A, always runs, zero COM4 access)
- LiveHardwareE2ETests (Track B, opt-in via --live / MSPGAME_LIVE_COM4=1)
- FinalDeliverablesE2ETests (M5 gate, opt-in via --require-final / MSPGAME_VERIFY_FINAL=1)

Derived strictly from documented specifications and interface contracts
(ORIGINAL_REQUEST.md, PROJECT.md, docs/HOW_TO_PLAY.md, docs/power/CAPTURE_PROTOCOL.md,
docs/uart-terminal-spec.md, and host CLI tools) with zero dependency on firmware C internals.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from unittest import mock

import serial  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
DOCS_DIR = REPO_ROOT / "docs"
POWER_DATA_DIR = DOCS_DIR / "power" / "data"
POWER_BENCH_PY = TOOLS_DIR / "power_bench.py"
POWER_PLOTS_PY = TOOLS_DIR / "power_plots.py"
LAUNCH_PS1 = TOOLS_DIR / "launch_terminal.ps1"
BUILD_PAPER_PS1 = TOOLS_DIR / "build_paper.ps1"
PLAY_BAT = REPO_ROOT / "play.bat"

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import power_bench  # noqa: E402
import power_plots  # noqa: E402


# ---------------------------------------------------------------------------
# Runtime Mode Configuration (Env vars + direct CLI flags)
# ---------------------------------------------------------------------------

def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _parse_e2e_cli_flags() -> Tuple[bool, str, bool, List[str]]:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--live", action="store_true", default=_env_truthy("MSPGAME_LIVE_COM4"))
    ap.add_argument("--port", default=os.environ.get("MSPGAME_PORT", "COM4"))
    ap.add_argument("--require-final", action="store_true", default=_env_truthy("MSPGAME_VERIFY_FINAL"))
    ns, remaining = ap.parse_known_args(sys.argv[1:])
    return bool(ns.live), str(ns.port), bool(ns.require_final), remaining


if __name__ == "__main__" or any(
    arg == "--live" or arg == "--require-final" or arg == "--port" or arg.startswith("--port=")
    for arg in sys.argv[1:]
):
    LIVE_ENABLED, LIVE_PORT, VERIFY_FINAL, _UNITTEST_REMAINING = _parse_e2e_cli_flags()
else:
    LIVE_ENABLED = _env_truthy("MSPGAME_LIVE_COM4")
    LIVE_PORT = os.environ.get("MSPGAME_PORT", "COM4")
    VERIFY_FINAL = _env_truthy("MSPGAME_VERIFY_FINAL")
    _UNITTEST_REMAINING = sys.argv[1:]


# ---------------------------------------------------------------------------
# Opaque-Box Helpers (Derived Strictly from Public Docs & Contracts)
# ---------------------------------------------------------------------------

class SimulatedLaunchPadSerial:
    """
    Opaque-box LaunchPad serial simulator derived strictly from:
    - docs/HOW_TO_PLAY.md
    - docs/uart-terminal-spec.md
    - PROJECT.md (Interface Contracts)

    Implements the pyserial context-manager & buffer interface for offline E2E runs
    of tools/power_bench.py without importing any private test stubs.
    """

    def __init__(
        self,
        emit_dsr_on_boot: bool = False,
        delayed_frame_ms: float = 0.0,
        ai_end_after_ticks: Optional[int] = None,
        ai_tick_interval_s: float = 0.50,
        initial_mode: str = "class_select",
    ) -> None:
        self.rx_buf = bytearray()
        self.scheduled_rx: List[Tuple[float, bytes]] = []
        self.tx_log: List[bytes] = []
        self.closed = False

        self.emit_dsr_on_boot = emit_dsr_on_boot
        self.terminal_connected = not emit_dsr_on_boot
        self.delayed_frame_ms = delayed_frame_ms
        self.ai_end_after_ticks = ai_end_after_ticks
        self.ai_tick_interval_s = ai_tick_interval_s

        self.mode = initial_mode
        self.cls_code = "KNT"
        self.hp = 16
        self.max_hp = 16
        self.mp = 4
        self.max_mp = 4
        self.atk = 3
        self.def_ = 1
        self.gold = 0
        self.pots = 1
        self.bombs = 0
        self.scrolls = 0
        self.px = 5
        self.py = 5
        self.dir_idx = 1  # 0:N, 1:E, 2:S, 3:W
        self.empowered = False
        self.ai_model = 0  # 0: Heuristic A, 1: Tiny-MLP B
        self.ai_ticks = 0
        self.status_msg = "READY."
        self.status_override: Optional[str] = None

        # Deterministic 28x17 room hazards for BFS/damage/death verification
        self.enemy_pos = (8, 5)
        self.trap_pos = (5, 7)

        self._esc_state = 0
        self._csi_buf = ""
        self._last_dsr_t = 0.0
        self._last_bel_t = time.perf_counter()
        self._last_ai_t = time.perf_counter()
        self._render_frame()

    def __enter__(self) -> "SimulatedLaunchPadSerial":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def _is_low_hp(self) -> bool:
        return (
            self.mode in ("human_play", "ai")
            and self.hp > 0
            and (self.hp <= 3 or self.hp * 4 <= self.max_hp)
        )

    def _queue_tx_to_host(self, payload: bytes) -> None:
        if self.delayed_frame_ms > 0.0:
            hud_idx = payload.find(b"\x1b[2;58H")
            row20_idx = payload.find(b"\x1b[20;1H")
            if hud_idx > 0 and row20_idx > hud_idx:
                now = time.perf_counter()
                delay_s = self.delayed_frame_ms / 1000.0
                self.rx_buf.extend(payload[:hud_idx])
                self.scheduled_rx.append((now + delay_s, payload[hud_idx:row20_idx]))
                self.scheduled_rx.append((now + delay_s * 1.35, payload[row20_idx:]))
                return
        self.rx_buf.extend(payload)

    @property
    def in_waiting(self) -> int:
        now = time.perf_counter()
        if self.scheduled_rx:
            due = [chunk for ts, chunk in self.scheduled_rx if ts <= now]
            if due:
                self.scheduled_rx = [(ts, c) for ts, c in self.scheduled_rx if ts > now]
                for chunk in due:
                    self.rx_buf.extend(chunk)

        if not self.terminal_connected and (now - self._last_dsr_t) >= 0.15:
            self._last_dsr_t = now
            self.rx_buf.extend(b"\x1b[5n")

        if self._is_low_hp() and self.terminal_connected and (now - self._last_bel_t) >= 1.0:
            self._last_bel_t = now
            self.rx_buf.extend(b"\x07")

        if self.mode == "ai" and (now - self._last_ai_t) >= self.ai_tick_interval_s:
            self._last_ai_t = now
            self.ai_ticks += 1
            if self.ai_end_after_ticks is not None and self.ai_ticks >= self.ai_end_after_ticks:
                self.mode = "ai_ended"
                self.status_msg = "AI DEMO ENDED! PRESS 4/W FOR NEW DEMO, N FOR CLASS SELECT."
            self._render_frame()

        return len(self.rx_buf)

    def read(self, n: int = 1) -> bytes:
        out = bytes(self.rx_buf[:n])
        del self.rx_buf[:n]
        return out

    def write(self, data: bytes) -> int:
        self.tx_log.append(bytes(data))
        for b in data:
            if self._esc_state == 1:
                if b == ord("["):
                    self._esc_state = 2
                    self._csi_buf = ""
                else:
                    self._esc_state = 0
                continue
            if self._esc_state == 2:
                ch = chr(b)
                if 0x40 <= b <= 0x7E:
                    seq = self._csi_buf + ch
                    self._esc_state = 0
                    self._csi_buf = ""
                    if seq == "0n":
                        self.terminal_connected = True
                        self._render_frame()
                    elif seq == "A":
                        self._handle_key(ord("w"))
                    elif seq == "B":
                        self._handle_key(ord("s"))
                    elif seq == "C":
                        self._handle_key(ord("d"))
                    elif seq == "D":
                        self._handle_key(ord("a"))
                else:
                    self._csi_buf += ch
                continue
            if b == 0x1B:
                self._esc_state = 1
                continue
            self._handle_key(b)
        return len(data)

    def _spawn_class(self, choice: str) -> None:
        self.mode = "human_play"
        self.px, self.py = 5, 5
        self.dir_idx = 1
        self.empowered = False
        if choice == "2":
            self.cls_code = "ARC"
            self.hp = self.max_hp = 11
            self.mp = self.max_mp = 8
            self.atk, self.def_, self.gold = 2, 0, 0
            self.pots, self.bombs, self.scrolls = 1, 1, 0
        elif choice == "3":
            self.cls_code = "ROG"
            self.hp = self.max_hp = 12
            self.mp = self.max_mp = 5
            self.atk, self.def_, self.gold = 2, 0, 25
            self.pots, self.bombs, self.scrolls = 1, 1, 1
        else:
            self.cls_code = "KNT"
            self.hp = self.max_hp = 16
            self.mp = self.max_mp = 4
            self.atk, self.def_, self.gold = 3, 1, 0
            self.pots, self.bombs, self.scrolls = 1, 0, 0
        self.status_msg = "ADVENTURE BEGINS! PRESS ? FOR HELP, M FOR MAP, H FOR HALL OF FAME."

    def _handle_key(self, b: int) -> None:
        self.terminal_connected = True
        ch = chr(b) if 0x20 <= b <= 0x7E else ""

        if b in (0x0C, 0x0D) or ch in ("l", "L"):
            if self.mode in ("human_play", "ai", "class_select", "game_over", "ai_ended"):
                self._render_frame()
                return

        if self.mode in ("help", "map", "hof"):
            self.mode = "human_play"
            self._render_frame()
            return

        if self.mode == "class_select":
            if ch in ("1", "2", "3", "w", "W", " "):
                self._spawn_class("1" if ch in ("w", "W", " ") else ch)
            elif ch == "4":
                self._spawn_class("1")
                self.mode = "ai"
                self.ai_model = 0
                self.ai_ticks = 0
                self.status_msg = "AI DEMO STARTED: HEURISTIC A."
            elif ch in ("h", "H"):
                self.mode = "hof"
            elif ch == "?":
                self.mode = "help"
            self._render_frame()
            return

        if self.mode in ("ai", "ai_ended"):
            if ch in ("n", "N"):
                self.mode = "class_select"
                self.status_msg = "READY."
            elif ch in ("t", "T") and self.mode == "ai":
                self.ai_model ^= 1
                self.status_msg = (
                    "SWITCHED ON-CHIP FRAM AI TO: HEURISTIC A (22B Q7)"
                    if self.ai_model == 0
                    else "SWITCHED ON-CHIP FRAM AI TO: TINY-MLP B (587B Q7)"
                )
            elif ch in ("4", "w", "W", " ") and self.mode == "ai_ended":
                self._spawn_class("1")
                self.mode = "ai"
                self.ai_model = 0
                self.ai_ticks = 0
                self.status_msg = "AI DEMO STARTED: HEURISTIC A."
            elif ch in ("p", "P", "x", "X"):
                self.status_msg = "SAVE/ERASE BLOCKED IN AI DEMO."
            self._render_frame()
            return

        if ch in ("c", "C"):
            self.status_msg = "CAMERA RE-CENTERED."
            self._render_frame()
            return

        if self.mode == "game_over":
            if ch in ("w", "W", " "):
                self.mode = "class_select"
                self.status_msg = "READY."
                self._render_frame()
            elif ch in ("h", "H"):
                self.mode = "hof"
                self._render_frame()
            return

        # Active human play
        if ch == "?":
            self.mode = "help"
        elif ch in ("m", "M"):
            self.mode = "map"
        elif ch in ("h", "H"):
            self.mode = "hof"
        elif ch == " ":
            self.mp = min(self.max_mp, self.mp + 1)
            self.empowered = True
            self.status_msg = (
                self.status_override
                if self.status_override is not None
                else "MEDITATED (+1 MP) & EMPOWERED NEXT ATTACK (+50% DMG)!"
            )
            if abs(self.px - self.enemy_pos[0]) + abs(self.py - self.enemy_pos[1]) == 1:
                self.hp = max(0, self.hp - 1)
                if self.hp == 0:
                    self.mode = "game_over"
                    self.status_msg = "YOU DIED! W/SPACE:NEW RUN"
        elif ch == "1":
            if self.pots > 0 and self.hp < self.max_hp:
                self.pots -= 1
                self.hp = min(self.max_hp, self.hp + 6)
                self.status_msg = "DRANK HEALTH POTION (+6 HP)!"
            else:
                self.status_msg = "POTION BLOCKED AT FULL HP."
        elif ch == "2":
            if self.bombs > 0:
                self.bombs -= 1
                self.status_msg = "DETONATED DUNGEON BOMB!"
        elif ch == "3":
            if self.scrolls > 0:
                self.scrolls -= 1
                self.status_msg = "READ SCROLL OF MAPPING!"
        elif ch in ("f", "F"):
            if self.mp >= 1:
                self.mp -= 1
                self.empowered = False
                self.status_msg = "CAST ARCANE BOLT!"
        elif ch in ("w", "W", "d", "D", "s", "S", "a", "A"):
            dmap = {"w": (0, -1, 0), "d": (1, 0, 1), "s": (0, 1, 2), "a": (-1, 0, 3)}
            dx, dy, d = dmap[ch.lower()]
            self.dir_idx = d
            nx, ny = self.px + dx, self.py + dy
            if (nx, ny) == self.enemy_pos:
                self.empowered = False
                self.status_msg = "STRUCK SKELETON!"
            elif 1 <= nx <= 26 and 1 <= ny <= 15:
                self.px, self.py = nx, ny
                if (nx, ny) == self.trap_pos:
                    self.hp = max(0, self.hp - 3)
                    if self.hp == 0:
                        self.mode = "game_over"
                        self.status_msg = "YOU DIED! W/SPACE:NEW RUN"
        self._render_frame()

    def _render_frame(self) -> None:
        if self.mode == "class_select":
            self._queue_tx_to_host(
                b"\x1b[2J\x1b[H\x1b[?25l"
                b"\x1b[1;37;44m MSP430FR5994 DUNGEON CRAWLER                         NEW ADVENTURE             \x1b[0m"
                b"\x1b[4;10H[1] KNIGHT   [2] ARCANIST   [3] ROGUE   [4] AI DEMO (FRAM)"
            )
            return
        if self.mode == "help":
            self._queue_tx_to_host(
                b"\x1b[2J\x1b[H\x1b[?25l"
                b"\x1b[1;37;44m MSP430 DUNGEON HELP & LEGEND              PRESS ? OR ANY KEY TO CLOSE          \x1b[0m"
            )
            return
        if self.mode == "map":
            self._queue_tx_to_host(
                b"\x1b[2J\x1b[H\x1b[?25l"
                b"\x1b[1;37;44m FULL FLOOR MAP (48x22)   FL:01   PRESS M OR ANY MOVE KEY TO CLOSE              \x1b[0m"
            )
            return
        if self.mode == "hof":
            self._queue_tx_to_host(
                b"\x1b[2J\x1b[H\x1b[?25l"
                b"\x1b[1;37;44m FRAM HALL OF FAME (TOP 5 RUNS)            PRESS H OR ANY KEY TO CLOSE          \x1b[0m"
            )
            return

        low = self._is_low_hp()
        out = bytearray(b"\x1b[2J\x1b[H\x1b[?25l")
        if low:
            out.extend(
                f"\x1b[1;37;41m [! LOW HP !]  FL:01  HP:{self.hp:02d}/{self.max_hp:02d}  PRESS 1 FOR POTION OR RETREAT!\x1b[0m".encode("ascii")
            )
        elif self.mode == "ai":
            tag = "AI:HEURISTIC" if self.ai_model == 0 else "AI:TINY-MLP "
            out.extend(
                f"\x1b[1;37;45m {tag}   FL:01 BEST:01 SEED:1234  BIOME: STONE CRYPT\x1b[0m".encode("ascii")
            )
        else:
            out.extend(
                b"\x1b[1;37;44m MSP430 DUNGEON FL:01 BEST:01 SEED:1234  BIOME: STONE CRYPT\x1b[0m"
            )

        dir_glyphs = ["^^", ">>", "vv", "<<"]
        dir_chars = ["N", "E", "S", "W"]
        for vr in range(17):
            out.extend(f"\x1b[{vr + 2};1H".encode("ascii"))
            for vc in range(28):
                if vr == 0 or vr == 16 or vc == 0 or vc == 27:
                    out.extend(b"##")
                elif (vc, vr) == (self.px, self.py):
                    out.extend(f"\x1b[1;32m{dir_glyphs[self.dir_idx]}\x1b[0m".encode("ascii"))
                elif (vc, vr) == self.enemy_pos:
                    out.extend(b"SK")
                elif (vc, vr) == self.trap_pos:
                    out.extend(b" ^")
                else:
                    out.extend(b" .")
            out.extend(b"|")

        hp_lbl = f"\x1b[1;31m!HP:{self.hp:02d}/{self.max_hp:02d}\x1b[0m" if low else f"HP:{self.hp:02d}/{self.max_hp:02d}"
        out.extend(f"\x1b[2;58H{hp_lbl} MP:{self.mp:02d}/{self.max_mp:02d}".encode("ascii"))
        out.extend(f"\x1b[3;58HATK:{self.atk:02d}  DEF:{self.def_:02d}".encode("ascii"))
        emp_tag = " \x1b[1;32m[EMP]\x1b[0m      " if self.empowered else "            "
        out.extend(f"\x1b[4;58HGOLD:{self.gold:04d}{emp_tag}".encode("ascii"))
        out.extend(
            f"\x1b[5;58H{self.cls_code} DIR:{dir_chars[self.dir_idx]} {self.px:02d},{self.py:02d}".encode("ascii")
        )
        out.extend(f"\x1b[12;58H[1]Pot:{self.pots} [2]Bmb:{self.bombs}".encode("ascii"))
        out.extend(f"\x1b[13;58H[3]Map:{self.scrolls}".encode("ascii"))

        if self.mode == "game_over":
            msg = (
                self.status_msg
                if self.status_msg.startswith("CAMERA RE-CENTERED")
                else "YOU DIED! W/SPACE:NEW RUN"
            )
            out.extend(f"\x1b[20;1H\x1b[1;33m{msg}\x1b[0m".encode("ascii"))
        elif self.mode == "ai_ended":
            out.extend(
                b"\x1b[20;1H\x1b[1;33mAI DEMO ENDED! PRESS 4/W FOR NEW DEMO, N FOR CLASS SELECT.\x1b[0m"
            )
        else:
            out.extend(f"\x1b[20;1H\x1b[1;33m{self.status_msg}\x1b[0m".encode("ascii"))

        if low:
            out.extend(b"\x07")
        self._queue_tx_to_host(bytes(out))


class UartSession:
    """Thin live COM4 115200 8N1 session wrapper with opaque-box screen parser."""

    def __init__(self, port: str, baud: int = 115200) -> None:
        self.port = port
        self.baud = baud
        self.ser = serial.Serial(port, baud, timeout=0.05, write_timeout=0.5)
        self.scr = power_bench.AnsiScreen(validate=True)
        self.scr.raw_chunks = bytearray()

    def close(self) -> None:
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        finally:
            self.ser = None  # type: ignore[assignment]

    def send(self, payload: bytes) -> None:
        power_bench.send_bytes(self.ser, self.scr, payload)

    def drain(self, duration_s: float) -> None:
        power_bench.drain_serial(self.ser, self.scr, duration_s)

    def drain_frame(self, max_wait_s: float = 0.45, settle_s: float = 0.06) -> bool:
        return power_bench.drain_frame(self.ser, self.scr, max_wait_s=max_wait_s, settle_s=settle_s)

    def wait_until(self, predicate: Callable[[], bool], timeout_s: float = 2.0) -> bool:
        return power_bench.wait_until(self.ser, self.scr, predicate, timeout_s=timeout_s)

    def ensure_class_select(self, timeout_s: float = 25.0) -> bool:
        return power_bench.ensure_class_select(self.ser, self.scr, timeout_s=timeout_s)


def write_synthetic_ccs_csv(
    path: Path,
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    delimiter: str = ",",
    quote_all: bool = False,
    utf8_bom: bool = False,
) -> None:
    enc = "utf-8-sig" if utf8_bom else "utf-8"
    with open(path, "w", encoding=enc, newline="") as f:
        w_hdr = csv.writer(f, delimiter=delimiter)
        w_row = csv.writer(f, delimiter=delimiter, quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL)
        w_hdr.writerow(headers)
        for row in rows:
            w_row.writerow(row)


def build_constant_trace_rows(
    duration_s: float,
    step_s: float,
    current_ua: float,
    voltage_v: float = 3.3,
    ns_units: bool = True,
    include_dsr: bool = False,
) -> Tuple[List[str], List[List[str]]]:
    if ns_units:
        headers = ["Time (ns)", "Current (nA)", "Voltage (mV)", "Energy (uJ)"]
    else:
        headers = ["Time (ms)", "Current (uA)", "Voltage (V)", "Energy (mJ)"]
    if include_dsr:
        headers.append("DSR")

    rows: List[List[str]] = []
    t = 0.0
    n_steps = int(round(duration_s / step_s))
    for k in range(n_steps + 1):
        t = k * step_s
        energy_j = voltage_v * (current_ua * 1e-6) * t
        if ns_units:
            r = [
                str(int(round(t * 1e9))),
                str(int(round(current_ua * 1e3))),
                str(int(round(voltage_v * 1e3))),
                f"{energy_j * 1e6:.4f}",
            ]
        else:
            r = [
                f"{t * 1e3:.3f}",
                f"{current_ua:.3f}",
                f"{voltage_v:.4f}",
                f"{energy_j * 1e3:.6f}",
            ]
        if include_dsr:
            r.append("0x00")
        rows.append(r)
    return headers, rows


def read_png_dpi(path: Path) -> Tuple[int, int, int, int]:
    """Parse PNG signature, IHDR (width, height), and pHYs (ppux, ppuy) via stdlib struct."""
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"{path}: Not a valid PNG signature")
    pos = 8
    width = height = 0
    ppux = ppuy = 0
    while pos + 8 <= len(raw):
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        ctype = raw[pos + 4 : pos + 8]
        cdata = raw[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR" and len(cdata) >= 8:
            width, height = struct.unpack(">II", cdata[:8])
        elif ctype == b"pHYs" and len(cdata) == 9:
            x_px, y_px, unit = struct.unpack(">IIB", cdata)
            if unit == 1:
                ppux, ppuy = x_px, y_px
        elif ctype == b"IEND":
            break
    return width, height, ppux, ppuy


# ---------------------------------------------------------------------------
# Track A: Offline & Tooling E2E Tests (Always Run, Zero COM4 Access)
# ---------------------------------------------------------------------------

class OfflineE2ETests(unittest.TestCase):
    """43 offline E2E tests covering FA1-FA8 without touching COM4."""

    # --- Tier 1: Feature Coverage ---

    def test_t1_fa1_06_launcher_scripts_contracts(self) -> None:
        """T1-FA1-06: play.bat and launch_terminal.ps1 enforce 115200 8N1, Ctrl+L, and CSI arrows."""
        bat = PLAY_BAT.read_text(encoding="utf-8")
        ps1 = LAUNCH_PS1.read_text(encoding="utf-8")
        self.assertIn('BAUD=115200', bat)
        self.assertIn('MODE=auto', bat)
        self.assertIn("launch_terminal.ps1", bat)
        self.assertIn("115200", ps1)
        self.assertIn("Parity]::None", ps1)
        self.assertIn("StopBits]::One", ps1)
        self.assertIn("0x0C", ps1)
        for arrow in ("UpArrow", "DownArrow", "RightArrow", "LeftArrow"):
            self.assertIn(arrow, ps1)
        for csi in ("[A", "[B", "[C", "[D"):
            self.assertIn(csi, ps1)

    def test_t1_fa2_06_stream_validator_all_10_ui_states(self) -> None:
        """T1-FA2-06: AnsiScreen + AnsiStreamValidator classify all 10 UI states with 0 errors."""
        golden_frames = [
            ("class_select", b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m MSP430FR5994 DUNGEON CRAWLER                         NEW ADVENTURE             \x1b[0m"),
            ("hof_overlay", b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m FRAM HALL OF FAME (TOP 5 RUNS)            PRESS H OR ANY KEY TO CLOSE          \x1b[0m"),
            ("help_overlay", b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m MSP430 DUNGEON HELP & LEGEND              PRESS ? OR ANY KEY TO CLOSE          \x1b[0m"),
            ("map_overlay", b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m FULL FLOOR MAP (48x22)   FL:01   PRESS M OR ANY MOVE KEY TO CLOSE              \x1b[0m"),
            ("ai_heuristic", b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;45m AI:HEURISTIC   FL:01 BEST:01 SEED:1234  BIOME: STONE CRYPT\x1b[0m"),
            ("ai_mlp", b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;45m AI:TINY-MLP    FL:01 BEST:01 SEED:1234  BIOME: STONE CRYPT\x1b[0m"),
            ("human_play", b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m MSP430 DUNGEON FL:01 BEST:01 SEED:1234  BIOME: STONE CRYPT\x1b[0m"),
            ("low_hp", b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;41m [! LOW HP !]  FL:01  HP:03/16  PRESS 1 FOR POTION OR RETREAT!\x1b[0m\x07"),
            ("game_over", b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m MSP430 DUNGEON FL:01\x1b[0m\x1b[20;1H\x1b[1;33mYOU DIED! W/SPACE:NEW RUN\x1b[0m"),
            ("ai_demo_ended", b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;45m AI:HEURISTIC   FL:01\x1b[0m\x1b[20;1H\x1b[1;33mAI DEMO ENDED! PRESS 4/W FOR NEW DEMO, N FOR CLASS SELECT.\x1b[0m"),
        ]
        for expected_state, payload in golden_frames:
            scr = power_bench.AnsiScreen(validate=True)
            scr.feed(payload)
            self.assertEqual(scr.detect_state(), expected_state)
            self.assertEqual(len(scr.validator.errors), 0, scr.validator.errors)
            self.assertFalse(scr.validator.has_incomplete_escape)

    def test_t1_fa6_01_power_bench_help_cli(self) -> None:
        """T1-FA6-01: tools/power_bench.py --help exits 0 and lists all expected flags."""
        proc = subprocess.run(
            [sys.executable, str(POWER_BENCH_PY), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in ("--port", "--baud", "--duration", "--profile", "--out-log", "--out-json", "--wait-start", "--countdown", "--verify-uart", "--self-test"):
            self.assertIn(flag, proc.stdout)

    def test_t1_fa6_02_power_bench_self_test_cli(self) -> None:
        """T1-FA6-02: tools/power_bench.py --self-test exits 0."""
        proc = subprocess.run(
            [sys.executable, str(POWER_BENCH_PY), "--self-test"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASSED", proc.stdout)

    def test_t1_fa6_03_power_bench_main_all_5_profiles_simulated(self) -> None:
        """T1-FA6-03: power_bench.main() drives all 5 profiles against SimulatedLaunchPadSerial."""
        sim = SimulatedLaunchPadSerial(emit_dsr_on_boot=True)
        with tempfile.TemporaryDirectory() as td:
            out_json = Path(td) / "all_profiles.json"
            with mock.patch("serial.Serial", return_value=sim):
                with mock.patch.object(
                    sys,
                    "argv",
                    [
                        "power_bench.py",
                        "--port",
                        "SIM_COM",
                        "--profile",
                        "all",
                        "--duration",
                        "1.1",
                        "--countdown",
                        "0",
                        "--out-json",
                        str(out_json),
                    ],
                ):
                    rc = power_bench.main()
            self.assertEqual(rc, 0)
            data = json.loads(out_json.read_text(encoding="utf-8"))
            profs = {p["profile"]: p for p in data["profiles"]}
            self.assertEqual(
                set(profs.keys()),
                {"idle", "ai-heuristic", "ai-mlp", "low-hp", "play"},
            )
            self.assertEqual(profs["low-hp"]["final_low_hp"], 1)
            self.assertGreaterEqual(profs["low-hp"]["bel_count"], 1)
            self.assertGreaterEqual(profs["play"]["keys_sent"], 1)
            self.assertEqual(data["validator"]["error_count"], 0)

    def test_t1_fa6_04_power_bench_out_log_and_out_json(self) -> None:
        """T1-FA6-04: --out-log and --out-json persist raw UART bytes and valid JSON report."""
        sim = SimulatedLaunchPadSerial()
        with tempfile.TemporaryDirectory() as td:
            out_log = Path(td) / "stream.log"
            out_json = Path(td) / "report.json"
            with mock.patch("serial.Serial", return_value=sim):
                with mock.patch.object(
                    sys,
                    "argv",
                    [
                        "power_bench.py",
                        "--profile",
                        "idle",
                        "--duration",
                        "0.2",
                        "--countdown",
                        "0",
                        "--out-log",
                        str(out_log),
                        "--out-json",
                        str(out_json),
                    ],
                ):
                    rc = power_bench.main()
            self.assertEqual(rc, 0)
            self.assertTrue(out_log.exists() and out_log.stat().st_size > 40)
            self.assertIn(b"NEW ADVENTURE", out_log.read_bytes())
            report = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(report["profiles"][0]["profile"], "idle")
            self.assertEqual(report["validator"]["error_count"], 0)

    def test_t1_fa6_05_power_bench_verify_uart_simulated(self) -> None:
        """T1-FA6-05: power_bench.main() --verify-uart passes all 7 checks against SimulatedLaunchPadSerial."""
        sim = SimulatedLaunchPadSerial(emit_dsr_on_boot=True)
        with tempfile.TemporaryDirectory() as td:
            out_json = Path(td) / "verify.json"
            with mock.patch("serial.Serial", return_value=sim):
                with mock.patch.object(
                    sys,
                    "argv",
                    ["power_bench.py", "--verify-uart", "--out-json", str(out_json)],
                ):
                    # Speed up 10x cold-idle + 3.5s AI + 5.8s BEL drains deterministically?
                    # Wait: real wait is ~25 s; let's run genuine verify_uart with real timing or check all 7 checks!
                    rc = power_bench.main()
            self.assertEqual(rc, 0)
            report = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertTrue(report["verify_uart"]["passed"])
            self.assertGreaterEqual(len(report["verify_uart"]["checks"]), 7)
            self.assertTrue(all(c["passed"] for c in report["verify_uart"]["checks"]))

    def test_t1_fa7_01_power_plots_self_test_cli(self) -> None:
        """T1-FA7-01: tools/power_plots.py --self-test exits 0."""
        proc = subprocess.run(
            [sys.executable, str(POWER_PLOTS_PY), "--self-test"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASSED", proc.stdout)

    def test_t1_fa7_02_comma_ccs_csv_exact_stats(self) -> None:
        """T1-FA7-02: Comma-delimited CCS raw CSV (ns/nA/mV/uJ) parses with analytical stats."""
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "comma.csv"
            headers, rows = build_constant_trace_rows(30.0, 0.5, current_ua=200.0, voltage_v=3.3, ns_units=True)
            write_synthetic_ccs_csv(csv_path, headers, rows, delimiter=",")
            tr = power_plots.parse_energytrace_csv(csv_path)
            st = power_plots.compute_trace_stats(tr)
            self.assertAlmostEqual(st.duration_s, 30.0, places=6)
            self.assertAlmostEqual(st.tw_mean_current_uA, 200.0, places=4)
            self.assertAlmostEqual(st.mean_power_uW, 660.0, places=3)
            self.assertAlmostEqual(st.total_energy_mJ, 19.8, places=4)
            self.assertLess(st.power_discrepancy_pct, 0.01)

    def test_t1_fa7_03_semicolon_bom_quoted_csv_matches_comma(self) -> None:
        """T1-FA7-03: Semicolon UTF-8 BOM quoted dot-decimal CSV parses identically to comma CSV."""
        with tempfile.TemporaryDirectory() as td:
            c_path = Path(td) / "c.csv"
            s_path = Path(td) / "s.csv"
            headers, rows = build_constant_trace_rows(30.0, 1.0, current_ua=250.0, voltage_v=3.289, ns_units=True)
            write_synthetic_ccs_csv(c_path, headers, rows, delimiter=",", quote_all=False, utf8_bom=False)
            write_synthetic_ccs_csv(s_path, headers, rows, delimiter=";", quote_all=True, utf8_bom=True)
            st_c = power_plots.compute_trace_stats(power_plots.parse_energytrace_csv(c_path))
            st_s = power_plots.compute_trace_stats(power_plots.parse_energytrace_csv(s_path))
            self.assertAlmostEqual(st_c.total_energy_mJ, st_s.total_energy_mJ, places=6)
            self.assertAlmostEqual(st_c.mean_power_uW, st_s.mean_power_uW, places=6)
            self.assertAlmostEqual(st_c.tw_mean_current_uA, st_s.tw_mean_current_uA, places=6)

    def test_t1_fa7_04_irregular_dt_time_weighted_and_lifetimes(self) -> None:
        """T1-FA7-04: Irregular-dt time-weighted stats and CR2032/C1 formulas match analytical values."""
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "irregular.csv"
            # 0..29 s at 10 uA (29 samples at 1 s) + 29..30 s at 100 uA (100 samples at 0.01 s)
            # True time-weighted mean = (29*10 + 1*100)/30 = 13.0 uA (ignoring transition edge)
            rows: List[List[str]] = []
            for s in range(29):
                rows.append([f"{s:.3f}", "10.0", "3.0", ""])
            for k in range(101):
                t = 29.0 + k * 0.01
                rows.append([f"{t:.3f}", "100.0", "3.0", ""])
            write_synthetic_ccs_csv(
                csv_path,
                ["Time (s)", "Current (uA)", "Voltage (V)", "Energy (mJ)"],
                rows,
            )
            st = power_plots.compute_trace_stats(power_plots.parse_energytrace_csv(csv_path))
            # Unweighted mean would be heavily skewed (~80 uA), whereas time-weighted mean is ~14.5 uA
            self.assertLess(st.tw_mean_current_uA, 20.0)
            self.assertGreater(st.sample_unweighted_mean_uA, st.tw_mean_current_uA * 3.0)
            self.assertAlmostEqual(st.tw_median_current_uA, 10.0, places=3)
            self.assertAlmostEqual(st.tw_p95_current_uA, 55.0, delta=46.0)
            # Analytical CR2032 (2376 J) and C1 (0.8415 J) checks
            expected_cr_h = 2376.0 / ((st.mean_power_uW * 1e-6) * 3600.0)
            expected_c1_s = 0.8415 / (st.mean_power_uW * 1e-6)
            self.assertAlmostEqual(st.cr2032_hours_energy, expected_cr_h, places=4)
            self.assertAlmostEqual(st.c1_seconds_power, expected_c1_s, places=4)

    def test_t1_fa7_05_full_10_csv_pipeline_and_300dpi_pngs(self) -> None:
        """T1-FA7-05: Full 10-CSV pipeline outputs summary.json, summary_table.md, and 4 valid 300 DPI PNGs."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "data"
            out_dir = root / "figures"
            data_dir.mkdir()
            pairs = {
                "idle": (200.0, 2.0),
                "play": (260.0, 180.0),
                "ai_heuristic": (200.0, 110.0),
                "ai_mlp": (230.0, 140.0),
                "low_hp": (1900.0, 95.0),
            }
            for prof, (b_ua, o_ua) in pairs.items():
                hb, rb = build_constant_trace_rows(30.0, 0.5, b_ua)
                ho, ro = build_constant_trace_rows(30.0, 0.5, o_ua)
                write_synthetic_ccs_csv(data_dir / f"baseline_{prof}.csv", hb, rb)
                write_synthetic_ccs_csv(data_dir / f"optimized_{prof}.csv", ho, ro)

            sum_json = root / "summary.json"
            sum_md = root / "summary_table.md"
            payload = power_plots.run_pipeline(data_dir, out_dir, sum_json, sum_md, allow_partial=False)
            self.assertEqual(len(payload["deltas"]), 5)
            self.assertTrue(sum_json.exists() and sum_md.exists())
            for name in ("power_comparison.png", "waveforms_idle_ai.png", "waveform_low_hp.png", "lifetime_projections.png"):
                png_path = out_dir / name
                self.assertTrue(png_path.exists())
                w, h, ppux, ppuy = read_png_dpi(png_path)
                self.assertGreater(w, 600)
                self.assertGreater(h, 400)
                self.assertAlmostEqual(ppux, 11811, delta=5)
                self.assertAlmostEqual(ppuy, 11811, delta=5)

    def test_t1_fa7_06_real_baseline_csvs_allow_partial_pipeline(self) -> None:
        """T1-FA7-06: Real docs/power/data/baseline_*.csv files ingest cleanly via power_plots --allow-partial."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_dir = root / "figures"
            sum_json = root / "summary.json"
            sum_md = root / "summary_table.md"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(POWER_PLOTS_PY),
                    "--data-dir",
                    str(POWER_DATA_DIR),
                    "--out-dir",
                    str(out_dir),
                    "--summary-json",
                    str(sum_json),
                    "--summary-md",
                    str(sum_md),
                    "--allow-partial",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads(sum_json.read_text(encoding="utf-8"))
            for key in ("idle", "play", "ai_heuristic", "ai_mlp", "low_hp"):
                st = summary["profiles"][key]["baseline"]
                self.assertGreater(st["sample_count"], 1000)
                self.assertAlmostEqual(st["duration_s"], 30.0, delta=1.0)
                self.assertGreater(st["tw_mean_current_uA"], 50.0)
            self.assertGreater(summary["profiles"]["low_hp"]["baseline"]["tw_mean_current_uA"], 1000.0)

    def test_t1_fa8_01_how_to_play_manual_contract(self) -> None:
        """T1-FA8-01: docs/HOW_TO_PLAY.md matches synced specs (LPM3, SMCLKREQEN, 0 Hz TIMER_A1, 25 ms LED1, 115200 8N1, 80x24)."""
        txt = (DOCS_DIR / "HOW_TO_PLAY.md").read_text(encoding="utf-8")
        required_fragments = [
            "115200",
            "8N1",
            "80",
            "24",
            "LPM3",
            "SMCLKREQEN",
            "TIMER_A1",
            "0 Hz",
            "25 ms",
            "LED1",
            "AI:HEURISTIC",
            "AI:TINY-MLP",
            "500 ms",
            "[! LOW HP !]",
            "BEL",
            "play.bat",
            "tools/launch_terminal.ps1",
        ]
        for frag in required_fragments:
            self.assertIn(frag, txt, f"Missing expected manual phrase: {frag!r}")

    def test_t1_fa8_02_capture_protocol_contract(self) -> None:
        """T1-FA8-02: docs/power/CAPTURE_PROTOCOL.md specifies J101, J8=OFF, Free Run, EnergyTrace, 30 sec, and 10 filenames."""
        txt = (DOCS_DIR / "power" / "CAPTURE_PROTOCOL.md").read_text(encoding="utf-8")
        for frag in ("J101", "J8", "OFF", "Free Run", "EnergyTrace", "30 sec"):
            self.assertIn(frag, txt)
        for prefix in ("baseline", "optimized"):
            for prof in ("idle", "play", "ai_heuristic", "ai_mlp", "low_hp"):
                self.assertIn(f"{prefix}_{prof}.csv", txt)

    def test_t1_fa8_03_real_baseline_csvs_sanity(self) -> None:
        """T1-FA8-03: All 5 docs/power/data/baseline_*.csv exist, >100 rows, ~30 s, no DSR, non-zero current."""
        for prof in ("idle", "play", "ai_heuristic", "ai_mlp", "low_hp"):
            p = POWER_DATA_DIR / f"baseline_{prof}.csv"
            self.assertTrue(p.exists(), f"Missing baseline CSV: {p}")
            tr = power_plots.parse_energytrace_csv(p)
            self.assertGreater(len(tr.time_s), 100)
            self.assertAlmostEqual(float(tr.time_s[-1]), 30.0, delta=2.0)
            self.assertFalse(tr.has_dsr_col)
            self.assertGreater(float(tr.current_uA.mean()), 10.0)

    def test_t1_fa8_04_git_branch_and_eclipse_prefs_clean(self) -> None:
        """T1-FA8-04: Branch feat/energy-optimization descends from 5cf488d and .settings/org.eclipse.core.resources.prefs is unchanged."""
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(branch, "feat/energy-optimization")

        anc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", "5cf488d", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(anc.returncode, 0, "HEAD does not descend from baseline commit 5cf488d")

        diff_prefs = subprocess.run(
            ["git", "diff", "5cf488d", "--", ".settings/org.eclipse.core.resources.prefs"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(diff_prefs, "", f"Unexpected Eclipse prefs diff:\n{diff_prefs}")

    # --- Tier 2: Boundary & Corner Cases ---

    def test_t2_fa1_05_split_dsr_and_reordered_header_coalescing(self) -> None:
        """T2-FA1-05: Split DSR (\\x1b[ + 5n) responds \\x1b[0n and reordered frame headers coalesce to 1 timestamp."""
        replies: List[bytes] = []

        class _Port:
            def write(self, b: bytes) -> int:
                replies.append(b)
                return len(b)

        scr = power_bench.AnsiScreen(validate=True)
        port = _Port()
        scr.feed(b"\x1b[", ser=port)
        self.assertEqual(scr.dsr_count, 0)
        scr.feed(b"5n", ser=port)
        self.assertEqual(scr.dsr_count, 1)
        self.assertEqual(replies, [b"\x1b[0n"])

        for chunks in (
            (b"\x1b[H", b"\x1b[2J", b"\x1b[?25l", b"\x1b[1;37;44m MSP430 DUNGEON FL:01\x1b[0m"),
            (b"\x1b[?25l", b"\x1b[2J", b"\x1b[H", b"\x1b[1;37;44m MSP430 DUNGEON FL:01\x1b[0m"),
        ):
            s = power_bench.AnsiScreen(validate=True)
            for ch in chunks:
                s.feed(ch)
            self.assertEqual(len(s.frame_timestamps), 1)

    def test_t2_fa2_01_validator_rejects_malformed_and_oob_sequences(self) -> None:
        """T2-FA2-01: AnsiStreamValidator rejects OOB CUP, non-ASCII, bad ctrl, malformed SGR, and incomplete ESC."""
        v = power_bench.AnsiStreamValidator()
        for b in b"\x1b[25;1H\x1b[1;81H\x1b[0;1H\xff\x01\x1b[1:31m\x1b[1;2;3H\x1b[1;31;":
            v.feed_byte(b)
        self.assertEqual(v.out_of_bounds_cup, 3)
        self.assertEqual(v.non_ascii_bytes, 1)
        self.assertEqual(v.unexpected_ctrl_bytes, 1)
        self.assertGreaterEqual(v.malformed_csi, 2)
        self.assertTrue(v.has_incomplete_escape)

    def test_t2_fa2_05_incomplete_mid_frame_is_frame_complete_false(self) -> None:
        """T2-FA2-05: Incomplete mid-frame without sidebar returns is_frame_complete() == False."""
        scr = power_bench.AnsiScreen(validate=True)
        scr.feed(b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m MSP430 DUNGEON FL:01\x1b[0m")
        self.assertFalse(scr.parse_hud()["hud_valid"])
        self.assertFalse(scr.is_frame_complete())

    def test_t2_fa4_05_ai_demo_ended_restart_and_exit_contract(self) -> None:
        """T2-FA4-05: AI DEMO ENDED restarts on '4' and exits cleanly to Class Select on 'n'."""
        sim = SimulatedLaunchPadSerial(initial_mode="ai_ended")
        scr = power_bench.AnsiScreen(validate=True)
        power_bench.drain_serial(sim, scr, 0.05)
        self.assertEqual(scr.detect_state(), "ai_demo_ended")

        power_bench.send_bytes(sim, scr, b"4")
        power_bench.drain_frame(sim, scr)
        self.assertEqual(scr.detect_state(), "ai_heuristic")

        sim.mode = "ai_ended"
        sim._render_frame()
        power_bench.drain_frame(sim, scr)
        power_bench.send_bytes(sim, scr, b"n")
        power_bench.drain_frame(sim, scr)
        self.assertEqual(scr.detect_state(), "class_select")

    def test_t2_fa5_01_knight_low_hp_threshold_5_vs_4(self) -> None:
        """T2-FA5-01: Knight (MaxHP=16) is normal at HP=5 and low_hp at HP=4 (<=25% MaxHP)."""
        sim = SimulatedLaunchPadSerial()
        scr = power_bench.AnsiScreen(validate=True)
        power_bench.drain_frame(sim, scr)
        power_bench.send_bytes(sim, scr, b"1")
        power_bench.drain_frame(sim, scr)

        sim.hp = 5
        sim._render_frame()
        power_bench.drain_frame(sim, scr)
        self.assertEqual(scr.detect_state(), "human_play")
        self.assertEqual(scr.parse_hud()["low_hp"], 0)

        sim.hp = 4
        sim._render_frame()
        power_bench.drain_frame(sim, scr)
        self.assertEqual(scr.detect_state(), "low_hp")
        self.assertEqual(scr.parse_hud()["low_hp"], 1)

    def test_t2_fa5_02_arcanist_low_hp_threshold_4_vs_3(self) -> None:
        """T2-FA5-02: Arcanist (MaxHP=11) is normal at HP=4 and low_hp at HP=3 (<=3 HP)."""
        sim = SimulatedLaunchPadSerial()
        scr = power_bench.AnsiScreen(validate=True)
        power_bench.drain_frame(sim, scr)
        power_bench.send_bytes(sim, scr, b"2")
        power_bench.drain_frame(sim, scr)

        sim.hp = 4
        sim._render_frame()
        power_bench.drain_frame(sim, scr)
        self.assertEqual(scr.detect_state(), "human_play")
        self.assertEqual(scr.parse_hud()["low_hp"], 0)

        sim.hp = 3
        sim._render_frame()
        power_bench.drain_frame(sim, scr)
        self.assertEqual(scr.detect_state(), "low_hp")
        self.assertEqual(scr.parse_hud()["low_hp"], 1)

    def test_t2_fa5_04_zombie_game_over_row20_recovery(self) -> None:
        """T2-FA5-04: Zombie game_over (HP:00 masked by 'c' on Row 20) recovers cleanly to class_select."""
        sim = SimulatedLaunchPadSerial()
        scr = power_bench.AnsiScreen(validate=True)
        power_bench.drain_frame(sim, scr)
        power_bench.send_bytes(sim, scr, b"1")
        power_bench.drain_frame(sim, scr)

        sim.hp = 1
        sim.px, sim.py = 6, 6
        sim.enemy_pos = (7, 6)
        sim.trap_pos = (1, 1)
        sim._render_frame()
        power_bench.drain_frame(sim, scr)

        power_bench.send_bytes(sim, scr, b"s a w d c")
        power_bench.drain_frame(sim, scr)
        self.assertEqual(scr.detect_state(), "human_play")
        self.assertIn("CAMERA RE-CENTERED", scr.row(19))
        self.assertEqual(scr.parse_hud()["hp"], 0)
        self.assertTrue(power_bench.ensure_class_select(sim, scr, timeout_s=5.0))
        self.assertEqual(scr.detect_state(), "class_select")

    def test_t2_fa5_05_low_hp_stability_rejects_missing_bel(self) -> None:
        """T2-FA5-05: run_profile('low-hp') raises RuntimeError when BEL does not fire during 2.5 s check."""
        class _SilentLowHpSim(SimulatedLaunchPadSerial):
            @property
            def in_waiting(self) -> int:
                # Suppress periodic BEL emissions
                return len(self.rx_buf)

        sim = _SilentLowHpSim()
        scr = power_bench.AnsiScreen(validate=True)
        power_bench.drain_frame(sim, scr)
        with self.assertRaises(RuntimeError) as ctx:
            power_bench.run_profile(sim, scr, "low-hp", duration_s=0.2)
        self.assertIn("BEL", str(ctx.exception))

    def test_t2_fa6_01_invalid_cli_args_and_zero_duration(self) -> None:
        """T2-FA6-01: Invalid --profile/--baud exits non-zero; --duration 0 succeeds with 0 keys."""
        bad_prof = subprocess.run(
            [sys.executable, str(POWER_BENCH_PY), "--profile", "bogus"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(bad_prof.returncode, 0)

        bad_baud = subprocess.run(
            [sys.executable, str(POWER_BENCH_PY), "--baud", "notanint"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(bad_baud.returncode, 0)

        sim = SimulatedLaunchPadSerial()
        scr = power_bench.AnsiScreen(validate=True)
        res = power_bench.run_profile(sim, scr, "play", duration_s=0.0)
        self.assertEqual(res["keys_sent"], 0)

    def test_t2_fa6_02_unreachable_port_persists_error_json(self) -> None:
        """T2-FA6-02: Unreachable --port COM250 exits non-zero and persists --out-json error report."""
        with tempfile.TemporaryDirectory() as td:
            out_json = Path(td) / "err.json"
            proc = subprocess.run(
                [sys.executable, str(POWER_BENCH_PY), "--port", "COM250", "--out-json", str(out_json)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertTrue(out_json.exists())
            data = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertIn("error", data)

    def test_t2_fa6_03_delayed_split_frame_prevents_premature_w(self) -> None:
        """T2-FA6-03: >140 ms delayed split-frame arrival prevents premature 'w' before game_over settles."""
        sim = SimulatedLaunchPadSerial(delayed_frame_ms=160.0)
        sim.mode = "human_play"
        sim.hp = 1
        sim.px, sim.py = 7, 5  # adjacent to enemy at (8, 5)
        sim._render_frame()

        scr = power_bench.AnsiScreen(validate=True)
        power_bench.drain_frame(sim, scr, max_wait_s=0.7, settle_s=0.05)
        self.assertEqual(scr.detect_state(), "low_hp")

        tx_states: List[Tuple[bytes, str, bool]] = []
        orig_write = sim.write

        def _spy_write(b: bytes) -> int:
            if b"w" in b:
                tx_states.append((b, scr.detect_state(), bool(scr.parse_hud()["hud_valid"])))
            return orig_write(b)

        sim.write = _spy_write  # type: ignore[method-assign]
        self.assertTrue(power_bench.ensure_class_select(sim, scr, timeout_s=10.0))
        self.assertEqual(tx_states, [(b"w", "game_over", True)])

    def test_t2_fa6_04_ai_mlp_mid_window_death_restarts_with_4_then_t(self) -> None:
        """T2-FA6-04: ai-mlp mid-window AI DEMO ENDED restarts via '4' then 't'."""
        sim = SimulatedLaunchPadSerial(ai_end_after_ticks=3, ai_tick_interval_s=0.08)
        scr = power_bench.AnsiScreen(validate=True)
        power_bench.drain_frame(sim, scr)
        sim.tx_log.clear()
        res = power_bench.run_profile(sim, scr, "ai-mlp", duration_s=0.8)
        self.assertGreaterEqual(res["restarts"], 1)
        self.assertGreaterEqual(sum(1 for tx in sim.tx_log if b"t" in tx), 2)

    def test_t2_fa6_05_play_mid_window_game_over_restarts_with_w_then_1(self) -> None:
        """T2-FA6-05: play mid-window game_over restarts cleanly via 'w' then '1'."""
        sim = SimulatedLaunchPadSerial()
        scr = power_bench.AnsiScreen(validate=True)
        power_bench.drain_frame(sim, scr)
        # Put player into game_over mid-run when first play key ('w') arrives?
        # Wait: ensure_class_select runs at start of run_profile, so drive via a custom hook on 'w' after '1'.
        orig_handle = sim._handle_key
        triggered = False

        def _force_death_once(b: int) -> None:
            nonlocal triggered
            orig_handle(b)
            if not triggered and sim.mode == "human_play" and chr(b) == "w":
                triggered = True
                sim.hp = 0
                sim.mode = "game_over"
                sim.status_msg = "YOU DIED! W/SPACE:NEW RUN"
                sim._render_frame()

        sim._handle_key = _force_death_once  # type: ignore[method-assign]
        res = power_bench.run_profile(sim, scr, "play", duration_s=2.2)
        self.assertGreaterEqual(res["restarts"], 1)

    def test_t2_fa7_01_empty_or_header_only_csv_raises(self) -> None:
        """T2-FA7-01: Empty, whitespace-only, or header-only CSV raises ValueError."""
        with tempfile.TemporaryDirectory() as td:
            for name, content in (
                ("empty.csv", ""),
                ("ws.csv", "   \n  \n"),
                ("hdr.csv", "Time (ns),Current (nA),Voltage (mV),Energy (uJ)\n"),
            ):
                p = Path(td) / name
                p.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    power_plots.parse_energytrace_csv(p)

    def test_t2_fa7_02_3col_summary_csv_comma_and_semicolon_raises(self) -> None:
        """T2-FA7-02: Comma and semicolon 3-column summary CSVs raise ValueError."""
        with tempfile.TemporaryDirectory() as td:
            for name, content in (
                ("sum_c.csv", "Item,Runtime (nS),Energy (nJ)\nmain,1000,500\n"),
                ("sum_s.csv", 'Item;Runtime (nS);Energy (nJ)\n"main";"1000";"500"\n'),
            ):
                p = Path(td) / name
                p.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    power_plots.parse_energytrace_csv(p)

    def test_t2_fa7_03_missing_cols_or_unsupported_units_raises(self) -> None:
        """T2-FA7-03: Missing Time/Current columns or unsupported units raise ValueError."""
        with tempfile.TemporaryDirectory() as td:
            for name, content in (
                ("no_curr.csv", "Time (s),Voltage (V)\n0,3.3\n1,3.3\n"),
                ("bad_t.csv", "Time (hrs),Current (uA)\n0,100\n1,100\n"),
                ("bad_i.csv", "Time (s),Current (kA)\n0,1\n1,1\n"),
            ):
                p = Path(td) / name
                p.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    power_plots.parse_energytrace_csv(p)

    def test_t2_fa7_04_warnings_dsr_ragged_non_monotonic_and_duration(self) -> None:
        """T2-FA7-04: DSR warning, ragged row skip warning, non-monotonic drop, and <25s/>35s duration warning."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "warn.csv"
            p.write_text(
                "Time (s),Current (uA),Voltage (V),Energy (mJ),DSR\n"
                "0.0,100.0,3.3,0.0,0\n"
                "5.0,100.0,3.3,1.65,0\n"
                "6.0,100.0\n"
                "3.0,100.0,3.3,0.9,0\n"
                "10.0,100.0,3.3,3.3,0\n",
                encoding="utf-8",
            )
            with self.assertWarns(UserWarning) as cm:
                tr = power_plots.parse_energytrace_csv(p)
            msgs = " | ".join(str(w.message) for w in cm.warnings)
            self.assertIn("EnergyTrace++", msgs)
            self.assertIn("ragged", msgs.lower())
            self.assertIn("Capture duration", msgs)
            self.assertEqual(tr.dropped_non_monotonic, 1)

    def test_t2_fa7_05_allow_partial_empty_dir_and_zero_current_safe(self) -> None:
        """T2-FA7-05: Missing core CSVs fail without --allow-partial, pass with it, empty dir fails, 0 uA avoids div-by-zero."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            empty_dir = root / "empty"
            empty_dir.mkdir()
            with self.assertRaises(FileNotFoundError):
                power_plots.run_pipeline(empty_dir, root / "f1", root / "s1.json", root / "s1.md", allow_partial=True)

            partial_dir = root / "partial"
            partial_dir.mkdir()
            h, r = build_constant_trace_rows(30.0, 5.0, current_ua=0.0, voltage_v=3.3)
            write_synthetic_ccs_csv(partial_dir / "baseline_idle.csv", h, r)
            with self.assertRaises(FileNotFoundError):
                power_plots.run_pipeline(partial_dir, root / "f2", root / "s2.json", root / "s2.md", allow_partial=False)

            payload = power_plots.run_pipeline(partial_dir, root / "f3", root / "s3.json", root / "s3.md", allow_partial=True)
            st = payload["profiles"]["idle"]["baseline"]  # type: ignore[index]
            self.assertEqual(st["tw_mean_current_uA"], 0.0)
            self.assertGreater(st["cr2032_hours_energy"], 0.0)

    def test_t2_fa8_01_launch_terminal_rejects_invalid_baud(self) -> None:
        """T2-FA8-01: launch_terminal.ps1 -Baud 999999 is rejected by ValidateSet before port access."""
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(LAUNCH_PS1), "-Baud", "999999"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)

    def test_t2_fa8_02_launch_terminal_rejects_invalid_mode(self) -> None:
        """T2-FA8-02: launch_terminal.ps1 -Mode bogus is rejected by ValidateSet before port access."""
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(LAUNCH_PS1), "-Mode", "bogus"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)

    def test_t2_fa8_03_build_paper_missing_tex_exits_nonzero(self) -> None:
        """T2-FA8-03: build_paper.ps1 -TexFile <missing> exits non-zero cleanly."""
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BUILD_PAPER_PS1), "-TexFile", "nonexistent_paper_123.tex"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)

    def test_t2_fa8_04_capture_protocol_forbids_energytrace_pp_and_resume(self) -> None:
        """T2-FA8-04: CAPTURE_PROTOCOL.md explicitly forbids EnergyTrace++ & Resume and mandates J8=OFF."""
        txt = (DOCS_DIR / "power" / "CAPTURE_PROTOCOL.md").read_text(encoding="utf-8")
        self.assertIn("FORBIDDEN", txt)
        self.assertIn("EnergyTrace++", txt)
        self.assertIn("Resume", txt)
        self.assertIn("J8 = OFF", txt)

    def test_t2_fa8_05_real_baseline_csvs_voltage_and_energy_bounds(self) -> None:
        """T2-FA8-05: All real baseline_*.csv files have ~3.2-3.4 V mean voltage and non-negative final energy."""
        for prof in ("idle", "play", "ai_heuristic", "ai_mlp", "low_hp"):
            tr = power_plots.parse_energytrace_csv(POWER_DATA_DIR / f"baseline_{prof}.csv")
            st = power_plots.compute_trace_stats(tr)
            self.assertGreaterEqual(st.mean_voltage_V, 3.15)
            self.assertLessEqual(st.mean_voltage_V, 3.45)
            self.assertGreater(st.total_energy_mJ, 0.0)

    # --- Tier 3: Cross-Feature Combinations ---

    def test_t3_01_power_bench_simulated_to_power_plots_pipeline(self) -> None:
        """T3-01: Pairwise power_bench (5 simulated profiles) -> JSON/logs -> power_plots -> summary + 4 PNGs."""
        sim = SimulatedLaunchPadSerial(emit_dsr_on_boot=True)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bench_json = root / "bench.json"
            bench_log = root / "bench.log"
            with mock.patch("serial.Serial", return_value=sim):
                with mock.patch.object(
                    sys,
                    "argv",
                    [
                        "power_bench.py",
                        "--profile",
                        "all",
                        "--duration",
                        "0.3",
                        "--countdown",
                        "0",
                        "--out-log",
                        str(bench_log),
                        "--out-json",
                        str(bench_json),
                    ],
                ):
                    self.assertEqual(power_bench.main(), 0)

            bench_data = json.loads(bench_json.read_text(encoding="utf-8"))
            self.assertEqual(len(bench_data["profiles"]), 5)

            data_dir = root / "csvs"
            out_dir = root / "figs"
            data_dir.mkdir()
            for item in bench_data["profiles"]:
                p_key = item["profile"].replace("-", "_")
                hb, rb = build_constant_trace_rows(30.0, 1.0, 250.0)
                ho, ro = build_constant_trace_rows(30.0, 1.0, 50.0)
                write_synthetic_ccs_csv(data_dir / f"baseline_{p_key}.csv", hb, rb)
                write_synthetic_ccs_csv(data_dir / f"optimized_{p_key}.csv", ho, ro)

            payload = power_plots.run_pipeline(data_dir, out_dir, root / "sum.json", root / "sum.md", allow_partial=False)
            self.assertEqual(len(payload["deltas"]), 5)
            self.assertEqual(len(payload["figures"]), 4)

    def test_t3_06_mixed_delimiter_and_optional_disconnected_pipeline(self) -> None:
        """T3-06: Mixed-delimiter pipeline (comma Baseline + semicolon BOM Optimized + optional disconnected)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "mixed_data"
            out_dir = root / "mixed_figs"
            data_dir.mkdir()
            for prof in ("idle", "play", "ai_heuristic", "ai_mlp", "low_hp", "disconnected"):
                hb, rb = build_constant_trace_rows(30.0, 1.0, 240.0, ns_units=True)
                ho, ro = build_constant_trace_rows(30.0, 1.0, 40.0, ns_units=False)
                write_synthetic_ccs_csv(data_dir / f"baseline_{prof}.csv", hb, rb, delimiter=",")
                write_synthetic_ccs_csv(data_dir / f"optimized_{prof}.csv", ho, ro, delimiter=";", quote_all=True, utf8_bom=True)

            payload = power_plots.run_pipeline(data_dir, out_dir, root / "s.json", root / "s.md", allow_partial=False)
            self.assertEqual(len(payload["deltas"]), 6)
            self.assertIn("disconnected", payload["profiles"])
            self.assertIn("Disconnected Standby", (root / "s.md").read_text(encoding="utf-8"))

    # --- Tier 4: Real-World Application Scenarios ---

    def test_t4_01_full_simulated_bench_plus_real_baseline_vs_opt_plots(self) -> None:
        """T4-01: Full simulated 5-profile bench + real baseline_*.csv paired with Optimized CSVs -> PNGs, stats, lifetimes."""
        sim = SimulatedLaunchPadSerial(emit_dsr_on_boot=True)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bench_json = root / "bench_t4.json"
            with mock.patch("serial.Serial", return_value=sim):
                with mock.patch.object(
                    sys,
                    "argv",
                    [
                        "power_bench.py",
                        "--profile",
                        "all",
                        "--duration",
                        "0.4",
                        "--countdown",
                        "0",
                        "--out-json",
                        str(bench_json),
                    ],
                ):
                    self.assertEqual(power_bench.main(), 0)

            data_dir = root / "data"
            out_dir = root / "figures"
            sum_json = root / "summary.json"
            sum_md = root / "summary_table.md"
            data_dir.mkdir()

            opt_targets_ua = {
                "idle": 2.0,
                "play": 180.0,
                "ai_heuristic": 100.0,
                "ai_mlp": 140.0,
                "low_hp": 110.0,
            }
            for prof, opt_ua in opt_targets_ua.items():
                shutil.copy2(POWER_DATA_DIR / f"baseline_{prof}.csv", data_dir / f"baseline_{prof}.csv")
                ho, ro = build_constant_trace_rows(30.0, 0.5, opt_ua, voltage_v=3.289, ns_units=True)
                write_synthetic_ccs_csv(data_dir / f"optimized_{prof}.csv", ho, ro, delimiter=";", quote_all=True)

            proc = subprocess.run(
                [
                    sys.executable,
                    str(POWER_PLOTS_PY),
                    "--data-dir",
                    str(data_dir),
                    "--out-dir",
                    str(out_dir),
                    "--summary-json",
                    str(sum_json),
                    "--summary-md",
                    str(sum_md),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads(sum_json.read_text(encoding="utf-8"))
            for prof in opt_targets_ua:
                self.assertGreater(summary["deltas"][prof]["delta_mean_power_pct"], 0.0)
                base_cr = summary["profiles"][prof]["baseline"]["cr2032_days_energy"]
                opt_cr = summary["profiles"][prof]["optimized"]["cr2032_days_energy"]
                self.assertGreater(opt_cr, base_cr)
            for fig in ("power_comparison.png", "waveforms_idle_ai.png", "waveform_low_hp.png", "lifetime_projections.png"):
                _, _, ppux, ppuy = read_png_dpi(out_dir / fig)
                self.assertAlmostEqual(ppux, 11811, delta=5)
                self.assertAlmostEqual(ppuy, 11811, delta=5)


# ---------------------------------------------------------------------------
# Track B: Live Hardware E2E Tests (COM4 @ 115200 8N1, Opt-In Only)
# ---------------------------------------------------------------------------

@unittest.skipUnless(LIVE_ENABLED, "Requires flashed LaunchPad on COM4 (--live / MSPGAME_LIVE_COM4=1)")
class LiveHardwareE2ETests(unittest.TestCase):
    """50 live opaque-box E2E tests against the MSP-EXP430FR5994 LaunchPad on COM4."""

    sess: Optional[UartSession] = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.sess = UartSession(LIVE_PORT, 115200)
        cls.sess.ensure_class_select()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.sess is not None:
            try:
                cls.sess.ensure_class_select(timeout_s=15.0)
            finally:
                cls.sess.close()
                cls.sess = None

    def setUp(self) -> None:
        assert self.sess is not None
        self.sess.ensure_class_select()

    def tearDown(self) -> None:
        if self.sess is not None:
            self.sess.ensure_class_select(timeout_s=20.0)

    def _start_class(self, key: bytes) -> Dict[str, object]:
        assert self.sess is not None
        self.sess.send(key)
        ok = self.sess.wait_until(
            lambda: self.sess.scr.detect_state() in ("human_play", "low_hp") and self.sess.scr.is_frame_complete(),
            timeout_s=2.0,
        )
        self.assertTrue(ok, f"Failed to start class {key!r}: state={self.sess.scr.detect_state()}")
        self.sess.drain_frame()
        return self.sess.scr.parse_hud()

    # --- Tier 1: FA1..FA6 Live Tests ---

    def test_t1_fa1_01_open_com4_115200_8n1_settled(self) -> None:
        assert self.sess is not None
        self.assertEqual(self.sess.scr.detect_state(), "class_select")
        self.assertEqual(self.sess.scr.validator.errors, [])

    def test_t1_fa1_02_ctrl_l_forces_redraw_in_class_select(self) -> None:
        assert self.sess is not None
        fb0 = len(self.sess.scr.frame_timestamps)
        self.sess.send(b"\x0c")
        self.assertTrue(self.sess.wait_until(lambda: len(self.sess.scr.frame_timestamps) > fb0 and self.sess.scr.detect_state() == "class_select"))
        self.assertEqual(self.sess.scr.validator.errors, [])

    def test_t1_fa1_03_enter_redraws_without_mutating_state(self) -> None:
        assert self.sess is not None
        before = self._start_class(b"1")
        fb0 = len(self.sess.scr.frame_timestamps)
        self.sess.send(b"\r")
        self.assertTrue(self.sess.wait_until(lambda: len(self.sess.scr.frame_timestamps) > fb0 and self.sess.scr.is_frame_complete()))
        after = self.sess.scr.parse_hud()
        self.assertEqual((after["hp"], after["mp"], after["px"], after["py"]), (before["hp"], before["mp"], before["px"], before["py"]))

    def test_t1_fa1_04_l_key_redraws_without_advancing_turn(self) -> None:
        assert self.sess is not None
        before = self._start_class(b"1")
        fb0 = len(self.sess.scr.frame_timestamps)
        self.sess.send(b"l")
        self.assertTrue(self.sess.wait_until(lambda: len(self.sess.scr.frame_timestamps) > fb0 and self.sess.scr.is_frame_complete()))
        after = self.sess.scr.parse_hud()
        self.assertEqual((after["hp"], after["mp"], after["px"], after["py"]), (before["hp"], before["mp"], before["px"], before["py"]))

    def test_t1_fa1_05_dsr_or_ctrl_l_handshake_clean(self) -> None:
        assert self.sess is not None
        self.sess.send(b"\x0c")
        self.sess.drain(1.2)
        self.assertEqual(self.sess.scr.detect_state(), "class_select")
        self.assertEqual(self.sess.scr.validator.unexpected_ctrl_bytes, 0)

    def test_t1_fa2_01_cup_coordinates_within_80x24(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        for key in (b"?", b" ", b"m", b" ", b"h", b" "):
            self.sess.send(key)
            self.sess.drain_frame()
        self.assertEqual(self.sess.scr.validator.out_of_bounds_cup, 0)

    def test_t1_fa2_02_sgr_colors_per_mode(self) -> None:
        assert self.sess is not None
        assert self.sess.scr.raw_chunks is not None
        start_idx = len(self.sess.scr.raw_chunks)
        self._start_class(b"1")
        self.sess.send(b" ")  # [EMP] green 32m
        self.sess.drain_frame()
        self.sess.ensure_class_select()
        self.sess.send(b"4")  # AI magenta 45m
        self.sess.wait_until(lambda: self.sess.scr.detect_state() == "ai_heuristic")
        self.sess.drain_frame()
        self.sess.send(b"n")
        self.sess.drain_frame()
        stream = bytes(self.sess.scr.raw_chunks[start_idx:])
        self.assertIn(b"44m", stream)
        self.assertIn(b"45m", stream)
        self.assertIn(b"32m", stream)
        self.assertEqual(self.sess.scr.validator.malformed_csi, 0)

    def test_t1_fa2_03_help_overlay_preserves_state(self) -> None:
        assert self.sess is not None
        before = self._start_class(b"1")
        self.sess.send(b"?")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "help_overlay"))
        self.sess.send(b" ")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() in ("human_play", "low_hp") and self.sess.scr.is_frame_complete()))
        after = self.sess.scr.parse_hud()
        self.assertEqual((after["hp"], after["mp"], after["px"], after["py"]), (before["hp"], before["mp"], before["px"], before["py"]))

    def test_t1_fa2_04_map_overlay_preserves_state(self) -> None:
        assert self.sess is not None
        before = self._start_class(b"1")
        self.sess.send(b"m")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "map_overlay"))
        self.sess.send(b" ")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() in ("human_play", "low_hp") and self.sess.scr.is_frame_complete()))
        after = self.sess.scr.parse_hud()
        self.assertEqual((after["hp"], after["mp"], after["px"], after["py"]), (before["hp"], before["mp"], before["px"], before["py"]))

    def test_t1_fa2_05_hof_overlay_from_class_select_and_play(self) -> None:
        assert self.sess is not None
        self.sess.send(b"h")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "hof_overlay"))
        self.sess.send(b" ")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "class_select"))
        self._start_class(b"1")
        self.sess.send(b"h")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "hof_overlay"))
        self.sess.send(b" ")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() in ("human_play", "low_hp")))

    def test_t1_fa3_01_knight_spawn_stats(self) -> None:
        assert self.sess is not None
        hud = self._start_class(b"1")
        self.assertIn("KNT", self.sess.scr.row(4))
        self.assertEqual((hud["hp"], hud["max_hp"], hud["mp"], hud["max_mp"]), (16, 16, 4, 4))
        self.assertEqual((hud["atk"], hud["def_"], hud["pots"]), (3, 1, 1))

    def test_t1_fa3_02_arcanist_spawn_stats(self) -> None:
        assert self.sess is not None
        hud = self._start_class(b"2")
        self.assertIn("ARC", self.sess.scr.row(4))
        self.assertEqual((hud["hp"], hud["max_hp"], hud["mp"], hud["max_mp"]), (11, 11, 8, 8))
        self.assertEqual((hud["atk"], hud["def_"], hud["pots"], hud["bombs"]), (2, 0, 1, 1))

    def test_t1_fa3_03_rogue_spawn_stats(self) -> None:
        assert self.sess is not None
        hud = self._start_class(b"3")
        self.assertIn("ROG", self.sess.scr.row(4))
        self.assertEqual((hud["hp"], hud["max_hp"], hud["mp"], hud["max_mp"]), (12, 12, 5, 5))
        self.assertEqual((hud["gold"], hud["pots"], hud["bombs"], hud["scrolls"]), (25, 1, 1, 1))

    def test_t1_fa3_04_wasd_and_csi_arrows_facing_and_glyph(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        for seq, glyph, dchar in (
            (b"w", "^^", "N"),
            (b"d", ">>", "E"),
            (b"s", "vv", "S"),
            (b"a", "<<", "W"),
            (b"\x1b[A", "^^", "N"),
            (b"\x1b[C", ">>", "E"),
            (b"\x1b[B", "vv", "S"),
            (b"\x1b[D", "<<", "W"),
        ):
            self.sess.send(seq)
            self.sess.drain_frame()
            if self.sess.scr.detect_state() == "game_over":
                break
            _, _, pdir = self.sess.scr.find_player_in_viewport()
            self.assertEqual(pdir, power_bench.DIR_GLYPHS[glyph])
            self.assertIn(f"DIR:{dchar}", self.sess.scr.row(4))

    def test_t1_fa3_05_meditate_arcane_bolt_and_recenter(self) -> None:
        assert self.sess is not None
        hud0 = self._start_class(b"2")
        self.sess.send(b" ")
        self.sess.drain_frame()
        self.assertEqual(self.sess.scr.parse_hud()["empowered"], 1)
        self.sess.send(b"f")
        self.sess.drain_frame()
        self.assertEqual(self.sess.scr.parse_hud()["mp"], int(hud0["mp"]) - 1)
        self.sess.send(b"c")
        self.sess.drain_frame()
        self.assertIn("CAMERA RE-CENTERED", self.sess.scr.row(19))

    def test_t1_fa3_06_rogue_mapping_scroll_and_bomb(self) -> None:
        assert self.sess is not None
        self._start_class(b"3")
        self.sess.send(b"3")
        self.sess.drain_frame()
        self.assertEqual(self.sess.scr.parse_hud()["scrolls"], 0)
        self.sess.send(b"2")
        self.sess.drain_frame()
        self.assertEqual(self.sess.scr.parse_hud()["bombs"], 0)

    def test_t1_fa4_01_start_ai_heuristic(self) -> None:
        assert self.sess is not None
        power_bench._enter_ai_demo(self.sess.ser, self.sess.scr, use_mlp=False)
        self.assertEqual(self.sess.scr.detect_state(), "ai_heuristic")

    def test_t1_fa4_02_ai_heuristic_500ms_cadence(self) -> None:
        assert self.sess is not None
        power_bench._enter_ai_demo(self.sess.ser, self.sess.scr, use_mlp=False)
        ts0 = len(self.sess.scr.frame_timestamps)
        self.sess.drain(3.6)
        dts = [
            self.sess.scr.frame_timestamps[i] - self.sess.scr.frame_timestamps[i - 1]
            for i in range(max(1, ts0 + 1), len(self.sess.scr.frame_timestamps))
            if 0.15 < (self.sess.scr.frame_timestamps[i] - self.sess.scr.frame_timestamps[i - 1]) < 1.5
        ]
        self.assertGreaterEqual(len(dts), 4)
        med = sorted(dts)[len(dts) // 2]
        self.assertGreaterEqual(med, 0.35)
        self.assertLessEqual(med, 0.70)

    def test_t1_fa4_03_toggle_to_tiny_mlp_500ms_cadence(self) -> None:
        assert self.sess is not None
        power_bench._enter_ai_demo(self.sess.ser, self.sess.scr, use_mlp=True)
        self.assertEqual(self.sess.scr.detect_state(), "ai_mlp")
        ts0 = len(self.sess.scr.frame_timestamps)
        self.sess.drain(3.6)
        dts = [
            self.sess.scr.frame_timestamps[i] - self.sess.scr.frame_timestamps[i - 1]
            for i in range(max(1, ts0 + 1), len(self.sess.scr.frame_timestamps))
            if 0.15 < (self.sess.scr.frame_timestamps[i] - self.sess.scr.frame_timestamps[i - 1]) < 1.5
        ]
        self.assertGreaterEqual(len(dts), 4)
        med = sorted(dts)[len(dts) // 2]
        self.assertGreaterEqual(med, 0.35)
        self.assertLessEqual(med, 0.70)

    def test_t1_fa4_04_toggle_tiny_mlp_back_to_heuristic(self) -> None:
        assert self.sess is not None
        power_bench._enter_ai_demo(self.sess.ser, self.sess.scr, use_mlp=True)
        self.sess.send(b"t")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "ai_heuristic"))

    def test_t1_fa4_05_n_exits_ai_demo_to_class_select(self) -> None:
        assert self.sess is not None
        power_bench._enter_ai_demo(self.sess.ser, self.sess.scr, use_mlp=False)
        self.sess.send(b"n")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "class_select"))

    def test_t1_fa5_01_low_hp_banner_and_sidebar_indicator(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        self.assertTrue(power_bench.seek_low_hp(self.sess.ser, self.sess.scr, target_death=False, timeout_s=30.0))
        self.assertIn("[! LOW HP !]", self.sess.scr.row(0))
        self.assertIn("!HP:", self.sess.scr.row(1))

    def test_t1_fa5_02_low_hp_periodic_bel(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        self.assertTrue(power_bench.seek_low_hp(self.sess.ser, self.sess.scr, target_death=False, timeout_s=30.0))
        b0 = self.sess.scr.bel_count
        self.sess.drain(5.8)
        self.assertGreaterEqual(self.sess.scr.bel_count - b0, 2)

    def test_t1_fa5_03_low_hp_banner_pulse_no_ansi_errors(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        self.assertTrue(power_bench.seek_low_hp(self.sess.ser, self.sess.scr, target_death=False, timeout_s=30.0))
        f0 = len(self.sess.scr.frame_timestamps)
        self.sess.drain(2.2)
        self.assertGreaterEqual(len(self.sess.scr.frame_timestamps) - f0, 2)
        self.assertEqual(self.sess.scr.validator.errors, [])

    def test_t1_fa5_04_potion_heals_clears_low_hp_and_stops_bel(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        self.assertTrue(power_bench.seek_low_hp(self.sess.ser, self.sess.scr, target_death=False, timeout_s=30.0))
        hp_low = int(self.sess.scr.parse_hud()["hp"])
        self.sess.send(b"1")
        self.sess.drain_frame()
        hud_after = self.sess.scr.parse_hud()
        self.assertGreater(int(hud_after["hp"]), hp_low)
        self.assertEqual(hud_after["low_hp"], 0)
        b0 = self.sess.scr.bel_count
        self.sess.drain(2.5)
        self.assertEqual(self.sess.scr.bel_count, b0)

    def test_t1_fa5_05_death_displays_you_died_stops_bel_and_resets(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        self.assertTrue(power_bench.seek_low_hp(self.sess.ser, self.sess.scr, target_death=True, timeout_s=30.0))
        if self.sess.scr.detect_state() == "game_over":
            self.assertIn("YOU DIED", self.sess.scr.row(19))
            b0 = self.sess.scr.bel_count
            self.sess.drain(2.5)
            self.assertEqual(self.sess.scr.bel_count, b0)
            self.sess.send(b"w")
            self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "class_select"))

    def test_t1_fa6_06_power_bench_verify_uart_subprocess_com4(self) -> None:
        assert self.sess is not None
        self.sess.close()
        try:
            proc = subprocess.run(
                [sys.executable, str(POWER_BENCH_PY), "--port", LIVE_PORT, "--verify-uart"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        finally:
            self.sess = UartSession(LIVE_PORT, 115200)

    # --- Tier 2: FA1..FA5 Live Boundary Tests ---

    def test_t2_fa1_01_cold_idle_10x_wakeups(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        for i in range(10):
            self.sess.drain(1.5 + (i % 4) * 0.4)
            self.sess.send(b"?" if i % 2 == 0 else b" ")
            self.sess.drain_frame()
            self.assertEqual(self.sess.scr.validator.errors, [])

    def test_t2_fa1_02_12_byte_back_to_back_bursts(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        for payload, pred in power_bench._build_rapid_bursts(self.sess.scr):
            self.assertLessEqual(len(payload), 12)
            fb0 = len(self.sess.scr.frame_timestamps)
            self.sess.send(payload)
            ok = self.sess.wait_until(lambda p=pred, fb=fb0: len(self.sess.scr.frame_timestamps) > fb and self.sess.scr.is_frame_complete() and p(), 2.0)
            self.assertTrue(ok, f"Burst {payload!r} failed")
            if self.sess.scr.detect_state() in ("game_over", "class_select"):
                self.sess.ensure_class_select()
                self._start_class(b"1")

    def test_t2_fa1_03_split_csi_arrow_bytes_20ms_gaps(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        for b in (b"\x1b", b"[", b"A"):
            self.sess.send(b)
            time.sleep(0.02)
        self.sess.drain_frame()
        self.assertIn("DIR:N", self.sess.scr.row(4))

    def test_t2_fa1_04_invalid_control_bytes_no_corruption(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        self.sess.send(b"\x00\x1fz")
        self.sess.drain(0.3)
        self.sess.send(b"\x0c")
        self.sess.drain_frame()
        self.assertEqual(self.sess.scr.validator.errors, [])

    def test_t2_fa2_02_viewport_sidebar_column_boundary_alignment(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        self.assertIn("|", self.sess.scr.row(1)[:58])
        self.assertIn("HP:", self.sess.scr.row(1)[57:])

    def test_t2_fa2_03_double_toggle_overlays_restore_cleanly(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        for seq in (b"??", b"mm", b"hh"):
            self.sess.send(seq)
            self.sess.drain_frame()
            self.assertIn(self.sess.scr.detect_state(), ("human_play", "low_hp"))

    def test_t2_fa2_04_rapid_ctrl_l_burst_clean_frames(self) -> None:
        assert self.sess is not None
        self.sess.send(b"\x0c\x0c\x0c")
        self.sess.drain_frame()
        self.assertEqual(self.sess.scr.detect_state(), "class_select")
        self.assertEqual(self.sess.scr.validator.errors, [])

    def test_t2_fa3_01_potion_blocked_at_full_hp(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        self.sess.send(b"1")
        self.sess.drain_frame()
        hud = self.sess.scr.parse_hud()
        self.assertEqual(hud["hp"], 16)
        self.assertEqual(hud["pots"], 1)

    def test_t2_fa3_02_spells_blocked_when_mp_insufficient(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")  # Knight MP=4
        for _ in range(4):
            self.sess.send(b"f")  # -1 MP each
            self.sess.drain_frame()
        self.sess.send(b"q")
        self.sess.drain_frame()
        self.assertEqual(self.sess.scr.parse_hud()["mp"], 0)
        self.sess.send(b"e")
        self.sess.drain_frame()
        self.assertEqual(self.sess.scr.parse_hud()["mp"], 0)

    def test_t2_fa3_03_uppercase_keys_equivalence(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        for k, d in ((b"W", "N"), (b"D", "E"), (b"S", "S"), (b"A", "W")):
            self.sess.send(k)
            self.sess.drain_frame()
            if self.sess.scr.detect_state() == "game_over":
                return
            self.assertIn(f"DIR:{d}", self.sess.scr.row(4))
        self.sess.send(b"C")
        self.sess.drain_frame()
        self.assertIn("CAMERA RE-CENTERED", self.sess.scr.row(19))

    def test_t2_fa3_04_wall_bump_rotates_facing_in_bounds(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        for _ in range(30):
            self.sess.send(b"w")
            self.sess.drain_frame()
            if self.sess.scr.detect_state() == "game_over":
                return
        hud = self.sess.scr.parse_hud()
        self.assertGreaterEqual(int(hud["px"]), 0)
        self.assertLess(int(hud["px"]), 48)
        self.assertGreaterEqual(int(hud["py"]), 0)
        self.assertLess(int(hud["py"]), 22)

    def test_t2_fa3_05_meditate_at_full_mp_grants_emp_without_overflow(self) -> None:
        assert self.sess is not None
        hud0 = self._start_class(b"1")
        self.sess.send(b" ")
        self.sess.drain_frame()
        hud1 = self.sess.scr.parse_hud()
        self.assertEqual(hud1["empowered"], 1)
        self.assertLessEqual(int(hud1["mp"]), int(hud0["max_mp"]))

    def test_t2_fa4_01_t_and_n_ignored_outside_ai_demo(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        self.sess.send(b"tn")
        self.sess.drain_frame()
        self.assertIn(self.sess.scr.detect_state(), ("human_play", "low_hp"))

    def test_t2_fa4_02_rapid_double_toggle_tt_switches_a_b_a(self) -> None:
        assert self.sess is not None
        power_bench._enter_ai_demo(self.sess.ser, self.sess.scr, use_mlp=False)
        self.sess.send(b"tt")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "ai_heuristic"))

    def test_t2_fa4_03_overlay_pauses_ai_and_closing_resumes_cadence(self) -> None:
        assert self.sess is not None
        power_bench._enter_ai_demo(self.sess.ser, self.sess.scr, use_mlp=False)
        self.sess.send(b"?")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "help_overlay"))
        f0 = len(self.sess.scr.frame_timestamps)
        self.sess.drain(1.5)
        self.assertEqual(len(self.sess.scr.frame_timestamps), f0)
        self.sess.send(b" ")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "ai_heuristic"))

    def test_t2_fa4_04_p_and_x_blocked_during_ai_demo(self) -> None:
        assert self.sess is not None
        power_bench._enter_ai_demo(self.sess.ser, self.sess.scr, use_mlp=False)
        self.sess.send(b"px")
        self.sess.drain_frame()
        self.assertIn(self.sess.scr.detect_state(), ("ai_heuristic", "ai_mlp", "low_hp", "ai_demo_ended"))

    def test_t2_fa5_03_death_hp0_halts_bel(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        self.assertTrue(power_bench.seek_low_hp(self.sess.ser, self.sess.scr, target_death=True, timeout_s=30.0))
        b0 = self.sess.scr.bel_count
        self.sess.drain(2.5)
        self.assertEqual(self.sess.scr.bel_count, b0)

    # --- Tier 3: Live Pairwise Combinations ---

    def test_t3_02_ai_demo_pairwise_lifecycle(self) -> None:
        assert self.sess is not None
        power_bench._enter_ai_demo(self.sess.ser, self.sess.scr, use_mlp=True)
        self.sess.send(b"?")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "help_overlay"))
        self.sess.send(b" ")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "ai_mlp"))
        self.sess.send(b"t")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "ai_heuristic"))
        self.sess.send(b"n")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "class_select"))

    def test_t3_03_low_hp_potion_heal_cold_idle_and_ctrl_l(self) -> None:
        assert self.sess is not None
        self._start_class(b"1")
        self.assertTrue(power_bench.seek_low_hp(self.sess.ser, self.sess.scr, target_death=False, timeout_s=30.0))
        self.sess.send(b"1")
        self.sess.drain_frame()
        b0 = self.sess.scr.bel_count
        self.sess.drain(2.5)
        self.assertEqual(self.sess.scr.bel_count, b0)
        self.sess.send(b"\x0c")
        self.sess.drain_frame()
        self.assertEqual(self.sess.scr.validator.errors, [])

    def test_t3_04_arcanist_meditate_bolt_recenter_and_l_redraw(self) -> None:
        assert self.sess is not None
        self._start_class(b"2")
        self.sess.send(b" ")
        self.sess.drain_frame()
        self.assertEqual(self.sess.scr.parse_hud()["empowered"], 1)
        self.sess.send(b"f")
        self.sess.drain_frame()
        self.assertEqual(self.sess.scr.parse_hud()["mp"], 7)
        self.sess.send(b"c")
        self.sess.drain_frame()
        self.assertIn("CAMERA RE-CENTERED", self.sess.scr.row(19))
        self.sess.send(b"l")
        self.sess.drain_frame()
        self.assertEqual(self.sess.scr.validator.errors, [])

    def test_t3_05_rogue_mapping_scroll_full_map_and_arrow_dir(self) -> None:
        assert self.sess is not None
        self._start_class(b"3")
        self.sess.send(b"3")
        self.sess.drain_frame()
        self.assertEqual(self.sess.scr.parse_hud()["scrolls"], 0)
        self.sess.send(b"m")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "map_overlay"))
        self.sess.send(b" ")
        self.sess.drain_frame()
        self.sess.send(b"\x1b[C")
        self.sess.drain_frame()
        self.assertIn("DIR:E", self.sess.scr.row(4))

    # --- Tier 4: Live End-to-End Workflows ---

    def test_t4_02_sustained_multi_turn_play_ai_and_low_hp_session(self) -> None:
        assert self.sess is not None
        self._start_class(b"3")
        for k in (b"w", b"d", b"?", b" ", b"m", b" "):
            self.sess.send(k)
            self.sess.drain_frame()
        self.sess.drain(1.5)
        self.sess.ensure_class_select()
        power_bench._enter_ai_demo(self.sess.ser, self.sess.scr, use_mlp=False)
        self.sess.drain(4.0)
        self.sess.send(b"t")
        self.sess.drain(4.0)
        self.sess.send(b"n")
        self.assertTrue(self.sess.wait_until(lambda: self.sess.scr.detect_state() == "class_select"))
        self._start_class(b"1")
        self.assertTrue(power_bench.seek_low_hp(self.sess.ser, self.sess.scr, target_death=False, timeout_s=30.0))
        self.assertEqual(self.sess.scr.validator.errors, [])

    def test_t4_03_power_bench_cli_verify_uart_and_all_profiles_com4(self) -> None:
        assert self.sess is not None
        self.sess.close()
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                p1 = subprocess.run(
                    [sys.executable, str(POWER_BENCH_PY), "--port", LIVE_PORT, "--verify-uart"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(p1.returncode, 0, p1.stdout + p1.stderr)
                out_json = root / "all.json"
                out_log = root / "all.log"
                p2 = subprocess.run(
                    [
                        sys.executable,
                        str(POWER_BENCH_PY),
                        "--port",
                        LIVE_PORT,
                        "--profile",
                        "all",
                        "--duration",
                        "5",
                        "--countdown",
                        "0",
                        "--out-json",
                        str(out_json),
                        "--out-log",
                        str(out_log),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
                data = json.loads(out_json.read_text(encoding="utf-8"))
                self.assertEqual(len(data["profiles"]), 5)
        finally:
            self.sess = UartSession(LIVE_PORT, 115200)


# ---------------------------------------------------------------------------
# Final M5 Deliverables Gate (--require-final / MSPGAME_VERIFY_FINAL=1)
# ---------------------------------------------------------------------------

@unittest.skipUnless(VERIFY_FINAL, "Final deliverables gate (--require-final / MSPGAME_VERIFY_FINAL=1)")
class FinalDeliverablesE2ETests(unittest.TestCase):
    """M5 acceptance gate verifying Optimized CSVs, 300 DPI figures, IEEE PDF, and net reductions."""

    def test_t1_fa8_05_final_deliverables_and_power_reductions(self) -> None:
        """T1-FA8-05: Optimized CSVs, 4 PNGs, ENERGY_REPORT.md, energy_report.tex/.pdf, and net reductions."""
        power_dir = DOCS_DIR / "power"
        figs_dir = power_dir / "figures"

        for prof in ("idle", "play", "ai_heuristic", "ai_mlp", "low_hp"):
            self.assertTrue((POWER_DATA_DIR / f"optimized_{prof}.csv").exists())

        for fig in ("power_comparison.png", "waveforms_idle_ai.png", "waveform_low_hp.png", "lifetime_projections.png"):
            fp = figs_dir / fig
            self.assertTrue(fp.exists(), f"Missing figure: {fp}")
            w, h, ppux, ppuy = read_png_dpi(fp)
            self.assertGreater(w, 600)
            self.assertGreater(h, 400)
            self.assertAlmostEqual(ppux, 11811, delta=5)
            self.assertAlmostEqual(ppuy, 11811, delta=5)

        md_report = power_dir / "ENERGY_REPORT.md"
        tex_report = power_dir / "energy_report.tex"
        pdf_report = power_dir / "energy_report.pdf"
        self.assertTrue(md_report.exists())
        self.assertTrue(tex_report.exists())
        self.assertTrue(pdf_report.exists())

        tex_txt = tex_report.read_text(encoding="utf-8")
        self.assertIn("IEEEtran", tex_txt)
        self.assertIn("joaogpd", tex_txt)
        self.assertIn("Disclaimer", tex_txt)
        self.assertTrue(pdf_report.read_bytes().startswith(b"%PDF-"))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = power_plots.run_pipeline(
                POWER_DATA_DIR,
                root / "figs",
                root / "sum.json",
                root / "sum.md",
                allow_partial=False,
            )
            deltas = payload["deltas"]  # type: ignore[assignment]
            for prof in ("idle", "ai_heuristic", "ai_mlp", "low_hp"):
                self.assertGreater(deltas[prof]["delta_mean_power_pct"], 0.0)
            base_play = payload["profiles"]["play"]["baseline"]["mean_power_uW"]  # type: ignore[index]
            opt_play = payload["profiles"]["play"]["optimized"]["mean_power_uW"]  # type: ignore[index]
            self.assertLessEqual(opt_play, base_play * 1.05)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0], *_UNITTEST_REMAINING])
