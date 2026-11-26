"""
Hardware-in-the-Loop (HIL) evaluator for running evolved GA genomes live on the
physical TI MSP430FR5994 LaunchPad over UART (COM4 @ 115200 baud).

Parses the live 80x24 ANSI/VT100 terminal output from the MCU, extracts the
fair fog-of-war features, queries a saved Heuristic or Tiny-MLP genome, and
transmits real keypresses back to the LaunchPad—optionally saving a replay log.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Tuple

import serial

from ga.genome import HeuristicGenome, TinyMLPGenome

W, H = 80, 24


class AnsiScreen:
    """Minimal 80x24 ANSI/VT100 terminal emulator for HIL perception."""

    def __init__(self) -> None:
        self.grid = [[" "] * W for _ in range(H)]
        self.r = 0
        self.c = 0
        self.esc = 0
        self.csi = ""
        self.bel_count = 0
        self.dsr_count = 0

    def clear(self) -> None:
        for r in range(H):
            self.grid[r] = [" "] * W

    def feed(self, data: bytes, ser: serial.Serial) -> None:
        for b in data:
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
                            self.r = 0; self.c = 0
                        else:
                            parts = self.csi.split(";")
                            r1 = int(parts[0]) if parts[0].isdigit() else 1
                            c1 = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
                            self.r = max(0, min(H - 1, r1 - 1))
                            self.c = max(0, min(W - 1, c1 - 1))
                    elif ch == "J" and self.csi == "2":
                        self.clear()
                    elif ch == "K":
                        for cc in range(self.c, W):
                            self.grid[self.r][cc] = " "
                    elif ch == "n" and self.csi == "5":
                        self.dsr_count += 1
                        ser.write(b"\x1b[0n")
                    self.esc = 0
                else:
                    self.csi += ch

    def row(self, r: int) -> str:
        return "".join(self.grid[r])

    def tile(self, vr: int, vc: int) -> str:
        r = vr + 1
        c = vc * 2
        if r < 1 or r > 17 or c < 0 or c > 54:
            return "##"
        return self.grid[r][c] + self.grid[r][c + 1]


def parse_hud(scr: AnsiScreen) -> Dict[str, int]:
    r0, r1, r3, r4, r11 = scr.row(0), scr.row(1), scr.row(3), scr.row(4), scr.row(11)
    m_fl = re.search(r"FL:(\d+)", r0)
    m_hp = re.search(r"HP:(\d+)/(\d+)", r1)
    m_mp = re.search(r"MP:(\d+)/(\d+)", r1)
    m_gd = re.search(r"GOLD:(\d+)", r3)
    m_pos = re.search(r"(\d+),(\d+)", r4)
    m_pot = re.search(r"\[1\]Pot:(\d+)", r11)
    m_bmb = re.search(r"\[2\]Bmb:(\d+)", r11)
    return {
        "floor": int(m_fl.group(1)) if m_fl else 1,
        "hp": int(m_hp.group(1)) if m_hp else 10,
        "max_hp": int(m_hp.group(2)) if m_hp else 16,
        "mp": int(m_mp.group(1)) if m_mp else 0,
        "max_mp": int(m_mp.group(2)) if m_mp else 4,
        "gold": int(m_gd.group(1)) if m_gd else 0,
        "px": int(m_pos.group(1)) if m_pos else 0,
        "py": int(m_pos.group(2)) if m_pos else 0,
        "pots": int(m_pot.group(1)) if m_pot else 0,
        "bombs": int(m_bmb.group(1)) if m_bmb else 0,
        "low_hp": 1 if "LOW HP" in r0 else 0,
        "dead": 1 if "YOU DIED" in scr.row(19) else 0,
        "class_select": 1 if "NEW ADVENTURE" in r0 else 0,
    }


def choose_hil_action(scr: AnsiScreen, hud: Dict[str, int], visited: Dict[Tuple[int, int], int], weights: List[float]) -> str:
    """Apply evolved heuristic weights directly to the live UART 28x17 viewport."""
    if hud["low_hp"] and hud["pots"] > 0:
        return "1"

    pvr = pvc = -1
    for vr in range(17):
        for vc in range(28):
            if scr.tile(vr, vc) in ("^^", ">>", "vv", "<<"):
                pvr, pvc = vr, vc
                break
        if pvr >= 0:
            break
    if pvr < 0:
        return "\x0c"

    foes = {"sl", "SK", "AR", "A!", "WR", "MM", "BW", "B!"}
    dirs = (("w", -1, 0, 0, -1), ("d", 0, 1, 1, 0), ("s", 1, 0, 0, 1), ("a", 0, -1, -1, 0))
    w_melee = abs(weights[0]) + 1.5
    w_stairs = abs(weights[2]) + 2.5
    w_loot = abs(weights[3]) + 1.5
    w_shop = abs(weights[4]) + 1.5
    w_door = abs(weights[5]) + 1.0
    w_visit = abs(weights[11]) + 0.6

    adj_foes = sum(1 for _, dr, dc, _, _ in dirs if scr.tile(pvr + dr, pvc + dc) in foes)
    if adj_foes >= 2 and hud["mp"] >= 2:
        return "q"

    best_k, best_s = " ", -1e9
    for k, dr, dc, dx, dy in dirs:
        t = scr.tile(pvr + dr, pvc + dc)
        if t in ("##", "  "):
            continue
        nx, ny = hud["px"] + dx, hud["py"] + dy
        vcnt = visited.get((nx, ny), 0)
        s = -w_visit * vcnt
        if t == " >": s += w_stairs * 4.0
        elif t in ("*V", "*A"): s += w_stairs * 5.0
        elif t in foes: s += w_melee * 3.0
        elif t in (" $", " +", " *", " &"): s += w_loot * 2.5
        elif t == "$$" and hud["gold"] >= 40: s += w_shop * 2.5
        elif t == "$$": continue
        elif t == "++": s += w_door * 1.8
        elif t == " ^": s -= 3.0
        elif t == "!!": s -= 6.0
        if s > best_s:
            best_s = s
            best_k = k
    return best_k


def run_hil(port: str, baud: int, genome_path: Path, turns: int, delay_ms: int, out_json: Path | None) -> Dict:
    gdata = json.loads(genome_path.read_text(encoding="utf-8"))
    weights = gdata.get("weights", [1.0] * 22)
    scr = AnsiScreen()
    visited: Dict[Tuple[int, int], int] = {}
    max_floor = 1
    max_gold = 0
    deaths = 0
    executed = 0

    with serial.Serial(port, baud, timeout=0.05, write_timeout=0.5) as ser:
        def drain(ms: int) -> None:
            t_end = time.perf_counter() + ms / 1000.0
            while time.perf_counter() < t_end:
                n = ser.in_waiting
                if n > 0:
                    scr.feed(ser.read(n), ser)
                else:
                    time.sleep(0.01)

        ser.write(b"\x0c")
        drain(300)

        for _ in range(turns):
            hud = parse_hud(scr)
            if hud["class_select"]:
                ser.write(b"1")
                drain(300)
                visited.clear()
                continue
            if hud["dead"]:
                deaths += 1
                ser.write(b"w")
                drain(300)
                continue

            max_floor = max(max_floor, hud["floor"])
            max_gold = max(max_gold, hud["gold"])
            pos = (hud["px"], hud["py"])
            visited[pos] = visited.get(pos, 0) + 1

            act = choose_hil_action(scr, hud, visited, weights)
            ser.write(act.encode("ascii"))
            drain(delay_ms)
            executed += 1

    res = {
        "port": port,
        "baud": baud,
        "genome": str(genome_path),
        "turns": executed,
        "max_floor": max_floor,
        "max_gold": max_gold,
        "deaths": deaths,
        "bel_count": scr.bel_count,
        "dsr_count": scr.dsr_count,
        "final_banner": scr.row(0).strip(),
        "final_status": scr.row(19).strip(),
    }
    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps(res, indent=2))
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description="Run evolved GA genome live on MSP430FR5994 over UART")
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--genome", required=True)
    ap.add_argument("--turns", type=int, default=80)
    ap.add_argument("--delay-ms", type=int, default=120)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    run_hil(args.port, args.baud, Path(args.genome), args.turns, args.delay_ms, Path(args.out) if args.out else None)


if __name__ == "__main__":
    main()
