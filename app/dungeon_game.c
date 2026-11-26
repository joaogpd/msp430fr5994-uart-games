#include "app/dungeon_game.h"
#include "drivers/msp430/uart.h"
#include "drivers/msp430/ckpt.h"
#include "drivers/msp430/timer.h"
#include "drivers/msp430/hal_common.h"
#include "drivers/board/board.h"
#include <string.h>

#define DEBOUNCE_PERIOD_MS  40u
#define HOLD_TICKS_1S       25u     /* 25 * 40 ms = 1.0 s */

/*
 * Persisted dungeon run stored in FRAM via ckpt.
 * Total size: 204 bytes (<= CKPT_MAX_PAYLOAD of 256 bytes).
 */
typedef struct
{
    uint16_t best_floor;
    uint16_t gold;
    uint16_t rng_state;
    uint8_t  floor;
    uint8_t  hp;
    uint8_t  max_hp;
    uint8_t  mana;
    uint8_t  max_mana;
    uint8_t  atk;
    uint8_t  px;
    uint8_t  py;
    uint8_t  dir;           /* 0=N, 1=E, 2=S, 3=W */
    uint8_t  has_saved_run;
    uint8_t  packed_grid[DNG_PACKED_BYTES];
    uint8_t  explored_bits[DNG_FOG_BYTES];
} dng_ckpt_t;

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

/* Active runtime state in SRAM */
static uint8_t  s_grid[DNG_H][DNG_W];
static uint8_t  s_explored[DNG_FOG_BYTES];
static uint8_t  s_px;
static uint8_t  s_py;
static uint8_t  s_dir;
static uint8_t  s_floor;
static uint16_t s_best_floor;
static uint16_t s_gold;
static uint8_t  s_hp;
static uint8_t  s_max_hp;
static uint8_t  s_mana;
static uint8_t  s_max_mana;
static uint8_t  s_atk;
static bool     s_game_over;
static bool     s_has_saved_run;
static bool     s_dirty;
static uint16_t s_rng = 0x5D17u;
static char     s_status_msg[42];

static dng_btn_t s_btn;
static volatile uint8_t s_pending_cmd = 0u;

static const int8_t DIR_DX[4] = {  0,  1,  0, -1 };
static const int8_t DIR_DY[4] = { -1,  0,  1,  0 };

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

static void set_status(const char *msg)
{
    uint8_t i = 0u;
    while (msg[i] != '\0' && i < (uint8_t)(sizeof(s_status_msg) - 1u))
    {
        s_status_msg[i] = msg[i];
        i++;
    }
    while (i < (uint8_t)(sizeof(s_status_msg) - 1u))
    {
        s_status_msg[i++] = ' ';
    }
    s_status_msg[sizeof(s_status_msg) - 1u] = '\0';
}

static bool is_explored(uint8_t x, uint8_t y)
{
    uint16_t idx = (uint16_t)((uint16_t)y * DNG_W + (uint16_t)x);
    return ((s_explored[idx >> 3] >> (idx & 7u)) & 1u) != 0u;
}

static void mark_explored(uint8_t x, uint8_t y)
{
    uint16_t idx = (uint16_t)((uint16_t)y * DNG_W + (uint16_t)x);
    s_explored[idx >> 3] |= (uint8_t)(1u << (idx & 7u));
}

static void reveal_around_player(void)
{
    uint8_t r, c;
    for (r = 0u; r < DNG_H; r++)
    {
        for (c = 0u; c < DNG_W; c++)
        {
            uint8_t dx = (c > s_px) ? (uint8_t)(c - s_px) : (uint8_t)(s_px - c);
            uint8_t dy = (r > s_py) ? (uint8_t)(r - s_py) : (uint8_t)(s_py - r);
            if ((dx + dy) <= 4u)
            {
                mark_explored(c, r);
            }
        }
    }
}

static void pack_grid(uint8_t out_packed[DNG_PACKED_BYTES])
{
    uint16_t idx = 0u;
    uint8_t r, c;

    for (r = 0u; r < DNG_H; r++)
    {
        for (c = 0u; c < DNG_W; c += 2u)
        {
            uint8_t hi = s_grid[r][c] & 0x0Fu;
            uint8_t lo = s_grid[r][c + 1u] & 0x0Fu;
            out_packed[idx++] = (uint8_t)((hi << 4) | lo);
        }
    }
}

