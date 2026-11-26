#include "app/dungeon_game.h"
#include "drivers/msp430/uart.h"
#include "drivers/msp430/ckpt.h"
#include "drivers/msp430/timer.h"
#include "drivers/msp430/hal_common.h"
#include "drivers/board/board.h"
#include <string.h>

#define DEBOUNCE_PERIOD_MS  20u
#define HOLD_TICKS_1S       40u     /* 40 * 20 ms = 0.8 s */
#define CMD_QUEUE_SIZE      16u     /* Power-of-2 RX/button command ring buffer */
#define CMD_QUEUE_MASK      (CMD_QUEUE_SIZE - 1u)
#define CMD_STEP_FWD        0x81u   /* Internal command for S2 button (step in facing dir) */
#define CMD_ROTATE          0x82u   /* Internal command for S1 button (rotate clockwise) */

#define LOS_RADIUS          6
#define CAM_DEADZONE        3u
#define STATUS_LEN          79u

/* Enemy flags */
#define EF_AIMING           0x01u   /* Archer telegraphed shot ready to fire next turn */
#define EF_AWAKE            0x02u   /* Mimic revealed and hostile */
#define EF_SLOW_SKIP        0x04u   /* Slime moves every 2nd turn */

typedef struct
{
    uint8_t x;
    uint8_t y;
    uint8_t type;       /* dng_enemy_type_t */
    uint8_t hp;
    uint8_t flags;
    int8_t  aim_dx;
    int8_t  aim_dy;
    uint8_t max_hp;
} dng_enemy_t;

typedef struct
{
    const char *name;
    const char *glyph;
    const char *ansi;
    uint8_t     base_hp;
    uint8_t     dmg;
    uint8_t     gold_melee;
    uint8_t     gold_bolt;
} dng_enemy_def_t;

static const dng_enemy_def_t k_enemy_defs[6] = {
    { "None",     "  ", "\x1b[0m",    0u, 0u,  0u,  0u },
    { "Slime",    "sl", "\x1b[1;31m", 3u, 1u,  6u,  8u },
    { "Skeleton", "SK", "\x1b[1;35m", 5u, 2u, 12u, 15u },
    { "Archer",   "AR", "\x1b[1;33m", 4u, 2u, 14u, 16u },
    { "Wraith",   "WR", "\x1b[1;36m", 3u, 1u, 16u, 18u },
    { "Mimic",    "MM", "\x1b[1;31m", 6u, 3u, 30u, 30u }
};

/*
 * Persisted dungeon run stored in FRAM via ckpt.
 * ~1,244 bytes (<= CKPT_MAX_PAYLOAD of 1,536 bytes).
 */
typedef struct
{
    uint16_t    best_floor;
    uint16_t    gold;
    uint16_t    rng_state;
    uint8_t     floor;
    uint8_t     hp;
    uint8_t     max_hp;
    uint8_t     mana;
    uint8_t     max_mana;
    uint8_t     atk;
    uint8_t     def;
    uint8_t     px;
    uint8_t     py;
    uint8_t     dir;            /* 0=N, 1=E, 2=S, 3=W */
    uint8_t     empowered;
    uint8_t     has_saved_run;
    uint8_t     map[DNG_H][DNG_W];
    dng_enemy_t enemies[DNG_MAX_ENEMIES];
} dng_ckpt_t;

typedef char ckpt_size_check[(sizeof(dng_ckpt_t) <= CKPT_MAX_PAYLOAD) ? 1 : -1];

/* Event-driven button debouncer (runs only while a button is pressed) */
typedef struct
{
    uint8_t s1_hist;
    uint8_t s2_hist;
    bool    s1_stable;
    bool    s2_stable;
    uint8_t s1_hold;
    uint8_t s2_hold;
    uint8_t both_hold;
    bool    s1_fired;
    bool    s2_fired;
    bool    both_fired;
} dng_btn_t;

/* Large runtime arrays placed in 0-wait-state FRAM (.TI.persistent) to keep RAM free */
#pragma PERSISTENT(s_map)
static uint8_t     s_map[DNG_H][DNG_W] = { { 0 } };
#pragma PERSISTENT(s_prev_vis)
static uint8_t     s_prev_vis[DNG_VIEW_H][DNG_VIEW_W] = { { 0 } };
#pragma PERSISTENT(s_enemies)
static dng_enemy_t s_enemies[DNG_MAX_ENEMIES] = { { 0 } };
#pragma PERSISTENT(s_ckpt_stage)
static dng_ckpt_t  s_ckpt_stage = { 0 };

/* Small active state in SRAM */
static uint8_t  s_px;
static uint8_t  s_py;
static uint8_t  s_dir;
static uint8_t  s_cam_x;
static uint8_t  s_cam_y;
static uint8_t  s_floor;
static uint16_t s_best_floor;
static uint16_t s_gold;
static uint8_t  s_hp;
static uint8_t  s_max_hp;
static uint8_t  s_mana;
static uint8_t  s_max_mana;
static uint8_t  s_atk;
static uint8_t  s_def;
static bool          s_empowered;
static volatile bool s_game_over;
static bool          s_has_saved_run;
static bool     s_dirty;
static bool     s_full_redraw;
static bool     s_map_mode;
static uint16_t s_rng = 0x5D17u;
static char     s_status_msg[STATUS_LEN + 1u];

static dng_btn_t        s_btn;
static volatile uint8_t s_cmd_q[CMD_QUEUE_SIZE];
static volatile uint8_t s_cmd_head = 0u;
static volatile uint8_t s_cmd_tail = 0u;
static volatile bool    s_probe_due = false;
static bool             s_terminal_connected = false;
static uint8_t          s_missed_probes = 0u;
static uint8_t          s_esc_state = 0u; /* 0=none, 1=ESC, 2=ESC[, 3=ESC[0 */

static const int8_t DIR_DX[4] = {  0,  1,  0, -1 };
static const int8_t DIR_DY[4] = { -1,  0,  1,  0 };
static const char   DIR_CH[4] = { 'N', 'E', 'S', 'W' };

/* --------------------------------------------------------------------------
 * Small utilities
 * -------------------------------------------------------------------------- */

static uint16_t next_rand(void)
{
    uint16_t lsb = s_rng & 1u;
    s_rng >>= 1;
    if (lsb != 0u)
    {
        s_rng ^= 0xB400u;
    }
    return s_rng;
}

static uint8_t u8_abs_diff(uint8_t a, uint8_t b)
{
    return (a > b) ? (uint8_t)(a - b) : (uint8_t)(b - a);
}

static uint8_t tile_type(uint8_t x, uint8_t y)
{
    return (uint8_t)(s_map[y][x] & TILE_TYPE_MASK);
}

static void set_tile_type(uint8_t x, uint8_t y, uint8_t t)
{
    s_map[y][x] = (uint8_t)((s_map[y][x] & (uint8_t)~TILE_TYPE_MASK) | (t & TILE_TYPE_MASK));
}

static bool tile_blocks_los(uint8_t t)
{
    return (t == DNG_WALL || t == DNG_CRACKED_WALL || t == DNG_DOOR_CLOSED);
}

static bool tile_blocks_move(uint8_t t)
{
    return (t == DNG_WALL || t == DNG_CRACKED_WALL || t == DNG_DOOR_CLOSED);
}

static bool is_cave_floor(uint8_t fl)
{
    /* Floors 3-4, 7-8, ... are Fungal Caverns; 1-2, 5-6, ... are Stone Crypt */
    return (((fl - 1u) & 0x02u) != 0u);
}

static void set_status(const char *msg)
{
    uint8_t i = 0u;
    while (msg[i] != '\0' && i < STATUS_LEN)
    {
        s_status_msg[i] = msg[i];
        i++;
    }
    while (i < STATUS_LEN)
    {
        s_status_msg[i++] = ' ';
    }
    s_status_msg[STATUS_LEN] = '\0';
}

static uint8_t append_u8_to_status(uint8_t p, uint8_t val)
{
    if (val >= 100u && p < STATUS_LEN) { s_status_msg[p++] = (char)('0' + (val / 100u)); }
    if (val >= 10u && p < STATUS_LEN)  { s_status_msg[p++] = (char)('0' + ((val / 10u) % 10u)); }
    if (p < STATUS_LEN)                { s_status_msg[p++] = (char)('0' + (val % 10u)); }
    return p;
}

static void set_status_combat(const char *prefix, const char *name, uint8_t val1, const char *mid, uint8_t val2, const char *suffix)
{
    uint8_t p = 0u;
    const char *s;

    for (s = prefix; *s && p < STATUS_LEN; s++) { s_status_msg[p++] = *s; }
    for (s = name; *s && p < STATUS_LEN; s++)   { s_status_msg[p++] = *s; }
    if (p < STATUS_LEN) { s_status_msg[p++] = ' '; }
    p = append_u8_to_status(p, val1);
    for (s = mid; *s && p < STATUS_LEN; s++)    { s_status_msg[p++] = *s; }
    if (val2 != 255u) { p = append_u8_to_status(p, val2); }
    for (s = suffix; *s && p < STATUS_LEN; s++) { s_status_msg[p++] = *s; }
    while (p < STATUS_LEN) { s_status_msg[p++] = ' '; }
    s_status_msg[STATUS_LEN] = '\0';
}

static int8_t find_enemy_at(uint8_t x, uint8_t y)
{
    uint8_t i;
    for (i = 0u; i < DNG_MAX_ENEMIES; i++)
    {
        if (s_enemies[i].type != ENEMY_NONE && s_enemies[i].x == x && s_enemies[i].y == y)
        {
            return (int8_t)i;
        }
    }
    return -1;
}

/* --------------------------------------------------------------------------
 * Integer Bresenham Line-of-Sight & Dead-Zone Camera
 * -------------------------------------------------------------------------- */

