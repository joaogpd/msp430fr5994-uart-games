"""
Integer-friendly feature extractors for Approach A (Heuristic) and Approach B (Tiny-MLP).

Designed so every feature has a direct, low-RAM, pure-integer C counterpart for
the 16-bit TI MSP430FR5994 (no BFS/A*, no dynamic allocations, only cardinal
neighbor checks, 4-direction raycasts up to 6 tiles, and Manhattan vectors).

Respects two observability regimes:
- omniscient=False (Fair Fog-of-War): only TILE_F_EXPLORED / TILE_F_VISIBLE
  information is used; dormant Mimics appear as chests; unrevealed traps appear
  as floor.
- omniscient=True (Omniscient): full visibility of sim.grid, all enemies, traps,
  and stairs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from ga.sim import (
    ACTIONS,
    CLASS_ROGUE,
    DIR_DX,
    DIR_DY,
    DNG_CHEST,
    DNG_CRACKED_WALL,
    DNG_DOOR_CLOSED,
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
    NUM_ACTIONS,
    SHOP_BOMB,
    SHOP_MAP,
    SHOP_POT,
    TILE_F_ALT,
    TILE_F_EXPLORED,
    TILE_F_REVEALED,
    TILE_F_VISIBLE,
    TILE_TYPE_MASK,
    DungeonSim,
    Enemy,
)

NUM_HEURISTIC_FEATURES = 22
NUM_STATE_FEATURES = 24
RAYCAST_MAX_DIST = 6

HEURISTIC_FEATURE_NAMES: Tuple[str, ...] = (
    "move_is_melee",
    "move_melee_kill",
    "move_into_stairs",
    "move_into_loot",
    "move_into_shop_buy",
    "move_into_door",
    "move_into_trap",
    "move_toward_stairs",
    "move_toward_loot",
    "move_toward_frontier",
    "move_toward_enemy",
    "move_unvisited_bonus",
    "move_danger_delta",
    "move_align_bolt",
    "bolt_hits_enemy",
    "nova_adj_enemies",
    "blink_tactical",
    "potion_urgency",
    "bomb_value",
    "scroll_value",
    "focus_value",
    "low_hp_escape",
)

STATE_FEATURE_NAMES: Tuple[str, ...] = (
    "hp_ratio",
    "mp_ratio",
    "empowered",
    "has_potion",
    "has_bomb_or_scroll",
    "bolt_target_ahead",
    "dir_n_tile",
    "dir_e_tile",
    "dir_s_tile",
    "dir_w_tile",
    "dir_n_enemy_ray",
    "dir_e_enemy_ray",
    "dir_s_enemy_ray",
    "dir_w_enemy_ray",
    "stairs_dx",
    "stairs_dy",
    "frontier_dx",
    "frontier_dy",
    "loot_dx",
    "loot_dy",
    "enemy_dx",
    "enemy_dy",
    "adj_enemies_3x3",
    "in_danger_or_boss",
)


@dataclass
class ScanContext:
    """Single-pass per-turn perception cache shared across action evaluations."""

    omniscient: bool
    perceived_enemies: List[Enemy]
    enemy_pos_map: dict[Tuple[int, int], Enemy]
    dormant_mimic_pos: set[Tuple[int, int]]
    stairs_pos: Optional[Tuple[int, int]]
    nearest_loot: Optional[Tuple[int, int]]
    nearest_frontier: Optional[Tuple[int, int]]
    nearest_enemy: Optional[Tuple[int, int]]
    adj_3x3_enemies: int
    adj_cracked_walls: int
    bolt_hits_ahead: bool
    player_in_aim_line: bool
    boss_alive: bool
    valid_mask: Tuple[bool, ...]


def _can_afford_shop(sim: DungeonSim, x: int, y: int) -> bool:
    si = sim.find_shop_at(x, y)
    if si < 0:
        return False
    sh = sim.shop[si]
    if sim.gold < sh.cost:
        return False
    if sh.kind == SHOP_POT and sim.potions >= 9:
        return False
    if sh.kind == SHOP_BOMB and sim.bombs >= 9:
        return False
    if sh.kind == SHOP_MAP and sim.scrolls >= 9:
        return False
    return True


def _is_usable_shrine(sim: DungeonSim, cell: int) -> bool:
    if (cell & TILE_F_REVEALED) != 0:
        return False
    if (cell & TILE_F_ALT) == 0:
        return sim.hp > 3
    return True


def _perceived_tile(sim: DungeonSim, x: int, y: int, omniscient: bool) -> int:
    cell = sim.grid[y][x]
    t = cell & TILE_TYPE_MASK
    if not omniscient and t == DNG_TRAP and (cell & TILE_F_REVEALED) == 0:
        return DNG_FLOOR
    return t


def _blink_destination(sim: DungeonSim) -> Optional[Tuple[int, int]]:
    best: Optional[Tuple[int, int]] = None
    cx, cy = sim.px, sim.py
    dx, dy = DIR_DX[sim.dir], DIR_DY[sim.dir]
    for _ in range(3):
        cx += dx
        cy += dy
        if cx <= 0 or cx >= DNG_W - 1 or cy <= 0 or cy >= DNG_H - 1:
            break
        t = sim.tile_type(cx, cy)
        if sim.tile_blocks_move(t):
            break
        if sim.find_enemy_at(cx, cy) < 0:
            best = (cx, cy)
    return best


def _ray_hits_target(
    sim: DungeonSim,
    d: int,
    max_dist: int,
    enemy_pos_map: dict[Tuple[int, int], Enemy],
    include_cracked: bool = False,
) -> int:
    """Return distance (1..max_dist) to first perceived enemy (or cracked wall) in dir d, else 0."""
    x, y = sim.px, sim.py
    dx, dy = DIR_DX[d], DIR_DY[d]
    grid = sim.grid
    for step in range(1, max_dist + 1):
        x += dx
        y += dy
        if x <= 0 or x >= DNG_W - 1 or y <= 0 or y >= DNG_H - 1:
            break
        if (x, y) in enemy_pos_map:
            return step
        t = grid[y][x] & TILE_TYPE_MASK
        if include_cracked and t == DNG_CRACKED_WALL:
            return step
        if t in (DNG_WALL, DNG_CRACKED_WALL, DNG_DOOR_CLOSED):
            break
    return 0


def _in_aim_line(sim: DungeonSim, x: int, y: int, enemies: List[Enemy]) -> bool:
    for e in enemies:
        if (e.flags & EF_AIMING) == 0:
            continue
        dist = abs(e.x - x) + abs(e.y - y)
        if e.etype == ENEMY_ARCHER and dist <= 6:
            if e.aim_dx != 0 and y == e.y and ((x - e.x) * e.aim_dx > 0):
                if sim._clear_straight_line(e.x, e.y, x, y):
                    return True
            elif e.aim_dy != 0 and x == e.x and ((y - e.y) * e.aim_dy > 0):
                if sim._clear_straight_line(e.x, e.y, x, y):
                    return True
        elif e.etype == ENEMY_BOSS and dist <= 6 and (x == e.x or y == e.y):
            if sim._clear_straight_line(e.x, e.y, x, y):
                return True
    return False


def _tile_danger(sim: DungeonSim, x: int, y: int, enemies: List[Enemy], omniscient: bool) -> float:
    danger = 0.0
    for e in enemies:
        if abs(e.x - x) + abs(e.y - y) == 1:
            danger += 1.0
    if _in_aim_line(sim, x, y, enemies):
        danger += 1.5
    if _perceived_tile(sim, x, y, omniscient) == DNG_TRAP:
        danger += 1.0
    return danger


def valid_action_mask(sim: DungeonSim, omniscient: bool = False) -> Tuple[bool, ...]:
    """Return length-11 boolean tuple indicating non-wasted tactical actions."""
    px, py = sim.px, sim.py
    grid = sim.grid
    mask = [False] * NUM_ACTIONS

    for d in range(4):
        nx, ny = px + DIR_DX[d], py + DIR_DY[d]
        if 0 <= nx < DNG_W and 0 <= ny < DNG_H:
            t = grid[ny][nx] & TILE_TYPE_MASK
            if t not in (DNG_WALL, DNG_CRACKED_WALL):
                if t == DNG_SHOP:
                    mask[d] = _can_afford_shop(sim, nx, ny)
                else:
                    mask[d] = True

    mask[4] = sim.mp >= 1  # f: bolt
    mask[5] = sim.mp >= 2  # q: nova
    mask[6] = sim.mp >= 2 and (_blink_destination(sim) is not None)  # e: blink
    mask[7] = sim.potions > 0 and sim.hp < sim.max_hp  # 1: potion
    mask[8] = sim.bombs > 0  # 2: bomb
    mask[9] = sim.scrolls > 0  # 3: scroll of mapping
    mask[10] = True  # ' ': focus/wait is always valid
    return tuple(mask)


def precompute_context(sim: DungeonSim, omniscient: bool = False) -> ScanContext:
    """Perform a single integer-friendly pass over enemies and the 48x22 grid."""
    px, py = sim.px, sim.py
    grid = sim.grid

    perceived_enemies: List[Enemy] = []
    enemy_pos_map: dict[Tuple[int, int], Enemy] = {}
    dormant_mimic_pos: set[Tuple[int, int]] = set()
    nearest_enemy: Optional[Tuple[int, int]] = None
    best_enemy_dist = 999
    adj_3x3_enemies = 0
    boss_alive = False

    for e in sim.enemies:
        if e.etype == ENEMY_NONE:
            continue
        if e.etype == ENEMY_BOSS:
            boss_alive = True
        cell = grid[e.y][e.x]
        if not omniscient:
            if (cell & TILE_F_VISIBLE) == 0:
                continue
            if e.etype == ENEMY_MIMIC and (e.flags & EF_AWAKE) == 0:
                dormant_mimic_pos.add((e.x, e.y))
                continue
        perceived_enemies.append(e)
        enemy_pos_map[(e.x, e.y)] = e
        dist = abs(e.x - px) + abs(e.y - py)
        if dist < best_enemy_dist:
            best_enemy_dist = dist
            nearest_enemy = (e.x, e.y)
        if abs(e.x - px) <= 1 and abs(e.y - py) <= 1:
            adj_3x3_enemies += 1

    stairs_pos: Optional[Tuple[int, int]] = None
    best_stairs_dist = 999
    nearest_loot: Optional[Tuple[int, int]] = None
    best_loot_dist = 999
    nearest_frontier: Optional[Tuple[int, int]] = None
    best_frontier_dist = 999

    for pos in dormant_mimic_pos:
        d = abs(pos[0] - px) + abs(pos[1] - py)
        if d < best_loot_dist:
            best_loot_dist = d
            nearest_loot = pos

    for r in range(1, DNG_H - 1):
        row = grid[r]
        row_up = grid[r - 1]
        row_dn = grid[r + 1]
        for c in range(1, DNG_W - 1):
            cell = row[c]
            explored = (cell & TILE_F_EXPLORED) != 0
            if not omniscient and not explored:
                continue
            t = cell & TILE_TYPE_MASK
            if t == DNG_WALL:
                continue
            dist = abs(c - px) + abs(r - py)

            if t == DNG_STAIRS:
                if dist < best_stairs_dist:
                    best_stairs_dist = dist
                    stairs_pos = (c, r)
            elif t in (DNG_POTION, DNG_MANA, DNG_CHEST, DNG_RELIC):
                if dist < best_loot_dist:
                    best_loot_dist = dist
                    nearest_loot = (c, r)
            elif t == DNG_SHRINE and _is_usable_shrine(sim, cell):
                if dist < best_loot_dist:
                    best_loot_dist = dist
                    nearest_loot = (c, r)
            elif t == DNG_SHOP and _can_afford_shop(sim, c, r):
                if dist < best_loot_dist:
                    best_loot_dist = dist
                    nearest_loot = (c, r)

            if not omniscient and t not in (DNG_CRACKED_WALL, DNG_SHOP):
                if (
                    (row_up[c] & TILE_F_EXPLORED) == 0
                    or (row_dn[c] & TILE_F_EXPLORED) == 0
                    or (row[c - 1] & TILE_F_EXPLORED) == 0
                    or (row[c + 1] & TILE_F_EXPLORED) == 0
                ):
                    if dist < best_frontier_dist and (c != px or r != py):
                        best_frontier_dist = dist
                        nearest_frontier = (c, r)

    adj_cracked_walls = 0
    for dy in (-1, 0, 1):
        ny = py + dy
        if ny <= 0 or ny >= DNG_H - 1:
            continue
        for dx in (-1, 0, 1):
            nx = px + dx
            if nx <= 0 or nx >= DNG_W - 1:
                continue
            if (grid[ny][nx] & TILE_TYPE_MASK) == DNG_CRACKED_WALL:
                adj_cracked_walls += 1

    bolt_hits_ahead = (
        sim.mp >= 1
        and _ray_hits_target(sim, sim.dir, 8, enemy_pos_map, include_cracked=True) > 0
    )
    player_in_aim_line = _in_aim_line(sim, px, py, perceived_enemies)
    mask = valid_action_mask(sim, omniscient=omniscient)

    return ScanContext(
        omniscient=omniscient,
        perceived_enemies=perceived_enemies,
        enemy_pos_map=enemy_pos_map,
        dormant_mimic_pos=dormant_mimic_pos,
        stairs_pos=stairs_pos,
        nearest_loot=nearest_loot,
        nearest_frontier=nearest_frontier,
        nearest_enemy=nearest_enemy,
        adj_3x3_enemies=adj_3x3_enemies,
        adj_cracked_walls=adj_cracked_walls,
        bolt_hits_ahead=bolt_hits_ahead,
        player_in_aim_line=player_in_aim_line,
        boss_alive=boss_alive,
        valid_mask=mask,
    )


def _manhattan_step_delta(px: int, py: int, nx: int, ny: int, target: Optional[Tuple[int, int]]) -> float:
    if target is None:
        return 0.0
    tx, ty = target
    d_now = abs(tx - px) + abs(ty - py)
    d_new = abs(tx - nx) + abs(ty - ny)
    if d_new < d_now:
        return 1.0
    if d_new > d_now:
        return -1.0
    return 0.0


def extract_action_features(
    sim: DungeonSim,
    action_idx: int,
    omniscient: bool = False,
    ctx: Optional[ScanContext] = None,
) -> np.ndarray:
    """
    Approach A feature vector (length 22, values in [-1, 1]) for scoring a single
    candidate action `ACTIONS[action_idx]`.
    """
    if ctx is None:
        ctx = precompute_context(sim, omniscient=omniscient)

    feats = np.zeros(NUM_HEURISTIC_FEATURES, dtype=np.float32)
    px, py = sim.px, sim.py
    low_hp = sim.is_low_hp()

    if 0 <= action_idx < 4:
        nx, ny = px + DIR_DX[action_idx], py + DIR_DY[action_idx]
        if 0 <= nx < DNG_W and 0 <= ny < DNG_H:
            enemy = ctx.enemy_pos_map.get((nx, ny))
            if enemy is not None:
                feats[0] = 1.0  # move_is_melee
                dmg = (sim.atk + ((sim.atk + 1) // 2)) if sim.empowered else sim.atk
                if sim.cls == CLASS_ROGUE and enemy.hp == enemy.max_hp:
                    dmg += 2
                if enemy.hp <= dmg:
                    feats[1] = 1.0  # move_melee_kill
            else:
                cell = sim.grid[ny][nx]
                t = _perceived_tile(sim, nx, ny, omniscient)
                if t == DNG_STAIRS:
                    feats[2] = 1.0  # move_into_stairs
                elif (
                    t in (DNG_POTION, DNG_MANA, DNG_CHEST, DNG_RELIC)
                    or (nx, ny) in ctx.dormant_mimic_pos
                    or (t == DNG_SHRINE and _is_usable_shrine(sim, cell))
                ):
                    feats[3] = 1.0  # move_into_loot
                elif t == DNG_SHOP and _can_afford_shop(sim, nx, ny):
                    feats[4] = 1.0  # move_into_shop_buy
                elif t == DNG_DOOR_CLOSED:
                    feats[5] = 1.0  # move_into_door
                elif t == DNG_TRAP:
                    feats[6] = 1.0  # move_into_trap

                if t not in (DNG_WALL, DNG_CRACKED_WALL, DNG_SHOP):
                    feats[7] = _manhattan_step_delta(px, py, nx, ny, ctx.stairs_pos)
                    feats[8] = _manhattan_step_delta(px, py, nx, ny, ctx.nearest_loot)
                    feats[9] = _manhattan_step_delta(px, py, nx, ny, ctx.nearest_frontier)
                    feats[10] = _manhattan_step_delta(px, py, nx, ny, ctx.nearest_enemy)

                    vc = sim.visited_counts.get((nx, ny), 0)
                    if vc == 0:
                        feats[11] = 1.0
                    elif vc == 1:
                        feats[11] = 0.0
                    else:
                        feats[11] = max(-1.0, -0.25 * (vc - 1))

                    d_now = _tile_danger(sim, px, py, ctx.perceived_enemies, omniscient)
                    d_new = _tile_danger(sim, nx, ny, ctx.perceived_enemies, omniscient)
                    feats[12] = max(-1.0, min(1.0, (d_now - d_new) * 0.5))

                    if low_hp and ctx.nearest_enemy is not None:
                        if _manhattan_step_delta(px, py, nx, ny, ctx.nearest_enemy) < 0.0:
                            feats[21] = 1.0

            if sim.mp >= 1 and _ray_hits_target(sim, action_idx, RAYCAST_MAX_DIST, ctx.enemy_pos_map) > 0:
                feats[13] = 1.0  # move_align_bolt

    elif action_idx == 4:  # 'f' bolt
        if ctx.bolt_hits_ahead:
            feats[14] = 1.0

    elif action_idx == 5:  # 'q' frost nova
        if sim.mp >= 2 and ctx.adj_3x3_enemies > 0:
            feats[15] = min(1.0, ctx.adj_3x3_enemies / 3.0)

    elif action_idx == 6:  # 'e' blink
        if sim.mp >= 2:
            dest = _blink_destination(sim)
            if dest is not None:
                bx, by = dest
                d_now = _tile_danger(sim, px, py, ctx.perceived_enemies, omniscient)
                d_new = _tile_danger(sim, bx, by, ctx.perceived_enemies, omniscient)
                toward_stairs = _manhattan_step_delta(px, py, bx, by, ctx.stairs_pos) > 0.0
                if (low_hp and d_new < d_now) or ctx.player_in_aim_line or toward_stairs:
                    feats[16] = 1.0
                if low_hp and ctx.nearest_enemy is not None:
                    ex, ey = ctx.nearest_enemy
                    if (abs(ex - bx) + abs(ey - by)) > (abs(ex - px) + abs(ey - py)):
                        feats[21] = 1.0

    elif action_idx == 7:  # '1' potion
        if sim.potions > 0 and sim.hp < sim.max_hp:
            missing = 1.0 - (sim.hp / max(1, sim.max_hp))
            feats[17] = min(1.0, missing * (1.5 if low_hp else 1.0))
            if low_hp:
                feats[21] = 1.0

    elif action_idx == 8:  # '2' bomb
        if sim.bombs > 0:
            val = ctx.adj_3x3_enemies + ctx.adj_cracked_walls
            if val > 0:
                feats[18] = min(1.0, val / 2.0)

    elif action_idx == 9:  # '3' scroll of mapping
        if sim.scrolls > 0 and (ctx.stairs_pos is None or sim.floor_explored < 120):
            feats[19] = 1.0

    elif action_idx == 10:  # ' ' focus/wait
        if (not sim.empowered) and sim.mp < sim.max_mp and ctx.adj_3x3_enemies == 0 and (not ctx.player_in_aim_line):
            feats[20] = 1.0

    return feats


def extract_state_vector(
    sim: DungeonSim,
    omniscient: bool = False,
    ctx: Optional[ScanContext] = None,
) -> np.ndarray:
    """
    Approach B state observation vector (length 24, all floats in [-1, 1]).
    """
    if ctx is None:
        ctx = precompute_context(sim, omniscient=omniscient)

    vec = np.zeros(NUM_STATE_FEATURES, dtype=np.float32)
    px, py = sim.px, sim.py

    # 0..5: Player vitals & resources
    vec[0] = 2.0 * (sim.hp / max(1, sim.max_hp)) - 1.0
    vec[1] = 2.0 * (sim.mp / max(1, sim.max_mp)) - 1.0
    vec[2] = 1.0 if sim.empowered else -1.0
    vec[3] = min(1.0, sim.potions / 3.0)
    vec[4] = (0.5 if sim.bombs > 0 else 0.0) + (0.5 if sim.scrolls > 0 else 0.0)
    vec[5] = 1.0 if ctx.bolt_hits_ahead else -1.0

    # 6..9: 4-dir immediate neighbor encoding
    for d in range(4):
        nx, ny = px + DIR_DX[d], py + DIR_DY[d]
        if nx < 0 or nx >= DNG_W or ny < 0 or ny >= DNG_H:
            vec[6 + d] = -1.0
            continue
        if (nx, ny) in ctx.enemy_pos_map:
            vec[6 + d] = 1.0
            continue
        t = _perceived_tile(sim, nx, ny, omniscient)
        if t in (DNG_WALL, DNG_CRACKED_WALL):
            vec[6 + d] = -1.0
        elif t == DNG_SHOP:
            vec[6 + d] = 1.0 if _can_afford_shop(sim, nx, ny) else -1.0
        elif t == DNG_TRAP:
            vec[6 + d] = -0.5
        elif (
            t in (DNG_STAIRS, DNG_POTION, DNG_MANA, DNG_CHEST, DNG_RELIC)
            or (nx, ny) in ctx.dormant_mimic_pos
            or (t == DNG_SHRINE and _is_usable_shrine(sim, sim.grid[ny][nx]))
        ):
            vec[6 + d] = 1.0
        else:
            vc = sim.visited_counts.get((nx, ny), 0)
            vec[6 + d] = 0.5 if vc == 0 else 0.0

    # 10..13: 4-dir enemy raycasts up to 6 tiles
    for d in range(4):
        dist = _ray_hits_target(sim, d, RAYCAST_MAX_DIST, ctx.enemy_pos_map, include_cracked=False)
        if dist > 0:
            vec[10 + d] = (RAYCAST_MAX_DIST + 1 - dist) / float(RAYCAST_MAX_DIST)

    # 14..19: Manhattan vectors to stairs, unexplored frontier, and loot
    if ctx.stairs_pos is not None:
        vec[14] = max(-1.0, min(1.0, (ctx.stairs_pos[0] - px) / 12.0))
        vec[15] = max(-1.0, min(1.0, (ctx.stairs_pos[1] - py) / 12.0))

    if ctx.nearest_frontier is not None:
        vec[16] = max(-1.0, min(1.0, (ctx.nearest_frontier[0] - px) / 12.0))
        vec[17] = max(-1.0, min(1.0, (ctx.nearest_frontier[1] - py) / 12.0))

    if ctx.nearest_loot is not None:
        vec[18] = max(-1.0, min(1.0, (ctx.nearest_loot[0] - px) / 12.0))
        vec[19] = max(-1.0, min(1.0, (ctx.nearest_loot[1] - py) / 12.0))

    # 20..23: Combat & danger context
    if ctx.nearest_enemy is not None:
        vec[20] = max(-1.0, min(1.0, (ctx.nearest_enemy[0] - px) / 8.0))
        vec[21] = max(-1.0, min(1.0, (ctx.nearest_enemy[1] - py) / 8.0))

    vec[22] = min(1.0, ctx.adj_3x3_enemies / 3.0)
    if ctx.player_in_aim_line or (sim.is_low_hp() and ctx.adj_3x3_enemies > 0):
        vec[23] = 1.0
    elif ctx.boss_alive:
        vec[23] = 0.5
    else:
        vec[23] = 0.0

    return vec