static void unpack_grid(const uint8_t in_packed[DNG_PACKED_BYTES])
{
    uint16_t idx = 0u;
    uint8_t r, c;

    for (r = 0u; r < DNG_H; r++)
    {
        for (c = 0u; c < DNG_W; c += 2u)
        {
            uint8_t byte = in_packed[idx++];
            s_grid[r][c]      = (byte >> 4) & 0x0Fu;
            s_grid[r][c + 1u] = byte & 0x0Fu;
        }
    }
}

static bool commit_checkpoint(bool save_active_run)
{
    dng_ckpt_t snap;

    if ((uint16_t)s_floor > s_best_floor)
    {
        s_best_floor = (uint16_t)s_floor;
    }
    if (save_active_run && !s_game_over)
    {
        s_has_saved_run = true;
    }

    snap.best_floor    = s_best_floor;
    snap.gold          = s_gold;
    snap.rng_state     = s_rng;
    snap.floor         = s_floor;
    snap.hp            = s_hp;
    snap.max_hp        = s_max_hp;
    snap.mana          = s_mana;
    snap.max_mana      = s_max_mana;
    snap.atk           = s_atk;
    snap.px            = s_px;
    snap.py            = s_py;
    snap.dir           = s_dir;
    snap.has_saved_run = s_has_saved_run ? 1u : 0u;
    pack_grid(snap.packed_grid);
    memcpy(snap.explored_bits, s_explored, sizeof(s_explored));

    /* Keep LED2 off after save so it does not draw ~1.5 mA in LPM3 */
    bool ok = ckpt_save(&snap, sizeof(snap));
    board_led2(false);
    return ok;
}

static bool load_checkpoint(bool restore_full_run)
{
    dng_ckpt_t snap;
    uint32_t seq = 0u;

    if (!ckpt_restore(&snap, sizeof(snap), &seq))
    {
        return false;
    }

    s_best_floor    = snap.best_floor;
    s_has_saved_run = (snap.has_saved_run != 0u);

    if (restore_full_run && s_has_saved_run)
    {
        s_gold      = snap.gold;
        s_rng       = (snap.rng_state != 0u) ? snap.rng_state : 0x5D17u;
        s_floor     = snap.floor;
        s_hp        = snap.hp;
        s_max_hp    = snap.max_hp;
        s_mana      = snap.mana;
        s_max_mana  = snap.max_mana;
        s_atk       = snap.atk;
        s_px        = (snap.px < DNG_W) ? snap.px : 2u;
        s_py        = (snap.py < DNG_H) ? snap.py : 2u;
        s_dir       = snap.dir & 0x03u;
        s_game_over = false;
        unpack_grid(snap.packed_grid);
        memcpy(s_explored, snap.explored_bits, sizeof(s_explored));
        board_led1(false);
        board_led2(false);
        set_status("RESTORED CHECKPOINT FROM FRAM!");
        s_dirty = true;
        return true;
    }

    return false;
}

static void carve_room(uint8_t x0, uint8_t y0, uint8_t w, uint8_t h)
{
    uint8_t r, c;
    for (r = y0; r < (uint8_t)(y0 + h) && r < (DNG_H - 1u); r++)
    {
        for (c = x0; c < (uint8_t)(x0 + w) && c < (DNG_W - 1u); c++)
        {
            s_grid[r][c] = DNG_FLOOR;
        }
    }
}

static void carve_corridor(uint8_t x0, uint8_t y0, uint8_t x1, uint8_t y1)
{
    uint8_t x = x0;
    uint8_t y = y0;

    while (x != x1)
    {
        s_grid[y][x] = DNG_FLOOR;
        x = (x < x1) ? (uint8_t)(x + 1u) : (uint8_t)(x - 1u);
    }
    while (y != y1)
    {
        s_grid[y][x] = DNG_FLOOR;
        y = (y < y1) ? (uint8_t)(y + 1u) : (uint8_t)(y - 1u);
    }
    s_grid[y][x] = DNG_FLOOR;
}

