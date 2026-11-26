#include "app/space_game.h"
#include "drivers/msp430/uart.h"
#include "drivers/msp430/ckpt.h"
#include "drivers/msp430/hal_common.h"
#include "drivers/board/board.h"
#include <string.h>

/* Persisted state stored in FRAM via ckpt (162 bytes <= CKPT_MAX_PAYLOAD 256) */
typedef struct
{
    uint16_t high_score;
    uint16_t score;
    uint8_t  player_x;
    uint8_t  lives;
    uint8_t  wave;
    uint8_t  game_over;
    uint8_t  has_saved_run;
    int8_t   fleet_dir;
    uint8_t  combo_hits;
    uint8_t  twin_ticks;
    uint8_t  packed_grid[GAME_PACKED_BYTES];
} game_ckpt_t;

/* Debounced LaunchPad button state (sampled at 50 Hz / 20 ms per tick) */
typedef struct
{
    uint8_t s1_hist;
    uint8_t s2_hist;
    bool    s1_stable;
    bool    s2_stable;
    uint8_t hold_ticks;     /* Autorepeat timer for single button hold */
    uint8_t both_ticks;     /* Hold timer for S1+S2 long-press FRAM save */
    uint8_t s1_long_ticks;  /* Hold timer for S1 long-press FRAM load */
    bool    both_saved;
    bool    s1_loaded;
} btn_debouncer_t;

/* Active runtime state in SRAM */
static uint8_t  s_grid[GAME_GRID_H][GAME_GRID_W];
static uint8_t  s_player_x;
static uint8_t  s_lives;
static uint8_t  s_wave;
static uint16_t s_score;
static uint16_t s_high_score;
static bool     s_game_over;
static bool     s_has_saved_run;
static int8_t   s_fleet_dir;
static uint8_t  s_combo_hits;
static uint8_t  s_twin_ticks;
static uint8_t  s_fire_cooldown;
static uint8_t  s_laser_div;
static uint8_t  s_enemy_shot_div;
static uint8_t  s_enemy_move_div;
static uint8_t  s_render_div;
static uint8_t  s_anim_frame;
static uint16_t s_prng_state = 0xACE1u;

static btn_debouncer_t s_btn;
static volatile uint8_t s_pending_cmd = 0u;

/* Static Game Over banner stored in FRAM (.const) */
static const uint8_t GAME_OVER_BANNER[5][GAME_GRID_W] = {
    {0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0},
    {0,0,5,5,5,0,5,5,5,0,5,0,5,0,5,5,5,0,0,0},
    {0,0,5,0,0,0,5,0,5,0,5,5,5,0,5,5,0,0,0,0},
    {0,0,5,5,5,0,5,0,5,0,5,0,5,0,5,5,5,0,0,0},
    {0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0}
};

static uint16_t next_rand(void)
{
    uint16_t lsb = s_prng_state & 1u;
    s_prng_state >>= 1;
    if (lsb != 0u)
    {
        s_prng_state ^= 0xB400u;
    }
    return s_prng_state;
}

static uint8_t current_multiplier(void)
{
    uint8_t mult = (uint8_t)(1u + (s_combo_hits / 3u));
    return (mult > 4u) ? 4u : mult;
}

static void pack_grid(uint8_t out_packed[GAME_PACKED_BYTES])
{
    uint16_t idx = 0u;
    uint8_t r, c;

    for (r = 0u; r < GAME_GRID_H; r++)
    {
        for (c = 0u; c < GAME_GRID_W; c += 2u)
        {
            uint8_t hi = s_grid[r][c] & 0x0Fu;
            uint8_t lo = s_grid[r][c + 1u] & 0x0Fu;
            out_packed[idx++] = (uint8_t)((hi << 4) | lo);
        }
    }
}

