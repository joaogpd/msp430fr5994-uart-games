"""
Hardware-in-the-Loop (HIL) evaluator for running evolved GA genomes (both
Approach A HeuristicGenome and Approach B TinyMLPGenome, float or Q7 int8)
live on the physical TI MSP430FR5994 LaunchPad over UART (COM4 @ 115200 baud).

Parses the live 80x24 ANSI/VT100 terminal output from the MCU into a Fair
Fog-of-War DungeonSim-compatible state proxy, runs the exact saved genome via
ga/features.py + ga/genome.py, and transmits real keypresses back over UART.
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
from ga.sim import (
    CLASS_ARCANIST,
    CLASS_KNIGHT,
    CLASS_ROGUE,
    DNG_CHEST,
    DNG_CRACKED_WALL,
    DNG_DOOR_CLOSED,
    DNG_DOOR_OPEN,
    DNG_FLOOR,
    DNG_H,
    DNG_MANA,
    DNG_POTION,
    DNG_RELIC,
    DNG_SHOP,
    DNG_SHRINE,
    DNG_STAIRS,
    DNG_TRAP,
    DNG_W,
    DNG_WALL,
    EF_AIMING,
    EF_AWAKE,
    ENEMY_ARCHER,
    ENEMY_BOSS,
    ENEMY_MIMIC,
    ENEMY_NONE,
    ENEMY_SKELETON,
    ENEMY_SLIME,
    ENEMY_WRAITH,
    SHOP_ATK,
    TILE_F_EXPLORED,
    TILE_F_REVEALED,
    TILE_F_VISIBLE,
    DungeonSim,
    Enemy,
    ShopSlot,
)

W, H = 80, 24

GLYPH_TO_ENEMY = {
    "sl": (ENEMY_SLIME, EF_AWAKE),
    "SK": (ENEMY_SKELETON, EF_AWAKE),
    "AR": (ENEMY_ARCHER, EF_AWAKE),
    "A!": (ENEMY_ARCHER, EF_AWAKE | EF_AIMING),
    "WR": (ENEMY_WRAITH, EF_AWAKE),
    "MM": (ENEMY_MIMIC, EF_AWAKE),
    "BW": (ENEMY_BOSS, EF_AWAKE),
    "B!": (ENEMY_BOSS, EF_AWAKE | EF_AIMING),
    "FR": (ENEMY_SKELETON, EF_AWAKE),  # Frozen visible enemy
}

DIR_GLYPH = {"^^": 0, ">>": 1, "vv": 2, "<<": 3}


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
    r0, r1, r2, r3, r4, r11, r12 = (
        scr.row(0), scr.row(1), scr.row(2), scr.row(3), scr.row(4), scr.row(11), scr.row(12)
    )
    m_fl = re.search(r"FL:(\d+)", r0)
    m_hp = re.search(r"HP:(\d+)/(\d+)", r1)
    m_mp = re.search(r"MP:(\d+)/(\d+)", r1)
    m_atk = re.search(r"ATK:(\d+)", r2)
    m_def = re.search(r"DEF:(\d+)", r2)
    m_gd = re.search(r"GOLD:(\d+)", r3)
    m_pos = re.search(r"(\d+),(\d+)", r4)
    m_pot = re.search(r"\[1\]Pot:(\d+)", r11)
    m_bmb = re.search(r"\[2\]Bmb:(\d+)", r11)
    m_map = re.search(r"\[3\]Map:(\d+)", r12)
    cls_id = CLASS_ARCANIST if "ARC" in r4 else (CLASS_ROGUE if "ROG" in r4 else CLASS_KNIGHT)
    return {
        "floor": int(m_fl.group(1)) if m_fl else 1,
        "hp": int(m_hp.group(1)) if m_hp else 10,
        "max_hp": int(m_hp.group(2)) if m_hp else 16,
        "mp": int(m_mp.group(1)) if m_mp else 0,
        "max_mp": int(m_mp.group(2)) if m_mp else 4,
        "atk": int(m_atk.group(1)) if m_atk else 3,
        "def_": int(m_def.group(1)) if m_def else 1,
        "gold": int(m_gd.group(1)) if m_gd else 0,
        "empowered": 1 if "[EMP]" in r3 else 0,
        "px": int(m_pos.group(1)) if m_pos else 0,
        "py": int(m_pos.group(2)) if m_pos else 0,
        "cls": cls_id,
        "pots": int(m_pot.group(1)) if m_pot else 0,
        "bombs": int(m_bmb.group(1)) if m_bmb else 0,
        "scrolls": int(m_map.group(1)) if m_map else 0,
        "low_hp": 1 if "LOW HP" in r0 else 0,
        "dead": 1 if "YOU DIED" in scr.row(19) or "AI DEMO ENDED" in scr.row(19) else 0,
        "class_select": 1 if "NEW ADVENTURE" in r0 else 0,
    }


def sync_proxy_from_screen(proxy: DungeonSim, scr: AnsiScreen, hud: Dict[str, int], last_floor: int) -> bool:
    """Populate a DungeonSim Fair-Fog-of-War proxy from the live 28x17 UART viewport."""
    pvr = pvc = -1
    pdir = proxy.dir
    for vr in range(17):
        for vc in range(28):
            g = scr.tile(vr, vc)
            if g in DIR_GLYPH:
                pvr, pvc = vr, vc
                pdir = DIR_GLYPH[g]
                break
        if pvr >= 0:
            break
    if pvr < 0:
        return False

    if hud["floor"] != last_floor:
        proxy.grid = [[DNG_WALL] * DNG_W for _ in range(DNG_H)]
        proxy.visited_counts.clear()
        proxy.floor_explored = 0

    proxy.floor = hud["floor"]
    proxy.hp = hud["hp"]
    proxy.max_hp = hud["max_hp"]
    proxy.mp = hud["mp"]
    proxy.max_mp = hud["max_mp"]
    proxy.atk = hud["atk"]
    proxy.def_ = hud["def_"]
    proxy.gold = hud["gold"]
    proxy.empowered = bool(hud["empowered"])
    proxy.px = hud["px"]
    proxy.py = hud["py"]
    proxy.dir = pdir
    proxy.cls = hud["cls"]
    proxy.potions = hud["pots"]
    proxy.bombs = hud["bombs"]
    proxy.scrolls = hud["scrolls"]
    proxy.game_over = bool(hud["dead"])
    proxy.visited_counts[(proxy.px, proxy.py)] = proxy.visited_counts.get((proxy.px, proxy.py), 0) + 1

    # Clear visibility & dynamic enemy/shop slots before scanning live viewport
    for r in range(DNG_H):
        for c in range(DNG_W):
            proxy.grid[r][c] &= ~TILE_F_VISIBLE
    for i in range(len(proxy.enemies)):
        proxy.enemies[i] = Enemy()
    for i in range(len(proxy.shop)):
        proxy.shop[i] = ShopSlot()

    cam_x = max(0, min(DNG_W - 28, proxy.px - pvc))
    cam_y = max(0, min(DNG_H - 17, proxy.py - pvr))
    e_slot = 0
    s_slot = 0

    for vr in range(17):
        for vc in range(28):
            mx, my = cam_x + vc, cam_y + vr
            if not (0 <= mx < DNG_W and 0 <= my < DNG_H):
                continue
            g = scr.tile(vr, vc)
            if g == "  ":
                continue
            was_exp = (proxy.grid[my][mx] & TILE_F_EXPLORED) != 0
            flags = TILE_F_EXPLORED | TILE_F_VISIBLE
            if not was_exp:
                proxy.floor_explored += 1

            if g in GLYPH_TO_ENEMY and e_slot < len(proxy.enemies):
                et, ef = GLYPH_TO_ENEMY[g]
                proxy.enemies[e_slot] = Enemy(x=mx, y=my, etype=et, hp=4, max_hp=5, flags=ef)
                e_slot += 1
                proxy.grid[my][mx] = DNG_FLOOR | flags
            elif g == "##":
                proxy.grid[my][mx] = DNG_WALL | flags
            elif g == " >":
                proxy.grid[my][mx] = DNG_STAIRS | flags
            elif g == "++":
                proxy.grid[my][mx] = DNG_DOOR_CLOSED | flags
            elif g == " /":
                proxy.grid[my][mx] = DNG_DOOR_OPEN | flags
            elif g == "%%":
                proxy.grid[my][mx] = DNG_CRACKED_WALL | flags
            elif g == " ^":
                proxy.grid[my][mx] = DNG_TRAP | flags | TILE_F_REVEALED
            elif g == " &":
                proxy.grid[my][mx] = DNG_SHRINE | flags
            elif g == " +":
                proxy.grid[my][mx] = DNG_POTION | flags
            elif g == " *":
                proxy.grid[my][mx] = DNG_MANA | flags
            elif g == " $":
                proxy.grid[my][mx] = DNG_CHEST | flags
            elif g in ("*V", "*A"):
                proxy.grid[my][mx] = DNG_RELIC | flags
            elif g == "$$":
                proxy.grid[my][mx] = DNG_SHOP | flags
                if s_slot < len(proxy.shop):
                    proxy.shop[s_slot] = ShopSlot(x=mx, y=my, kind=SHOP_ATK, cost=35, sold=False)
                    s_slot += 1
            else:
                proxy.grid[my][mx] = DNG_FLOOR | flags

    return True


def load_policy(genome_path: Path, quantized: bool = True):
    gdata = json.loads(genome_path.read_text(encoding="utf-8"))
    model_type = gdata.get("model_type") or gdata.get("model") or ("mlp" if "w1" in gdata else "heuristic")
    if model_type == "mlp":
        g = TinyMLPGenome.from_dict(gdata)
    else:
        g = HeuristicGenome.from_dict(gdata)
    return (g.quantize_int8() if quantized else g), model_type


def run_hil(port: str, baud: int, genome_path: Path, turns: int, delay_ms: int, out_json: Path | None, quantized: bool = True) -> Dict:
    policy, model_type = load_policy(genome_path, quantized=quantized)
    scr = AnsiScreen()
    proxy = DungeonSim(seed=0x5D17, cls=CLASS_KNIGHT)
    last_floor = -1
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
                last_floor = -1
                continue
            if hud["dead"]:
                deaths += 1
                ser.write(b"w")
                drain(300)
                continue

            if not sync_proxy_from_screen(proxy, scr, hud, last_floor):
                ser.write(b"\x0c")
                drain(250)
                continue

            last_floor = hud["floor"]
            max_floor = max(max_floor, hud["floor"])
            max_gold = max(max_gold, hud["gold"])

            act = policy.select_action(proxy, omniscient=False)
            ser.write(act.encode("ascii"))
            drain(delay_ms)
            executed += 1

    res = {
        "port": port,
        "baud": baud,
        "model": model_type,
        "quantized_int8": quantized,
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
    ap = argparse.ArgumentParser(description="Run evolved GA genome (Heuristic or Tiny-MLP) live on MSP430FR5994 over UART")
    ap.add_argument("--port", default="COM4")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--genome", required=True)
    ap.add_argument("--turns", type=int, default=60)
    ap.add_argument("--delay-ms", type=int, default=120)
    ap.add_argument("--float", dest="use_float", action="store_true", help="Use float32 instead of Q7 int8")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    run_hil(
        args.port,
        args.baud,
        Path(args.genome),
        args.turns,
        args.delay_ms,
        Path(args.out) if args.out else None,
        quantized=not args.use_float,
    )


if __name__ == "__main__":
    main()