static void generate_floor(void)
{
    static const uint8_t rx[4] = { 1u, 11u, 11u,  2u };
    static const uint8_t ry[4] = { 1u,  1u,  8u,  8u };
    uint8_t cx[4];
    uint8_t cy[4];
    uint8_t i, r, c;

    memset(s_grid, DNG_WALL, sizeof(s_grid));
    memset(s_explored, 0, sizeof(s_explored));

    for (i = 0u; i < 4u; i++)
    {
        uint8_t rw = (uint8_t)(5u + (next_rand() % 3u));
        uint8_t rh = (uint8_t)(4u + (next_rand() % 2u));
        carve_room(rx[i], ry[i], rw, rh);
        cx[i] = (uint8_t)(rx[i] + (rw / 2u));
        cy[i] = (uint8_t)(ry[i] + (rh / 2u));
    }

    for (i = 0u; i < 4u; i++)
    {
        uint8_t next = (uint8_t)((i + 1u) & 0x03u);
        carve_corridor(cx[i], cy[i], cx[next], cy[next]);
    }

    s_px = cx[0];
    s_py = cy[0];
    s_grid[cy[2]][cx[2]] = DNG_STAIRS;

    uint8_t monsters_to_spawn = (uint8_t)(4u + s_floor);
    if (monsters_to_spawn > 12u) monsters_to_spawn = 12u;

    for (i = 0u; i < monsters_to_spawn; i++)
    {
        r = (uint8_t)(1u + (next_rand() % (DNG_H - 2u)));
        c = (uint8_t)(1u + (next_rand() % (DNG_W - 2u)));
        if (s_grid[r][c] == DNG_FLOOR && (c != s_px || r != s_py))
        {
            uint8_t roll = (uint8_t)(next_rand() & 0x07u);
            if (roll < 6u && roll >= 4u && s_floor >= 2u)
                s_grid[r][c] = DNG_SKELETON;
            else
                s_grid[r][c] = DNG_SLIME;
        }
    }

    for (i = 0u; i < 4u; i++)
    {
        r = (uint8_t)(1u + (next_rand() % (DNG_H - 2u)));
        c = (uint8_t)(1u + (next_rand() % (DNG_W - 2u)));
        if (s_grid[r][c] == DNG_FLOOR && (c != s_px || r != s_py))
        {
            if (i == 0u)      s_grid[r][c] = DNG_POTION;
            else if (i == 1u) s_grid[r][c] = DNG_MANA;
            else              s_grid[r][c] = DNG_CHEST;
        }
    }

    reveal_around_player();
}

static void start_new_run(void)
{
    s_floor     = 1u;
    s_gold      = 0u;
    s_max_hp    = 12u;
    s_hp        = 12u;
    s_max_mana  = 5u;
    s_mana      = 5u;
    s_atk       = 2u;
    s_dir       = 1u;
    s_game_over = false;

    board_led1(false);
    board_led2(false);
    generate_floor();
    set_status("EXPLORE THE DUNGEON! FIND STAIRS (>)");
    s_dirty = true;
}

void dungeon_game_init(void)
{
    memset(&s_btn, 0, sizeof(s_btn));
    s_best_floor    = 1u;
    s_has_saved_run = false;

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
    s_pending_cmd = cmd;
    return true;
}

/*
 * Triggered by GPIO falling-edge interrupt on S1 (P5.6) or S2 (P5.5).
 * Starts the 40 ms debounce timer only if it is not already running.
 */
bool dungeon_game_on_button_irq(void)
{
    if (!timer_running(TIMER_A0))
    {
        (void)timer_start_oneshot_ms(TIMER_A0, DEBOUNCE_PERIOD_MS, dungeon_game_on_debounce_timer);
    }
    return false; /* Stay in LPM3 until debounce timer confirms button state */
}

/*
 * Runs every 40 ms ONLY while S1/S2 are pressed or settling.
 * Once both buttons stabilize as released, does not re-arm (0 Hz idle).
 */
bool dungeon_game_on_debounce_timer(void)
{
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
                cmd = 'e';
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
                cmd = 'w';
            }
        }
    }

    /* Re-arm one-shot timer only if buttons are still active or settling */
    if (raw_s1 || raw_s2 || s1 || s2 || s_btn.s1_hist != 0u || s_btn.s2_hist != 0u)
    {
        (void)timer_start_oneshot_ms(TIMER_A0, DEBOUNCE_PERIOD_MS, dungeon_game_on_debounce_timer);
    }

    if (cmd != 0u)
    {
        s_pending_cmd = cmd;
        return true; /* Wake main() from LPM3 to execute turn */
    }
    return false;
}