static void unpack_grid(const uint8_t in_packed[GAME_PACKED_BYTES])
{
    uint16_t idx = 0u;
    uint8_t r, c;

    for (r = 0u; r < GAME_GRID_H; r++)
    {
        for (c = 0u; c < GAME_GRID_W; c += 2u)
        {
            uint8_t byte = in_packed[idx++];
            s_grid[r][c]      = (byte >> 4) & 0x0Fu;
            s_grid[r][c + 1u] = byte & 0x0Fu;
        }
    }
}

static bool commit_checkpoint(bool save_active_run)
{
    game_ckpt_t snap;

    if (s_score > s_high_score)
    {
        s_high_score = s_score;
    }
    if (save_active_run && !s_game_over)
    {
        s_has_saved_run = true;
    }

    snap.high_score    = s_high_score;
    snap.score         = s_score;
    snap.player_x      = s_player_x;
    snap.lives         = s_lives;
    snap.wave          = s_wave;
    snap.game_over     = s_game_over ? 1u : 0u;
    snap.has_saved_run = s_has_saved_run ? 1u : 0u;
    snap.fleet_dir     = s_fleet_dir;
    snap.combo_hits    = s_combo_hits;
    snap.twin_ticks    = s_twin_ticks;
    pack_grid(snap.packed_grid);

    bool ok = ckpt_save(&snap, sizeof(snap));
    if (ok)
    {
        board_led2_toggle();
    }
    return ok;
}

static bool load_checkpoint(bool restore_full_run)
{
    game_ckpt_t snap;
    uint32_t seq = 0u;

    if (!ckpt_restore(&snap, sizeof(snap), &seq))
    {
        return false;
    }

    s_high_score    = snap.high_score;
    s_has_saved_run = (snap.has_saved_run != 0u);

    if (restore_full_run && s_has_saved_run)
    {
        s_score          = snap.score;
        s_player_x       = (snap.player_x < GAME_GRID_W) ? snap.player_x : (GAME_GRID_W / 2u);
        s_lives          = snap.lives;
        s_wave           = snap.wave;
        s_game_over      = (snap.game_over != 0u);
        s_fleet_dir      = (snap.fleet_dir < 0) ? -1 : 1;
        s_combo_hits     = snap.combo_hits;
        s_twin_ticks     = snap.twin_ticks;
        s_fire_cooldown  = 0u;
        s_laser_div      = 0u;
        s_enemy_shot_div = 0u;
        s_enemy_move_div = 0u;
        s_render_div     = 4u; /* Force immediate screen refresh on restore */
        unpack_grid(snap.packed_grid);
        board_led1(s_game_over);
        board_led2_toggle();
        return true;
    }

    return false;
}

static void build_shields(void)
{
    uint8_t c;
    for (c = 3u; c < GAME_GRID_W - 3u; c += 4u)
    {
        s_grid[GAME_GRID_H - 3u][c]      = TILE_SHIELD;
        s_grid[GAME_GRID_H - 3u][c + 1u] = TILE_SHIELD;
    }
}

static void spawn_wave(uint8_t wave)
{
    uint8_t r, c;
    uint8_t rows = (wave > 4u) ? 4u : (uint8_t)(wave + 1u);

    s_fleet_dir = 1;
    for (r = 1u; r <= rows; r++)
    {
        for (c = 2u; c < GAME_GRID_W - 4u; c += 2u)
        {
            s_grid[r][c] = TILE_ENEMY;
        }
    }
}

static void reset_new_game(void)
{
    memset(s_grid, 0, sizeof(s_grid));
    s_player_x       = GAME_GRID_W / 2u;
    s_lives          = 3u;
    s_wave           = 1u;
    s_score          = 0u;
    s_game_over      = false;
    s_fleet_dir      = 1;
    s_combo_hits     = 0u;
    s_twin_ticks     = 0u;
    s_fire_cooldown  = 0u;
    s_laser_div      = 0u;
    s_enemy_shot_div = 0u;
    s_enemy_move_div = 0u;
    s_render_div     = 4u;

    build_shields();
    spawn_wave(s_wave);
}