static void cast_los_ray(int8_t x1, int8_t y1)
{
    int8_t x0 = (int8_t)s_px;
    int8_t y0 = (int8_t)s_py;
    int8_t dx = (x1 > x0) ? (int8_t)(x1 - x0) : (int8_t)(x0 - x1);
    int8_t dy = (y1 > y0) ? (int8_t)(y1 - y0) : (int8_t)(y0 - y1);
    int8_t sx = (x0 < x1) ? 1 : -1;
    int8_t sy = (y0 < y1) ? 1 : -1;
    int8_t err = (int8_t)(dx - dy);

    while (1)
    {
        if (x0 < 0 || x0 >= (int8_t)DNG_W || y0 < 0 || y0 >= (int8_t)DNG_H)
        {
            break;
        }

        uint8_t ux = (uint8_t)x0;
        uint8_t uy = (uint8_t)y0;
        s_map[uy][ux] |= (uint8_t)(TILE_F_VISIBLE | TILE_F_EXPLORED);

        uint8_t t = tile_type(ux, uy);
        /* Reveal hidden traps when close enough */
        if (t == DNG_TRAP)
        {
            uint8_t dist = (uint8_t)(u8_abs_diff(ux, s_px) + u8_abs_diff(uy, s_py));
            if (dist <= 2u)
            {
                s_map[uy][ux] |= TILE_F_REVEALED;
            }
        }

        /* Opaque tiles are visible themselves, but block sight behind them */
        if ((ux != s_px || uy != s_py) && tile_blocks_los(t))
        {
            break;
        }
        if (x0 == x1 && y0 == y1)
        {
            break;
        }

        int8_t e2 = (int8_t)(err * 2);
        if (e2 > -dy)
        {
            err = (int8_t)(err - dy);
            x0 = (int8_t)(x0 + sx);
        }
        if (e2 < dx)
        {
            err = (int8_t)(err + dx);
            y0 = (int8_t)(y0 + sy);
        }
    }
}

static void compute_los(void)
{
    uint8_t r, c;
    int8_t d;

    for (r = 0u; r < DNG_H; r++)
    {
        for (c = 0u; c < DNG_W; c++)
        {
            s_map[r][c] &= (uint8_t)~TILE_F_VISIBLE;
        }
    }

    s_map[s_py][s_px] |= (uint8_t)(TILE_F_VISIBLE | TILE_F_EXPLORED);

    /* Cast rays to all 48 perimeter points of the +/-LOS_RADIUS box */
    for (d = -LOS_RADIUS; d <= LOS_RADIUS; d++)
    {
        cast_los_ray((int8_t)((int8_t)s_px + d), (int8_t)((int8_t)s_py - LOS_RADIUS));
        cast_los_ray((int8_t)((int8_t)s_px + d), (int8_t)((int8_t)s_py + LOS_RADIUS));
        cast_los_ray((int8_t)((int8_t)s_px - LOS_RADIUS), (int8_t)((int8_t)s_py + d));
        cast_los_ray((int8_t)((int8_t)s_px + LOS_RADIUS), (int8_t)((int8_t)s_py + d));
    }
}

static void update_camera(bool force_center)
{
    const uint8_t max_cx = (uint8_t)(DNG_W - DNG_VIEW_W);
    const uint8_t max_cy = (uint8_t)(DNG_H - DNG_VIEW_H);
    bool need_pan = force_center;

    if (!need_pan)
    {
        if (s_px < (uint8_t)(s_cam_x + CAM_DEADZONE) && s_cam_x > 0u)
            need_pan = true;
        else if (s_px >= (uint8_t)(s_cam_x + DNG_VIEW_W - CAM_DEADZONE) && s_cam_x < max_cx)
            need_pan = true;
        else if (s_py < (uint8_t)(s_cam_y + CAM_DEADZONE) && s_cam_y > 0u)
            need_pan = true;
        else if (s_py >= (uint8_t)(s_cam_y + DNG_VIEW_H - CAM_DEADZONE) && s_cam_y < max_cy)
            need_pan = true;
    }

    if (need_pan)
    {
        uint8_t nx = (s_px > (DNG_VIEW_W / 2u)) ? (uint8_t)(s_px - (DNG_VIEW_W / 2u)) : 0u;
        uint8_t ny = (s_py > (DNG_VIEW_H / 2u)) ? (uint8_t)(s_py - (DNG_VIEW_H / 2u)) : 0u;
        if (nx > max_cx) { nx = max_cx; }
        if (ny > max_cy) { ny = max_cy; }
        if (nx != s_cam_x || ny != s_cam_y || force_center)
        {
            s_cam_x = nx;
            s_cam_y = ny;
            s_full_redraw = true;
        }
    }
}

/* --------------------------------------------------------------------------
 * FRAM Checkpoint Save / Restore (0 bytes on stack via s_ckpt_stage in FRAM)
 * -------------------------------------------------------------------------- */

static bool commit_checkpoint(bool save_active_run)
{
    if ((uint16_t)s_floor > s_best_floor)
    {
        s_best_floor = (uint16_t)s_floor;
    }
    if (save_active_run && !s_game_over)
    {
        s_has_saved_run = true;
    }

    s_ckpt_stage.best_floor    = s_best_floor;
    s_ckpt_stage.gold          = s_gold;
    s_ckpt_stage.rng_state     = s_rng;
    s_ckpt_stage.floor         = s_floor;
    s_ckpt_stage.hp            = s_hp;
    s_ckpt_stage.max_hp        = s_max_hp;
    s_ckpt_stage.mana          = s_mana;
    s_ckpt_stage.max_mana      = s_max_mana;
    s_ckpt_stage.atk           = s_atk;
    s_ckpt_stage.def           = s_def;
    s_ckpt_stage.px            = s_px;
    s_ckpt_stage.py            = s_py;
    s_ckpt_stage.dir           = s_dir;
    s_ckpt_stage.empowered     = s_empowered ? 1u : 0u;
    s_ckpt_stage.has_saved_run = s_has_saved_run ? 1u : 0u;
    memcpy(s_ckpt_stage.map, s_map, sizeof(s_map));
    memcpy(s_ckpt_stage.enemies, s_enemies, sizeof(s_enemies));

    bool ok = ckpt_save(&s_ckpt_stage, (uint16_t)sizeof(s_ckpt_stage));
    board_led2(false);
    return ok;
}

static bool load_checkpoint(bool restore_full_run)
{
    uint32_t seq = 0u;

    if (!ckpt_restore(&s_ckpt_stage, (uint16_t)sizeof(s_ckpt_stage), &seq))
    {
        return false;
    }

    s_best_floor    = s_ckpt_stage.best_floor;
    s_has_saved_run = (s_ckpt_stage.has_saved_run != 0u);

    if (restore_full_run && s_has_saved_run &&
        s_ckpt_stage.hp > 0u && s_ckpt_stage.max_hp > 0u && s_ckpt_stage.floor > 0u)
    {
        s_gold      = s_ckpt_stage.gold;
        s_rng       = (s_ckpt_stage.rng_state != 0u) ? s_ckpt_stage.rng_state : 0x5D17u;
        s_floor     = s_ckpt_stage.floor;
        s_hp        = s_ckpt_stage.hp;
        s_max_hp    = s_ckpt_stage.max_hp;
        s_mana      = s_ckpt_stage.mana;
        s_max_mana  = s_ckpt_stage.max_mana;
        s_atk       = (s_ckpt_stage.atk > 0u) ? s_ckpt_stage.atk : 2u;
        s_def       = s_ckpt_stage.def;
        s_px        = (s_ckpt_stage.px < DNG_W) ? s_ckpt_stage.px : 2u;
        s_py        = (s_ckpt_stage.py < DNG_H) ? s_ckpt_stage.py : 2u;
        s_dir       = s_ckpt_stage.dir & 0x03u;
        s_empowered = (s_ckpt_stage.empowered != 0u);
        s_game_over = false;
        s_map_mode  = false;
        memcpy(s_map, s_ckpt_stage.map, sizeof(s_map));
        memcpy(s_enemies, s_ckpt_stage.enemies, sizeof(s_enemies));
        compute_los();
        update_camera(true);
        board_led1(false);
        board_led2(false);
        set_status("RESTORED CHECKPOINT FROM FRAM!");
        s_full_redraw = true;
        s_dirty = true;
        return true;
    }

    return false;
}

/* --------------------------------------------------------------------------
 * Procedural 48x22 Dungeon Generation (Stone Crypt & Fungal Caverns)
 * -------------------------------------------------------------------------- */

static void carve_room(uint8_t x0, uint8_t y0, uint8_t w, uint8_t h)
{
    uint8_t r, c;
    for (r = y0; r < (uint8_t)(y0 + h) && r < (DNG_H - 1u); r++)
    {
        for (c = x0; c < (uint8_t)(x0 + w) && c < (DNG_W - 1u); c++)
        {
            s_map[r][c] = DNG_FLOOR;
        }
    }
}

static void carve_corridor(uint8_t x0, uint8_t y0, uint8_t x1, uint8_t y1)
{
    uint8_t x = x0;
    uint8_t y = y0;

    if ((next_rand() & 1u) == 0u)
    {
        while (x != x1)
        {
            s_map[y][x] = DNG_FLOOR;
            x = (x < x1) ? (uint8_t)(x + 1u) : (uint8_t)(x - 1u);
        }
        while (y != y1)
        {
            s_map[y][x] = DNG_FLOOR;
            y = (y < y1) ? (uint8_t)(y + 1u) : (uint8_t)(y - 1u);
        }
    }
    else
    {
        while (y != y1)
        {
            s_map[y][x] = DNG_FLOOR;
            y = (y < y1) ? (uint8_t)(y + 1u) : (uint8_t)(y - 1u);
        }
        while (x != x1)
        {
            s_map[y][x] = DNG_FLOOR;
            x = (x < x1) ? (uint8_t)(x + 1u) : (uint8_t)(x - 1u);
        }
    }
    s_map[y][x] = DNG_FLOOR;
}

static bool find_open_floor(uint8_t *ox, uint8_t *oy, uint8_t min_dist_player)
{
    uint8_t tries;
    for (tries = 0u; tries < 80u; tries++)
    {
        uint8_t c = (uint8_t)(1u + (next_rand() % (DNG_W - 2u)));
        uint8_t r = (uint8_t)(1u + (next_rand() % (DNG_H - 2u)));
        if (tile_type(c, r) != DNG_FLOOR) { continue; }
        if (c == s_px && r == s_py) { continue; }
        if (find_enemy_at(c, r) >= 0) { continue; }
        uint8_t dist = (uint8_t)(u8_abs_diff(c, s_px) + u8_abs_diff(r, s_py));
        if (dist < min_dist_player) { continue; }
        *ox = c;
        *oy = r;
        return true;
    }
    return false;
}