static bool tile_is_enemy(uint8_t t)
{
    return (t == DNG_SLIME || t == DNG_SKELETON);
}

static void monster_turn(void)
{
    uint8_t moved[DNG_H][DNG_W];
    uint8_t r, c;
    memset(moved, 0, sizeof(moved));

    for (r = 1u; r < (DNG_H - 1u); r++)
    {
        for (c = 1u; c < (DNG_W - 1u); c++)
        {
            uint8_t m = s_grid[r][c];
            if (!tile_is_enemy(m) || moved[r][c] != 0u)
            {
                continue;
            }

            uint8_t dx = (c > s_px) ? (uint8_t)(c - s_px) : (uint8_t)(s_px - c);
            uint8_t dy = (r > s_py) ? (uint8_t)(r - s_py) : (uint8_t)(s_py - r);

            if ((dx + dy) > 6u)
            {
                continue;
            }

            if ((dx + dy) == 1u)
            {
                uint8_t dmg = (m == DNG_SKELETON) ? 2u : 1u;
                if (s_hp > dmg)
                {
                    s_hp = (uint8_t)(s_hp - dmg);
                    set_status((m == DNG_SKELETON) ? "SKELETON SLASHES YOU (-2 HP)!"
                                                   : "SLIME BITES YOU (-1 HP)!");
                }
                else
                {
                    s_hp = 0u;
                    s_game_over = true;
                    board_led1(true);
                    (void)commit_checkpoint(false);
                    set_status("YOU DIED! S2=NEW RUN  HOLD S1=LOAD");
                }
                return;
            }

            int8_t step_x = 0;
            int8_t step_y = 0;
            if (dx >= dy)
                step_x = (s_px > c) ? 1 : -1;
            else
                step_y = (s_py > r) ? 1 : -1;

            uint8_t nx = (uint8_t)((int8_t)c + step_x);
            uint8_t ny = (uint8_t)((int8_t)r + step_y);
            if (s_grid[ny][nx] == DNG_FLOOR && (nx != s_px || ny != s_py))
            {
                s_grid[r][c]   = DNG_FLOOR;
                s_grid[ny][nx] = m;
                moved[ny][nx]  = 1u;
            }
        }
    }
}

static void cast_arcane_bolt(void)
{
    if (s_mana == 0u)
    {
        set_status("NO MANA! HOLD S1 TO MEDITATE (+1 MP)");
        s_dirty = true;
        return;
    }

    s_mana--;
    int8_t x = (int8_t)s_px;
    int8_t y = (int8_t)s_py;

    while (1)
    {
        x = (int8_t)(x + DIR_DX[s_dir]);
        y = (int8_t)(y + DIR_DY[s_dir]);
        if (x <= 0 || x >= (int8_t)(DNG_W - 1u) || y <= 0 || y >= (int8_t)(DNG_H - 1u))
        {
            break;
        }
        uint8_t tile = s_grid[(uint8_t)y][(uint8_t)x];
        if (tile == DNG_WALL)
        {
            set_status("ARCANE BOLT HIT A WALL.");
            break;
        }
        if (tile_is_enemy(tile))
        {
            s_grid[(uint8_t)y][(uint8_t)x] = DNG_FLOOR;
            uint16_t reward = (tile == DNG_SKELETON) ? 15u : 8u;
            s_gold = (uint16_t)(s_gold + reward);
            set_status("ZAP! ARCANE BOLT VAPORIZED FOE!");
            break;
        }
    }

    monster_turn();
    s_dirty = true;
}

