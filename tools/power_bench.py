"""
Host-side workload driver and UART reliability verifier for mspgame power
benchmarking on the TI MSP-EXP430FR5994 LaunchPad (COM4 @ 115200 8N1).

Supports:
- Reproducible 30 s workload profiles synchronized with CCS EnergyTrace:
  idle, play, ai-heuristic, ai-mlp, low-hp, all
- 7-part Zero-UX-Loss UART reliability verification (--verify-uart)
- Comprehensive offline unit test suite (--self-test)
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

W, H = 80, 24
VIEW_W, VIEW_H = 28, 17
MAP_W, MAP_H = 48, 22

DIR_GLYPHS = {"^^": 0, ">>": 1, "vv": 2, "<<": 3}
ENEMY_GLYPHS = {"sl", "SK", "AR", "A!", "WR", "MM", "BW", "B!", "FR"}
SOLID_GLYPHS = {"##", "%%", "$$"}
TRAP_GLYPH = " ^"
SHRINE_GLYPH = " &"
STAIRS_GLYPH = " >"
DOOR_CLOSED_GLYPH = "++"
POTION_GLYPH = " +"
UNEXPLORED_GLYPH = "  "

PLAY_SEQUENCE: Tuple[str, ...] = ("w", "d", "s", "a", " ")

MOVE_DELTAS: Tuple[Tuple[int, int, str], ...] = (
    (0, -1, "w"),
    (1, 0, "d"),
    (0, 1, "s"),
    (-1, 0, "a"),
)


@dataclass
class AnsiStreamValidator:
    """Byte-level ANSI/VT100 + SGR stream validator."""

    total_bytes: int = 0
    csi_count: int = 0
    frames_cleared: int = 0
    dsr_queries: int = 0
    bel_count: int = 0
    unexpected_ctrl_bytes: int = 0
    non_ascii_bytes: int = 0
    malformed_csi: int = 0
    out_of_bounds_cup: int = 0
    errors: List[str] = field(default_factory=list)

    _esc: int = 0
    _csi: str = ""

    def _record_error(self, msg: str) -> None:
        if len(self.errors) < 50:
            self.errors.append(msg)

    def feed_byte(self, b: int) -> Optional[str]:
        """Validate one byte; returns completed CSI sequence if any."""
        self.total_bytes += 1
        if self._esc == 0:
            if b == 0x1B:
                self._esc = 1
                self._csi = ""
            elif b == 0x07:
                self.bel_count += 1
            elif b in (0x0A, 0x0D):
                pass
            elif 0x20 <= b <= 0x7E:
                pass
            elif b >= 0x7F:
                self.non_ascii_bytes += 1
                self._record_error(f"Non-ASCII byte 0x{b:02X} at offset {self.total_bytes}")
            else:
                self.unexpected_ctrl_bytes += 1
                self._record_error(f"Unexpected control byte 0x{b:02X} at offset {self.total_bytes}")
            return None

        if self._esc == 1:
            if b == ord("["):
                self._esc = 2
                self._csi = ""
            elif b == 0x1B:
                self.malformed_csi += 1
                self._record_error(f"Aborted ESC before 0x1B at offset {self.total_bytes}")
                self._esc = 1
                self._csi = ""
            else:
                self.malformed_csi += 1
                self._record_error(f"Unsupported ESC sequence ESC 0x{b:02X} at offset {self.total_bytes}")
                self._esc = 0
            return None

        # _esc == 2 (inside CSI)
        if b == 0x1B:
            self.malformed_csi += 1
            self._record_error(f"Truncated CSI '\\x1b[{self._csi}' before 0x1B at offset {self.total_bytes}")
            self._esc = 1
            self._csi = ""
            return None

        if 0x20 <= b <= 0x3F:
            self._csi += chr(b)
            if len(self._csi) > 24:
                self.malformed_csi += 1
                self._record_error(f"Overlong CSI sequence '\\x1b[{self._csi}' at offset {self.total_bytes}")
                self._esc = 0
                self._csi = ""
            return None

        if 0x40 <= b <= 0x7E:
            final = chr(b)
            seq = self._csi + final
            self.csi_count += 1
            self._validate_csi(self._csi, final)
            self._esc = 0
            self._csi = ""
            return seq

        self.malformed_csi += 1
        self._record_error(f"Invalid CSI byte 0x{b:02X} after '\\x1b[{self._csi}' at offset {self.total_bytes}")
        self._esc = 0
        self._csi = ""
        return None

    def _validate_csi(self, params: str, final: str) -> None:
        if final == "n" and params == "5":
            self.dsr_queries += 1
            return
        if final == "J" and params == "2":
            self.frames_cleared += 1
            return
        if final in ("H", "f"):
            if params == "":
                return
            if not re.fullmatch(r"\d*(;\d*)?", params):
                self.malformed_csi += 1
                self._record_error(f"Malformed CUP '\\x1b[{params}{final}'")
                return
            parts = params.split(";")
            r1 = int(parts[0]) if parts[0] else 1
            c1 = int(parts[1]) if len(parts) > 1 and parts[1] else 1
            if not (1 <= r1 <= H and 1 <= c1 <= W):
                self.out_of_bounds_cup += 1
                self._record_error(f"Out-of-bounds CUP row={r1} col={c1}")
            return
        if final == "m":
            if params != "" and not re.fullmatch(r"\d+(;\d+)*", params):
                self.malformed_csi += 1
                self._record_error(f"Malformed SGR '\\x1b[{params}m'")
            return
        if final in ("h", "l"):
            if not re.fullmatch(r"\??\d+(;\d+)*", params):
                self.malformed_csi += 1
                self._record_error(f"Malformed mode CSI '\\x1b[{params}{final}'")
            return
        if final == "K":
            if params not in ("", "0", "1", "2"):
                self.malformed_csi += 1
                self._record_error(f"Malformed EL '\\x1b[{params}K'")
            return
        # Other valid CSI final byte: check basic parameter chars
        if params and not re.fullmatch(r"[?!<=>]?[\d;]*", params):
            self.malformed_csi += 1
            self._record_error(f"Malformed CSI '\\x1b[{params}{final}'")

    @property
    def has_incomplete_escape(self) -> bool:
        return self._esc != 0

    def summary(self) -> Dict[str, object]:
        return {
            "total_bytes": self.total_bytes,
            "csi_count": self.csi_count,
            "frames_cleared": self.frames_cleared,
            "dsr_queries": self.dsr_queries,
            "bel_count": self.bel_count,
            "unexpected_ctrl_bytes": self.unexpected_ctrl_bytes,
            "non_ascii_bytes": self.non_ascii_bytes,
            "malformed_csi": self.malformed_csi,
            "out_of_bounds_cup": self.out_of_bounds_cup,
            "error_count": len(self.errors),
            "errors": list(self.errors),
        }


class AnsiScreen:
    """80x24 VT100/ANSI screen state + DSR responder + stream validator."""

    def __init__(
        self,
        on_dsr: Optional[Callable[[], None]] = None,
        validate: bool = True,
    ) -> None:
        self.grid: List[List[str]] = [[" "] * W for _ in range(H)]
        self.r: int = 0
        self.c: int = 0
        self.esc: int = 0
        self.csi: str = ""
        self.bel_count: int = 0
        self.dsr_count: int = 0
        self.rx_bytes: int = 0
        self.tx_bytes: int = 0
        self.frame_timestamps: List[float] = []
        self.on_dsr = on_dsr
        self.validator = AnsiStreamValidator() if validate else None
        self.raw_chunks: Optional[bytearray] = None
        self._frame_header_open: bool = False
        self._awaiting_full_frame: bool = False

    def clear(self) -> None:
        for rr in range(H):
            self.grid[rr] = [" "] * W

    def feed(self, data: bytes, ser=None, now: Optional[float] = None) -> None:
        if not data:
            return
        t_now = time.perf_counter() if now is None else now
        self.rx_bytes += len(data)
        if self.raw_chunks is not None:
            self.raw_chunks.extend(data)

        for b in data:
            if self.validator is not None:
                self.validator.feed_byte(b)

            if self.esc == 0:
                if b == 0x1B:
                    self.esc = 1
                elif b == 0x07:
                    self.bel_count += 1
                elif b == 0x0D:
                    self.c = 0
                elif b == 0x0A:
                    if self.r < H - 1:
                        self.r += 1
                elif 0x20 <= b <= 0x7E:
                    self._frame_header_open = False
                    if 0 <= self.r < H and 0 <= self.c < W:
                        self.grid[self.r][self.c] = chr(b)
                        self.c += 1
            elif self.esc == 1:
                if b == ord("["):
                    self.esc = 2
                    self.csi = ""
                else:
                    self.esc = 0
            elif self.esc == 2:
                ch = chr(b)
                if 0x40 <= b <= 0x7E:
                    if ch in ("H", "f"):
                        if not self.csi:
                            self.r = 0
                            self.c = 0
                        else:
                            parts = self.csi.split(";")
                            r1 = int(parts[0]) if parts[0].isdigit() else 1
                            c1 = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
                            self.r = max(0, min(H - 1, r1 - 1))
                            self.c = max(0, min(W - 1, c1 - 1))
                        if self.r == 0 and self.c == 0 and not self._frame_header_open:
                            self.frame_timestamps.append(t_now)
                            self._frame_header_open = True
                    elif ch == "J" and self.csi == "2":
                        self.clear()
                        self._awaiting_full_frame = True
                        if not self._frame_header_open:
                            self.frame_timestamps.append(t_now)
                            self._frame_header_open = True
                    elif ch == "K":
                        for cc in range(self.c, W):
                            self.grid[self.r][cc] = " "
                    elif ch == "l" and self.csi == "?25":
                        # Firmware begins every rendered frame with \x1b[2J\x1b[H\x1b[?25l or \x1b[?25l
                        if not self._frame_header_open:
                            self.frame_timestamps.append(t_now)
                            self._frame_header_open = True
                    elif ch == "n" and self.csi == "5":
                        self.dsr_count += 1
                        if self.on_dsr is not None:
                            self.on_dsr()
                        elif ser is not None:
                            ser.write(b"\x1b[0n")
                            self.tx_bytes += 4
                    self.esc = 0
                    self.csi = ""
                elif b == 0x1B:
                    self.esc = 1
                    self.csi = ""
                else:
                    self.csi += ch

    def row(self, r: int) -> str:
        return "".join(self.grid[r])

    def tile(self, vr: int, vc: int) -> str:
        r = vr + 1
        c = vc * 2
        if r < 1 or r > VIEW_H or c < 0 or c > (VIEW_W - 1) * 2:
            return "##"
        return self.grid[r][c] + self.grid[r][c + 1]

    def find_player_in_viewport(self) -> Tuple[int, int, int]:
        """Returns (vr, vc, dir_id) or (-1, -1, 0) if not found."""
        for vr in range(VIEW_H):
            for vc in range(VIEW_W):
                g = self.tile(vr, vc)
                if g in DIR_GLYPHS:
                    return vr, vc, DIR_GLYPHS[g]
        return -1, -1, 0

    def parse_hud(self) -> Dict[str, object]:
        r0 = self.row(0)
        r1 = self.row(1)
        r2 = self.row(2)
        r3 = self.row(3)
        r4 = self.row(4)
        r11 = self.row(11)
        r12 = self.row(12)
        r19 = self.row(19)

        m_fl = re.search(r"FL:(\d+)", r0)
        m_hp = re.search(r"HP:(\d+)/(\d+)", r1)
        m_mp = re.search(r"MP:(\d+)/(\d+)", r1)
        m_atk = re.search(r"ATK:(\d+)", r2)
        m_def = re.search(r"DEF:(\d+)", r2)
        m_gd = re.search(r"GOLD:(\d+)", r3)
        m_pos = re.search(r"(\d+),(\d+)", r4)
        m_dir = re.search(r"DIR:([NESW])", r4)
        m_pot = re.search(r"\[1\]Pot:(\d+)", r11)
        m_bmb = re.search(r"\[2\]Bmb:(\d+)", r11)
        m_map = re.search(r"\[3\]Map:(\d+)", r12)

        cls_id = 1 if "ARC" in r4 else (2 if "ROG" in r4 else 0)
        dir_map = {"N": 0, "E": 1, "S": 2, "W": 3}
        hud_valid = bool(m_hp and m_pos and ("DIR:" in r4))
        return {
            "floor": int(m_fl.group(1)) if m_fl else 1,
            "hp": int(m_hp.group(1)) if m_hp else 0,
            "max_hp": int(m_hp.group(2)) if m_hp else 16,
            "mp": int(m_mp.group(1)) if m_mp else 0,
            "max_mp": int(m_mp.group(2)) if m_mp else 4,
            "atk": int(m_atk.group(1)) if m_atk else 3,
            "def_": int(m_def.group(1)) if m_def else 1,
            "gold": int(m_gd.group(1)) if m_gd else 0,
            "empowered": 1 if "[EMP]" in r3 else 0,
            "px": int(m_pos.group(1)) if m_pos else 0,
            "py": int(m_pos.group(2)) if m_pos else 0,
            "dir": dir_map[m_dir.group(1)] if m_dir else -1,
            "cls": cls_id,
            "pots": int(m_pot.group(1)) if m_pot else 0,
            "bombs": int(m_bmb.group(1)) if m_bmb else 0,
            "scrolls": int(m_map.group(1)) if m_map else 0,
            "low_hp": 1 if "LOW HP" in r0 else 0,
            "human_dead": 1 if "YOU DIED" in r19 else 0,
            "ai_dead": 1 if "AI DEMO ENDED" in r19 else 0,
            "dead": 1 if ("YOU DIED" in r19 or "AI DEMO ENDED" in r19) else 0,
            "class_select": 1 if "NEW ADVENTURE" in r0 else 0,
            "ai_heuristic": 1 if "AI:HEURISTIC" in r0 else 0,
            "ai_mlp": 1 if "AI:TINY-MLP" in r0 else 0,
            "hud_valid": hud_valid,
        }

    def is_frame_complete(self) -> bool:
        if self.esc != 0:
            return False
        st = self.detect_state()
        if st in ("class_select", "hof_overlay", "help_overlay", "map_overlay", "game_over", "ai_demo_ended"):
            return True
        if st in ("human_play", "low_hp", "ai_heuristic", "ai_mlp"):
            return bool(self.parse_hud()["hud_valid"])
        return False

    def detect_state(self) -> str:
        r0 = self.row(0)
        r19 = self.row(19)
        if "NEW ADVENTURE" in r0:
            return "class_select"
        if "FRAM HALL OF FAME" in r0:
            return "hof_overlay"
        if "MSP430 DUNGEON HELP" in r0:
            return "help_overlay"
        if "FULL FLOOR MAP" in r0:
            return "map_overlay"
        if "AI DEMO ENDED" in r19:
            return "ai_demo_ended"
        if "YOU DIED" in r19:
            return "game_over"
        if "LOW HP" in r0:
            return "low_hp"
        if "AI:HEURISTIC" in r0:
            return "ai_heuristic"
        if "AI:TINY-MLP" in r0:
            return "ai_mlp"
        if "MSP430 DUNGEON" in r0:
            return "human_play"
        return "unknown"


def send_bytes(ser, scr: AnsiScreen, payload: bytes) -> None:
    ser.write(payload)
    scr.tx_bytes += len(payload)


def drain_serial(ser, scr: AnsiScreen, duration_s: float, poll_s: float = 0.01) -> None:
    deadline = time.perf_counter() + duration_s
    while True:
        n = getattr(ser, "in_waiting", 0)
        if n and n > 0:
            chunk = ser.read(n)
            if chunk:
                scr.feed(chunk, ser=ser)
        if time.perf_counter() >= deadline:
            break
        time.sleep(min(poll_s, max(0.001, deadline - time.perf_counter())))


def drain_frame(
    ser,
    scr: AnsiScreen,
    max_wait_s: float = 0.45,
    settle_s: float = 0.06,
    poll_s: float = 0.01,
) -> bool:
    """Drain serial until the current frame completes and settles, or max_wait_s elapses."""
    t0 = time.perf_counter()
    deadline = t0 + max_wait_s
    last_rx_t: Optional[float] = None

    while True:
        n = getattr(ser, "in_waiting", 0)
        if n and n > 0:
            chunk = ser.read(n)
            if chunk:
                scr.feed(chunk, ser=ser)
                last_rx_t = time.perf_counter()
                continue

        now = time.perf_counter()
        if now >= deadline:
            break

        quiet_s = (now - last_rx_t) if last_rx_t is not None else (now - t0)
        if quiet_s >= settle_s and scr.is_frame_complete():
            scr._awaiting_full_frame = False
            break

        time.sleep(min(poll_s, max(0.001, deadline - now)))

    return scr.is_frame_complete()


def wait_until(
    ser,
    scr: AnsiScreen,
    predicate: Callable[[], bool],
    timeout_s: float,
    poll_s: float = 0.015,
) -> bool:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        n = getattr(ser, "in_waiting", 0)
        if n and n > 0:
            chunk = ser.read(n)
            if chunk:
                scr.feed(chunk, ser=ser)
        if predicate():
            return True
        time.sleep(poll_s)
    # Final drain check
    n = getattr(ser, "in_waiting", 0)
    if n and n > 0:
        chunk = ser.read(n)
        if chunk:
            scr.feed(chunk, ser=ser)
    return predicate()


def seek_low_hp(
    ser,
    scr: AnsiScreen,
    target_death: bool = False,
    timeout_s: float = 28.0,
    max_steps: int = 260,
) -> bool:
    """
    Deterministically navigate the visible 28x17 viewport (starting from Knight
    run or active human play) to reach Low-HP (or death if target_death=True).

    Priority:
    1. If adjacent to a visible enemy, send ' ' (Meditate) so monster_turn()
       damages the player without killing the enemy.
    2. BFS on known walkable map coordinates to:
       a) Tile adjacent to nearest visible enemy
       b) Visible spike trap (" ^")
       c) Closed door ("++") or frontier tile next to unexplored ("  ")
       d) Least-visited passable tile
    Avoids stairs (" >") and potions (" +") when seeking damage.
    """
    world: Dict[Tuple[int, int], str] = {}
    visited: Dict[Tuple[int, int], int] = {}
    last_floor = -1
    deadline = time.perf_counter() + timeout_s

    for _step in range(max_steps):
        if time.perf_counter() >= deadline:
            return False

        if not scr.is_frame_complete() or scr._awaiting_full_frame:
            drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)

        st = scr.detect_state()
        hud = scr.parse_hud()
        if target_death:
            if st == "game_over" or hud["human_dead"]:
                return True
            if st == "class_select":
                return True
        else:
            if hud["low_hp"] == 1 and not hud["dead"]:
                return True
            if st in ("game_over", "ai_demo_ended"):
                # Restart Knight if accidental death during setup (only on settled terminal state)
                send_bytes(ser, scr, b"w")
                wait_until(ser, scr, lambda: scr.detect_state() == "class_select", 1.5)
                drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
                send_bytes(ser, scr, b"1")
                wait_until(
                    ser,
                    scr,
                    lambda: scr.detect_state() in ("human_play", "low_hp") and scr.is_frame_complete(),
                    1.5,
                )
                world.clear()
                visited.clear()
                last_floor = -1
                continue

        if st == "class_select":
            send_bytes(ser, scr, b"1")
            wait_until(
                ser,
                scr,
                lambda: scr.detect_state() in ("human_play", "low_hp") and scr.is_frame_complete(),
                1.5,
            )
            continue

        if st in ("hof_overlay", "help_overlay", "map_overlay"):
            send_bytes(ser, scr, b" ")
            drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)
            continue

        if not hud["hud_valid"]:
            send_bytes(ser, scr, b"\x0c")
            drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)
            continue

        pvr, pvc, _ = scr.find_player_in_viewport()
        if pvr < 0:
            send_bytes(ser, scr, b"\x0c")
            drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)
            continue

        if hud["floor"] != last_floor:
            world.clear()
            visited.clear()
            last_floor = hud["floor"]

        px, py = hud["px"], hud["py"]
        visited[(px, py)] = visited.get((px, py), 0) + 1
        cam_x = max(0, min(MAP_W - VIEW_W, px - pvc))
        cam_y = max(0, min(MAP_H - VIEW_H, py - pvr))

        enemies: List[Tuple[int, int]] = []
        traps: List[Tuple[int, int]] = []
        shrines: List[Tuple[int, int]] = []

        for vr in range(VIEW_H):
            for vc in range(VIEW_W):
                mx, my = cam_x + vc, cam_y + vr
                g = scr.tile(vr, vc)
                if g != UNEXPLORED_GLYPH:
                    world[(mx, my)] = g
                if g in ENEMY_GLYPHS:
                    enemies.append((mx, my))
                elif g == TRAP_GLYPH:
                    traps.append((mx, my))
                elif g == SHRINE_GLYPH and hud["floor"] == 1 and hud["hp"] > 4:
                    shrines.append((mx, my))

        # 1. If already adjacent to an enemy, meditate (' ') to take damage cleanly
        adj_enemy = any(abs(ex - px) + abs(ey - py) == 1 for ex, ey in enemies)
        if adj_enemy:
            send_bytes(ser, scr, b" ")
            drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)
            continue

        # 2. Build BFS over known walkable world tiles
        enemy_adj_targets = set()
        for ex, ey in enemies:
            for dx, dy, _ in MOVE_DELTAS:
                nx, ny = ex + dx, ey + dy
                g = world.get((nx, ny))
                if g is not None and g not in SOLID_GLYPHS and g not in ENEMY_GLYPHS:
                    enemy_adj_targets.add((nx, ny))

        trap_targets = set(traps)
        shrine_targets = set(shrines)

        def bfs_first_move(
            goal_fn: Callable[[int, int], bool],
            allow_doors: bool = True,
            avoid_potions: bool = True,
        ) -> Optional[str]:
            q: Deque[Tuple[int, int, Optional[str]]] = collections.deque()
            seen = {(px, py)}
            for dx, dy, key in MOVE_DELTAS:
                nx, ny = px + dx, py + dy
                if not (0 <= nx < MAP_W and 0 <= ny < MAP_H):
                    continue
                g = world.get((nx, ny))
                if g is None or g == UNEXPLORED_GLYPH or g in SOLID_GLYPHS:
                    continue
                if g == STAIRS_GLYPH:
                    continue
                if avoid_potions and g == POTION_GLYPH:
                    continue
                if g == DOOR_CLOSED_GLYPH and not allow_doors:
                    continue
                if (nx, ny) not in seen:
                    seen.add((nx, ny))
                    q.append((nx, ny, key))
                    if goal_fn(nx, ny):
                        return key
            while q:
                cx, cy, first_key = q.popleft()
                cg = world.get((cx, cy))
                # Closed door blocks further pass-through in one step, but can be opened
                if cg == DOOR_CLOSED_GLYPH:
                    continue
                for dx, dy, _ in MOVE_DELTAS:
                    nx, ny = cx + dx, cy + dy
                    if (nx, ny) in seen or not (0 <= nx < MAP_W and 0 <= ny < MAP_H):
                        continue
                    g = world.get((nx, ny))
                    if g is None or g == UNEXPLORED_GLYPH or g in SOLID_GLYPHS:
                        continue
                    if g == STAIRS_GLYPH:
                        continue
                    if avoid_potions and g == POTION_GLYPH:
                        continue
                    if g == DOOR_CLOSED_GLYPH and not allow_doors:
                        continue
                    seen.add((nx, ny))
                    if goal_fn(nx, ny):
                        return first_key
                    q.append((nx, ny, first_key))
            return None

        # Priority 2a: path to stand next to an enemy
        action: Optional[str] = None
        if enemy_adj_targets:
            action = bfs_first_move(lambda x, y: (x, y) in enemy_adj_targets)

        # Priority 2b: path onto a spike trap (if already on one, step off then back on)
        if action is None and trap_targets:
            if (px, py) in trap_targets:
                for dx, dy, key in MOVE_DELTAS:
                    g = world.get((px + dx, py + dy))
                    if g is not None and g not in SOLID_GLYPHS and g != STAIRS_GLYPH:
                        action = key
                        break
            else:
                action = bfs_first_move(lambda x, y: (x, y) in trap_targets)

        # Priority 2c: Floor 1 shrine (Blood Altar -3 HP) if HP > 4
        if action is None and shrine_targets:
            action = bfs_first_move(lambda x, y: (x, y) in shrine_targets)

        # Priority 2d: closed door or frontier adjacent to unexplored tile
        if action is None:
            def is_frontier(x: int, y: int) -> bool:
                if world.get((x, y)) == DOOR_CLOSED_GLYPH:
                    return True
                for dx, dy, _ in MOVE_DELTAS:
                    if (x + dx, y + dy) not in world:
                        return True
                return False

            action = bfs_first_move(is_frontier)

        # Priority 2e: least-visited neighbor (or relax potion avoidance)
        if action is None:
            action = bfs_first_move(
                lambda x, y: visited.get((x, y), 0) == 0,
                allow_doors=True,
                avoid_potions=False,
            )

        if action is None:
            best_score = 10**9
            for dx, dy, key in MOVE_DELTAS:
                nx, ny = px + dx, py + dy
                g = world.get((nx, ny))
                if g is not None and g not in SOLID_GLYPHS and g != STAIRS_GLYPH:
                    sc = visited.get((nx, ny), 0)
                    if sc < best_score:
                        best_score = sc
                        action = key

        if action is None:
            # All neighbors blocked or unknown; wait safely via Meditate only when HUD is valid (never blind 'w')
            if st in ("human_play", "low_hp") and hud["hud_valid"] and not hud["dead"]:
                send_bytes(ser, scr, b" ")
            else:
                send_bytes(ser, scr, b"\x0c")
            drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)
            continue

        send_bytes(ser, scr, action.encode("ascii"))
        drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)

    return False


def _is_dead_or_game_over(scr: AnsiScreen) -> bool:
    """Return True if screen is game_over or zombie s_game_over masked by 'c' (HP:00)."""
    st = scr.detect_state()
    if st == "game_over":
        return True
    if st in ("human_play", "low_hp"):
        hud = scr.parse_hud()
        return bool(hud["hud_valid"]) and hud["hp"] == 0
    return False


def _enter_ai_demo(
    ser,
    scr: AnsiScreen,
    use_mlp: bool,
    timeout_s: float = 2.0,
) -> None:
    """Start AI Demo ('4'), wait/drain full frame, and optionally toggle Tiny-MLP ('t')."""
    send_bytes(ser, scr, b"4")
    if not wait_until(
        ser,
        scr,
        lambda: scr.detect_state() in ("ai_heuristic", "low_hp"),
        timeout_s,
    ):
        raise RuntimeError(f"Failed to start AI Heuristic demo (state={scr.detect_state()})")
    drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)
    if use_mlp:
        send_bytes(ser, scr, b"t")
        if not wait_until(
            ser,
            scr,
            lambda: scr.detect_state() == "ai_mlp" or "TINY-MLP" in scr.row(19),
            timeout_s,
        ):
            raise RuntimeError(
                f"Failed to switch AI demo to Tiny-MLP (row0={scr.row(0).strip()})"
            )
        drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)


def _build_rapid_bursts(scr: AnsiScreen) -> List[Tuple[bytes, Callable[[], bool]]]:
    """Deterministic <=12 B UART burst payloads and seed-proof post-conditions."""
    return [
        (
            b"w d s a c",
            lambda: "CAMERA RE-CENTERED" in scr.row(19) or _is_dead_or_game_over(scr),
        ),
        (
            b"a s d w  ",
            lambda: (
                ("DIR:N" in scr.row(4) and scr.parse_hud()["empowered"] == 1)
                or _is_dead_or_game_over(scr)
                or scr.detect_state() == "class_select"
                or (
                    "DIR:E" in scr.row(4)
                    and ("ADVENTURE BEGINS" in scr.row(19) or scr.parse_hud()["empowered"] == 1)
                )
            ),
        ),
        (
            b"\x1b[A\x1b[C\x1b[B\x1b[D",
            lambda: "DIR:W" in scr.row(4) or _is_dead_or_game_over(scr),
        ),
        (
            b"\x1b[C\x1b[B\x1b[D\x1b[A",
            lambda: (
                "DIR:N" in scr.row(4)
                or _is_dead_or_game_over(scr)
                or scr.detect_state() == "class_select"
            ),
        ),
        (
            b"s a w d c",
            lambda: (
                ("CAMERA RE-CENTERED" in scr.row(19) and "DIR:E" in scr.row(4))
                or _is_dead_or_game_over(scr)
                or scr.detect_state() == "class_select"
            ),
        ),
    ]


def ensure_class_select(ser, scr: AnsiScreen, timeout_s: float = 30.0) -> bool:
    """Reliably transition the firmware to Class Select ('NEW ADVENTURE')."""
    send_bytes(ser, scr, b"\x0c")
    drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)

    deadline = time.perf_counter() + timeout_s
    tried_n_in_low_hp = False

    while time.perf_counter() < deadline:
        if not scr.is_frame_complete() or scr._awaiting_full_frame:
            drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)

        st = scr.detect_state()
        if st == "class_select":
            return True
        if st in ("hof_overlay", "help_overlay", "map_overlay"):
            send_bytes(ser, scr, b" ")
            wait_until(ser, scr, lambda: scr.detect_state() not in ("hof_overlay", "help_overlay", "map_overlay"), 1.0)
            drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
            continue
        if st in ("ai_heuristic", "ai_mlp", "ai_demo_ended"):
            send_bytes(ser, scr, b"n")
            wait_until(ser, scr, lambda: scr.detect_state() == "class_select", 1.5)
            drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
            continue
        if _is_dead_or_game_over(scr):
            send_bytes(ser, scr, b"w")
            wait_until(ser, scr, lambda: scr.detect_state() == "class_select", 1.5)
            drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
            continue
        if st == "low_hp" and not tried_n_in_low_hp:
            # [! LOW HP !] masks AI:HEURISTIC / AI:TINY-MLP on Row 0; probe 'n' once first
            tried_n_in_low_hp = True
            send_bytes(ser, scr, b"n")
            if wait_until(ser, scr, lambda: scr.detect_state() == "class_select", 0.5):
                drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
                return True
            continue
        if st in ("human_play", "low_hp"):
            rem = max(2.0, deadline - time.perf_counter() - 2.0)
            seek_low_hp(ser, scr, target_death=True, timeout_s=rem)
            if not scr.is_frame_complete() or scr._awaiting_full_frame:
                drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)
            if _is_dead_or_game_over(scr):
                send_bytes(ser, scr, b"w")
                wait_until(ser, scr, lambda: scr.detect_state() == "class_select", 1.5)
                drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
            continue

        # Unknown state: ask for redraw
        send_bytes(ser, scr, b"\x0c")
        drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)

    raise TimeoutError(
        "Unable to reach Class Select ('NEW ADVENTURE') within timeout. "
        "Press hardware RESET (or hold S1 + RESET to clear FRAM save) and retry."
    )


def wait_for_user_start(
    ser,
    scr: AnsiScreen,
    profile: str,
    wait_start: bool,
    countdown: int,
) -> None:
    """Optional interactive barrier & countdown while keeping DSR/AI-demo alive."""

    def pump_once() -> None:
        n = getattr(ser, "in_waiting", 0)
        if n and n > 0:
            chunk = ser.read(n)
            if chunk:
                scr.feed(chunk, ser=ser)
        if profile in ("ai-heuristic", "ai-mlp") and scr.detect_state() == "ai_demo_ended":
            _enter_ai_demo(ser, scr, use_mlp=(profile == "ai-mlp"))

    if wait_start:
        print(
            f"[SYNC] Profile '{profile}' is staged in state '{scr.detect_state()}'.\n"
            f"       Verify CCS is in Free Run & EnergyTrace (energy-only), then press ENTER to begin countdown...",
            flush=True,
        )
        done = threading.Event()

        def _wait_enter() -> None:
            try:
                sys.stdin.readline()
            finally:
                done.set()

        t = threading.Thread(target=_wait_enter, daemon=True)
        t.start()
        while not done.is_set():
            pump_once()
            time.sleep(0.02)

    if countdown > 0:
        for sec in range(countdown, 0, -1):
            print(f"[SYNC] Starting '{profile}' capture window in {sec}...", flush=True)
            t_end = time.perf_counter() + 1.0
            while time.perf_counter() < t_end:
                pump_once()
                time.sleep(0.02)
        print(f"[SYNC] GO! ({profile})", flush=True)


def run_profile(
    ser,
    scr: AnsiScreen,
    profile: str,
    duration_s: float,
    wait_start: bool = False,
    countdown: int = 0,
) -> Dict[str, object]:
    """Stage firmware state, optionally wait/countdown, and run deterministic profile window."""
    if not ensure_class_select(ser, scr):
        raise RuntimeError("Failed to reach Class Select")

    if profile == "idle":
        send_bytes(ser, scr, b"\x0c")
        wait_until(ser, scr, lambda: scr.detect_state() == "class_select", 1.5)
        drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)
    elif profile == "play":
        send_bytes(ser, scr, b"1")
        if not wait_until(
            ser,
            scr,
            lambda: scr.detect_state() in ("human_play", "low_hp") and scr.is_frame_complete(),
            2.0,
        ):
            raise RuntimeError(f"Failed to start Knight run for play profile (state={scr.detect_state()})")
        drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)
    elif profile == "ai-heuristic":
        _enter_ai_demo(ser, scr, use_mlp=False)
    elif profile == "ai-mlp":
        _enter_ai_demo(ser, scr, use_mlp=True)
    elif profile == "low-hp":
        send_bytes(ser, scr, b"1")
        if not wait_until(ser, scr, lambda: scr.detect_state() in ("human_play", "low_hp"), 2.0):
            raise RuntimeError("Failed to enter Knight run for low-hp setup")
        if not seek_low_hp(ser, scr, target_death=False, timeout_s=30.0):
            raise RuntimeError("Could not reach Low-HP state within 30 s setup window")
        # Verify 2.5 s stability before EnergyTrace window begins
        bel_before = scr.bel_count
        drain_serial(ser, scr, 2.5)
        hud_check = scr.parse_hud()
        if hud_check["low_hp"] != 1 or hud_check["dead"] == 1:
            raise RuntimeError(f"Low-HP stability check failed: {hud_check}")
        if scr.bel_count <= bel_before:
            raise RuntimeError(f"Expected BEL increment during 2.5 s Low-HP stability check (before={bel_before}, after={scr.bel_count})")
    else:
        raise ValueError(f"Unknown profile: {profile}")

    wait_for_user_start(ser, scr, profile, wait_start=wait_start, countdown=countdown)

    # Reset per-window counters
    rx0 = scr.rx_bytes
    tx0 = scr.tx_bytes
    dsr0 = scr.dsr_count
    bel0 = scr.bel_count
    err0 = len(scr.validator.errors) if scr.validator else 0
    frames0 = len(scr.frame_timestamps)
    restarts = 0
    keys_sent = 0

    t_start = time.perf_counter()
    utc_start = datetime.now(timezone.utc).isoformat()
    t_end = t_start + duration_s
    next_key_t = t_start
    seq_idx = 0

    while True:
        now = time.perf_counter()
        if now >= t_end:
            break

        n = getattr(ser, "in_waiting", 0)
        if n and n > 0:
            chunk = ser.read(n)
            if chunk:
                scr.feed(chunk, ser=ser, now=now)

        if profile == "play":
            st = scr.detect_state()
            if st == "game_over":
                drain_frame(ser, scr, max_wait_s=0.30, settle_s=0.05)
                if scr.detect_state() == "game_over":
                    send_bytes(ser, scr, b"w")
                    wait_until(ser, scr, lambda: scr.detect_state() == "class_select", 1.5)
                    drain_frame(ser, scr, max_wait_s=0.30, settle_s=0.05)
                    send_bytes(ser, scr, b"1")
                    wait_until(
                        ser,
                        scr,
                        lambda: scr.detect_state() in ("human_play", "low_hp") and scr.is_frame_complete(),
                        1.5,
                    )
                    restarts += 1
                    next_key_t = time.perf_counter() + 1.0
            elif now >= next_key_t:
                key = PLAY_SEQUENCE[seq_idx % len(PLAY_SEQUENCE)]
                seq_idx += 1
                send_bytes(ser, scr, key.encode("ascii"))
                keys_sent += 1
                next_key_t += 1.0
                if next_key_t < now:
                    next_key_t = now + 1.0

        elif profile in ("ai-heuristic", "ai-mlp"):
            if scr.detect_state() == "ai_demo_ended":
                _enter_ai_demo(ser, scr, use_mlp=(profile == "ai-mlp"))
                restarts += 1

        time.sleep(0.01)

    utc_end = datetime.now(timezone.utc).isoformat()
    elapsed = time.perf_counter() - t_start

    # Clean up AI profiles back to Class Select
    if profile in ("ai-heuristic", "ai-mlp"):
        send_bytes(ser, scr, b"n")
        wait_until(ser, scr, lambda: scr.detect_state() == "class_select", 1.5)
        drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)

    hud_end = scr.parse_hud()
    return {
        "profile": profile,
        "utc_start": utc_start,
        "utc_end": utc_end,
        "duration_s": round(elapsed, 4),
        "rx_bytes": scr.rx_bytes - rx0,
        "tx_bytes": scr.tx_bytes - tx0,
        "dsr_count": scr.dsr_count - dsr0,
        "bel_count": scr.bel_count - bel0,
        "frame_count": len(scr.frame_timestamps) - frames0,
        "keys_sent": keys_sent,
        "restarts": restarts,
        "final_state": scr.detect_state(),
        "final_low_hp": hud_end["low_hp"],
        "validator_errors_delta": (len(scr.validator.errors) - err0) if scr.validator else 0,
    }


def verify_uart(
    ser,
    scr: AnsiScreen,
    results_out: Optional[List[Dict[str, object]]] = None,
) -> Dict[str, object]:
    """Execute the 7-part Zero-UX-Loss COM4 UART verification suite."""
    results: List[Dict[str, object]] = results_out if results_out is not None else []

    def record(name: str, passed: bool, detail: str) -> None:
        results.append({"check": name, "passed": passed, "detail": detail})
        status = "PASS" if passed else "FAIL"
        print(f"[VERIFY-UART] {status}: {name} -- {detail}", flush=True)
        if not passed:
            raise AssertionError(f"{name}: {detail}")

    # 1. DSR / Ctrl+L Handshake
    ensure_class_select(ser, scr)
    dsr_before = scr.dsr_count
    fb0 = len(scr.frame_timestamps)
    send_bytes(ser, scr, b"\x0c")
    ok_redraw = wait_until(
        ser,
        scr,
        lambda: len(scr.frame_timestamps) > fb0 and scr.detect_state() == "class_select",
        1.5,
    )
    # Also wait up to 1.2 s to verify DSR responder if Baseline sends \x1b[5n
    drain_serial(ser, scr, 1.2)
    record(
        "1_dsr_and_ctrl_l",
        ok_redraw and (scr.validator.errors == [] if scr.validator else True),
        f"state={scr.detect_state()}, dsr_delta={scr.dsr_count - dsr_before}, errors={len(scr.validator.errors) if scr.validator else 0}",
    )

    # 2. Cold-Idle Single-Byte Wakeups (10 iterations of 1.5..2.7 s silence)
    send_bytes(ser, scr, b"1")
    if not wait_until(ser, scr, lambda: scr.detect_state() in ("human_play", "low_hp") and scr.is_frame_complete(), 2.0):
        raise RuntimeError("Failed to enter human_play for cold-idle wakeup check")
    drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
    wake_checks = [
        (b"?", lambda: scr.detect_state() == "help_overlay", "help_overlay"),
        (b" ", lambda: scr.detect_state() in ("human_play", "low_hp"), "exit_help"),
        (b"m", lambda: scr.detect_state() == "map_overlay", "map_overlay"),
        (b" ", lambda: scr.detect_state() in ("human_play", "low_hp"), "exit_map"),
        (b"c", lambda: "CAMERA RE-CENTERED" in scr.row(19), "recenter"),
    ]
    for i in range(10):
        delay_s = 1.5 + (i % 4) * 0.4
        drain_serial(ser, scr, delay_s)
        tx_byte, pred, label = wake_checks[i % len(wake_checks)]
        frames_before = len(scr.frame_timestamps)
        send_bytes(ser, scr, tx_byte)
        ok = wait_until(
            ser,
            scr,
            lambda p=pred, fb=frames_before: len(scr.frame_timestamps) > fb and scr.is_frame_complete() and p(),
            1.2,
        )
        drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
        if not ok:
            record("2_cold_idle_wakeups", False, f"iter={i} delay={delay_s:.1f}s key={tx_byte!r} expected={label} row0={scr.row(0).strip()} row19={scr.row(19).strip()}")
    record("2_cold_idle_wakeups", True, "10/10 cold-idle (1.5-2.7 s) single-byte wakeups succeeded")

    # 3. 4x CSI Arrow Keys (\x1b[A..D)
    arrow_tests = [
        (b"\x1b[A", "^^", "N"),
        (b"\x1b[C", ">>", "E"),
        (b"\x1b[B", "vv", "S"),
        (b"\x1b[D", "<<", "W"),
    ]
    for seq, expected_glyph, expected_dir in arrow_tests:
        def _arrow_pred(g: str = expected_glyph, d: str = expected_dir, fb: int = 0) -> bool:
            if len(scr.frame_timestamps) <= fb or not scr.is_frame_complete():
                return False
            pvr, _, pdir = scr.find_player_in_viewport()
            return (
                (pvr >= 0 and pdir == DIR_GLYPHS[g])
                or f"DIR:{d}" in scr.row(4)
                or scr.detect_state() == "game_over"
            )

        fb0 = len(scr.frame_timestamps)
        send_bytes(ser, scr, seq)
        ok = wait_until(ser, scr, lambda fb=fb0, g=expected_glyph, d=expected_dir: _arrow_pred(g, d, fb), 1.2)
        drain_frame(ser, scr, max_wait_s=0.30, settle_s=0.04)
        if scr.detect_state() == "game_over":
            send_bytes(ser, scr, b"w")
            wait_until(ser, scr, lambda: scr.detect_state() == "class_select", 1.5)
            drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
            send_bytes(ser, scr, b"1")
            wait_until(ser, scr, lambda: scr.detect_state() in ("human_play", "low_hp") and scr.is_frame_complete(), 1.5)
            drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
            fb0 = len(scr.frame_timestamps)
            send_bytes(ser, scr, seq)
            ok = wait_until(ser, scr, lambda fb=fb0, g=expected_glyph, d=expected_dir: _arrow_pred(g, d, fb), 1.2)
            drain_frame(ser, scr, max_wait_s=0.30, settle_s=0.04)
        if not ok:
            record("3_csi_arrows", False, f"arrow={seq!r} expected={expected_glyph}/{expected_dir} row4={scr.row(4).strip()}")
    record("3_csi_arrows", True, "4/4 CSI arrow sequences (Up/Right/Down/Left) verified")

    # 4. 5x Rapid Multi-Byte Bursts (<= 12 B < 15 B CMD_QUEUE_SIZE)
    bursts = _build_rapid_bursts(scr)
    for idx, (burst, pred) in enumerate(bursts):
        assert len(burst) <= 12
        if _is_dead_or_game_over(scr):
            drain_frame(ser, scr, max_wait_s=0.30, settle_s=0.05)
            send_bytes(ser, scr, b"w")
            wait_until(ser, scr, lambda: scr.detect_state() == "class_select", 1.5)
            drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
            send_bytes(ser, scr, b"1")
            wait_until(ser, scr, lambda: scr.detect_state() in ("human_play", "low_hp") and scr.is_frame_complete(), 1.5)
            drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
        elif scr.detect_state() == "class_select":
            send_bytes(ser, scr, b"1")
            wait_until(ser, scr, lambda: scr.detect_state() in ("human_play", "low_hp") and scr.is_frame_complete(), 1.5)
            drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
        fb0 = len(scr.frame_timestamps)
        send_bytes(ser, scr, burst)
        ok = wait_until(ser, scr, lambda p=pred, fb=fb0: len(scr.frame_timestamps) > fb and scr.is_frame_complete() and p(), 1.8)
        drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
        if not ok:
            record("4_rapid_bursts", False, f"burst #{idx} {burst!r} failed: state={scr.detect_state()} row4={scr.row(4).strip()} row19={scr.row(19).strip()}")
    if _is_dead_or_game_over(scr):
        send_bytes(ser, scr, b"w")
        wait_until(ser, scr, lambda: scr.detect_state() == "class_select", 1.5)
        drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
    record("4_rapid_bursts", True, "5/5 rapid bursts (<= 12 B) processed with zero dropped commands")

    # 5. 80x24 ANSI/SGR Integrity across Class Select, Help, Full Map, Gameplay
    ensure_class_select(ser, scr)
    send_bytes(ser, scr, b"1")
    wait_until(ser, scr, lambda: scr.detect_state() in ("human_play", "low_hp") and scr.is_frame_complete(), 1.5)
    drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
    send_bytes(ser, scr, b"?")
    ok_help = wait_until(ser, scr, lambda: scr.detect_state() == "help_overlay", 1.0)
    drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
    send_bytes(ser, scr, b" ")
    wait_until(ser, scr, lambda: scr.detect_state() in ("human_play", "low_hp") and scr.is_frame_complete(), 1.0)
    drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
    send_bytes(ser, scr, b"m")
    ok_map = wait_until(ser, scr, lambda: scr.detect_state() == "map_overlay", 1.0)
    drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
    send_bytes(ser, scr, b" ")
    wait_until(ser, scr, lambda: scr.detect_state() in ("human_play", "low_hp") and scr.is_frame_complete(), 1.0)
    drain_frame(ser, scr, max_wait_s=0.35, settle_s=0.05)
    err_cnt = len(scr.validator.errors) if scr.validator else 0
    record(
        "5_ansi_sgr_integrity",
        ok_help and ok_map and err_cnt == 0 and not scr.validator.has_incomplete_escape,
        f"csi_count={scr.validator.csi_count}, frames_cleared={scr.validator.frames_cleared}, errors={err_cnt}",
    )

    # 6. 500 ms AI Demo Cadence (Heuristic A & Tiny-MLP B)
    ensure_class_select(ser, scr)
    for model_name, use_mlp in (("heuristic", False), ("tiny_mlp", True)):
        _enter_ai_demo(ser, scr, use_mlp=use_mlp)
        ts_start_idx = len(scr.frame_timestamps)
        t_end = time.perf_counter() + 3.5
        while time.perf_counter() < t_end:
            drain_serial(ser, scr, 0.05)
            if scr.detect_state() == "ai_demo_ended":
                _enter_ai_demo(ser, scr, use_mlp=use_mlp)
        new_ts = scr.frame_timestamps[ts_start_idx:]
        dts = [new_ts[k] - new_ts[k - 1] for k in range(1, len(new_ts)) if 0.15 < (new_ts[k] - new_ts[k - 1]) < 1.5]
        med_dt = sorted(dts)[len(dts) // 2] if dts else 0.0
        ok_ai = len(new_ts) >= 4 and (0.35 <= med_dt <= 0.70)
        record(
            f"6_ai_cadence_{model_name}",
            ok_ai,
            f"frames={len(new_ts)} in 3.5 s, median_dt={med_dt:.3f} s",
        )
        send_bytes(ser, scr, b"n")
        wait_until(ser, scr, lambda: scr.detect_state() == "class_select", 1.5)
        drain_frame(ser, scr, max_wait_s=0.45, settle_s=0.06)

    # 7. Low-HP BEL + Banner Pulse Cadence
    send_bytes(ser, scr, b"1")
    wait_until(ser, scr, lambda: scr.detect_state() in ("human_play", "low_hp") and scr.is_frame_complete(), 2.0)
    ok_low = seek_low_hp(ser, scr, target_death=False, timeout_s=30.0)
    if not ok_low:
        record("7_low_hp_bel_cadence", False, "Failed to reach Low-HP state within 30 s")
    bel0 = scr.bel_count
    drain_serial(ser, scr, 5.8)
    bel_delta = scr.bel_count - bel0
    hud_low = scr.parse_hud()
    record(
        "7_low_hp_bel_cadence",
        hud_low["low_hp"] == 1 and bel_delta >= 2 and len(scr.validator.errors) == 0,
        f"low_hp={hud_low['low_hp']}, bel_delta={bel_delta} over 5.8 s, errors={len(scr.validator.errors)}",
    )

    # Return cleanly to Class Select
    ensure_class_select(ser, scr)
    return {
        "passed": True,
        "checks": results,
        "validator": scr.validator.summary() if scr.validator else {},
    }


# ---------------------------------------------------------------------------
# Offline Self-Test (--self-test)
# ---------------------------------------------------------------------------

class _FakeSerial:
    """In-memory serial stub simulating MCU responses for offline --self-test."""

    def __init__(self) -> None:
        self.rx_buf = bytearray()
        self.tx_log: List[bytes] = []
        self.mode = "class_select"
        self.hp = 16
        self.max_hp = 16
        self.px = 5
        self.py = 5
        self.dir = 0
        self.ai_model = 0
        self.ai_ticks = 0
        self.empowered = False
        self.status_msg = "READY."
        self.meditate_status_override: Optional[str] = None
        # Simple 28x17 room with an enemy at (8, 5) and trap at (5, 7)
        self.enemy_pos = (8, 5)
        self.trap_pos = (5, 7)
        self._esc = 0
        self._csi = ""
        self._last_bel_t = time.perf_counter()
        self._render_current()

    @property
    def in_waiting(self) -> int:
        low = self.mode == "human" and self.hp > 0 and (self.hp <= 3 or self.hp * 4 <= self.max_hp)
        if low and (time.perf_counter() - self._last_bel_t) >= 1.0:
            self._last_bel_t = time.perf_counter()
            self.rx_buf.extend(b"\x07")
        return len(self.rx_buf)

    def read(self, n: int) -> bytes:
        out = bytes(self.rx_buf[:n])
        del self.rx_buf[:n]
        return out

    def _emit(self, payload: bytes) -> None:
        self.rx_buf.extend(payload)

    def _render_current(self) -> None:
        if self.mode == "class_select":
            self._emit(b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m MSP430FR5994 DUNGEON CRAWLER                         NEW ADVENTURE             \x1b[0m")
            return
        if self.mode == "help":
            self._emit(b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m MSP430 DUNGEON HELP & LEGEND              PRESS ? OR ANY KEY TO CLOSE          \x1b[0m")
            return
        if self.mode == "map":
            self._emit(b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m FULL FLOOR MAP (48x22)   FL:01   PRESS M OR ANY MOVE KEY TO CLOSE              \x1b[0m")
            return
        if self.mode == "hof":
            self._emit(b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m FRAM HALL OF FAME (TOP 5 RUNS)            PRESS H OR ANY KEY TO CLOSE          \x1b[0m")
            return

        # Gameplay / AI frame
        low = self.hp > 0 and (self.hp <= 3 or self.hp * 4 <= self.max_hp) and self.mode != "game_over"
        out = bytearray(b"\x1b[2J\x1b[H\x1b[?25l")
        if low:
            out.extend(f"\x1b[1;37;41m [! LOW HP !]  FL:01  HP:{self.hp:02d}/{self.max_hp:02d}  PRESS 1 FOR POTION OR RETREAT!\x1b[0m".encode("ascii"))
        elif self.mode == "ai":
            tag = "AI:HEURISTIC" if self.ai_model == 0 else "AI:TINY-MLP "
            out.extend(f"\x1b[1;37;45m {tag}   FL:01 BEST:01 SEED:1234  BIOME: STONE CRYPT\x1b[0m".encode("ascii"))
        else:
            out.extend(b"\x1b[1;37;44m MSP430 DUNGEON FL:01 BEST:01 SEED:1234  BIOME: STONE CRYPT\x1b[0m")

        dir_glyphs = ["^^", ">>", "vv", "<<"]
        dir_chars = ["N", "E", "S", "W"]
        for vr in range(VIEW_H):
            out.extend(f"\x1b[{vr + 2};1H".encode("ascii"))
            for vc in range(VIEW_W):
                if vr == 0 or vr == 16 or vc == 0 or vc == 27:
                    out.extend(b"##")
                elif (vc, vr) == (self.px, self.py):
                    out.extend(dir_glyphs[self.dir].encode("ascii"))
                elif (vc, vr) == self.enemy_pos:
                    out.extend(b"SK")
                elif (vc, vr) == self.trap_pos:
                    out.extend(b" ^")
                else:
                    out.extend(b" .")
            out.extend(b"|")

        out.extend(f"\x1b[2;58HHP:{self.hp:02d}/{self.max_hp:02d} MP:02/04".encode("ascii"))
        out.extend(b"\x1b[3;58HATK:03  DEF:01")
        emp_tag = " \x1b[1;32m[EMP]\x1b[0m      " if self.empowered else "            "
        out.extend(f"\x1b[4;58HGOLD:0000{emp_tag}".encode("ascii"))
        out.extend(f"\x1b[5;58HKNT DIR:{dir_chars[self.dir]} {self.px:02d},{self.py:02d}".encode("ascii"))
        if self.mode == "game_over":
            msg = (
                self.status_msg
                if self.status_msg.startswith("CAMERA RE-CENTERED")
                else "YOU DIED! W/SPACE:NEW RUN"
            )
            out.extend(f"\x1b[20;1H{msg}".encode("ascii"))
        elif self.mode == "ai_ended":
            out.extend(b"\x1b[20;1HAI DEMO ENDED! PRESS 4/W FOR NEW DEMO, N FOR CLASS SELECT.")
        else:
            out.extend(f"\x1b[20;1H{self.status_msg}".encode("ascii"))
        if low:
            out.extend(b"\x07")
        self._emit(bytes(out))

    def write(self, data: bytes) -> int:
        self.tx_log.append(data)
        for b in data:
            if self._esc == 1:
                if b == ord("["):
                    self._esc = 2
                    self._csi = ""
                else:
                    self._esc = 0
                continue
            if self._esc == 2:
                ch = chr(b)
                if 0x40 <= b <= 0x7E:
                    seq = self._csi + ch
                    self._esc = 0
                    self._csi = ""
                    if seq == "A":
                        self._handle_cmd(ord("w"))
                    elif seq == "B":
                        self._handle_cmd(ord("s"))
                    elif seq == "C":
                        self._handle_cmd(ord("d"))
                    elif seq == "D":
                        self._handle_cmd(ord("a"))
                    elif seq == "0n":
                        pass
                else:
                    self._csi += ch
                continue
            if b == 0x1B:
                self._esc = 1
                continue
            self._handle_cmd(b)
        return len(data)

    def _handle_cmd(self, b: int) -> None:
        ch = chr(b)
        if b == 0x0C:
            self._render_current()
            return
        if self.mode in ("help", "map", "hof"):
            self.mode = "human"
            self._render_current()
            return
        if self.mode == "class_select":
            if ch in ("1", "w", "W", " "):
                self.mode = "human"
                self.hp = 16
                self.px, self.py = 5, 5
                self.dir = 1
                self.empowered = False
                self.status_msg = "ADVENTURE BEGINS! PRESS ? FOR HELP, M FOR MAP, H FOR HALL OF FAME."
            elif ch == "4":
                self.mode = "ai"
                self.ai_model = 0
                self.hp = 16
                self.ai_ticks = 0
                self.empowered = False
                self.status_msg = "READY."
            self._render_current()
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
            elif ch == "4" and self.mode == "ai_ended":
                self.mode = "ai"
                self.ai_model = 0
                self.hp = 16
                self.status_msg = "READY."
            self._render_current()
            return
        if ch in ("c", "C"):
            self.status_msg = "CAMERA RE-CENTERED."
            self._render_current()
            return
        if self.mode == "game_over":
            if ch in ("w", "W", " "):
                self.mode = "class_select"
                self.status_msg = "READY."
                self._render_current()
            return
        # human mode
        if ch == "?":
            self.mode = "help"
        elif ch in ("m", "M"):
            self.mode = "map"
        elif ch in ("h", "H"):
            self.mode = "hof"
        elif ch == " ":
            self.empowered = True
            self.status_msg = (
                self.meditate_status_override
                if self.meditate_status_override is not None
                else "MEDITATED (+1 MP) & EMPOWERED NEXT ATTACK (+50% DMG)!"
            )
            if abs(self.px - self.enemy_pos[0]) + abs(self.py - self.enemy_pos[1]) == 1:
                self.hp = max(0, self.hp - 1)
                if self.hp == 0:
                    self.mode = "game_over"
                    self.status_msg = "YOU DIED! W/SPACE:NEW RUN"
        elif ch in ("w", "d", "s", "a"):
            dmap = {"w": (0, -1, 0), "d": (1, 0, 1), "s": (0, 1, 2), "a": (-1, 0, 3)}
            dx, dy, d = dmap[ch]
            self.dir = d
            nx, ny = self.px + dx, self.py + dy
            if (nx, ny) == self.enemy_pos:
                self.empowered = False
            elif 1 <= nx <= 26 and 1 <= ny <= 15:
                self.px, self.py = nx, ny
                if (nx, ny) == self.trap_pos:
                    self.hp = max(0, self.hp - 3)
                    if self.hp == 0:
                        self.mode = "game_over"
                        self.status_msg = "YOU DIED! W/SPACE:NEW RUN"
        self._render_current()


def run_self_test() -> None:
    print("[SELF-TEST] Running offline tools/power_bench.py test suite...", flush=True)

    # 1. AnsiStreamValidator positive & negative checks
    v = AnsiStreamValidator()
    good = b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44mHELLO\x1b[0m\x1b[5n\x07\r\n"
    for b in good:
        v.feed_byte(b)
    assert len(v.errors) == 0
    assert v.dsr_queries == 1 and v.bel_count == 1 and v.frames_cleared == 1, v.summary()

    v_bad = AnsiStreamValidator()
    for b in b"\x1b[99;1H\x01\xff\x1b[1;31;":
        v_bad.feed_byte(b)
    assert v_bad.out_of_bounds_cup == 1
    assert v_bad.unexpected_ctrl_bytes == 1
    assert v_bad.non_ascii_bytes == 1
    assert v_bad.has_incomplete_escape
    assert len(v_bad.errors) >= 3

    # 2. Split DSR packet handling (\x1b[ + 5n across chunks)
    dsr_replies: List[bytes] = []

    class _StubPort:
        def write(self, b: bytes) -> int:
            dsr_replies.append(b)
            return len(b)

    scr_split = AnsiScreen()
    stub_port = _StubPort()
    scr_split.feed(b"\x1b[", ser=stub_port)
    assert scr_split.dsr_count == 0
    scr_split.feed(b"5n", ser=stub_port)
    assert scr_split.dsr_count == 1
    assert dsr_replies == [b"\x1b[0n"]
    assert scr_split.tx_bytes == 4

    # 3. detect_state() across all 10 states
    state_banners = [
        ("class_select", b"\x1b[2J\x1b[H MSP430FR5994 DUNGEON CRAWLER                         NEW ADVENTURE             "),
        ("hof_overlay", b"\x1b[2J\x1b[H FRAM HALL OF FAME (TOP 5 RUNS)"),
        ("help_overlay", b"\x1b[2J\x1b[H MSP430 DUNGEON HELP & LEGEND"),
        ("map_overlay", b"\x1b[2J\x1b[H FULL FLOOR MAP (48x22)"),
        ("ai_heuristic", b"\x1b[2J\x1b[H AI:HEURISTIC   FL:01"),
        ("ai_mlp", b"\x1b[2J\x1b[H AI:TINY-MLP    FL:01"),
        ("human_play", b"\x1b[2J\x1b[H MSP430 DUNGEON FL:01"),
        ("low_hp", b"\x1b[2J\x1b[H [! LOW HP !]  FL:01  HP:03/16"),
        ("game_over", b"\x1b[2J\x1b[H MSP430 DUNGEON FL:01\x1b[20;1HYOU DIED! W/SPACE:NEW RUN"),
        ("ai_demo_ended", b"\x1b[2J\x1b[H AI:HEURISTIC   FL:01\x1b[20;1HAI DEMO ENDED! PRESS 4/W FOR NEW DEMO, N FOR CLASS SELECT."),
    ]
    for expected, frame in state_banners:
        s = AnsiScreen()
        s.feed(frame)
        assert s.detect_state() == expected, f"Expected {expected}, got {s.detect_state()}"
        assert len(s.validator.errors) == 0

    # 4. ensure_class_select & seek_low_hp BFS on synthetic 28x17 room
    fake = _FakeSerial()
    scr = AnsiScreen()
    drain_serial(fake, scr, 0.05)
    assert ensure_class_select(fake, scr)

    # Start from human_play -> ensure_class_select must walk to hazards, die, send 'w'
    fake.write(b"1")
    drain_serial(fake, scr, 0.05)
    assert scr.detect_state() == "human_play"
    assert ensure_class_select(fake, scr, timeout_s=15.0)
    assert scr.detect_state() == "class_select"

    # Low-HP profile on synthetic serial
    res_low = run_profile(fake, scr, "low-hp", duration_s=0.3)
    assert res_low["final_low_hp"] == 1
    assert scr.detect_state() == "low_hp"

    # AI Heuristic & AI Tiny-MLP restart test (simulating AI DEMO ENDED mid-window)
    fake.mode = "class_select"
    fake._render_current()
    drain_serial(fake, scr, 0.05)
    res_heur = run_profile(fake, scr, "ai-heuristic", duration_s=0.2)
    assert res_heur["final_state"] == "class_select"

    fake.mode = "ai_ended"
    fake._render_current()
    drain_serial(fake, scr, 0.05)
    assert ensure_class_select(fake, scr)
    res_mlp = run_profile(fake, scr, "ai-mlp", duration_s=0.2)
    assert res_mlp["final_state"] == "class_select"
    assert any(b"t" in tx for tx in fake.tx_log)

    # Play profile cadence test (1.2 s -> 2 keys sent)
    res_play = run_profile(fake, scr, "play", duration_s=1.2)
    assert res_play["keys_sent"] >= 2
    assert len(scr.validator.errors) == 0

    # 5. Split frame header timestamp coalescing & hud_valid mid-frame guard
    s_ts = AnsiScreen()
    for ch_chunk in (b"\x1b", b"[2J\x1b[", b"H\x1b[?25", b"l", b"\x1b[1;37;41m [! LOW HP !] \x1b[0m"):
        s_ts.feed(ch_chunk)
    assert len(s_ts.frame_timestamps) == 1, f"Expected 1 coalesced timestamp, got {len(s_ts.frame_timestamps)}"
    assert s_ts.parse_hud()["hud_valid"] is False
    assert s_ts.is_frame_complete() is False

    # 6. Chunked/split-frame \x1b[2J delayed-arrival (>140 ms) regression test
    class _DelayedChunkSerial(_FakeSerial):
        """Simulates 115200-baud mid-frame delays where \\x1b[2J arrives >140 ms before HUD/YOU DIED!."""

        def __init__(self) -> None:
            self._scheduled: List[Tuple[float, bytes]] = []
            self.tx_states_at_send: List[Tuple[bytes, str, bool]] = []
            super().__init__()

        def _emit(self, payload: bytes) -> None:
            marker_2j = payload.find(b"\x1b[2J")
            marker_hud = payload.find(b"\x1b[2;58H")
            marker_row20 = payload.find(b"\x1b[20;1H")
            if marker_2j >= 0 and marker_hud > 0 and marker_row20 > marker_hud:
                now = time.perf_counter()
                self.rx_buf.extend(payload[:marker_hud])
                self._scheduled.append((now + 0.16, payload[marker_hud:marker_row20]))
                self._scheduled.append((now + 0.22, payload[marker_row20:]))
            else:
                self.rx_buf.extend(payload)

        @property
        def in_waiting(self) -> int:
            now = time.perf_counter()
            due = [chunk for ts, chunk in self._scheduled if ts <= now]
            if due:
                self._scheduled = [(ts, chunk) for ts, chunk in self._scheduled if ts > now]
                for chunk in due:
                    self.rx_buf.extend(chunk)
            return super().in_waiting

    dser = _DelayedChunkSerial()
    dscr = AnsiScreen()
    dser.mode = "human"
    dser.hp = 1
    dser.px, dser.py = 7, 5  # Adjacent to enemy at (8, 5)
    dser._render_current()
    drain_frame(dser, dscr, max_wait_s=0.6, settle_s=0.05)
    assert dscr.detect_state() == "low_hp" and dscr.parse_hud()["hud_valid"] is True

    # Wrap write to capture screen state at the exact moment 'w' is transmitted
    orig_write = dser.write

    def _tracked_write(data: bytes) -> int:
        if b"w" in data:
            dser.tx_states_at_send.append((data, dscr.detect_state(), bool(dscr.parse_hud()["hud_valid"])))
        return orig_write(data)

    dser.write = _tracked_write  # type: ignore[method-assign]
    assert ensure_class_select(dser, dscr, timeout_s=10.0)
    assert dscr.detect_state() == "class_select"
    assert dser.tx_states_at_send == [(b"w", "game_over", True)], (
        f"Expected 'w' only on settled game_over, got {dser.tx_states_at_send}"
    )

    # Verify seek_low_hp never emits 'w' when HUD is invalid mid-frame (emits Ctrl+L instead)
    dscr_mid = AnsiScreen()
    dscr_mid.feed(b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m MSP430 DUNGEON FL:01\x1b[0m")
    assert dscr_mid.parse_hud()["hud_valid"] is False
    dser2 = _FakeSerial()
    dser2.mode = "human"
    dser2.hp = 16
    dser2.px, dser2.py = 7, 5
    dser2.rx_buf.clear()
    assert seek_low_hp(dser2, dscr_mid, target_death=False, timeout_s=5.0, max_steps=20)
    assert dser2.tx_log[0] == b"\x0c", f"Expected Ctrl+L when HUD invalid, got {dser2.tx_log[0]!r}"
    assert all(b"w" not in tx for tx in dser2.tx_log), f"Unexpected 'w' emitted: {dser2.tx_log}"

    # 7. Reordered CSI header coalescing (\x1b[H\x1b[2J\x1b[?25l and \x1b[?25l\x1b[2J\x1b[H)
    for hdr_chunks in (
        (b"\x1b[H", b"\x1b[2J", b"\x1b[?25l", b"\x1b[1;37;44m MSP430 DUNGEON FL:01\x1b[0m"),
        (b"\x1b[?25l", b"\x1b[2J", b"\x1b[H", b"\x1b[1;37;44m MSP430 DUNGEON FL:01\x1b[0m"),
    ):
        s_hdr = AnsiScreen()
        for ch_chunk in hdr_chunks:
            s_hdr.feed(ch_chunk)
        assert len(s_hdr.frame_timestamps) == 1, (
            f"Expected 1 coalesced timestamp for {hdr_chunks!r}, got {len(s_hdr.frame_timestamps)}"
        )

    # 8. Burst #1 Row 19 Archer-overwrite regression + negative predicate checks
    b_ser = _FakeSerial()
    b_scr = AnsiScreen()
    b_ser.meditate_status_override = "WARNING: CULTIST ARCHER (A!) IS AIMING AT YOU! SIDESTEP!"
    b_ser.write(b"1")
    drain_serial(b_ser, b_scr, 0.05)
    bursts_b = _build_rapid_bursts(b_scr)
    burst1_payload, burst1_pred = bursts_b[1]
    assert burst1_payload == b"a s d w  "
    b_ser.write(burst1_payload)
    drain_serial(b_ser, b_scr, 0.05)
    assert "WARNING: CULTIST ARCHER" in b_scr.row(19)
    assert "DIR:N" in b_scr.row(4) and b_scr.parse_hud()["empowered"] == 1
    assert burst1_pred() is True, "Burst #1 predicate must pass when Archer overwrites Row 19"

    # Negative check: missing [EMP] with DIR:N must fail Burst #1 predicate
    b_scr_no_emp = AnsiScreen()
    b_scr_no_emp.feed(
        b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m MSP430 DUNGEON FL:01\x1b[0m"
        b"\x1b[2;58HHP:16/16 MP:02/04\x1b[4;58HGOLD:0000            \x1b[5;58HKNT DIR:N 05,05\x1b[20;1HREADY."
    )
    assert _build_rapid_bursts(b_scr_no_emp)[1][1]() is False

    # Negative check: wrong DIR:W with [EMP] must fail Burst #1 predicate
    b_scr_dir_w = AnsiScreen()
    b_scr_dir_w.feed(
        b"\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m MSP430 DUNGEON FL:01\x1b[0m"
        b"\x1b[2;58HHP:16/16 MP:02/04\x1b[4;58HGOLD:0000 \x1b[1;32m[EMP]\x1b[0m      \x1b[5;58HKNT DIR:W 05,05\x1b[20;1HREADY."
    )
    assert _build_rapid_bursts(b_scr_dir_w)[1][1]() is False

    # 9. Mid-burst death & zombie-'c' regression checks on Bursts #1, #3, #4
    # 9a. Burst #1 death on 'd' -> 'w' -> class_select -> ' ' -> Knight (DIR:E) -> ' ' -> [EMP]
    m1 = _FakeSerial()
    m1_scr = AnsiScreen()
    m1.mode = "human"
    m1.hp = 3
    m1.px, m1.py = 5, 6
    m1.trap_pos = (5, 7)
    m1._render_current()
    drain_serial(m1, m1_scr, 0.05)
    m1.write(b"a s d w  ")
    drain_serial(m1, m1_scr, 0.05)
    assert m1_scr.detect_state() in ("human_play", "low_hp")
    assert "DIR:E" in m1_scr.row(4) and m1_scr.parse_hud()["empowered"] == 1
    assert _build_rapid_bursts(m1_scr)[1][1]() is True

    # 9b. Burst #1 death on 'w' -> ' ' -> class_select -> ' ' -> Knight (DIR:E, ADVENTURE BEGINS)
    m1b = _FakeSerial()
    m1b_scr = AnsiScreen()
    m1b.mode = "human"
    m1b.hp = 3
    m1b.px, m1b.py = 5, 6
    m1b.trap_pos = (5, 6)
    m1b._render_current()
    drain_serial(m1b, m1b_scr, 0.05)
    m1b.write(b"a s d w  ")
    drain_serial(m1b, m1b_scr, 0.05)
    assert "DIR:E" in m1b_scr.row(4) and "ADVENTURE BEGINS" in m1b_scr.row(19)
    assert _build_rapid_bursts(m1b_scr)[1][1]() is True

    # 9c. Burst #1 death on first ' ' -> second ' ' -> class_select
    m1c = _FakeSerial()
    m1c_scr = AnsiScreen()
    m1c.mode = "human"
    m1c.hp = 1
    m1c.px, m1c.py = 7, 6
    m1c.enemy_pos = (7, 5)
    m1c.trap_pos = (1, 1)
    m1c._render_current()
    drain_serial(m1c, m1c_scr, 0.05)
    m1c.write(b"a s d w  ")
    drain_serial(m1c, m1c_scr, 0.05)
    assert m1c_scr.detect_state() == "class_select"
    assert _build_rapid_bursts(m1c_scr)[1][1]() is True

    # 9d. Burst #3 death on 'd' (\x1b[C) -> trailing \x1b[A ('w') lands in class_select
    m3 = _FakeSerial()
    m3_scr = AnsiScreen()
    m3.mode = "human"
    m3.hp = 3
    m3.px, m3.py = 4, 7
    m3.trap_pos = (5, 7)
    m3._render_current()
    drain_serial(m3, m3_scr, 0.05)
    m3.write(b"\x1b[C\x1b[B\x1b[D\x1b[A")
    drain_serial(m3, m3_scr, 0.05)
    assert m3_scr.detect_state() == "class_select"
    assert _build_rapid_bursts(m3_scr)[3][1]() is True

    # 9e. Burst #4 death on trailing ' ' before 'c' -> 'c' overwrites Row 19 (zombie s_game_over)
    m4 = _FakeSerial()
    m4_scr = AnsiScreen()
    m4.mode = "human"
    m4.hp = 1
    m4.px, m4.py = 6, 6
    m4.enemy_pos = (7, 6)
    m4.trap_pos = (1, 1)
    m4._render_current()
    drain_serial(m4, m4_scr, 0.05)
    m4.write(b"s a w d c")
    drain_serial(m4, m4_scr, 0.05)
    assert m4_scr.detect_state() == "human_play"
    assert "CAMERA RE-CENTERED" in m4_scr.row(19)
    assert m4_scr.parse_hud()["hp"] == 0
    assert _is_dead_or_game_over(m4_scr) is True
    assert _build_rapid_bursts(m4_scr)[4][1]() is True
    m4.tx_log.clear()
    assert ensure_class_select(m4, m4_scr, timeout_s=5.0)
    assert m4_scr.detect_state() == "class_select"
    assert m4.tx_log == [b"\x0c", b"w"], f"Expected zombie recovery via Ctrl+L then 'w', got {m4.tx_log}"

    # 10. wait_for_user_start pump_once ai-mlp restart with countdown=1
    w_ser = _FakeSerial()
    w_scr = AnsiScreen()
    w_ser.mode = "ai_ended"
    w_ser._render_current()
    drain_serial(w_ser, w_scr, 0.05)
    assert w_scr.detect_state() == "ai_demo_ended"
    wait_for_user_start(w_ser, w_scr, "ai-mlp", wait_start=False, countdown=1)
    assert w_scr.detect_state() == "ai_mlp"
    assert w_ser.ai_model == 1

    # Temp file write & _persist_outputs failure-persistence sanity
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "test_summary.json"
        p.write_text(json.dumps(res_play), encoding="utf-8")
        assert p.exists()
        fail_log = Path(td) / "fail.log"
        fail_json = Path(td) / "fail.json"
        scr_fail = AnsiScreen()
        scr_fail.raw_chunks = bytearray(b"PARTIAL_STREAM")
        _persist_outputs(str(fail_log), str(fail_json), scr_fail, {"error": "TimeoutError: test"})
        assert fail_log.read_bytes() == b"PARTIAL_STREAM"
        assert json.loads(fail_json.read_text(encoding="utf-8"))["error"] == "TimeoutError: test"

    print("[SELF-TEST] All power_bench.py offline checks PASSED.", flush=True)


def _persist_outputs(
    out_log_path: Optional[str],
    out_json_path: Optional[str],
    scr: AnsiScreen,
    summary: Dict[str, object],
) -> None:
    if out_log_path and scr.raw_chunks is not None:
        out_log = Path(out_log_path)
        out_log.parent.mkdir(parents=True, exist_ok=True)
        out_log.write_bytes(bytes(scr.raw_chunks))

    if out_json_path:
        out_json = Path(out_json_path)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Reproducible COM4 workload driver and UART reliability verifier for mspgame."
    )
    ap.add_argument("--port", default="COM4", help="Serial port (default: COM4)")
    ap.add_argument("--baud", type=int, default=115200, help="UART baud rate (default: 115200)")
    ap.add_argument("--duration", type=float, default=30.0, help="Capture window duration in seconds (default: 30.0)")
    ap.add_argument(
        "--profile",
        choices=["idle", "play", "ai-heuristic", "ai-mlp", "low-hp", "all"],
        default="idle",
        help="Workload profile to run (default: idle)",
    )
    ap.add_argument("--out-log", default=None, help="Optional path to save raw captured UART stream")
    ap.add_argument("--out-json", default=None, help="Optional path to save JSON summary")
    ap.add_argument(
        "--wait-start",
        "--wait-for-enter",
        dest="wait_start",
        action="store_true",
        help="Pause after staging profile and wait for ENTER before starting countdown",
    )
    ap.add_argument("--countdown", type=int, default=3, help="Countdown seconds before capture window (default: 3)")
    ap.add_argument("--verify-uart", action="store_true", help="Run 7-part Zero-UX-Loss UART verification suite")
    ap.add_argument("--self-test", action="store_true", help="Run offline unit tests without serial hardware")
    args = ap.parse_args()

    if args.self_test:
        run_self_test()
        return 0

    import serial  # Imported lazily so --self-test has zero serial dependency

    scr = AnsiScreen(validate=True)
    if args.out_log:
        scr.raw_chunks = bytearray()

    summary: Dict[str, object] = {
        "port": args.port,
        "baud": args.baud,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    verify_checks: List[Dict[str, object]] = []
    runs: List[Dict[str, object]] = []

    try:
        with serial.Serial(args.port, args.baud, timeout=0.05, write_timeout=0.5) as ser:
            if args.verify_uart:
                v_res = verify_uart(ser, scr, results_out=verify_checks)
                summary["verify_uart"] = v_res
            else:
                profiles = (
                    ["idle", "ai-heuristic", "ai-mlp", "low-hp", "play"]
                    if args.profile == "all"
                    else [args.profile]
                )
                for idx, prof in enumerate(profiles):
                    res = run_profile(
                        ser,
                        scr,
                        profile=prof,
                        duration_s=args.duration,
                        wait_start=args.wait_start,
                        countdown=args.countdown if (args.wait_start or idx == 0) else 0,
                    )
                    runs.append(res)
                    print(json.dumps(res, indent=2), flush=True)
                ensure_class_select(ser, scr)
                summary["profiles"] = runs
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        if args.verify_uart and "verify_uart" not in summary:
            summary["verify_uart"] = {"passed": False, "checks": verify_checks}
        elif not args.verify_uart and "profiles" not in summary:
            summary["profiles"] = runs
        raise
    finally:
        summary["validator"] = scr.validator.summary() if scr.validator else {}
        _persist_outputs(args.out_log, args.out_json, scr, summary)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
