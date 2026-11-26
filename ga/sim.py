"""
Headless, deterministic Python simulator mirroring app/dungeon_game.c (Wave 2).

Uses the exact 16-bit Galois LFSR (0xB400), 48x22 map dimensions, Stone Crypt /
Fungal Caverns / Floor-5 Boss Sanctum generators, integer Bresenham LOS, all 6
enemy archetypes (Slime, Skeleton, Cultist Archer, Wraith, Mimic, Bone Warden),
interactive terrain, in-world shops, spells, consumable belt, relics, and 3
playable classes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Map dimensions
DNG_W = 48
DNG_H = 22
LOS_RADIUS = 6
DNG_MAX_ENEMIES = 20
DNG_MAX_SHOP_ITEMS = 3

# Tile types (low 4 bits)
DNG_WALL = 0
DNG_FLOOR = 1
DNG_STAIRS = 2
DNG_DOOR_CLOSED = 3
DNG_DOOR_OPEN = 4
DNG_CRACKED_WALL = 5
DNG_TRAP = 6
DNG_SHRINE = 7
DNG_POTION = 8
DNG_MANA = 9
DNG_CHEST = 10
DNG_SHOP = 11
DNG_RELIC = 12

# Tile flags (high 4 bits)
TILE_TYPE_MASK = 0x0F
TILE_F_EXPLORED = 0x10
TILE_F_VISIBLE = 0x20
TILE_F_REVEALED = 0x40
TILE_F_ALT = 0x80

# Enemy types
ENEMY_NONE = 0
ENEMY_SLIME = 1
ENEMY_SKELETON = 2
ENEMY_ARCHER = 3
ENEMY_WRAITH = 4
ENEMY_MIMIC = 5
ENEMY_BOSS = 6

# Enemy flags
EF_AIMING = 0x01
EF_AWAKE = 0x02
EF_SLOW_SKIP = 0x04
EF_FREEZE_SHIFT = 3
EF_FREEZE_MASK = 0x18
EF_BOSS_SUMMONED = 0x20

# Classes
CLASS_KNIGHT = 0
CLASS_ARCANIST = 1
CLASS_ROGUE = 2
CLASS_NAMES = ("Knight", "Arcanist", "Rogue")

# Relics
RELIC_VAMP = 0x01
RELIC_ARCANE = 0x02

# Shop kinds
SHOP_NONE = 0
SHOP_ATK = 1
SHOP_DEF = 2
SHOP_POT = 3
SHOP_BOMB = 4
SHOP_MAP = 5

# Directions: 0=N, 1=E, 2=S, 3=W
DIR_DX = (0, 1, 0, -1)
DIR_DY = (-1, 0, 1, 0)

# Discrete action space (11 tactical actions)
ACTIONS = ("w", "d", "s", "a", "f", "q", "e", "1", "2", "3", " ")
NUM_ACTIONS = len(ACTIONS)

# Enemy definitions: (name, base_hp, dmg, gold_melee, gold_bolt)
ENEMY_DEFS = (
    ("None", 0, 0, 0, 0),
    ("Slime", 3, 1, 6, 8),
    ("Skeleton", 5, 2, 12, 15),
    ("Archer", 4, 2, 14, 16),
    ("Wraith", 3, 1, 16, 18),
    ("Mimic", 6, 3, 30, 30),
    ("Warden", 24, 3, 100, 100),
)


@dataclass
class Enemy:
    x: int = 0
    y: int = 0
    etype: int = ENEMY_NONE
    hp: int = 0
    max_hp: int = 0
    flags: int = 0
    aim_dx: int = 0
    aim_dy: int = 0

    def freeze_turns(self) -> int:
        return (self.flags & EF_FREEZE_MASK) >> EF_FREEZE_SHIFT

    def set_freeze(self, turns: int) -> None:
        t = min(3, max(0, turns))
        self.flags = (self.flags & ~EF_FREEZE_MASK) | (t << EF_FREEZE_SHIFT)


@dataclass
class ShopSlot:
    x: int = 0
    y: int = 0
    kind: int = SHOP_NONE
    cost: int = 0
    sold: bool = False


class DungeonSim:
    """Bit-faithful headless simulator of MSP430FR5994 dungeon_game.c."""

    def __init__(self, seed: int = 0x5D17, cls: int = CLASS_KNIGHT) -> None:
        self.grid: List[List[int]] = [[DNG_WALL] * DNG_W for _ in range(DNG_H)]
        self.enemies: List[Enemy] = [Enemy() for _ in range(DNG_MAX_ENEMIES)]
        self.shop: List[ShopSlot] = [ShopSlot() for _ in range(DNG_MAX_SHOP_ITEMS)]
        self.reset(seed=seed, cls=cls)

    def reset(self, seed: int = 0x5D17, cls: int = CLASS_KNIGHT) -> None:
        self.cls = cls if 0 <= cls < 3 else CLASS_KNIGHT
        seed &= 0xFFFF
        if seed == 0:
            seed = 0x5D17
        self.run_seed = seed
        self.rng = seed

        self.floor = 1
        self.best_floor = 1
        self.gold = 25 if self.cls == CLASS_ROGUE else 0
        self.relics = 0
        self.dir = 1
        self.shop_preview = -1
        self.empowered = False
        self.game_over = False

        if self.cls == CLASS_KNIGHT:
            self.max_hp = 16; self.hp = 16; self.max_mp = 4; self.mp = 4
            self.atk = 3; self.def_ = 1; self.potions = 1; self.bombs = 0; self.scrolls = 0
        elif self.cls == CLASS_ARCANIST:
            self.max_hp = 11; self.hp = 11; self.max_mp = 8; self.mp = 8
            self.atk = 2; self.def_ = 0; self.potions = 1; self.bombs = 1; self.scrolls = 0
        else:
            self.max_hp = 12; self.hp = 12; self.max_mp = 5; self.mp = 5
            self.atk = 2; self.def_ = 0; self.potions = 1; self.bombs = 1; self.scrolls = 1

        # Telemetry for GA fitness evaluation
        self.turns = 0
        self.floor_turns = 0
        self.kills = 0
        self.boss_kills = 0
        self.shops_bought = 0
        self.wall_bumps = 0
        self.total_explored = 0
        self.floor_explored = 0
        self.visited_counts: dict[Tuple[int, int], int] = {}
        self.last_status = ""

        self.px = 2
        self.py = 2
        self._generate_floor()

    # ------------------------------------------------------------------ RNG
    def next_rand(self) -> int:
        lsb = self.rng & 1
        self.rng >>= 1
        if lsb:
            self.rng ^= 0xB400
        self.rng &= 0xFFFF
        return self.rng

    # ------------------------------------------------------------------ Tile helpers
    def tile_type(self, x: int, y: int) -> int:
        return self.grid[y][x] & TILE_TYPE_MASK

    def set_tile_type(self, x: int, y: int, t: int) -> None:
        self.grid[y][x] = (self.grid[y][x] & ~TILE_TYPE_MASK) | (t & TILE_TYPE_MASK)

    @staticmethod
    def tile_blocks_los(t: int) -> bool:
        return t in (DNG_WALL, DNG_CRACKED_WALL, DNG_DOOR_CLOSED)

    @staticmethod
    def tile_blocks_move(t: int) -> bool:
        return t in (DNG_WALL, DNG_CRACKED_WALL, DNG_DOOR_CLOSED, DNG_SHOP)

    @staticmethod
    def is_boss_floor(fl: int) -> bool:
        return fl > 0 and (fl % 5) == 0

    @staticmethod
    def is_cave_floor(fl: int) -> bool:
        if DungeonSim.is_boss_floor(fl):
            return False
        return (((fl - 1) & 0x02) != 0)

    def find_enemy_at(self, x: int, y: int) -> int:
        for i, e in enumerate(self.enemies):
            if e.etype != ENEMY_NONE and e.x == x and e.y == y:
                return i
        return -1

    def find_shop_at(self, x: int, y: int) -> int:
        for i, sh in enumerate(self.shop):
            if not sh.sold and sh.kind != SHOP_NONE and sh.x == x and sh.y == y:
                return i
        return -1

    def is_low_hp(self) -> bool:
        return (not self.game_over) and self.hp > 0 and (self.hp <= 3 or self.hp * 4 <= self.max_hp)

    # ------------------------------------------------------------------ Bresenham LOS
    def _cast_los_ray(self, x1: int, y1: int) -> None:
        x0, y0 = self.px, self.py
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        spot_r = 4 if self.cls == CLASS_ROGUE else 2
        grid = self.grid

        while True:
            if x0 < 0 or x0 >= DNG_W or y0 < 0 or y0 >= DNG_H:
                break
            cell = grid[y0][x0]
            if (cell & TILE_F_EXPLORED) == 0:
                self.floor_explored += 1
                self.total_explored += 1
            cell |= (TILE_F_VISIBLE | TILE_F_EXPLORED)
            t = cell & TILE_TYPE_MASK
            if t == DNG_TRAP and (abs(x0 - self.px) + abs(y0 - self.py)) <= spot_r:
                cell |= TILE_F_REVEALED
            grid[y0][x0] = cell

            if (x0 != self.px or y0 != self.py) and (t == DNG_WALL or t == DNG_CRACKED_WALL or t == DNG_DOOR_CLOSED):
                break
            if x0 == x1 and y0 == y1:
                break
            e2 = err * 2
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    def compute_los(self) -> None:
        grid = self.grid
        inv_vis = ~TILE_F_VISIBLE & 0xFF
        for r in range(DNG_H):
            row = grid[r]
            for c in range(DNG_W):
                row[c] &= inv_vis

        if (grid[self.py][self.px] & TILE_F_EXPLORED) == 0:
            self.floor_explored += 1
            self.total_explored += 1
        grid[self.py][self.px] |= (TILE_F_VISIBLE | TILE_F_EXPLORED)

        rad = (LOS_RADIUS + 1) if self.cls == CLASS_ROGUE else LOS_RADIUS
        px, py = self.px, self.py
        for d in range(-rad, rad + 1):
            self._cast_los_ray(px + d, py - rad)
            self._cast_los_ray(px + d, py + rad)
            self._cast_los_ray(px - rad, py + d)
            self._cast_los_ray(px + rad, py + d)

    # ------------------------------------------------------------------ Floor Generation
    def _carve_room(self, x0: int, y0: int, w: int, h: int) -> None:
        for r in range(y0, min(y0 + h, DNG_H - 1)):
            for c in range(x0, min(x0 + w, DNG_W - 1)):
                self.grid[r][c] = DNG_FLOOR

    def _carve_corridor(self, x0: int, y0: int, x1: int, y1: int) -> None:
        x, y = x0, y0
        if (self.next_rand() & 1) == 0:
            while x != x1:
                self.grid[y][x] = DNG_FLOOR
                x += 1 if x < x1 else -1
            while y != y1:
                self.grid[y][x] = DNG_FLOOR
                y += 1 if y < y1 else -1
        else:
            while y != y1:
                self.grid[y][x] = DNG_FLOOR
                y += 1 if y < y1 else -1
            while x != x1:
                self.grid[y][x] = DNG_FLOOR
                x += 1 if x < x1 else -1
        self.grid[y][x] = DNG_FLOOR

    def _find_open_floor(self, min_dist_player: int) -> Optional[Tuple[int, int]]:
        for _ in range(80):
            c = 1 + (self.next_rand() % (DNG_W - 2))
            r = 1 + (self.next_rand() % (DNG_H - 2))
            if self.tile_type(c, r) != DNG_FLOOR:
                continue
            if c == self.px and r == self.py:
                continue
            if self.find_enemy_at(c, r) >= 0:
                continue
            if (abs(c - self.px) + abs(r - self.py)) < min_dist_player:
                continue
            return (c, r)
        return None

    def _place_doors(self) -> None:
        placed = 0
        for r in range(2, DNG_H - 2):
            if placed >= 6:
                break
            for c in range(2, DNG_W - 2):
                if placed >= 6:
                    break
                if self.tile_type(c, r) != DNG_FLOOR or (c == self.px and r == self.py):
                    continue
                ns_wall = (
                    self.tile_type(c, r - 1) == DNG_WALL
                    and self.tile_type(c, r + 1) == DNG_WALL
                    and self.tile_type(c - 1, r) == DNG_FLOOR
                    and self.tile_type(c + 1, r) == DNG_FLOOR
                )
                ew_wall = (
                    self.tile_type(c - 1, r) == DNG_WALL
                    and self.tile_type(c + 1, r) == DNG_WALL
                    and self.tile_type(c, r - 1) == DNG_FLOOR
                    and self.tile_type(c, r + 1) == DNG_FLOOR
                )
                if (ns_wall or ew_wall) and ((self.next_rand() & 0x07) == 0):
                    self.grid[r][c] = DNG_DOOR_CLOSED
                    placed += 1

    def _place_secret_alcove(self) -> None:
        for _ in range(80):
            c = 2 + (self.next_rand() % (DNG_W - 4))
            r = 2 + (self.next_rand() % (DNG_H - 4))
            if self.tile_type(c, r) != DNG_WALL:
                continue
            for d in range(4):
                fc, fr = c + DIR_DX[d], r + DIR_DY[d]
                bc, br = c - DIR_DX[d], r - DIR_DY[d]
                if bc <= 1 or bc >= DNG_W - 2 or br <= 1 or br >= DNG_H - 2:
                    continue
                if self.tile_type(fc, fr) == DNG_FLOOR and self.tile_type(bc, br) == DNG_WALL:
                    self.grid[r][c] = DNG_CRACKED_WALL
                    self.grid[br][bc] = DNG_CHEST if (self.next_rand() & 1) == 0 else DNG_POTION
                    return

    def _place_shop(self) -> None:
        scale = (self.floor * 5) if self.floor <= 10 else 50
        k2 = SHOP_POT + (self.next_rand() % 3)
        c2 = 15 if k2 == SHOP_POT else (20 if k2 == SHOP_BOMB else 18)
        kinds = (SHOP_ATK, SHOP_DEF, k2)
        costs = (25 + scale, 30 + scale, c2)

        for i in range(DNG_MAX_SHOP_ITEMS):
            for _ in range(80):
                c = 2 + (self.next_rand() % (DNG_W - 4))
                r = 2 + (self.next_rand() % (DNG_H - 4))
                if self.tile_type(c, r) != DNG_FLOOR or (c == self.px and r == self.py):
                    continue
                if (
                    self.tile_type(c - 1, r) != DNG_FLOOR
                    or self.tile_type(c + 1, r) != DNG_FLOOR
                    or self.tile_type(c, r - 1) != DNG_FLOOR
                    or self.tile_type(c, r + 1) != DNG_FLOOR
                ):
                    continue
                self.grid[r][c] = DNG_SHOP
                self.shop[i] = ShopSlot(x=c, y=r, kind=kinds[i], cost=costs[i], sold=False)
                break

    def _spawn_enemy(self, slot: int, x: int, y: int, etype: int) -> None:
        base_hp = ENEMY_DEFS[etype][1]
        if etype == ENEMY_BOSS:
            bonus = ((self.floor - 5) // 5) * 6 if self.floor > 5 else 0
        else:
            bonus = (self.floor - 2) // 2 if self.floor >= 4 else 0
        hp = min(250, base_hp + bonus)
        self.enemies[slot] = Enemy(
            x=x,
            y=y,
            etype=etype,
            hp=hp,
            max_hp=hp,
            flags=0 if etype == ENEMY_MIMIC else EF_AWAKE,
        )

    def _generate_boss_sanctum(self) -> None:
        self._carve_room(4, 7, 8, 8)
        self._carve_room(18, 3, 24, 16)
        self._carve_corridor(11, 11, 18, 11)
        self.grid[11][14] = DNG_DOOR_CLOSED

        for r, c in ((6, 23), (6, 24), (15, 23), (15, 24), (6, 35), (6, 36), (15, 35), (15, 36)):
            self.grid[r][c] = DNG_WALL

        self.grid[8][6] = DNG_POTION
        self.grid[13][6] = DNG_MANA
        self.px, self.py = 7, 11
        self._spawn_enemy(0, 32, 11, ENEMY_BOSS)
        self.compute_los()

    def _generate_floor(self) -> None:
        for r in range(DNG_H):
            row = self.grid[r]
            for c in range(DNG_W):
                row[c] = DNG_WALL
        for i in range(DNG_MAX_ENEMIES):
            self.enemies[i] = Enemy()
        for i in range(DNG_MAX_SHOP_ITEMS):
            self.shop[i] = ShopSlot()
        self.shop_preview = -1
        self.floor_turns = 0
        self.floor_explored = 0
        self.visited_counts.clear()

        if self.is_boss_floor(self.floor):
            self._generate_boss_sanctum()
            self.visited_counts[(self.px, self.py)] = 1
            return

        stairs_x, stairs_y = 2, 2
        if self.is_cave_floor(self.floor):
            wx, wy = DNG_W // 2, DNG_H // 2
            self.px, self.py = wx, wy
            self._carve_room(wx - 1, wy - 1, 3, 3)
            best_dist = 0
            for _ in range(420):
                d = self.next_rand() & 0x03
                nx, ny = wx + DIR_DX[d], wy + DIR_DY[d]
                if 2 <= nx <= DNG_W - 3 and 2 <= ny <= DNG_H - 3:
                    wx, wy = nx, ny
                    self.grid[wy][wx] = DNG_FLOOR
                    dist = abs(wx - self.px) + abs(wy - self.py)
                    if dist > best_dist:
                        best_dist = dist
                        stairs_x, stairs_y = wx, wy
        else:
            rx: List[int] = []; ry: List[int] = []; rw: List[int] = []; rh: List[int] = []
            cx: List[int] = []; cy: List[int] = []
            for _ in range(45):
                if len(rx) >= 8:
                    break
                w = 5 + (self.next_rand() % 5)
                h = 4 + (self.next_rand() % 3)
                x0 = 1 + (self.next_rand() % (DNG_W - w - 2))
                y0 = 1 + (self.next_rand() % (DNG_H - h - 2))
                overlap = False
                for i in range(len(rx)):
                    if x0 <= rx[i] + rw[i] and x0 + w >= rx[i] and y0 <= ry[i] + rh[i] and y0 + h >= ry[i]:
                        overlap = True
                        break
                if not overlap:
                    rx.append(x0); ry.append(y0); rw.append(w); rh.append(h)
                    cx.append(x0 + w // 2); cy.append(y0 + h // 2)
                    self._carve_room(x0, y0, w, h)

            if len(rx) < 2:
                self._carve_room(2, 2, 8, 5)
                self._carve_room(34, 13, 8, 5)
                cx = [6, 38]; cy = [4, 15]

            nrooms = len(cx)
            for i in range(nrooms - 1):
                self._carve_corridor(cx[i], cy[i], cx[i + 1], cy[i + 1])
            if nrooms >= 4:
                self._carve_corridor(cx[0], cy[0], cx[nrooms - 1], cy[nrooms - 1])
                self._carve_corridor(cx[1], cy[1], cx[nrooms - 2], cy[nrooms - 2])

            self.px, self.py = cx[0], cy[0]
            best_dist = 0
            for i in range(1, nrooms):
                dist = abs(cx[i] - self.px) + abs(cy[i] - self.py)
                if dist >= best_dist:
                    best_dist = dist
                    stairs_x, stairs_y = cx[i], cy[i]
            self._place_doors()

        self.grid[stairs_y][stairs_x] = DNG_STAIRS
        self._place_secret_alcove()
        if self.floor >= 2:
            self._place_secret_alcove()

        pos = self._find_open_floor(6)
        if pos is not None:
            self.grid[pos[1]][pos[0]] = DNG_SHRINE | (TILE_F_ALT if (self.floor & 1) == 0 else 0)

        if (self.floor & 1) == 0:
            self._place_shop()

        for _ in range(4):
            pos = self._find_open_floor(4)
            if pos is not None:
                self.grid[pos[1]][pos[0]] = DNG_TRAP

        for i in range(6):
            pos = self._find_open_floor(4)
            if pos is not None:
                t = DNG_POTION if i < 2 else (DNG_MANA if i < 4 else DNG_CHEST)
                self.grid[pos[1]][pos[0]] = t

        count = min(14, 6 + self.floor)
        for i in range(count):
            pos = self._find_open_floor(6)
            if pos is None:
                break
            roll = self.next_rand() % 10
            if i == count - 1 and self.floor >= 2:
                etype = ENEMY_MIMIC
            elif self.floor >= 3 and roll >= 8:
                etype = ENEMY_WRAITH
            elif roll >= 6:
                etype = ENEMY_ARCHER
            elif self.floor >= 2 and roll >= 3:
                etype = ENEMY_SKELETON
            else:
                etype = ENEMY_SLIME
            self._spawn_enemy(i, pos[0], pos[1], etype)

        self.compute_los()
        self.visited_counts[(self.px, self.py)] = 1

    # ------------------------------------------------------------------ Combat & AI
    def _clear_straight_line(self, x0: int, y0: int, x1: int, y1: int) -> bool:
        dx = 1 if x1 > x0 else (-1 if x1 < x0 else 0)
        dy = 1 if y1 > y0 else (-1 if y1 < y0 else 0)
        if dx != 0 and dy != 0:
            return False
        x, y = x0 + dx, y0 + dy
        while x != x1 or y != y1:
            if x < 0 or x >= DNG_W or y < 0 or y >= DNG_H:
                return False
            if self.tile_blocks_los(self.tile_type(x, y)):
                return False
            x += dx
            y += dy
        return True

    def _damage_player(self, raw_dmg: int, source: str) -> None:
        dmg = (raw_dmg - self.def_) if raw_dmg > self.def_ else 1
        if self.hp > dmg:
            self.hp -= dmg
            self.last_status = f"{source} {dmg}"
        else:
            self.hp = 0
            self.game_over = True
            self.last_status = "YOU DIED"

    def _damage_enemy(self, idx: int, dmg: int, by_bolt: bool) -> None:
        e = self.enemies[idx]
        edef = ENEMY_DEFS[e.etype]
        e.flags |= EF_AWAKE
        if e.hp > dmg:
            e.hp -= dmg
            self.last_status = f"HIT {edef[0]}"
        else:
            killed = e.etype
            self.gold += edef[4] if by_bolt else edef[3]
            e.etype = ENEMY_NONE
            self.kills += 1
            if (self.relics & RELIC_VAMP) != 0 and self.hp < self.max_hp:
                self.hp += 1
            if killed == ENEMY_BOSS:
                self.boss_kills += 1
                self.grid[11][38] = DNG_STAIRS | TILE_F_EXPLORED
                self.grid[9][30] = DNG_RELIC | TILE_F_EXPLORED
                self.grid[13][30] = DNG_RELIC | TILE_F_ALT | TILE_F_EXPLORED
                self.compute_los()
                self.last_status = "VICTORY"
            else:
                self.last_status = f"SLAIN {edef[0]}"

    def _monster_turn(self) -> None:
        for e in self.enemies:
            if self.game_over:
                break
            if e.etype == ENEMY_NONE:
                continue
            if e.etype == ENEMY_MIMIC and (e.flags & EF_AWAKE) == 0:
                continue

            frz = e.freeze_turns()
            if frz > 0:
                e.set_freeze(frz - 1)
                e.flags &= ~EF_AIMING
                continue

            dx = abs(e.x - self.px)
            dy = abs(e.y - self.py)
            dist = dx + dy

            if e.etype == ENEMY_BOSS:
                if (e.flags & EF_AIMING) != 0:
                    e.flags &= ~EF_AIMING
                    if (self.px == e.x or self.py == e.y) and dist <= 6 and self._clear_straight_line(e.x, e.y, self.px, self.py):
                        self._damage_player(3, "BONE WAVE")
                    continue
                if e.hp <= (e.max_hp // 2) and (e.flags & EF_BOSS_SUMMONED) == 0:
                    e.flags |= EF_BOSS_SUMMONED
                    for slot in range(1, DNG_MAX_ENEMIES):
                        if self.enemies[slot].etype == ENEMY_NONE:
                            for d in range(4):
                                sx, sy = e.x + DIR_DX[d], e.y + DIR_DY[d]
                                if 0 < sx < DNG_W - 1 and 0 < sy < DNG_H - 1:
                                    if (not self.tile_blocks_move(self.tile_type(sx, sy))
                                        and self.find_enemy_at(sx, sy) < 0
                                        and not (sx == self.px and sy == self.py)):
                                        self._spawn_enemy(slot, sx, sy, ENEMY_SKELETON)
                                        break
                            break
                    continue
                if 2 <= dist <= 6 and (self.grid[e.y][e.x] & TILE_F_VISIBLE) != 0 and (self.next_rand() & 1) == 0:
                    e.flags |= EF_AIMING
                    continue

            if e.etype == ENEMY_ARCHER and (e.flags & EF_AIMING) != 0:
                e.flags &= ~EF_AIMING
                bx, by = e.x, e.y
                for _ in range(6):
                    bx += e.aim_dx
                    by += e.aim_dy
                    if bx <= 0 or bx >= DNG_W - 1 or by <= 0 or by >= DNG_H - 1:
                        break
                    if bx == self.px and by == self.py:
                        self._damage_player(ENEMY_DEFS[ENEMY_ARCHER][2], "ARCHER")
                        break
                    if self.tile_blocks_los(self.tile_type(bx, by)):
                        break
                continue

            if dist > 8:
                continue

            if dist == 1:
                self._damage_player(ENEMY_DEFS[e.etype][2], ENEMY_DEFS[e.etype][0])
                if e.etype == ENEMY_WRAITH and not self.game_over and self.mp > 0:
                    self.mp -= 1
                continue

            if (e.etype == ENEMY_ARCHER and 2 <= dist <= 5
                and (e.x == self.px or e.y == self.py)
                and self._clear_straight_line(e.x, e.y, self.px, self.py)):
                e.flags |= EF_AIMING
                e.aim_dx = 1 if self.px > e.x else (-1 if self.px < e.x else 0)
                e.aim_dy = 1 if self.py > e.y else (-1 if self.py < e.y else 0)
                continue

            if e.etype == ENEMY_SLIME:
                e.flags ^= EF_SLOW_SKIP
                if (e.flags & EF_SLOW_SKIP) != 0:
                    continue

            sx = 1 if self.px > e.x else (-1 if self.px < e.x else 0)
            sy = 1 if self.py > e.y else (-1 if self.py < e.y else 0)
            cands = ((e.x + sx, e.y), (e.x, e.y + sy)) if dx >= dy else ((e.x, e.y + sy), (e.x + sx, e.y))
            for nx, ny in cands:
                if nx <= 0 or nx >= DNG_W - 1 or ny <= 0 or ny >= DNG_H - 1:
                    continue
                if nx == e.x and ny == e.y:
                    continue
                if nx == self.px and ny == self.py:
                    continue
                if self.find_enemy_at(nx, ny) >= 0:
                    continue
                tt = self.tile_type(nx, ny)
                if e.etype != ENEMY_WRAITH and self.tile_blocks_move(tt):
                    continue
                e.x, e.y = nx, ny
                if tt == DNG_TRAP and e.etype != ENEMY_WRAITH:
                    self.grid[ny][nx] |= TILE_F_REVEALED
                    if e.hp > 2:
                        e.hp -= 2
                    else:
                        self.gold += ENEMY_DEFS[e.etype][3]
                        e.etype = ENEMY_NONE
                        self.kills += 1
                break

    # ------------------------------------------------------------------ Actions
    def _step_dir(self, d: int) -> None:
        self.dir = d & 0x03
        nx, ny = self.px + DIR_DX[self.dir], self.py + DIR_DY[self.dir]
        if nx < 0 or nx >= DNG_W or ny < 0 or ny >= DNG_H:
            self.wall_bumps += 1
            return

        ei = self.find_enemy_at(nx, ny)
        if ei >= 0:
            dmg = (self.atk + ((self.atk + 1) // 2)) if self.empowered else self.atk
            if self.cls == CLASS_ROGUE and self.enemies[ei].hp == self.enemies[ei].max_hp:
                dmg += 2
            self.empowered = False
            if self.enemies[ei].etype == ENEMY_MIMIC and (self.enemies[ei].flags & EF_AWAKE) == 0:
                self.enemies[ei].flags |= EF_AWAKE
            else:
                self._damage_enemy(ei, dmg, False)
            self._monster_turn()
            return

        target = self.tile_type(nx, ny)
        if target == DNG_DOOR_CLOSED:
            self.set_tile_type(nx, ny, DNG_DOOR_OPEN)
            self.compute_los()
            self._monster_turn()
            return

        if target == DNG_SHOP:
            si = self.find_shop_at(nx, ny)
            if si >= 0:
                sh = self.shop[si]
                if self.shop_preview != si:
                    self.shop_preview = si
                elif self.gold >= sh.cost:
                    full = (
                        (sh.kind == SHOP_POT and self.potions >= 9)
                        or (sh.kind == SHOP_BOMB and self.bombs >= 9)
                        or (sh.kind == SHOP_MAP and self.scrolls >= 9)
                    )
                    if not full:
                        self.gold -= sh.cost
                        sh.sold = True
                        self.shop_preview = -1
                        self.set_tile_type(sh.x, sh.y, DNG_FLOOR)
                        self.shops_bought += 1
                        if sh.kind == SHOP_ATK: self.atk += 1
                        elif sh.kind == SHOP_DEF: self.def_ += 1
                        elif sh.kind == SHOP_POT: self.potions += 1
                        elif sh.kind == SHOP_BOMB: self.bombs += 1
                        elif sh.kind == SHOP_MAP: self.scrolls += 1
            return

        self.shop_preview = -1
        if target in (DNG_WALL, DNG_CRACKED_WALL):
            self.wall_bumps += 1
            return

        self.px, self.py = nx, ny
        self.visited_counts[(nx, ny)] = self.visited_counts.get((nx, ny), 0) + 1
        self.compute_los()

        if target == DNG_SHRINE:
            if (self.grid[ny][nx] & TILE_F_REVEALED) == 0:
                if (self.grid[ny][nx] & TILE_F_ALT) == 0:
                    if self.hp > 3:
                        self.hp -= 3
                        self.atk += 1
                        self.grid[ny][nx] |= TILE_F_REVEALED
                else:
                    self.grid[ny][nx] |= TILE_F_REVEALED
                    if self.hp < self.max_hp:
                        self.hp = self.max_hp
                    else:
                        self.def_ += 1
        elif target == DNG_TRAP:
            self.grid[ny][nx] |= TILE_F_REVEALED
            self._damage_player(2, "TRAP")
            if self.game_over:
                return
        elif target == DNG_POTION:
            self.set_tile_type(nx, ny, DNG_FLOOR)
            if self.potions < 9:
                self.potions += 1
            else:
                self.hp = min(self.max_hp, self.hp + 6)
        elif target == DNG_MANA:
            self.set_tile_type(nx, ny, DNG_FLOOR)
            self.mp = min(self.max_mp, self.mp + 3)
        elif target == DNG_CHEST:
            self.set_tile_type(nx, ny, DNG_FLOOR)
            self.gold += 25
            self.max_hp += 1
            self.hp += 1
            bonus = self.next_rand() & 0x03
            if bonus == 0:
                self.atk += 1
            elif bonus == 1 and self.bombs < 9:
                self.bombs += 1
            elif bonus == 2 and self.scrolls < 9:
                self.scrolls += 1
        elif target == DNG_RELIC:
            is_arc = (self.grid[ny][nx] & TILE_F_ALT) != 0
            for rr in range(DNG_H):
                for cc in range(DNG_W):
                    if self.tile_type(cc, rr) == DNG_RELIC:
                        self.set_tile_type(cc, rr, DNG_FLOOR)
            if is_arc:
                self.relics |= RELIC_ARCANE
                self.max_mp += 2
                self.mp = self.max_mp
            else:
                self.relics |= RELIC_VAMP
        elif target == DNG_STAIRS:
            self.floor += 1
            if self.floor > self.best_floor:
                self.best_floor = self.floor
            if (self.relics & RELIC_ARCANE) != 0 and self.mp < self.max_mp:
                self.mp += 1
            self._generate_floor()
            return

        self._monster_turn()

    def _cast_bolt(self) -> None:
        if self.mp == 0:
            self.wall_bumps += 1
            return
        self.mp -= 1
        dmg = self.atk + (3 if self.empowered else 2)
        self.empowered = False
        max_hits = 2 if self.cls == CLASS_ARCANIST else 1
        hits = 0
        x, y = self.px, self.py
        for _ in range(8):
            x += DIR_DX[self.dir]
            y += DIR_DY[self.dir]
            if x <= 0 or x >= DNG_W - 1 or y <= 0 or y >= DNG_H - 1:
                break
            ei = self.find_enemy_at(x, y)
            if ei >= 0:
                self._damage_enemy(ei, dmg, True)
                hits += 1
                if hits >= max_hits:
                    break
                continue
            t = self.tile_type(x, y)
            if t == DNG_CRACKED_WALL:
                self.set_tile_type(x, y, DNG_FLOOR)
                self.compute_los()
                break
            if self.tile_blocks_los(t):
                break
        self._monster_turn()

    def _cast_nova(self) -> None:
        if self.mp < 2:
            self.wall_bumps += 1
            return
        self.mp -= 2
        dmg = self.atk + (1 if self.empowered else 0)
        self.empowered = False
        for i, e in enumerate(self.enemies):
            if e.etype == ENEMY_NONE:
                continue
            if abs(e.x - self.px) <= 1 and abs(e.y - self.py) <= 1:
                e.set_freeze(2)
                e.flags &= ~EF_AIMING
                self._damage_enemy(i, dmg, True)
        self._monster_turn()

    def _cast_blink(self) -> None:
        if self.mp < 2:
            self.wall_bumps += 1
            return
        best = None
        cx, cy = self.px, self.py
        for _ in range(3):
            cx += DIR_DX[self.dir]
            cy += DIR_DY[self.dir]
            if cx <= 0 or cx >= DNG_W - 1 or cy <= 0 or cy >= DNG_H - 1:
                break
            t = self.tile_type(cx, cy)
            if self.tile_blocks_move(t):
                break
            if self.find_enemy_at(cx, cy) < 0:
                best = (cx, cy)
        if best is None:
            self.wall_bumps += 1
            return
        self.mp -= 2
        self.px, self.py = best
        self.shop_preview = -1
        self.visited_counts[best] = self.visited_counts.get(best, 0) + 1
        self.compute_los()
        self._monster_turn()

    def _use_potion(self) -> None:
        if self.potions == 0 or self.hp >= self.max_hp:
            self.wall_bumps += 1
            return
        self.potions -= 1
        self.hp = min(self.max_hp, self.hp + 6)
        self._monster_turn()

    def _use_bomb(self) -> None:
        if self.bombs == 0:
            self.wall_bumps += 1
            return
        self.bombs -= 1
        dmg = self.atk + 4
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                nx, ny = self.px + dx, self.py + dy
                if 1 < nx < DNG_W - 2 and 1 < ny < DNG_H - 2:
                    t = self.tile_type(nx, ny)
                    if t in (DNG_CRACKED_WALL, DNG_WALL):
                        self.set_tile_type(nx, ny, DNG_FLOOR)
        for i, e in enumerate(self.enemies):
            if e.etype == ENEMY_NONE:
                continue
            if abs(e.x - self.px) <= 1 and abs(e.y - self.py) <= 1:
                self._damage_enemy(i, dmg, True)
        self.compute_los()
        self._monster_turn()

    def _use_scroll_mapping(self) -> None:
        if self.scrolls == 0:
            self.wall_bumps += 1
            return
        self.scrolls -= 1
        for r in range(DNG_H):
            for c in range(DNG_W):
                if (self.grid[r][c] & TILE_F_EXPLORED) == 0:
                    self.floor_explored += 1
                    self.total_explored += 1
                self.grid[r][c] |= TILE_F_EXPLORED
                if self.tile_type(c, r) == DNG_TRAP:
                    self.grid[r][c] |= TILE_F_REVEALED

    def step(self, action: str) -> bool:
        """Execute one tactical command. Returns True if game is still active."""
        if self.game_over:
            return False
        self.turns += 1
        self.floor_turns += 1

        if action == "w": self._step_dir(0)
        elif action == "d": self._step_dir(1)
        elif action == "s": self._step_dir(2)
        elif action == "a": self._step_dir(3)
        elif action == "f": self._cast_bolt()
        elif action == "q": self._cast_nova()
        elif action == "e": self._cast_blink()
        elif action == "1": self._use_potion()
        elif action == "2": self._use_bomb()
        elif action == "3": self._use_scroll_mapping()
        elif action == " ":
            if self.mp < self.max_mp:
                self.mp += 1
            self.empowered = True
            self._monster_turn()
        return not self.game_over

    def fitness(self) -> float:
        """Shaped multi-objective fitness for GA evolution."""
        floors_cleared = self.best_floor - 1
        return (
            500.0 * floors_cleared
            + 2.0 * self.total_explored
            + 15.0 * self.kills
            + 1.0 * self.gold
            + 60.0 * self.shops_bought
            + 1500.0 * self.boss_kills
            - 0.5 * self.wall_bumps
        )