static void step_in_direction(uint8_t dir)
{
    s_dir = dir & 0x03u;
    uint8_t nx = (uint8_t)((int8_t)s_px + DIR_DX[s_dir]);
    uint8_t ny = (uint8_t)((int8_t)s_py + DIR_DY[s_dir]);

    if (nx >= DNG_W || ny >= DNG_H)
    {
        return;
    }

    uint8_t target = s_grid[ny][nx];

    if (target == DNG_WALL)
    {
        set_status("BLOCKED BY STONE WALL.");
        s_dirty = true;
        return;
    }

    if (tile_is_enemy(target))
    {
        s_grid[ny][nx] = DNG_FLOOR;
        uint16_t loot = (target == DNG_SKELETON) ? 12u : 6u;
        s_gold = (uint16_t)(s_gold + loot);
        set_status((target == DNG_SKELETON) ? "SMASHED SKELETON! (+12 GOLD)"
                                            : "DEFEATED SLIME! (+6 GOLD)");
        monster_turn();
        s_dirty = true;
        return;
    }

    s_px = nx;
    s_py = ny;
    reveal_around_player();

    if (target == DNG_POTION)
    {
        s_grid[ny][nx] = DNG_FLOOR;
        s_hp = (uint8_t)(s_hp + 4u);
        if (s_hp > s_max_hp) s_hp = s_max_hp;
        set_status("DRANK HEALTH POTION (+4 HP)!");
    }
    else if (target == DNG_MANA)
    {
        s_grid[ny][nx] = DNG_FLOOR;
        s_mana = (uint8_t)(s_mana + 3u);
        if (s_mana > s_max_mana) s_mana = s_max_mana;
        set_status("ABSORBED MANA CRYSTAL (+3 MP)!");
    }
    else if (target == DNG_CHEST)
    {
        s_grid[ny][nx] = DNG_FLOOR;
        s_gold = (uint16_t)(s_gold + 25u);
        s_max_hp++;
        s_hp++;
        set_status("TREASURE CHEST! +25G & +1 MAX HP!");
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
        set_status("DESCENDED STAIRS! FLOOR SAVED TO FRAM");
        s_dirty = true;
        return;
    }
    else
    {
        set_status("S1:TURN  S2:STEP/ATK  HOLD S2:BOLT");
    }

    monster_turn();
    s_dirty = true;
}

/* Zero-snprintf, ULP-compliant buffer helpers */
static uint16_t buf_puts(char *dst, uint16_t pos, uint16_t cap, const char *str)
{
    while (*str != '\0' && pos < cap)
    {
        dst[pos++] = *str++;
    }
    return pos;
}