static void place_doors(void)
{
    uint8_t placed = 0u;
    uint8_t r, c;

    for (r = 2u; r < (DNG_H - 2u) && placed < 6u; r++)
    {
        for (c = 2u; c < (DNG_W - 2u) && placed < 6u; c++)
        {
            if (tile_type(c, r) != DNG_FLOOR || (c == s_px && r == s_py))
            {
                continue;
            }
            bool ns_wall = (tile_type(c, (uint8_t)(r - 1u)) == DNG_WALL &&
                            tile_type(c, (uint8_t)(r + 1u)) == DNG_WALL &&
                            tile_type((uint8_t)(c - 1u), r) == DNG_FLOOR &&
                            tile_type((uint8_t)(c + 1u), r) == DNG_FLOOR);
            bool ew_wall = (tile_type((uint8_t)(c - 1u), r) == DNG_WALL &&
                            tile_type((uint8_t)(c + 1u), r) == DNG_WALL &&
                            tile_type(c, (uint8_t)(r - 1u)) == DNG_FLOOR &&
                            tile_type(c, (uint8_t)(r + 1u)) == DNG_FLOOR);

            if ((ns_wall || ew_wall) && ((next_rand() & 0x07u) == 0u))
            {
                s_map[r][c] = DNG_DOOR_CLOSED;
                placed++;
            }
        }
    }
}

static void place_secret_alcove(void)
{
    uint8_t tries;
    for (tries = 0u; tries < 80u; tries++)
    {
        uint8_t c = (uint8_t)(2u + (next_rand() % (DNG_W - 4u)));
        uint8_t r = (uint8_t)(2u + (next_rand() % (DNG_H - 4u)));
        if (tile_type(c, r) != DNG_WALL) { continue; }

        uint8_t d;
        for (d = 0u; d < 4u; d++)
        {
            uint8_t fc = (uint8_t)((int8_t)c + DIR_DX[d]);
            uint8_t fr = (uint8_t)((int8_t)r + DIR_DY[d]);
            uint8_t bc = (uint8_t)((int8_t)c - DIR_DX[d]);
            uint8_t br = (uint8_t)((int8_t)r - DIR_DY[d]);
            if (bc <= 1u || bc >= (DNG_W - 2u) || br <= 1u || br >= (DNG_H - 2u))
            {
                continue;
            }
            if (tile_type(fc, fr) == DNG_FLOOR && tile_type(bc, br) == DNG_WALL)
            {
                s_map[r][c]   = DNG_CRACKED_WALL;
                s_map[br][bc] = ((next_rand() & 1u) == 0u) ? DNG_CHEST : DNG_POTION;
                return;
            }
        }
    }
}

static void spawn_enemy(uint8_t slot, uint8_t x, uint8_t y, uint8_t type)
{
    const dng_enemy_def_t *def = &k_enemy_defs[type];
    uint8_t bonus_hp = (s_floor >= 4u) ? (uint8_t)((s_floor - 2u) / 2u) : 0u;

    s_enemies[slot].x      = x;
    s_enemies[slot].y      = y;
    s_enemies[slot].type   = type;
    s_enemies[slot].hp     = (uint8_t)(def->base_hp + bonus_hp);
    s_enemies[slot].max_hp = s_enemies[slot].hp;
    s_enemies[slot].flags  = (type == ENEMY_MIMIC) ? 0u : EF_AWAKE;
    s_enemies[slot].aim_dx = 0;
    s_enemies[slot].aim_dy = 0;
}

static void generate_floor(void)
{
    uint8_t i, x, y;
    uint8_t stairs_x = 2u, stairs_y = 2u;

    memset(s_map, DNG_WALL, sizeof(s_map));
    memset(s_enemies, 0, sizeof(s_enemies));

    if (is_cave_floor(s_floor))
    {
        /* Fungal Caverns: drunkard's walk carver from center */
        uint8_t wx = (uint8_t)(DNG_W / 2u);
        uint8_t wy = (uint8_t)(DNG_H / 2u);
        uint16_t step;
        uint8_t best_dist = 0u;

        s_px = wx;
        s_py = wy;
        carve_room((uint8_t)(wx - 1u), (uint8_t)(wy - 1u), 3u, 3u);

        for (step = 0u; step < 420u; step++)
        {
            uint8_t d = (uint8_t)(next_rand() & 0x03u);
            int8_t nx = (int8_t)((int8_t)wx + DIR_DX[d]);
            int8_t ny = (int8_t)((int8_t)wy + DIR_DY[d]);
            if (nx >= 2 && nx <= (int8_t)(DNG_W - 3u) && ny >= 2 && ny <= (int8_t)(DNG_H - 3u))
            {
                wx = (uint8_t)nx;
                wy = (uint8_t)ny;
                s_map[wy][wx] = DNG_FLOOR;
                uint8_t dist = (uint8_t)(u8_abs_diff(wx, s_px) + u8_abs_diff(wy, s_py));
                if (dist > best_dist)
                {
                    best_dist = dist;
                    stairs_x = wx;
                    stairs_y = wy;
                }
            }
        }
    }
    else
    {
        /* Stone Crypt: 8 random rooms + sequential corridors + 2 loop corridors */
        uint8_t rx[8], ry[8], rw[8], rh[8], cx[8], cy[8];
        uint8_t nrooms = 0u;
        uint8_t attempt;

        for (attempt = 0u; attempt < 45u && nrooms < 8u; attempt++)
        {
            uint8_t w = (uint8_t)(5u + (next_rand() % 5u)); /* 5..9 */
            uint8_t h = (uint8_t)(4u + (next_rand() % 3u)); /* 4..6 */
            uint8_t x0 = (uint8_t)(1u + (next_rand() % (DNG_W - w - 2u)));
            uint8_t y0 = (uint8_t)(1u + (next_rand() % (DNG_H - h - 2u)));
            bool overlap = false;

            for (i = 0u; i < nrooms; i++)
            {
                if (x0 <= (uint8_t)(rx[i] + rw[i]) && (uint8_t)(x0 + w) >= rx[i] &&
                    y0 <= (uint8_t)(ry[i] + rh[i]) && (uint8_t)(y0 + h) >= ry[i])
                {
                    overlap = true;
                    break;
                }
            }
            if (!overlap)
            {
                rx[nrooms] = x0; ry[nrooms] = y0;
                rw[nrooms] = w;  rh[nrooms] = h;
                cx[nrooms] = (uint8_t)(x0 + (w / 2u));
                cy[nrooms] = (uint8_t)(y0 + (h / 2u));
                carve_room(x0, y0, w, h);
                nrooms++;
            }
        }

        if (nrooms < 2u)
        {
            carve_room(2u, 2u, 8u, 5u);
            carve_room(34u, 13u, 8u, 5u);
            cx[0] = 6u;  cy[0] = 4u;
            cx[1] = 38u; cy[1] = 15u;
            nrooms = 2u;
        }

        for (i = 0u; i < (uint8_t)(nrooms - 1u); i++)
        {
            carve_corridor(cx[i], cy[i], cx[i + 1u], cy[i + 1u]);
        }
        /* Loop corridors for flanking and kiting */
        if (nrooms >= 4u)
        {
            carve_corridor(cx[0], cy[0], cx[nrooms - 1u], cy[nrooms - 1u]);
            carve_corridor(cx[1], cy[1], cx[nrooms - 2u], cy[nrooms - 2u]);
        }

        s_px = cx[0];
        s_py = cy[0];

        /* Place stairs in the room farthest from start */
        uint8_t best_dist = 0u;
        for (i = 1u; i < nrooms; i++)
        {
            uint8_t dist = (uint8_t)(u8_abs_diff(cx[i], s_px) + u8_abs_diff(cy[i], s_py));
            if (dist >= best_dist)
            {
                best_dist = dist;
                stairs_x = cx[i];
                stairs_y = cy[i];
            }
        }

        place_doors();
    }

    s_map[stairs_y][stairs_x] = DNG_STAIRS;

    /* 1-2 secret cracked-wall alcoves per floor */
    place_secret_alcove();
    if (s_floor >= 2u) { place_secret_alcove(); }

    /* 1 Shrine per floor (alternates Blood Altar / Aegis Font) */
    if (find_open_floor(&x, &y, 6u))
    {
        s_map[y][x] = (uint8_t)(DNG_SHRINE | (((s_floor & 1u) == 0u) ? TILE_F_ALT : 0u));
    }

    /* 4 Spike Traps per floor (hidden until within 2 tiles) */
    for (i = 0u; i < 4u; i++)
    {
        if (find_open_floor(&x, &y, 4u))
        {
            s_map[y][x] = DNG_TRAP;
        }
    }

    /* Items: 2 Potions, 2 Mana Crystals, 2 Chests */
    for (i = 0u; i < 6u; i++)
    {
        if (find_open_floor(&x, &y, 4u))
        {
            if (i < 2u)      s_map[y][x] = DNG_POTION;
            else if (i < 4u) s_map[y][x] = DNG_MANA;
            else             s_map[y][x] = DNG_CHEST;
        }
    }

    /* Spawn 7..14 enemies scaled by floor */
    uint8_t count = (uint8_t)(6u + s_floor);
    if (count > 14u) { count = 14u; }

    for (i = 0u; i < count; i++)
    {
        if (!find_open_floor(&x, &y, 6u)) { break; }

        uint8_t etype = ENEMY_SLIME;
        uint8_t roll = (uint8_t)(next_rand() % 10u);

        if (i == (uint8_t)(count - 1u) && s_floor >= 2u)
        {
            etype = ENEMY_MIMIC; /* 1 Mimic disguised as a chest on Floor 2+ */
        }
        else if (s_floor >= 3u && roll >= 8u)
        {
            etype = ENEMY_WRAITH;
        }
        else if (roll >= 6u)
        {
            etype = ENEMY_ARCHER;
        }
        else if (s_floor >= 2u && roll >= 3u)
        {
            etype = ENEMY_SKELETON;
        }

        spawn_enemy(i, x, y, etype);
    }

    compute_los();
    update_camera(true);
}