void space_game_init(void)
{
    memset(&s_btn, 0, sizeof(s_btn));
    s_high_score    = 0u;
    s_has_saved_run = false;

    /* Hold S1 during reset/power-up to auto-resume saved FRAM run */
    bool resume_on_boot = board_s1_pressed();
    if (!load_checkpoint(resume_on_boot))
    {
        reset_new_game();
    }
}

bool space_game_push_input(uint8_t cmd)
{
    s_pending_cmd = cmd;
    return true;
}

/*
 * High-speed debouncer sampled at 50 Hz (20 ms per tick):
 * - 2 matching samples (40 ms) to confirm press/release.
 * - Zero-delay move on initial press for instant dodge response.
 * - Initial 140 ms (7-tick) tap-to-hold threshold, then 60 ms (3-tick) glide.
 * - Hold S1+S2 for 1.5 s (75 ticks) -> save checkpoint to FRAM ('p').
 * - Hold S1 alone for 1.5 s (75 ticks) -> restore checkpoint from FRAM ('r').
 */
static uint8_t poll_debounced_buttons(void)
{
    bool prev_s1 = s_btn.s1_stable;
    bool prev_s2 = s_btn.s2_stable;

    s_btn.s1_hist = (uint8_t)(((s_btn.s1_hist << 1) | (board_s1_pressed() ? 1u : 0u)) & 0x03u);
    s_btn.s2_hist = (uint8_t)(((s_btn.s2_hist << 1) | (board_s2_pressed() ? 1u : 0u)) & 0x03u);

    if (s_btn.s1_hist == 0x03u)      s_btn.s1_stable = true;
    else if (s_btn.s1_hist == 0x00u) s_btn.s1_stable = false;

    if (s_btn.s2_hist == 0x03u)      s_btn.s2_stable = true;
    else if (s_btn.s2_hist == 0x00u) s_btn.s2_stable = false;

    bool s1 = s_btn.s1_stable;
    bool s2 = s_btn.s2_stable;

    /* Both buttons pressed: immediate fire, 1.5 s hold saves FRAM checkpoint */
    if (s1 && s2)
    {
        s_btn.hold_ticks    = 0u;
        s_btn.s1_long_ticks = 0u;

        if (!(prev_s1 && prev_s2))
        {
            s_btn.both_ticks = 1u;
            s_btn.both_saved = false;
            return ' ';
        }

        if (!s_btn.both_saved)
        {
            s_btn.both_ticks++;
            if (s_btn.both_ticks >= 75u) /* 75 * 20 ms = 1.5 s */
            {
                s_btn.both_saved = true;
                return 'p';
            }
        }
        return 0u;
    }

    s_btn.both_ticks = 0u;
    s_btn.both_saved = false;

    /* Single button pressed (S1 = left, S2 = right) */
    if (s1 ^ s2)
    {
        uint8_t dir_cmd = s1 ? 'a' : 'd';

        if (s1 && !s_btn.s1_loaded)
        {
            s_btn.s1_long_ticks++;
            if (s_btn.s1_long_ticks >= 75u) /* 1.5 s hold on S1 loads FRAM save */
            {
                s_btn.s1_loaded = true;
                return 'r';
            }
        }
        else
        {
            s_btn.s1_long_ticks = 0u;
        }

        /* Move immediately on press edge (no chord_wait lag) */
        if ((s1 && !prev_s1) || (s2 && !prev_s2))
        {
            s_btn.hold_ticks = 1u;
            return dir_cmd;
        }

        /* Smooth autorepeat after 140 ms (7 ticks), stepping every 60 ms (3 ticks) */
        s_btn.hold_ticks++;
        if (s_btn.hold_ticks >= 7u && ((s_btn.hold_ticks - 7u) % 3u) == 0u)
        {
            return dir_cmd;
        }
        return 0u;
    }

    /* Neither button pressed */
    s_btn.hold_ticks    = 0u;
    s_btn.s1_long_ticks = 0u;
    s_btn.s1_loaded     = false;
    return 0u;
}