static uint16_t buf_put_u16_pad3(char *dst, uint16_t pos, uint16_t cap, uint16_t val)
{
    if ((uint16_t)(pos + 3u) <= cap)
    {
        dst[pos++] = (char)('0' + ((val / 100u) % 10u));
        dst[pos++] = (char)('0' + ((val / 10u)  % 10u));
        dst[pos++] = (char)('0' + (val % 10u));
    }
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

static void render_if_dirty(void)
{
    static char frame[1024];
    const uint16_t cap = (uint16_t)sizeof(frame);
    uint16_t pos = 0u;
    uint8_t r, c;

    if (!s_dirty)
    {
        return;
    }
    s_dirty = false;

    pos = buf_puts(frame, pos, cap, "\x1b[H\x1b[?25l\x1b[1;37;44m FL:");
    pos = buf_put_u8_pad2(frame, pos, cap, s_floor);
    pos = buf_puts(frame, pos, cap, " HP:");
    pos = buf_put_u8_pad2(frame, pos, cap, s_hp);
    pos = buf_puts(frame, pos, cap, "/");
    pos = buf_put_u8_pad2(frame, pos, cap, s_max_hp);
    pos = buf_puts(frame, pos, cap, " MP:");
    pos = buf_put_u8_pad2(frame, pos, cap, s_mana);
    pos = buf_puts(frame, pos, cap, " G:");
    pos = buf_put_u16_pad3(frame, pos, cap, s_gold);
    pos = buf_puts(frame, pos, cap, " BEST:");
    pos = buf_put_u8_pad2(frame, pos, cap, (uint8_t)s_best_floor);
    pos = buf_puts(frame, pos, cap, " \x1b[0m\r\n");

    for (r = 0u; r < DNG_H; r++)
    {
        uint8_t active_color = 0u;
        for (c = 0u; c < DNG_W; c++)
        {
            uint8_t color_id = 0u;
            const char *ansi_seq = "";
            const char *glyph = "  ";

            if (c == s_px && r == s_py)
            {
                color_id = 1u;
                ansi_seq = "\x1b[1;32m";
                if (s_dir == 0u)      glyph = "^^";
                else if (s_dir == 1u) glyph = ">>";
                else if (s_dir == 2u) glyph = "vv";
                else                  glyph = "<<";
            }
            else if (!is_explored(c, r))
            {
                color_id = 0u;
                glyph = "  ";
            }
            else
            {
                uint8_t dx = (c > s_px) ? (uint8_t)(c - s_px) : (uint8_t)(s_px - c);
                uint8_t dy = (r > s_py) ? (uint8_t)(r - s_py) : (uint8_t)(s_py - r);
                bool in_sight = ((dx + dy) <= 4u);
                uint8_t tile = s_grid[r][c];

                switch (tile)
                {
                    case DNG_WALL:
                        color_id = 2u; ansi_seq = "\x1b[90m"; glyph = "##";
                        break;
                    case DNG_STAIRS:
                        color_id = 3u; ansi_seq = "\x1b[1;33m"; glyph = " >";
                        break;
                    case DNG_SLIME:
                        if (in_sight) { color_id = 4u; ansi_seq = "\x1b[1;31m"; glyph = "sl"; }
                        else          { color_id = 2u; ansi_seq = "\x1b[90m";   glyph = " ."; }
                        break;
                    case DNG_SKELETON:
                        if (in_sight) { color_id = 5u; ansi_seq = "\x1b[1;35m"; glyph = "SK"; }
                        else          { color_id = 2u; ansi_seq = "\x1b[90m";   glyph = " ."; }
                        break;
                    case DNG_POTION:
                        color_id = 6u; ansi_seq = "\x1b[1;36m"; glyph = " +";
                        break;
                    case DNG_MANA:
                        color_id = 7u; ansi_seq = "\x1b[1;34m"; glyph = " *";
                        break;
                    case DNG_CHEST:
                        color_id = 3u; ansi_seq = "\x1b[1;33m"; glyph = " $";
                        break;
                    default:
                        color_id = 2u; ansi_seq = "\x1b[90m";
                        glyph = in_sight ? " ." : "  ";
                        break;
                }
            }

            if (color_id != active_color)
            {
                pos = buf_puts(frame, pos, cap, (color_id == 0u) ? "\x1b[0m" : ansi_seq);
                active_color = color_id;
            }
            pos = buf_puts(frame, pos, cap, glyph);
        }

        if (active_color != 0u)
        {
            pos = buf_puts(frame, pos, cap, "\x1b[0m");
        }
        pos = buf_puts(frame, pos, cap, "\r\n");
    }

    pos = buf_puts(frame, pos, cap, "\x1b[1;33m");
    pos = buf_puts(frame, pos, cap, s_status_msg);
    pos = buf_puts(frame, pos, cap, "\x1b[0m\r\n\x1b[90mS1:TURN(HOLD:WAIT) S2:STEP/ATK(HOLD:BOLT)\x1b[0m\r\n");

    uart_write_all(UART_A0, frame, pos);
    (void)uart_flush(UART_A0); /* Ensure last byte leaves TX pin before entering LPM3 */
}

void dungeon_game_process(void)
{
    uint16_t sr = hal_enter_critical();
    uint8_t cmd = s_pending_cmd;
    s_pending_cmd = 0u;
    hal_exit_critical(sr);

    if (cmd != 0u)
    {
        if (cmd == 'p' || cmd == 'P')
        {
            if (commit_checkpoint(true))
                set_status("CHECKPOINT SAVED TO FRAM!");
            else
                set_status("FRAM SAVE FAILED!");
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
            if (cmd == 'w' || cmd == 'W' || cmd == ' ')
            {
                start_new_run();
            }
        }
        else
        {
            switch (cmd)
            {
                case 'e':
                case 'E':
                    s_dir = (uint8_t)((s_dir + 1u) & 0x03u);
                    s_dirty = true;
                    break;
                case 'w':
                case 'W':
                    step_in_direction(s_dir);
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
                    set_status("MEDITATED 1 TURN (+1 MANA).");
                    monster_turn();
                    s_dirty = true;
                    break;
                default:
                    break;
            }
        }
    }

    render_if_dirty();
}