static void start_new_run(void)
{
    s_floor     = 1u;
    s_gold      = 0u;
    s_max_hp    = 14u;
    s_hp        = 14u;
    s_max_mana  = 5u;
    s_mana      = 5u;
    s_atk       = 2u;
    s_def       = 0u;
    s_dir       = 1u;
    s_empowered = false;
    s_game_over = false;
    s_map_mode  = false;

    board_led1(false);
    board_led2(false);
    generate_floor();
    set_status("WELCOME! EXPLORE THE 48x22 DUNGEON. PRESS M FOR FULL MAP.");
    s_full_redraw = true;
    s_dirty = true;
}

void dungeon_game_init(void)
{
    memset(&s_btn, 0, sizeof(s_btn));
    memset(s_prev_vis, 0xFF, sizeof(s_prev_vis));
    s_cmd_head           = 0u;
    s_cmd_tail           = 0u;
    s_probe_due          = false;
    s_terminal_connected = false;
    s_missed_probes      = 0u;
    s_esc_state          = 0u;
    s_full_redraw        = true;
    s_map_mode           = false;
    s_best_floor         = 1u;
    s_has_saved_run      = false;

    /* Normal reboot auto-resumes saved FRAM floor; hold S1 on boot for Floor 1 */
    bool force_new_run = board_s1_pressed();
    if (force_new_run)
    {
        s_btn.s1_stable = true;
        s_btn.s1_hist   = 0x03u;
        s_btn.s1_fired  = true;
        (void)timer_start_oneshot_ms(TIMER_A0, DEBOUNCE_PERIOD_MS, dungeon_game_on_debounce_timer);
    }

    if (force_new_run || !load_checkpoint(true))
    {
        start_new_run();
    }
}

bool dungeon_game_push_input(uint8_t cmd)
{
    uint8_t next = (uint8_t)((s_cmd_head + 1u) & CMD_QUEUE_MASK);
    if (next != s_cmd_tail)
    {
        s_cmd_q[s_cmd_head] = cmd;
        s_cmd_head = next;
    }
    return true;
}

bool dungeon_game_on_probe_timer(timer_id_t id)
{
    (void)id;
    s_probe_due = true;
    return true;
}

bool dungeon_game_has_pending_work(void)
{
    return (s_cmd_head != s_cmd_tail) || s_probe_due || s_dirty;
}

/* --------------------------------------------------------------------------
 * Hardware Button Debouncer (S1/S2)
 * -------------------------------------------------------------------------- */

bool dungeon_game_on_button_irq(void)
{
    if (!timer_running(TIMER_A0))
    {
        (void)timer_start_oneshot_ms(TIMER_A0, DEBOUNCE_PERIOD_MS, dungeon_game_on_debounce_timer);
    }
    return false;
}

bool dungeon_game_on_debounce_timer(timer_id_t id)
{
    (void)id;
    bool prev_s1 = s_btn.s1_stable;
    bool prev_s2 = s_btn.s2_stable;
    bool raw_s1  = board_s1_pressed();
    bool raw_s2  = board_s2_pressed();
    uint8_t cmd  = 0u;

    s_btn.s1_hist = (uint8_t)(((s_btn.s1_hist << 1) | (raw_s1 ? 1u : 0u)) & 0x03u);
    s_btn.s2_hist = (uint8_t)(((s_btn.s2_hist << 1) | (raw_s2 ? 1u : 0u)) & 0x03u);

    if (s_btn.s1_hist == 0x03u)      s_btn.s1_stable = true;
    else if (s_btn.s1_hist == 0x00u) s_btn.s1_stable = false;

    if (s_btn.s2_hist == 0x03u)      s_btn.s2_stable = true;
    else if (s_btn.s2_hist == 0x00u) s_btn.s2_stable = false;

    bool s1 = s_btn.s1_stable;
    bool s2 = s_btn.s2_stable;

    if (s1 && s2)
    {
        s_btn.s1_hold = 0u;
        s_btn.s2_hold = 0u;
        s_btn.both_hold++;
        if (!s_btn.both_fired && s_btn.both_hold >= HOLD_TICKS_1S)
        {
            s_btn.both_fired = true;
            s_btn.s1_fired   = true;
            s_btn.s2_fired   = true;
            cmd = 'p';
        }
    }
    else
    {
        s_btn.both_hold  = 0u;
        s_btn.both_fired = false;

        if (s1 && !s2)
        {
            s_btn.s1_hold++;
            if (!s_btn.s1_fired && s_btn.s1_hold >= HOLD_TICKS_1S)
            {
                s_btn.s1_fired = true;
                cmd = s_game_over ? 'r' : ' ';
            }
        }
        else if (!s1 && prev_s1)
        {
            bool was_fired = s_btn.s1_fired;
            s_btn.s1_hold  = 0u;
            s_btn.s1_fired = false;
            if (!was_fired && !s2)
            {
                cmd = CMD_ROTATE;
            }
        }

        if (s2 && !s1)
        {
            s_btn.s2_hold++;
            if (!s_btn.s2_fired && s_btn.s2_hold >= HOLD_TICKS_1S)
            {
                s_btn.s2_fired = true;
                cmd = 'f';
            }
        }
        else if (!s2 && prev_s2)
        {
            bool was_fired = s_btn.s2_fired;
            s_btn.s2_hold  = 0u;
            s_btn.s2_fired = false;
            if (!was_fired && !s1)
            {
                cmd = CMD_STEP_FWD;
            }
        }
    }

    if (raw_s1 || raw_s2 || s1 || s2 || s_btn.s1_hist != 0u || s_btn.s2_hist != 0u)
    {
        (void)timer_start_oneshot_ms(TIMER_A0, DEBOUNCE_PERIOD_MS, dungeon_game_on_debounce_timer);
    }

    if (cmd != 0u)
    {
        return dungeon_game_push_input(cmd);
    }
    return false;
}

/* --------------------------------------------------------------------------
 * Combat, Traps, Shrines & 5-Archetype Enemy AI
 * -------------------------------------------------------------------------- */

static void damage_player(uint8_t raw_dmg, const char *source_name)
{
    uint8_t dmg = (raw_dmg > s_def) ? (uint8_t)(raw_dmg - s_def) : 1u;

    if (s_hp > dmg)
    {
        s_hp = (uint8_t)(s_hp - dmg);
        set_status_combat("", source_name, dmg, " DMG! (", s_hp, " HP LEFT)");
    }
    else
    {
        s_hp = 0u;
        s_game_over = true;
        board_led1(true);
        /* Never overwrite an active FRAM save with dead (0 HP) state */
        if (!s_has_saved_run)
        {
            (void)commit_checkpoint(false);
        }
        set_status("YOU DIED! PRESS W/SPACE FOR NEW RUN, OR R TO LOAD FRAM SAVE.");
    }
}

static bool clear_straight_line(uint8_t x0, uint8_t y0, uint8_t x1, uint8_t y1)
{
    int8_t dx = (x1 > x0) ? 1 : ((x1 < x0) ? -1 : 0);
    int8_t dy = (y1 > y0) ? 1 : ((y1 < y0) ? -1 : 0);
    if (dx != 0 && dy != 0) { return false; }

    int8_t x = (int8_t)x0 + dx;
    int8_t y = (int8_t)y0 + dy;
    while (x != (int8_t)x1 || y != (int8_t)y1)
    {
        if (x < 0 || x >= (int8_t)DNG_W || y < 0 || y >= (int8_t)DNG_H) { return false; }
        if (tile_blocks_los(tile_type((uint8_t)x, (uint8_t)y))) { return false; }
        x = (int8_t)(x + dx);
        y = (int8_t)(y + dy);
    }
    return true;
}

static void damage_enemy(uint8_t idx, uint8_t dmg, bool by_bolt)
{
    dng_enemy_t *e = &s_enemies[idx];
    const dng_enemy_def_t *def = &k_enemy_defs[e->type];

    e->flags |= EF_AWAKE;
    if (e->hp > dmg)
    {
        e->hp = (uint8_t)(e->hp - dmg);
        set_status_combat(by_bolt ? "BOLT HIT " : "HIT ", def->name, dmg, " DMG (", e->hp, " HP LEFT)");
    }
    else
    {
        uint8_t loot = by_bolt ? def->gold_bolt : def->gold_melee;
        s_gold = (uint16_t)(s_gold + loot);
        set_status_combat(by_bolt ? "ZAP! SLAIN " : "DEFEATED ", def->name, loot, " GOLD!", 255u, "");
        e->type = ENEMY_NONE;
    }
}