static uint8_t consume_input(void)
{
    uint8_t btn_cmd = poll_debounced_buttons();

    uint16_t sr = hal_enter_critical();
    uint8_t uart_cmd = s_pending_cmd;
    s_pending_cmd = 0u;
    hal_exit_critical(sr);

    return (uart_cmd != 0u) ? uart_cmd : btn_cmd;
}

static void trigger_game_over(void)
{
    s_game_over = true;
    board_led1(true);
    (void)commit_checkpoint(false);
    memcpy(&s_grid[4][0], GAME_OVER_BANNER, sizeof(GAME_OVER_BANNER));
    s_render_div = 4u;
}

static void damage_player(void)
{
    s_combo_hits = 0u;
    s_twin_ticks = 0u;
    if (s_lives > 0u)
    {
        s_lives--;
    }
    if (s_lives == 0u)
    {
        trigger_game_over();
    }
}

void space_game_tick(void)
{
    uint8_t cmd = consume_input();
    uint8_t r, c;

    /* Global FRAM save/restore/erase commands */
    if (cmd == 'p' || cmd == 'P')
    {
        (void)commit_checkpoint(true);
        s_render_div = 4u;
        return;
    }
    if (cmd == 'r' || cmd == 'R')
    {
        (void)load_checkpoint(true);
        return;
    }
    if (cmd == 'x' || cmd == 'X')
    {
        ckpt_erase();
        s_high_score    = 0u;
        s_has_saved_run = false;
        s_render_div    = 4u;
        return;
    }

    if (s_game_over)
    {
        if (cmd == ' ')
        {
            board_led1(false);
            reset_new_game();
        }
        return;
    }

    if (s_fire_cooldown > 0u) s_fire_cooldown--;
    if (s_twin_ticks > 0u)    s_twin_ticks--;

    /* 1. Player movement & firing (evaluated at full 50 Hz) */
    if ((cmd == 'a' || cmd == 'A') && s_player_x > 0u)
    {
        s_player_x--;
    }
    else if ((cmd == 'd' || cmd == 'D') && s_player_x < (GAME_GRID_W - 1u))
    {
        s_player_x++;
    }
    else if (cmd == ' ' && s_fire_cooldown == 0u)
    {
        if (s_twin_ticks > 0u)
        {
            uint8_t left_x  = (s_player_x > 0u) ? (uint8_t)(s_player_x - 1u) : s_player_x;
            uint8_t right_x = (s_player_x < GAME_GRID_W - 1u) ? (uint8_t)(s_player_x + 1u) : s_player_x;
            if (s_grid[GAME_GRID_H - 2u][left_x] == TILE_EMPTY)
                s_grid[GAME_GRID_H - 2u][left_x] = TILE_LASER;
            if (s_grid[GAME_GRID_H - 2u][right_x] == TILE_EMPTY)
                s_grid[GAME_GRID_H - 2u][right_x] = TILE_LASER;
            s_fire_cooldown = 6u; /* 120 ms cooldown in Twin mode */
        }
        else if (s_grid[GAME_GRID_H - 2u][s_player_x] == TILE_EMPTY)
        {
            s_grid[GAME_GRID_H - 2u][s_player_x] = TILE_LASER;
            s_fire_cooldown = 8u; /* 160 ms cooldown */
        }
    }

    /* 2. Clear explosions and advance player lasers upward (every 2 ticks = 40 ms) */
    s_laser_div++;
    if (s_laser_div >= 2u)
    {
        s_laser_div = 0u;
        for (r = 0u; r < GAME_GRID_H; r++)
        {
            for (c = 0u; c < GAME_GRID_W; c++)
            {
                if (s_grid[r][c] == TILE_EXPLOSION)
                {
                    s_grid[r][c] = TILE_EMPTY;
                }
                else if (s_grid[r][c] == TILE_LASER)
                {
                    s_grid[r][c] = TILE_EMPTY;
                    if (r == 0u)
                    {
                        s_combo_hits = 0u;
                    }
                    else
                    {
                        uint8_t target = s_grid[r - 1u][c];
                        if (target == TILE_ENEMY)
                        {
                            s_grid[r - 1u][c] = TILE_EXPLOSION;
                            s_combo_hits++;
                            s_score += (uint16_t)(10u * current_multiplier());
                            if (s_score > s_high_score)
                            {
                                s_high_score = s_score;
                            }
                            if ((next_rand() & 0x07u) == 0u && r < GAME_GRID_H - 2u)
                            {
                                s_grid[r][c] = TILE_POWERUP;
                            }
                        }
                        else if (target == TILE_ENEMY_SHOT)
                        {
                            s_grid[r - 1u][c] = TILE_EXPLOSION;
                            s_score += 5u;
                        }
                        else if (target == TILE_SHIELD)
                        {
                            s_grid[r - 1u][c] = TILE_EMPTY;
                            s_combo_hits = 0u;
                        }
                        else
                        {
                            s_grid[r - 1u][c] = TILE_LASER;
                        }
                    }
                }
            }
        }
    }

    /* 3. Advance falling enemy shots & power-up capsules (every 3 ticks = 60 ms) */
    s_enemy_shot_div++;
    if (s_enemy_shot_div >= 3u)
    {
        s_enemy_shot_div = 0u;
        for (r = GAME_GRID_H - 1u; r < GAME_GRID_H; r--)
        {
            for (c = 0u; c < GAME_GRID_W; c++)
            {
                uint8_t cell = s_grid[r][c];
                if (cell == TILE_ENEMY_SHOT || cell == TILE_POWERUP)
                {
                    s_grid[r][c] = TILE_EMPTY;
                    uint8_t next_r = (uint8_t)(r + 1u);

                    if (next_r == (GAME_GRID_H - 1u) && c == s_player_x)
                    {
                        if (cell == TILE_POWERUP)
                        {
                            s_twin_ticks = 250u; /* 5.0 s of twin lasers at 20 ms/tick */
                            s_score += 25u;
                        }
                        else
                        {
                            damage_player();
                            if (s_game_over) return;
                        }
                    }
                    else if (next_r < GAME_GRID_H)
                    {
                        if (cell == TILE_ENEMY_SHOT && s_grid[next_r][c] == TILE_SHIELD)
                        {
                            s_grid[next_r][c] = TILE_EMPTY;
                        }
                        else if (s_grid[next_r][c] == TILE_EMPTY)
                        {
                            s_grid[next_r][c] = cell;
                        }
                    }
                }
            }
        }
    }

    /* 4. Count remaining enemies & march fleet */
    uint8_t enemies_alive = 0u;
    bool hit_edge = false;
    for (r = 0u; r < GAME_GRID_H; r++)
    {
        for (c = 0u; c < GAME_GRID_W; c++)
        {
            if (s_grid[r][c] == TILE_ENEMY)
            {
                enemies_alive++;
                if ((s_fleet_dir > 0 && c >= GAME_GRID_W - 1u) ||
                    (s_fleet_dir < 0 && c == 0u))
                {
                    hit_edge = true;
                }
            }
        }
    }

    if (enemies_alive == 0u)
    {
        s_wave++;
        s_score += (uint16_t)(50u * s_wave);
        if (s_score > s_high_score) s_high_score = s_score;
        build_shields();
        spawn_wave(s_wave);
        return;
    }

    /* Fleet speed scaled for 50 Hz (20 ms) tick */
    uint8_t step_period = (uint8_t)(8u + (enemies_alive / 2u));
    uint8_t wave_bonus  = (uint8_t)((s_wave - 1u) * 2u);
    step_period = (step_period > wave_bonus) ? (uint8_t)(step_period - wave_bonus) : 6u;
    if (step_period < 6u) step_period = 6u;

    s_enemy_move_div++;
    if (s_enemy_move_div >= step_period)
    {
        s_enemy_move_div = 0u;
        s_anim_frame ^= 1u;

        if (hit_edge)
        {
            s_fleet_dir = (int8_t)(-s_fleet_dir);
            for (r = GAME_GRID_H - 1u; r < GAME_GRID_H; r--)
            {
                for (c = 0u; c < GAME_GRID_W; c++)
                {
                    if (s_grid[r][c] == TILE_ENEMY)
                    {
                        s_grid[r][c] = TILE_EMPTY;
                        if ((r + 1u) >= (GAME_GRID_H - 1u))
                        {
                            trigger_game_over();
                            return;
                        }
                        s_grid[r + 1u][c] = TILE_ENEMY;
                    }
                }
            }
        }
        else if (s_fleet_dir > 0)
        {
            for (r = 0u; r < GAME_GRID_H; r++)
            {
                for (c = GAME_GRID_W - 1u; c < GAME_GRID_W; c--)
                {
                    if (s_grid[r][c] == TILE_ENEMY)
                    {
                        s_grid[r][c] = TILE_EMPTY;
                        s_grid[r][c + 1u] = TILE_ENEMY;
                    }
                }
            }
        }
        else
        {
            for (r = 0u; r < GAME_GRID_H; r++)
            {
                for (c = 0u; c < GAME_GRID_W; c++)
                {
                    if (s_grid[r][c] == TILE_ENEMY)
                    {
                        s_grid[r][c] = TILE_EMPTY;
                        s_grid[r][c - 1u] = TILE_ENEMY;
                    }
                }
            }
        }

        uint8_t shoot_col = (uint8_t)(next_rand() % GAME_GRID_W);
        for (r = GAME_GRID_H - 3u; r < GAME_GRID_H; r--)
        {
            if (s_grid[r][shoot_col] == TILE_ENEMY)
            {
                if (s_grid[r + 1u][shoot_col] == TILE_EMPTY)
                {
                    s_grid[r + 1u][shoot_col] = TILE_ENEMY_SHOT;
                }
                break;
            }
        }
    }
}

/* Zero-snprintf, ULP-compliant buffer writers */
static uint16_t buf_puts(char *dst, uint16_t pos, uint16_t cap, const char *str)
{
    while (*str != '\0' && pos < cap)
    {
        dst[pos++] = *str++;
    }
    return pos;
}

static uint16_t buf_put_u16_pad4(char *dst, uint16_t pos, uint16_t cap, uint16_t val)
{
    if ((uint16_t)(pos + 4u) <= cap)
    {
        dst[pos++] = (char)('0' + ((val / 1000u) % 10u));
        dst[pos++] = (char)('0' + ((val / 100u)  % 10u));
        dst[pos++] = (char)('0' + ((val / 10u)   % 10u));
        dst[pos++] = (char)('0' + (val % 10u));
    }
    return pos;
}

static uint16_t buf_put_digit(char *dst, uint16_t pos, uint16_t cap, uint8_t val)
{
    if (pos < cap)
    {
        dst[pos++] = (char)('0' + (val % 10u));
    }
    return pos;
}

void space_game_render(void)
{
    static char frame[1024];
    const uint16_t cap = (uint16_t)sizeof(frame);
    uint16_t pos = 0u;
    uint8_t r, c;

    /* Render at 12.5 FPS (every 4th 20 ms tick = 80 ms) to match 115,200 baud */
    s_render_div++;
    if (s_render_div < 4u)
    {
        return;
    }
    s_render_div = 0u;

    pos = buf_puts(frame, pos, cap, "\x1b[H\x1b[?25l\x1b[1;37;44m SCORE:");
    pos = buf_put_u16_pad4(frame, pos, cap, s_score);
    pos = buf_puts(frame, pos, cap, " (");
    pos = buf_put_digit(frame, pos, cap, current_multiplier());
    pos = buf_puts(frame, pos, cap, "x) HI:");
    pos = buf_put_u16_pad4(frame, pos, cap, s_high_score);
    pos = buf_puts(frame, pos, cap, " W:");
    pos = buf_put_digit(frame, pos, cap, s_wave);
    pos = buf_puts(frame, pos, cap, " L:");
    pos = buf_put_digit(frame, pos, cap, s_lives);
    pos = buf_puts(frame, pos, cap, (s_twin_ticks > 0u) ? " [TWIN]" : "       ");
    pos = buf_puts(frame, pos, cap, s_has_saved_run     ? "[SAVED]" : "       ");
    pos = buf_puts(frame, pos, cap, "\x1b[0m\r\n\x1b[90m+----------------------------------------+\x1b[0m\r\n");

    for (r = 0u; r < GAME_GRID_H; r++)
    {
        uint8_t active_color = 0u;
        if (pos < cap)
        {
            frame[pos++] = '|';
        }

        for (c = 0u; c < GAME_GRID_W; c++)
        {
            uint8_t tile = s_grid[r][c];
            if (!s_game_over && r == (GAME_GRID_H - 1u) && c == s_player_x)
            {
                tile = TILE_PLAYER;
            }

            uint8_t color_id = 0u;
            const char *ansi_seq = "";
            const char *glyph = "  ";

            switch (tile)
            {
                case TILE_PLAYER:
                    if (s_twin_ticks > 0u) { color_id = 1u; ansi_seq = "\x1b[1;36m"; }
                    else                   { color_id = 2u; ansi_seq = "\x1b[1;32m"; }
                    glyph = "/\\";
                    break;
                case TILE_ENEMY:
                    if (s_anim_frame != 0u) { color_id = 3u; ansi_seq = "\x1b[1;31m"; glyph = "WM"; }
                    else                    { color_id = 4u; ansi_seq = "\x1b[1;35m"; glyph = "MW"; }
                    break;
                case TILE_LASER:
                    color_id = 5u; ansi_seq = "\x1b[1;33m"; glyph = "||";
                    break;
                case TILE_SHIELD:
                    color_id = 6u; ansi_seq = "\x1b[34m";   glyph = "##";
                    break;
                case TILE_EXPLOSION:
                    color_id = 5u; ansi_seq = "\x1b[1;33m"; glyph = "**";
                    break;
                case TILE_ENEMY_SHOT:
                    color_id = 3u; ansi_seq = "\x1b[1;31m"; glyph = "v ";
                    break;
                case TILE_POWERUP:
                    color_id = 1u; ansi_seq = "\x1b[1;36m"; glyph = "()";
                    break;
                default:
                    color_id = 0u; ansi_seq = "";          glyph = "  ";
                    break;
            }

            if (color_id != active_color)
            {
                if (color_id == 0u)
                {
                    pos = buf_puts(frame, pos, cap, "\x1b[0m");
                }
                else
                {
                    pos = buf_puts(frame, pos, cap, ansi_seq);
                }
                active_color = color_id;
            }
            pos = buf_puts(frame, pos, cap, glyph);
        }

        if (active_color != 0u)
        {
            pos = buf_puts(frame, pos, cap, "\x1b[0m");
        }
        pos = buf_puts(frame, pos, cap, "|\r\n");
    }

    if (s_game_over)
    {
        pos = buf_puts(frame, pos, cap,
            "\x1b[1;33m+---- GAME OVER: S1+S2=NEW  HOLD S1=LOAD ----+\x1b[0m\r\n");
    }
    else
    {
        pos = buf_puts(frame, pos, cap,
            "\x1b[90m+-- S1:LEFT  S2:RIGHT  S1+S2:FIRE/HOLD:SAVE --+\x1b[0m\r\n");
    }

    uart_write_all(UART_A0, frame, pos);
}