static void monster_turn(void)
{
    uint8_t i;

    for (i = 0u; i < DNG_MAX_ENEMIES && !s_game_over; i++)
    {
        dng_enemy_t *e = &s_enemies[i];
        if (e->type == ENEMY_NONE) { continue; }

        /* Dormant Mimic waits until bumped/attacked */
        if (e->type == ENEMY_MIMIC && (e->flags & EF_AWAKE) == 0u)
        {
            continue;
        }

        uint8_t dx = u8_abs_diff(e->x, s_px);
        uint8_t dy = u8_abs_diff(e->y, s_py);
        uint8_t dist = (uint8_t)(dx + dy);

        /* Cultist Archer firing a previously telegraphed shot */
        if (e->type == ENEMY_ARCHER && (e->flags & EF_AIMING) != 0u)
        {
            e->flags &= (uint8_t)~EF_AIMING;
            int8_t bx = (int8_t)e->x;
            int8_t by = (int8_t)e->y;
            uint8_t step;
            bool hit = false;

            for (step = 0u; step < 6u; step++)
            {
                bx = (int8_t)(bx + e->aim_dx);
                by = (int8_t)(by + e->aim_dy);
                if (bx <= 0 || bx >= (int8_t)(DNG_W - 1u) || by <= 0 || by >= (int8_t)(DNG_H - 1u))
                {
                    break;
                }
                if ((uint8_t)bx == s_px && (uint8_t)by == s_py)
                {
                    damage_player(k_enemy_defs[ENEMY_ARCHER].dmg, "ARCHER ARROW HITS FOR");
                    hit = true;
                    break;
                }
                if (tile_blocks_los(tile_type((uint8_t)bx, (uint8_t)by)))
                {
                    break;
                }
            }
            if (!hit)
            {
                set_status("YOU DODGED THE ARCHER'S TELEGRAPHED SHOT!");
            }
            continue;
        }

        if (dist > 8u) { continue; }

        /* Adjacent melee attack */
        if (dist == 1u)
        {
            const dng_enemy_def_t *def = &k_enemy_defs[e->type];
            damage_player(def->dmg, def->name);
            if (e->type == ENEMY_WRAITH && !s_game_over && s_mana > 0u)
            {
                s_mana--;
                set_status("WRAITH CHILLS YOU (-1 HP & -1 MP)!");
            }
            continue;
        }

        /* Archer telegraph: if aligned in LOS within 2..5 tiles, aim on Turn 1 */
        if (e->type == ENEMY_ARCHER && dist >= 2u && dist <= 5u &&
            (e->x == s_px || e->y == s_py) &&
            clear_straight_line(e->x, e->y, s_px, s_py))
        {
            e->flags |= EF_AIMING;
            e->aim_dx = (s_px > e->x) ? 1 : ((s_px < e->x) ? -1 : 0);
            e->aim_dy = (s_py > e->y) ? 1 : ((s_py < e->y) ? -1 : 0);
            set_status("WARNING: CULTIST ARCHER (A!) IS AIMING AT YOU! SIDESTEP!");
            continue;
        }

        /* Slime moves every 2nd turn */
        if (e->type == ENEMY_SLIME)
        {
            e->flags ^= EF_SLOW_SKIP;
            if ((e->flags & EF_SLOW_SKIP) != 0u)
            {
                continue;
            }
        }

        /* Choose primary & fallback step toward player */
        int8_t sx = (s_px > e->x) ? 1 : ((s_px < e->x) ? -1 : 0);
        int8_t sy = (s_py > e->y) ? 1 : ((s_py < e->y) ? -1 : 0);
        int8_t cand_x[2], cand_y[2];

        if (dx >= dy)
        {
            cand_x[0] = (int8_t)e->x + sx; cand_y[0] = (int8_t)e->y;
            cand_x[1] = (int8_t)e->x;      cand_y[1] = (int8_t)e->y + sy;
        }
        else
        {
            cand_x[0] = (int8_t)e->x;      cand_y[0] = (int8_t)e->y + sy;
            cand_x[1] = (int8_t)e->x + sx; cand_y[1] = (int8_t)e->y;
        }

        uint8_t k;
        for (k = 0u; k < 2u; k++)
        {
            if (cand_x[k] <= 0 || cand_x[k] >= (int8_t)(DNG_W - 1u) ||
                cand_y[k] <= 0 || cand_y[k] >= (int8_t)(DNG_H - 1u))
            {
                continue;
            }
            uint8_t nx = (uint8_t)cand_x[k];
            uint8_t ny = (uint8_t)cand_y[k];
            if (nx == e->x && ny == e->y) { continue; }
            if (nx == s_px && ny == s_py) { continue; }
            if (find_enemy_at(nx, ny) >= 0) { continue; }

            uint8_t tt = tile_type(nx, ny);
            /* Wraiths phase through walls/doors; all other foes respect walls/closed doors */
            if (e->type != ENEMY_WRAITH && tile_blocks_move(tt))
            {
                continue;
            }

            e->x = nx;
            e->y = ny;

            /* Spike traps hurt non-Wraith enemies too! */
            if (tt == DNG_TRAP && e->type != ENEMY_WRAITH)
            {
                s_map[ny][nx] |= TILE_F_REVEALED;
                if (e->hp > 2u)
                {
                    e->hp = (uint8_t)(e->hp - 2u);
                    set_status("FOE STEPPED ON SPIKE TRAP (-2 HP)!");
                }
                else
                {
                    s_gold = (uint16_t)(s_gold + k_enemy_defs[e->type].gold_melee);
                    e->type = ENEMY_NONE;
                    set_status("SPIKE TRAP IMPALED A FOE! (+GOLD)");
                }
            }
            break;
        }
    }
}

static void cast_arcane_bolt(void)
{
    if (s_mana == 0u)
    {
        set_status("NO MANA! PRESS SPACE TO MEDITATE (+1 MP & EMPOWER).");
        s_dirty = true;
        return;
    }

    s_mana--;
    uint8_t bolt_dmg = (uint8_t)(s_atk + (s_empowered ? 3u : 2u));
    s_empowered = false;

    int8_t x = (int8_t)s_px;
    int8_t y = (int8_t)s_py;
    uint8_t step;
    bool hit_anything = false;

    for (step = 0u; step < 8u; step++)
    {
        x = (int8_t)(x + DIR_DX[s_dir]);
        y = (int8_t)(y + DIR_DY[s_dir]);
        if (x <= 0 || x >= (int8_t)(DNG_W - 1u) || y <= 0 || y >= (int8_t)(DNG_H - 1u))
        {
            break;
        }

        int8_t ei = find_enemy_at((uint8_t)x, (uint8_t)y);
        if (ei >= 0)
        {
            damage_enemy((uint8_t)ei, bolt_dmg, true);
            hit_anything = true;
            break;
        }

        uint8_t t = tile_type((uint8_t)x, (uint8_t)y);
        if (t == DNG_CRACKED_WALL)
        {
            set_tile_type((uint8_t)x, (uint8_t)y, DNG_FLOOR);
            compute_los();
            set_status("BOOM! ARCANE BOLT SHATTERED A CRACKED WALL!");
            hit_anything = true;
            break;
        }
        if (tile_blocks_los(t))
        {
            set_status("ARCANE BOLT STRUCK STONE.");
            hit_anything = true;
            break;
        }
    }

    if (!hit_anything)
    {
        set_status("ARCANE BOLT FIZZLED INTO THE DARK.");
    }

    monster_turn();
    s_dirty = true;
}

static void activate_shrine(uint8_t x, uint8_t y)
{
    if ((s_map[y][x] & TILE_F_REVEALED) != 0u)
    {
        set_status("THIS SHRINE'S POWER IS SPENT.");
        return;
    }

    if ((s_map[y][x] & TILE_F_ALT) == 0u)
    {
        /* Blood Altar: -3 HP for +1 ATK */
        if (s_hp > 3u)
        {
            s_hp = (uint8_t)(s_hp - 3u);
            s_atk++;
            s_map[y][x] |= TILE_F_REVEALED;
            set_status("BLOOD ALTAR: SACRIFICED 3 HP FOR +1 ATK!");
        }
        else
        {
            set_status("BLOOD ALTAR REQUIRES >3 HP TO SACRIFICE.");
        }
    }
    else
    {
        /* Aegis Font: full HP, or +1 DEF if already full */
        s_map[y][x] |= TILE_F_REVEALED;
        if (s_hp < s_max_hp)
        {
            s_hp = s_max_hp;
            set_status("AEGIS FONT: FULLY RESTORED YOUR HP!");
        }
        else
        {
            s_def++;
            set_status("AEGIS FONT BLESSES YOUR ARMOR (+1 DEF)!");
        }
    }
}

static void step_in_direction(uint8_t dir)
{
    s_dir = dir & 0x03u;
    int8_t ix = (int8_t)((int8_t)s_px + DIR_DX[s_dir]);
    int8_t iy = (int8_t)((int8_t)s_py + DIR_DY[s_dir]);

    if (ix < 0 || ix >= (int8_t)DNG_W || iy < 0 || iy >= (int8_t)DNG_H)
    {
        return;
    }

    uint8_t nx = (uint8_t)ix;
    uint8_t ny = (uint8_t)iy;

    /* 1. Check enemy / Mimic bump-attack */
    int8_t ei = find_enemy_at(nx, ny);
    if (ei >= 0)
    {
        uint8_t dmg = s_empowered ? (uint8_t)(s_atk + ((s_atk + 1u) / 2u)) : s_atk;
        s_empowered = false;
        if (s_enemies[ei].type == ENEMY_MIMIC && (s_enemies[ei].flags & EF_AWAKE) == 0u)
        {
            s_enemies[ei].flags |= EF_AWAKE;
            set_status("IT'S A MIMIC!! THE CHEST SNAPS AT YOU!");
        }
        else
        {
            damage_enemy((uint8_t)ei, dmg, false);
        }
        monster_turn();
        s_dirty = true;
        return;
    }

    uint8_t target = tile_type(nx, ny);

    /* 2. Bump-open Closed Door */
    if (target == DNG_DOOR_CLOSED)
    {
        set_tile_type(nx, ny, DNG_DOOR_OPEN);
        compute_los();
        set_status("OPENED THE WOODEN DOOR.");
        monster_turn();
        s_dirty = true;
        return;
    }

    /* 3. Wall / Cracked Wall block */
    if (target == DNG_CRACKED_WALL)
    {
        set_status("CRACKED WALL (%) - BLAST IT OPEN WITH ARCANE BOLT (F)!");
        s_dirty = true;
        return;
    }
    if (target == DNG_WALL)
    {
        set_status("BLOCKED BY STONE WALL.");
        s_dirty = true;
        return;
    }

    /* 4. Move into tile (Shrines are walkable so corridors can never softlock) */
    s_px = nx;
    s_py = ny;
    compute_los();
    update_camera(false);

    if (target == DNG_SHRINE)
    {
        activate_shrine(nx, ny);
    }
    else if (target == DNG_TRAP)
    {
        s_map[ny][nx] |= TILE_F_REVEALED;
        damage_player(2u, "SPIKE TRAP PIERCES YOU FOR");
        if (s_game_over) { s_dirty = true; return; }
    }
    else if (target == DNG_POTION)
    {
        set_tile_type(nx, ny, DNG_FLOOR);
        s_hp = (uint8_t)(s_hp + 5u);
        if (s_hp > s_max_hp) { s_hp = s_max_hp; }
        set_status("DRANK HEALTH POTION (+5 HP)!");
    }
    else if (target == DNG_MANA)
    {
        set_tile_type(nx, ny, DNG_FLOOR);
        s_mana = (uint8_t)(s_mana + 3u);
        if (s_mana > s_max_mana) { s_mana = s_max_mana; }
        set_status("ABSORBED MANA CRYSTAL (+3 MP)!");
    }
    else if (target == DNG_CHEST)
    {
        set_tile_type(nx, ny, DNG_FLOOR);
        s_gold = (uint16_t)(s_gold + 25u);
        s_max_hp++;
        s_hp++;
        if ((next_rand() & 0x03u) == 0u)
        {
            s_atk++;
            set_status("TREASURE CHEST! +25G, +1 MAX HP & +1 ATK!");
        }
        else
        {
            set_status("TREASURE CHEST! +25G & +1 MAX HP!");
        }
    }
    else if (target == DNG_STAIRS)
    {
        s_floor++;
        if ((uint16_t)s_floor > s_best_floor)
        {
            s_best_floor = (uint16_t)s_floor;
        }
        generate_floor();
        (void)commit_checkpoint(true);
        set_status(is_cave_floor(s_floor) ? "DESCENDED INTO FUNGAL CAVERNS! SAVED TO FRAM."
                                          : "DESCENDED INTO STONE CRYPT! SAVED TO FRAM.");
        s_full_redraw = true;
        s_dirty = true;
        return;
    }

    monster_turn();
    s_dirty = true;
}

/* --------------------------------------------------------------------------
 * Chunked Differential ANSI Renderer (256 B RAM buffer, 80x24 exact layout)
 * -------------------------------------------------------------------------- */

static uint16_t buf_puts(char *dst, uint16_t pos, uint16_t cap, const char *str)
{
    while (*str != '\0' && pos < cap)
    {
        dst[pos++] = *str++;
    }
    return pos;
}

static uint16_t buf_put_u8_dec(char *dst, uint16_t pos, uint16_t cap, uint8_t val)
{
    if (val >= 100u && pos < cap) { dst[pos++] = (char)('0' + (val / 100u)); }
    if (val >= 10u && pos < cap)  { dst[pos++] = (char)('0' + ((val / 10u) % 10u)); }
    if (pos < cap)                { dst[pos++] = (char)('0' + (val % 10u)); }
    return pos;
}

static uint16_t buf_put_u8_pad2(char *dst, uint16_t pos, uint16_t cap, uint8_t val)
{
    if ((uint16_t)(pos + 2u) <= cap)
    {
        dst[pos++] = (char)('0' + ((val / 10u) % 10u));
        dst[pos++] = (char)('0' + (val % 10u));
    }
    return pos;
}

static uint16_t buf_put_u16_pad4(char *dst, uint16_t pos, uint16_t cap, uint16_t val)
{
    if (val > 9999u) { val = 9999u; }
    if ((uint16_t)(pos + 4u) <= cap)
    {
        dst[pos++] = (char)('0' + ((val / 1000u) % 10u));
        dst[pos++] = (char)('0' + ((val / 100u)  % 10u));
        dst[pos++] = (char)('0' + ((val / 10u)   % 10u));
        dst[pos++] = (char)('0' + (val % 10u));
    }
    return pos;
}

static uint16_t buf_put_cup(char *dst, uint16_t pos, uint16_t cap, uint8_t row1, uint8_t col1)
{
    pos = buf_puts(dst, pos, cap, "\x1b[");
    pos = buf_put_u8_dec(dst, pos, cap, row1);
    if (pos < cap) { dst[pos++] = ';'; }
    pos = buf_put_u8_dec(dst, pos, cap, col1);
    if (pos < cap) { dst[pos++] = 'H'; }
    return pos;
}

static void frame_flush_if_needed(char *frame, uint16_t *pos, uint16_t cap)
{
    /* Keep >=112 B headroom so no single 97-byte ANSI line can ever truncate */
    if (*pos >= (uint16_t)(cap - 112u))
    {
        uart_write_all(UART_A0, frame, *pos);
        *pos = 0u;
    }
}

/*
 * Compact visual code for differential rendering (0..22).
 */
static uint8_t compute_cell_vis(uint8_t mx, uint8_t my)
{
    if (mx == s_px && my == s_py)
    {
        return (uint8_t)(1u + (s_dir & 0x03u)); /* 1=N, 2=E, 3=S, 4=W */
    }

    uint8_t cell = s_map[my][mx];
    if ((cell & TILE_F_EXPLORED) == 0u)
    {
        return 0u; /* Unexplored fog */
    }

    bool vis = ((cell & TILE_F_VISIBLE) != 0u);
    if (vis)
    {
        int8_t ei = find_enemy_at(mx, my);
        if (ei >= 0)
        {
            const dng_enemy_t *e = &s_enemies[ei];
            if (e->type == ENEMY_MIMIC && (e->flags & EF_AWAKE) == 0u)
            {
                return 14u; /* Disguised as Chest ($) */
            }
            if (e->type == ENEMY_ARCHER && (e->flags & EF_AIMING) != 0u)
            {
                return 22u; /* Telegraphed Archer (A!) */
            }
            return (uint8_t)(16u + e->type); /* 17=Slime..21=Mimic */
        }
    }

    uint8_t t = (uint8_t)(cell & TILE_TYPE_MASK);
    switch (t)
    {
        case DNG_WALL:         return vis ? 5u : 6u;
        case DNG_STAIRS:       return 7u;
        case DNG_DOOR_CLOSED:  return 8u;
        case DNG_DOOR_OPEN:    return 9u;
        case DNG_CRACKED_WALL: return 10u;
        case DNG_TRAP:
            if ((cell & TILE_F_REVEALED) != 0u) { return 11u; }
            return vis ? 15u : 16u; /* Hidden trap looks like floor */
        case DNG_SHRINE:       return ((cell & TILE_F_REVEALED) != 0u) ? 9u : 12u;
        case DNG_POTION:       return 13u;
        case DNG_MANA:         return 23u;
        case DNG_CHEST:        return 14u;
        default:               return vis ? 15u : 16u;
    }
}

static void vis_style(uint8_t vis, uint8_t *color_id, const char **ansi_seq, const char **glyph)
{
    switch (vis)
    {
        case 1u:  *color_id = 1u;  *ansi_seq = "\x1b[1;32m"; *glyph = "^^"; break;
        case 2u:  *color_id = 1u;  *ansi_seq = "\x1b[1;32m"; *glyph = ">>"; break;
        case 3u:  *color_id = 1u;  *ansi_seq = "\x1b[1;32m"; *glyph = "vv"; break;
        case 4u:  *color_id = 1u;  *ansi_seq = "\x1b[1;32m"; *glyph = "<<"; break;
        case 5u:  *color_id = 2u;  *ansi_seq = "\x1b[37m";   *glyph = "##"; break; /* Lit wall */
        case 6u:  *color_id = 3u;  *ansi_seq = "\x1b[90m";   *glyph = "##"; break; /* Dim wall */
        case 7u:  *color_id = 4u;  *ansi_seq = "\x1b[1;33m"; *glyph = " >"; break; /* Stairs */
        case 8u:  *color_id = 5u;  *ansi_seq = "\x1b[33m";   *glyph = "++"; break; /* Closed door */
        case 9u:  *color_id = 3u;  *ansi_seq = "\x1b[90m";   *glyph = " /"; break; /* Open door / spent shrine */
        case 10u: *color_id = 5u;  *ansi_seq = "\x1b[33m";   *glyph = "%%"; break; /* Cracked wall */
        case 11u: *color_id = 6u;  *ansi_seq = "\x1b[1;31m"; *glyph = " ^"; break; /* Spike trap */
        case 12u: *color_id = 7u;  *ansi_seq = "\x1b[1;35m"; *glyph = " &"; break; /* Shrine */
        case 13u: *color_id = 8u;  *ansi_seq = "\x1b[1;36m"; *glyph = " +"; break; /* Potion */
        case 14u: *color_id = 4u;  *ansi_seq = "\x1b[1;33m"; *glyph = " $"; break; /* Chest / dormant Mimic */
        case 15u: *color_id = 3u;  *ansi_seq = "\x1b[90m";   *glyph = " ."; break; /* Lit floor */
        case 16u: *color_id = 3u;  *ansi_seq = "\x1b[90m";   *glyph = "  "; break; /* Dim floor */
        case 17u: *color_id = 6u;  *ansi_seq = "\x1b[1;31m"; *glyph = "sl"; break; /* Slime */
        case 18u: *color_id = 7u;  *ansi_seq = "\x1b[1;35m"; *glyph = "SK"; break; /* Skeleton */
        case 19u: *color_id = 4u;  *ansi_seq = "\x1b[1;33m"; *glyph = "AR"; break; /* Archer */
        case 20u: *color_id = 8u;  *ansi_seq = "\x1b[1;36m"; *glyph = "WR"; break; /* Wraith */
        case 21u: *color_id = 6u;  *ansi_seq = "\x1b[1;31m"; *glyph = "MM"; break; /* Awake Mimic */
        case 22u: *color_id = 9u;  *ansi_seq = "\x1b[1;37;41m"; *glyph = "A!"; break; /* Aiming Archer */
        case 23u: *color_id = 10u; *ansi_seq = "\x1b[1;34m"; *glyph = " *"; break; /* Mana crystal */
        default:  *color_id = 0u;  *ansi_seq = "";           *glyph = "  "; break;
    }
}

static void render_sidebar(char *frame, uint16_t *pos, uint16_t cap)
{
    uint8_t i, listed = 0u;

    /* Row 2: HP & MP */
    *pos = buf_put_cup(frame, *pos, cap, 2u, 58u);
    *pos = buf_puts(frame, *pos, cap, "\x1b[1;37mHP:\x1b[0m");
    *pos = buf_put_u8_pad2(frame, *pos, cap, s_hp);
    *pos = buf_puts(frame, *pos, cap, "/");
    *pos = buf_put_u8_pad2(frame, *pos, cap, s_max_hp);
    *pos = buf_puts(frame, *pos, cap, " \x1b[1;34mMP:\x1b[0m");
    *pos = buf_put_u8_pad2(frame, *pos, cap, s_mana);
    *pos = buf_puts(frame, *pos, cap, "/");
    *pos = buf_put_u8_pad2(frame, *pos, cap, s_max_mana);
    *pos = buf_puts(frame, *pos, cap, "  ");
    frame_flush_if_needed(frame, pos, cap);

    /* Row 3: ATK & DEF */
    *pos = buf_put_cup(frame, *pos, cap, 3u, 58u);
    *pos = buf_puts(frame, *pos, cap, "\x1b[1;33mATK:\x1b[0m");
    *pos = buf_put_u8_pad2(frame, *pos, cap, s_atk);
    *pos = buf_puts(frame, *pos, cap, "  \x1b[1;36mDEF:\x1b[0m");
    *pos = buf_put_u8_pad2(frame, *pos, cap, s_def);
    *pos = buf_puts(frame, *pos, cap, "      ");
    frame_flush_if_needed(frame, pos, cap);

    /* Row 4: GOLD & Empower tag */
    *pos = buf_put_cup(frame, *pos, cap, 4u, 58u);
    *pos = buf_puts(frame, *pos, cap, "\x1b[1;33mGOLD:\x1b[0m");
    *pos = buf_put_u16_pad4(frame, *pos, cap, s_gold);
    *pos = buf_puts(frame, *pos, cap, s_empowered ? " \x1b[1;32m[EMP]\x1b[0m      " : "            ");
    frame_flush_if_needed(frame, pos, cap);

    /* Row 5: Facing & Pos */
    *pos = buf_put_cup(frame, *pos, cap, 5u, 58u);
    *pos = buf_puts(frame, *pos, cap, "\x1b[90mDIR:");
    if (*pos < cap) { frame[(*pos)++] = DIR_CH[s_dir & 0x03u]; }
    *pos = buf_puts(frame, *pos, cap, " POS:");
    *pos = buf_put_u8_pad2(frame, *pos, cap, s_px);
    *pos = buf_puts(frame, *pos, cap, ",");
    *pos = buf_put_u8_pad2(frame, *pos, cap, s_py);
    *pos = buf_puts(frame, *pos, cap, "      \x1b[0m");
    frame_flush_if_needed(frame, pos, cap);

    /* Row 6-7: Divider + Nearby Foes header */
    *pos = buf_put_cup(frame, *pos, cap, 6u, 58u);
    *pos = buf_puts(frame, *pos, cap, "\x1b[90m---------------------\x1b[0m");
    *pos = buf_put_cup(frame, *pos, cap, 7u, 58u);
    *pos = buf_puts(frame, *pos, cap, "\x1b[1;31mVISIBLE FOES:\x1b[0m        ");
    frame_flush_if_needed(frame, pos, cap);

    /* Rows 8-11: Up to 4 visible enemies with live HP */
    for (i = 0u; i < DNG_MAX_ENEMIES && listed < 4u; i++)
    {
        const dng_enemy_t *e = &s_enemies[i];
        if (e->type == ENEMY_NONE) { continue; }
        if (e->type == ENEMY_MIMIC && (e->flags & EF_AWAKE) == 0u) { continue; }
        if ((s_map[e->y][e->x] & TILE_F_VISIBLE) == 0u) { continue; }

        const dng_enemy_def_t *def = &k_enemy_defs[e->type];
        *pos = buf_put_cup(frame, *pos, cap, (uint8_t)(8u + listed), 58u);
        *pos = buf_puts(frame, *pos, cap, def->ansi);
        *pos = buf_puts(frame, *pos, cap, (e->flags & EF_AIMING) ? "A!" : def->glyph);
        *pos = buf_puts(frame, *pos, cap, "\x1b[0m ");
        *pos = buf_puts(frame, *pos, cap, def->name);
        *pos = buf_puts(frame, *pos, cap, " ");
        *pos = buf_put_u8_dec(frame, *pos, cap, e->hp);
        *pos = buf_puts(frame, *pos, cap, "/");
        *pos = buf_put_u8_dec(frame, *pos, cap, e->max_hp);
        *pos = buf_puts(frame, *pos, cap, "      ");
        frame_flush_if_needed(frame, pos, cap);
        listed++;
    }
    while (listed < 4u)
    {
        *pos = buf_put_cup(frame, *pos, cap, (uint8_t)(8u + listed), 58u);
        *pos = buf_puts(frame, *pos, cap, (listed == 0u) ? "\x1b[90m(none in sight)      \x1b[0m"
                                                         : "                     ");
        frame_flush_if_needed(frame, pos, cap);
        listed++;
    }
}

static void render_sidebar_static_legend(char *frame, uint16_t *pos, uint16_t cap)
{
    static const char *const k_leg[7] = {
        "\x1b[90m---------------------\x1b[0m",
        "\x1b[33m++\x1b[0mDoor  \x1b[33m%%\x1b[0mCrack(F)",
        "\x1b[1;31m ^\x1b[0mTrap  \x1b[1;35m &\x1b[0mShrine",
        "\x1b[1;36m +\x1b[0mPot   \x1b[1;34m *\x1b[0mMana",
        "\x1b[1;33m $\x1b[0mChest \x1b[1;33m >\x1b[0mStairs",
        "\x1b[1;31msl\x1b[0m/\x1b[1;35mSK\x1b[0m/\x1b[1;33mAR\x1b[0m/\x1b[1;36mWR\x1b[0m/\x1b[1;31mMM\x1b[0m Foes",
        "\x1b[90mM:FullMap C:Center\x1b[0m"
    };
    uint8_t i;
    for (i = 0u; i < 7u; i++)
    {
        *pos = buf_put_cup(frame, *pos, cap, (uint8_t)(12u + i), 58u);
        *pos = buf_puts(frame, *pos, cap, k_leg[i]);
        frame_flush_if_needed(frame, pos, cap);
    }
}

static void render_full_map_overlay(char *frame, uint16_t *pos, uint16_t cap)
{
    uint8_t r, c;

    *pos = buf_puts(frame, *pos, cap,
        "\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m FULL FLOOR MAP (48x22)   FL:");
    *pos = buf_put_u8_pad2(frame, *pos, cap, s_floor);
    *pos = buf_puts(frame, *pos, cap, "   PRESS M OR ANY MOVE KEY TO CLOSE              \x1b[0m");
    frame_flush_if_needed(frame, pos, cap);

    for (r = 0u; r < DNG_H; r++)
    {
        uint8_t active_color = 0u;
        *pos = buf_put_cup(frame, *pos, cap, (uint8_t)(r + 2u), 17u);
        for (c = 0u; c < DNG_W; c++)
        {
            uint8_t cid = 0u;
            const char *ansi = "";
            char ch = ' ';

            if (c == s_px && r == s_py)
            {
                cid = 1u; ansi = "\x1b[1;32m"; ch = '@';
            }
            else if ((s_map[r][c] & TILE_F_EXPLORED) != 0u)
            {
                int8_t ei = ((s_map[r][c] & TILE_F_VISIBLE) != 0u) ? find_enemy_at(c, r) : -1;
                if (ei >= 0)
                {
                    if (s_enemies[ei].type == ENEMY_MIMIC && (s_enemies[ei].flags & EF_AWAKE) == 0u)
                    {
                        cid = 4u; ansi = "\x1b[1;33m"; ch = '$'; /* Keep Mimic disguise */
                    }
                    else
                    {
                        cid = 2u; ansi = "\x1b[1;31m"; ch = '!';
                    }
                }
                else
                {
                    uint8_t t = tile_type(c, r);
                    switch (t)
                    {
                        case DNG_WALL:         cid = 3u; ansi = "\x1b[90m";   ch = '#'; break;
                        case DNG_STAIRS:       cid = 4u; ansi = "\x1b[1;33m"; ch = '>'; break;
                        case DNG_DOOR_CLOSED:  cid = 5u; ansi = "\x1b[33m";   ch = '+'; break;
                        case DNG_DOOR_OPEN:    cid = 3u; ansi = "\x1b[90m";   ch = '/'; break;
                        case DNG_CRACKED_WALL: cid = 5u; ansi = "\x1b[33m";   ch = '%'; break;
                        case DNG_TRAP:
                            if ((s_map[r][c] & TILE_F_REVEALED) != 0u)
                            { cid = 2u; ansi = "\x1b[1;31m"; ch = '^'; }
                            else
                            { cid = 3u; ansi = "\x1b[90m"; ch = '.'; }
                            break;
                        case DNG_SHRINE:       cid = 6u; ansi = "\x1b[1;35m"; ch = '&'; break;
                        case DNG_POTION:       cid = 7u; ansi = "\x1b[1;36m"; ch = '+'; break;
                        case DNG_MANA:         cid = 8u; ansi = "\x1b[1;34m"; ch = '*'; break;
                        case DNG_CHEST:        cid = 4u; ansi = "\x1b[1;33m"; ch = '$'; break;
                        default:               cid = 3u; ansi = "\x1b[90m";   ch = '.'; break;
                    }
                }
            }

            if (cid != active_color)
            {
                *pos = buf_puts(frame, *pos, cap, (cid == 0u) ? "\x1b[0m" : ansi);
                active_color = cid;
            }
            if (*pos < cap) { frame[(*pos)++] = ch; }
            frame_flush_if_needed(frame, pos, cap);
        }
        if (active_color != 0u)
        {
            *pos = buf_puts(frame, *pos, cap, "\x1b[0m");
        }
        frame_flush_if_needed(frame, pos, cap);
    }

    *pos = buf_put_cup(frame, *pos, cap, 24u, 1u);
    *pos = buf_puts(frame, *pos, cap,
        "\x1b[90m  @:You  >:Stairs  !:Foe  +:Door  %:Crack  ^:Trap  &:Shrine  $:Chest\x1b[0m");
}

static void render_if_dirty(void)
{
    static char frame[256];
    const uint16_t cap = (uint16_t)sizeof(frame);
    uint16_t pos = 0u;
    uint8_t vr, vc;

    if (!s_dirty)
    {
        return;
    }
    s_dirty = false;

    if (s_map_mode)
    {
        render_full_map_overlay(frame, &pos, cap);
        if (pos > 0u) { uart_write_all(UART_A0, frame, pos); }
        return;
    }

    if (s_full_redraw)
    {
        s_full_redraw = false;

        /* Row 1: Top banner (80 cols) */
        pos = buf_puts(frame, pos, cap, "\x1b[2J\x1b[H\x1b[?25l\x1b[1;37;44m MSP430 DUNGEON  FL:");
        pos = buf_put_u8_pad2(frame, pos, cap, s_floor);
        pos = buf_puts(frame, pos, cap, "  BEST:");
        pos = buf_put_u8_pad2(frame, pos, cap, (uint8_t)s_best_floor);
        pos = buf_puts(frame, pos, cap, is_cave_floor(s_floor)
            ? "  BIOME: FUNGAL CAVERNS                          \x1b[0m"
            : "  BIOME: STONE CRYPT                             \x1b[0m");
        frame_flush_if_needed(frame, &pos, cap);

        /* Rows 2..18: 28x17 viewport + col 57 vertical divider */
        for (vr = 0u; vr < DNG_VIEW_H; vr++)
        {
            uint8_t active_color = 0u;
            pos = buf_put_cup(frame, pos, cap, (uint8_t)(vr + 2u), 1u);
            for (vc = 0u; vc < DNG_VIEW_W; vc++)
            {
                uint8_t vis = compute_cell_vis((uint8_t)(s_cam_x + vc), (uint8_t)(s_cam_y + vr));
                uint8_t color_id;
                const char *ansi_seq;
                const char *glyph;

                s_prev_vis[vr][vc] = vis;
                vis_style(vis, &color_id, &ansi_seq, &glyph);

                if (color_id != active_color)
                {
                    pos = buf_puts(frame, pos, cap, (color_id == 0u) ? "\x1b[0m" : ansi_seq);
                    active_color = color_id;
                }
                pos = buf_puts(frame, pos, cap, glyph);
                frame_flush_if_needed(frame, &pos, cap);
            }
            pos = buf_puts(frame, pos, cap, "\x1b[90m|\x1b[0m");
            frame_flush_if_needed(frame, &pos, cap);
        }

        render_sidebar(frame, &pos, cap);
        render_sidebar_static_legend(frame, &pos, cap);

        /* Row 19: Divider, Row 20: Status, Row 21: Controls */
        pos = buf_put_cup(frame, pos, cap, 19u, 1u);
        pos = buf_puts(frame, pos, cap,
            "\x1b[90m--------------------------------------------------------------------------------\x1b[0m");
        frame_flush_if_needed(frame, &pos, cap);

        pos = buf_put_cup(frame, pos, cap, 20u, 1u);
        pos = buf_puts(frame, pos, cap, "\x1b[1;33m");
        pos = buf_puts(frame, pos, cap, s_status_msg);
        pos = buf_puts(frame, pos, cap, "\x1b[0m");
        frame_flush_if_needed(frame, &pos, cap);

        pos = buf_put_cup(frame, pos, cap, 21u, 1u);
        pos = buf_puts(frame, pos, cap,
            "\x1b[90mWASD/ARROWS:MOVE  F:BOLT  SPACE:MEDITATE(+EMP)  M:MAP  C:CENTER  P/R:SAVE/LOAD\x1b[0m");
    }
    else
    {
        /* Differential redraw: only changed viewport cells + sidebar stats + status */
        uint8_t active_color = 0u;
        uint8_t cur_r = 255u;
        uint8_t cur_c = 255u;

        pos = buf_puts(frame, pos, cap, "\x1b[?25l");
        for (vr = 0u; vr < DNG_VIEW_H; vr++)
        {
            for (vc = 0u; vc < DNG_VIEW_W; vc++)
            {
                uint8_t vis = compute_cell_vis((uint8_t)(s_cam_x + vc), (uint8_t)(s_cam_y + vr));
                if (vis == s_prev_vis[vr][vc]) { continue; }
                s_prev_vis[vr][vc] = vis;

                uint8_t color_id;
                const char *ansi_seq;
                const char *glyph;
                vis_style(vis, &color_id, &ansi_seq, &glyph);

                if (vr != cur_r || vc != cur_c)
                {
                    pos = buf_put_cup(frame, pos, cap, (uint8_t)(vr + 2u), (uint8_t)(vc * 2u + 1u));
                }
                if (color_id != active_color)
                {
                    pos = buf_puts(frame, pos, cap, (color_id == 0u) ? "\x1b[0m" : ansi_seq);
                    active_color = color_id;
                }
                pos = buf_puts(frame, pos, cap, glyph);
                cur_r = vr;
                cur_c = (uint8_t)(vc + 1u);
                frame_flush_if_needed(frame, &pos, cap);
            }
        }

        if (active_color != 0u)
        {
            pos = buf_puts(frame, pos, cap, "\x1b[0m");
        }

        render_sidebar(frame, &pos, cap);

        pos = buf_put_cup(frame, pos, cap, 20u, 1u);
        pos = buf_puts(frame, pos, cap, "\x1b[1;33m");
        pos = buf_puts(frame, pos, cap, s_status_msg);
        pos = buf_puts(frame, pos, cap, "\x1b[0m");
    }

    if (pos > 0u)
    {
        uart_write_all(UART_A0, frame, pos);
    }
}

/* --------------------------------------------------------------------------
 * Input Queue & Main Turn Processor
 * -------------------------------------------------------------------------- */

static bool pop_cmd(uint8_t *out_cmd)
{
    uint16_t sr = hal_enter_critical();
    if (s_cmd_head == s_cmd_tail)
    {
        hal_exit_critical(sr);
        return false;
    }
    *out_cmd = s_cmd_q[s_cmd_tail];
    s_cmd_tail = (uint8_t)((s_cmd_tail + 1u) & CMD_QUEUE_MASK);
    hal_exit_critical(sr);
    return true;
}

static void handle_command(uint8_t cmd)
{
    if (cmd != CMD_STEP_FWD && cmd != CMD_ROTATE && !s_terminal_connected)
    {
        s_terminal_connected = true;
        s_full_redraw = true;
    }
    s_missed_probes = 0u;

    /* Manual full-screen redraw without advancing a turn */
    if (cmd == 0x0Cu || cmd == '\r' || cmd == '\n' || cmd == 'l' || cmd == 'L')
    {
        s_full_redraw = true;
        s_dirty = true;
        return;
    }

    /* Toggle 1:1 full-floor map overlay */
    if (cmd == 'm' || cmd == 'M')
    {
        s_map_mode = !s_map_mode;
        s_full_redraw = true;
        s_dirty = true;
        return;
    }

    /* Any other command exits map overlay first */
    if (s_map_mode)
    {
        s_map_mode = false;
        s_full_redraw = true;
        s_dirty = true;
        if (cmd == ' ') { return; }
    }

    if (cmd == 'c' || cmd == 'C')
    {
        update_camera(true);
        set_status("CAMERA RE-CENTERED.");
        s_dirty = true;
        return;
    }

    if (cmd == 'p' || cmd == 'P')
    {
        if (s_game_over)
        {
            set_status("CANNOT SAVE WHILE DEAD! PRESS R TO LOAD OR W/SPACE FOR NEW RUN.");
        }
        else if (commit_checkpoint(true))
        {
            set_status("CHECKPOINT SAVED TO FRAM!");
        }
        else
        {
            set_status("FRAM SAVE FAILED!");
        }
        s_dirty = true;
    }
    else if (cmd == 'r' || cmd == 'R')
    {
        if (!load_checkpoint(true))
        {
            set_status("NO VALID FRAM SAVE FOUND.");
            s_dirty = true;
        }
    }
    else if (cmd == 'x' || cmd == 'X')
    {
        ckpt_erase();
        s_best_floor    = (uint16_t)s_floor;
        s_has_saved_run = false;
        set_status("FRAM CHECKPOINTS ERASED.");
        s_dirty = true;
    }
    else if (s_game_over)
    {
        if (cmd == 'w' || cmd == 'W' || cmd == ' ' || cmd == CMD_STEP_FWD)
        {
            start_new_run();
        }
    }
    else
    {
        switch (cmd)
        {
            case CMD_ROTATE:
            case 'e':
            case 'E':
                s_dir = (uint8_t)((s_dir + 1u) & 0x03u);
                s_dirty = true;
                break;
            case CMD_STEP_FWD:
                step_in_direction(s_dir);
                break;
            case 'w':
            case 'W':
                step_in_direction(0u);
                break;
            case 'a':
            case 'A':
                step_in_direction(3u);
                break;
            case 's':
            case 'S':
                step_in_direction(2u);
                break;
            case 'd':
            case 'D':
                step_in_direction(1u);
                break;
            case 'f':
            case 'F':
                cast_arcane_bolt();
                break;
            case ' ':
                if (s_mana < s_max_mana)
                {
                    s_mana++;
                }
                s_empowered = true;
                set_status("MEDITATED (+1 MP) & EMPOWERED NEXT ATTACK (+50% DMG)!");
                monster_turn();
                s_dirty = true;
                break;
            default:
                break;
        }
    }
}

void dungeon_game_process(void)
{
    uint8_t b;
    uint16_t sr = hal_enter_critical();
    bool do_probe = s_probe_due;
    s_probe_due = false;
    hal_exit_critical(sr);

    while (pop_cmd(&b))
    {
        if (s_esc_state == 1u)
        {
            if (b == '[')
            {
                s_esc_state = 2u;
                continue;
            }
            s_esc_state = (b == 0x1Bu) ? 1u : 0u;
            continue;
        }
        else if (s_esc_state == 2u)
        {
            if (b == 'A')      { s_esc_state = 0u; handle_command('w'); continue; }
            else if (b == 'B') { s_esc_state = 0u; handle_command('s'); continue; }
            else if (b == 'C') { s_esc_state = 0u; handle_command('d'); continue; }
            else if (b == 'D') { s_esc_state = 0u; handle_command('a'); continue; }
            else if (b == '0') { s_esc_state = 3u; continue; }
            s_esc_state = 0u;
            continue;
        }
        else if (s_esc_state == 3u)
        {
            s_esc_state = 0u;
            if (b == 'n')
            {
                /* VT100 Device Status Report reply (\x1b[0n): terminal is present */
                s_missed_probes = 0u;
                if (!s_terminal_connected)
                {
                    s_terminal_connected = true;
                    s_full_redraw = true;
                    s_dirty = true;
                }
            }
            continue;
        }

        if (b == 0x1Bu)
        {
            s_esc_state = 1u;
            continue;
        }

        handle_command(b);
    }

    if (do_probe)
    {
        if (s_missed_probes < 3u)
        {
            s_missed_probes++;
        }
        if (s_missed_probes >= 2u)
        {
            s_terminal_connected = false;
        }
        if (!s_dirty && uart_tx_idle(UART_A0))
        {
            static const char dsr_query[4] = "\x1b[5n";
            (void)uart_write(UART_A0, dsr_query, 4u);
        }
    }

    render_if_dirty();
}
