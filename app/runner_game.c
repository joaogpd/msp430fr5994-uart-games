#include "app/runner_game.h"
#include "drivers/msp430/uart.h"
#include "drivers/msp430/timer.h"
#include "drivers/msp430/hal_common.h"
#include "drivers/board/board.h"
#include <string.h>

#define RUN_W               48u
#define RUN_H               7u      /* Rows 0 (ceiling) .. 6 (floor) */
#define RUN_PX              7u      /* Fixed player column */
#define RUN_CMD_Q_SIZE      16u
#define RUN_CMD_Q_MASK      (RUN_CMD_Q_SIZE - 1u)
#define RUN_DEBOUNCE_MS     20u
#define RUN_HOLD_TICKS      40u     /* ~0.8 s hold S1+S2 to exit to menu */
#define RUN_TICK_START_MS   110u
#define RUN_TICK_MIN_MS     65u

#define CMD_BTN_FLIP        0x81u
#define CMD_BTN_PHASE       0x82u
#define CMD_BTN_EXIT        0x83u

typedef enum
{
    RT_EMPTY    = 0,
    RT_SPIKE_DN = 1,    /* Floor spike '^' */
    RT_SPIKE_UP = 2,    /* Ceiling spike 'v' */
    RT_WALL     = 3,    /* Gate block '#' */
    RT_LASER    = 4,    /* Mid-air laser '|' */
    RT_CELL     = 5     /* Phase energy cell '*' */
} run_tile_t;

typedef struct
{
    uint16_t best_score;
    uint16_t best_dist;
    uint16_t runs;
    uint16_t magic;
} run_stats_t;

#define RUN_STATS_MAGIC     0x4752u /* "GR" */

#pragma PERSISTENT(s_runner_stats)
static run_stats_t s_runner_stats = { 0u, 0u, 0u, 0u };

#pragma PERSISTENT(s_grid)
static uint8_t s_grid[RUN_H][RUN_W] = { { 0 } };

static uint8_t  s_py;
static int8_t   s_grav;             /* +1 = down (floor), -1 = up (ceiling) */
static bool     s_flip_buffered;
static uint8_t  s_phase_charges;
static uint8_t  s_phase_ticks;
static uint16_t s_score;
static uint16_t s_dist;
static uint16_t s_tick_ms;
static uint16_t s_rng = 0xACE1u;
static uint8_t  s_next_chunk_cd;
static bool     s_game_over;
static bool     s_dirty;
static bool     s_full_redraw;
static bool     s_exit_req;
static uint8_t  s_esc_state;

static volatile bool    s_tick_due;
static volatile uint8_t s_cmd_q[RUN_CMD_Q_SIZE];
static volatile uint8_t s_cmd_head;
static volatile uint8_t s_cmd_tail;

typedef struct
{
    uint8_t s1_hist;
    uint8_t s2_hist;
    bool    s1_stable;
    bool    s2_stable;
    uint8_t both_hold;
    bool    both_fired;
} run_btn_t;

static run_btn_t s_btn;

/* --------------------------------------------------------------------------
 * Utilities & Formatting
 * -------------------------------------------------------------------------- */

static uint16_t next_rand(void)
{
    uint16_t lsb = s_rng & 1u;
    s_rng >>= 1;
    if (lsb != 0u) { s_rng ^= 0xB400u; }
    return s_rng;
}

static uint16_t buf_puts(char *dst, uint16_t pos, uint16_t cap, const char *str)
{
    while (*str != '\0' && pos < cap) { dst[pos++] = *str++; }
    return pos;
}

static uint16_t buf_put_u8(char *dst, uint16_t pos, uint16_t cap, uint8_t val)
{
    if (val >= 100u && pos < cap) { dst[pos++] = (char)('0' + (val / 100u)); }
    if (val >= 10u && pos < cap)  { dst[pos++] = (char)('0' + ((val / 10u) % 10u)); }
    if (pos < cap)                { dst[pos++] = (char)('0' + (val % 10u)); }
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
    pos = buf_put_u8(dst, pos, cap, row1);
    if (pos < cap) { dst[pos++] = ';'; }
    pos = buf_put_u8(dst, pos, cap, col1);
    if (pos < cap) { dst[pos++] = 'H'; }
    return pos;
}

static void frame_flush(char *frame, uint16_t *pos, uint16_t cap)
{
    if (*pos >= (uint16_t)(cap - 96u))
    {
        uart_write_all(UART_A0, frame, *pos);
        *pos = 0u;
    }
}

static bool on_tick_timer(timer_id_t id)
{
    (void)id;
    s_tick_due = true;
    return true;
}

static bool is_grounded(void)
{
    return (s_grav > 0 && s_py == (RUN_H - 1u)) || (s_grav < 0 && s_py == 0u);
}

/* --------------------------------------------------------------------------
 * Chunk-Based Obstacle Generator (Always Fair & Solvable)
 * -------------------------------------------------------------------------- */

static void spawn_chunk(void)
{
    uint8_t col = (uint8_t)(RUN_W - 6u);
    uint8_t pat = (uint8_t)(next_rand() & 0x07u);
    uint8_t r;

    switch (pat)
    {
        case 0u: /* Floor spike pair + ceiling energy cell */
            s_grid[RUN_H - 1u][col]      = RT_SPIKE_DN;
            s_grid[RUN_H - 1u][col + 1u] = RT_SPIKE_DN;
            s_grid[0u][col + 1u]         = RT_CELL;
            break;

        case 1u: /* Ceiling spike pair + floor energy cell */
            s_grid[0u][col]      = RT_SPIKE_UP;
            s_grid[0u][col + 1u] = RT_SPIKE_UP;
            s_grid[RUN_H - 1u][col + 1u] = RT_CELL;
            break;

        case 2u: /* Mid-air laser beam (safe on floor or ceiling) */
            s_grid[2u][col] = RT_LASER;
            s_grid[3u][col] = RT_LASER;
            s_grid[4u][col] = RT_LASER;
            break;

        case 3u: /* Staggered floor -> ceiling spikes */
            s_grid[RUN_H - 1u][col]      = RT_SPIKE_DN;
            s_grid[RUN_H - 1u][col + 1u] = RT_SPIKE_DN;
            s_grid[0u][col + 4u]         = RT_SPIKE_UP;
            s_grid[0u][col + 5u]         = RT_SPIKE_UP;
            break;

        case 4u: /* Top-gap gate (open rows 0..1) */
            for (r = 2u; r < RUN_H; r++) { s_grid[r][col] = RT_WALL; }
            break;

        case 5u: /* Bottom-gap gate (open rows 5..6) */
            for (r = 0u; r < (RUN_H - 2u); r++) { s_grid[r][col] = RT_WALL; }
            break;

        case 6u: /* Center-gap gate (flip through or Phase-Shift) */
            s_grid[0u][col] = RT_WALL;
            s_grid[1u][col] = RT_WALL;
            s_grid[5u][col] = RT_WALL;
            s_grid[6u][col] = RT_WALL;
            s_grid[3u][col] = RT_CELL;
            break;

        default: /* Ceiling -> floor stagger */
            s_grid[0u][col]              = RT_SPIKE_UP;
            s_grid[0u][col + 1u]         = RT_SPIKE_UP;
            s_grid[RUN_H - 1u][col + 4u] = RT_SPIKE_DN;
            s_grid[RUN_H - 1u][col + 5u] = RT_SPIKE_DN;
            break;
    }

    /* Gap tightens slightly as distance grows, always >= 9 cols */
    uint8_t base_gap = (s_dist > 300u) ? 9u : 11u;
    s_next_chunk_cd = (uint8_t)(base_gap + (next_rand() & 0x03u));
}

static void start_run(void)
{
    memset(s_grid, RT_EMPTY, sizeof(s_grid));
    s_rng ^= (uint16_t)TA1R ^ (uint16_t)TA0R;
    if (s_rng == 0u) { s_rng = 0xACE1u; }

    s_py            = (uint8_t)(RUN_H - 1u);
    s_grav          = 1;
    s_flip_buffered = false;
    s_phase_charges = 2u;
    s_phase_ticks   = 0u;
    s_score         = 0u;
    s_dist          = 0u;
    s_tick_ms       = RUN_TICK_START_MS;
    s_next_chunk_cd = 6u;
    s_game_over     = false;
    s_tick_due      = false;
    s_full_redraw   = true;
    s_dirty         = true;

    board_led1(false);
    board_led2(false);
    (void)timer_start_periodic_ms(TIMER_A1, s_tick_ms, on_tick_timer);
}

/* --------------------------------------------------------------------------
 * Public Lifecycle & Input Hooks
 * -------------------------------------------------------------------------- */

void runner_game_init(void)
{
    if (s_runner_stats.magic != RUN_STATS_MAGIC)
    {
        s_runner_stats.best_score = 0u;
        s_runner_stats.best_dist  = 0u;
        s_runner_stats.runs       = 0u;
        s_runner_stats.magic      = RUN_STATS_MAGIC;
    }

    memset(&s_btn, 0, sizeof(s_btn));
    s_cmd_head  = 0u;
    s_cmd_tail  = 0u;
    s_esc_state = 0u;
    s_exit_req  = false;
    start_run();
}

uint16_t runner_game_best_score(void)
{
    return (s_runner_stats.magic == RUN_STATS_MAGIC) ? s_runner_stats.best_score : 0u;
}

bool runner_game_push_input(uint8_t cmd)
{
    uint8_t next = (uint8_t)((s_cmd_head + 1u) & RUN_CMD_Q_MASK);
    if (next != s_cmd_tail)
    {
        s_cmd_q[s_cmd_head] = cmd;
        s_cmd_head = next;
    }
    return true;
}

bool runner_game_on_button_irq(void)
{
    if (!timer_running(TIMER_A0))
    {
        (void)timer_start_oneshot_ms(TIMER_A0, RUN_DEBOUNCE_MS, runner_game_on_debounce_timer);
    }
    return false;
}

bool runner_game_on_debounce_timer(timer_id_t id)
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
        s_btn.both_hold++;
        if (!s_btn.both_fired && s_btn.both_hold >= RUN_HOLD_TICKS)
        {
            s_btn.both_fired = true;
            cmd = CMD_BTN_EXIT;
        }
    }
    else
    {
        s_btn.both_hold = 0u;
        if (!s1 && !s2) { s_btn.both_fired = false; }

        /* Trigger on press edge for crisp arcade latency */
        if (s1 && !prev_s1 && !s_btn.both_fired)      { cmd = CMD_BTN_FLIP; }
        else if (s2 && !prev_s2 && !s_btn.both_fired) { cmd = CMD_BTN_PHASE; }
    }

    if (raw_s1 || raw_s2 || s1 || s2 || s_btn.s1_hist != 0u || s_btn.s2_hist != 0u)
    {
        (void)timer_start_oneshot_ms(TIMER_A0, RUN_DEBOUNCE_MS, runner_game_on_debounce_timer);
    }

    return (cmd != 0u) ? runner_game_push_input(cmd) : false;
}

bool runner_game_has_pending_work(void)
{
    return (s_cmd_head != s_cmd_tail) || s_tick_due || s_dirty;
}

bool runner_game_exit_requested(void)
{
    return s_exit_req;
}

/* --------------------------------------------------------------------------
 * Simulation Step & ANSI Renderer
 * -------------------------------------------------------------------------- */

static void trigger_flip(void)
{
    if (is_grounded())
    {
        s_grav = (int8_t)-s_grav;
        s_flip_buffered = false;
    }
    else
    {
        s_flip_buffered = true;
    }
}

static void trigger_phase(void)
{
    if (s_phase_charges > 0u && s_phase_ticks == 0u)
    {
        s_phase_charges--;
        s_phase_ticks = 6u;
        board_led2(true);
    }
}

static void step_simulation(void)
{
    uint8_t r, c;

    /* 1. Scroll corridor left by 1 column */
    for (r = 0u; r < RUN_H; r++)
    {
        for (c = 0u; c < (RUN_W - 1u); c++)
        {
            s_grid[r][c] = s_grid[r][c + 1u];
        }
        s_grid[r][RUN_W - 1u] = RT_EMPTY;
    }

    /* 2. Spawn next verified chunk */
    if (s_next_chunk_cd == 0u)
    {
        spawn_chunk();
    }
    else
    {
        s_next_chunk_cd--;
    }

    /* 3. Apply gravity & buffered flip */
    int8_t ny = (int8_t)s_py + s_grav;
    if (ny < 0)                 { ny = 0; }
    if (ny >= (int8_t)RUN_H)    { ny = (int8_t)(RUN_H - 1u); }
    s_py = (uint8_t)ny;

    if (s_flip_buffered && is_grounded())
    {
        s_grav = (int8_t)-s_grav;
        s_flip_buffered = false;
    }

    /* 4. Check collision at player cell */
    uint8_t cell = s_grid[s_py][RUN_PX];
    if (cell == RT_CELL)
    {
        s_grid[s_py][RUN_PX] = RT_EMPTY;
        s_score = (uint16_t)(s_score + 25u);
        if (s_phase_charges < 3u) { s_phase_charges++; }
    }
    else if (cell != RT_EMPTY && s_phase_ticks == 0u)
    {
        s_game_over = true;
        timer_stop(TIMER_A1);
        board_led1(true);
        board_led2(false);
        s_runner_stats.runs++;
        if (s_score > s_runner_stats.best_score) { s_runner_stats.best_score = s_score; }
        if (s_dist  > s_runner_stats.best_dist)  { s_runner_stats.best_dist  = s_dist; }
        s_full_redraw = true;
        s_dirty = true;
        return;
    }

    /* 5. Advance phase timer, score, distance & speed curve */
    if (s_phase_ticks > 0u)
    {
        s_phase_ticks--;
        if (s_phase_ticks == 0u) { board_led2(false); }
    }

    s_dist++;
    s_score++;
    if ((s_dist % 100u) == 0u)
    {
        if (s_phase_charges < 3u) { s_phase_charges++; }
        if (s_tick_ms > RUN_TICK_MIN_MS)
        {
            s_tick_ms = (uint16_t)(s_tick_ms - 5u);
            (void)timer_start_periodic_ms(TIMER_A1, s_tick_ms, on_tick_timer);
        }
    }

    s_dirty = true;
}

static void render_frame(void)
{
    static char frame[256];
    const uint16_t cap = (uint16_t)sizeof(frame);
    uint16_t pos = 0u;
    uint8_t r, c;

    if (!s_dirty) { return; }
    s_dirty = false;

    if (s_full_redraw)
    {
        s_full_redraw = false;
        pos = buf_puts(frame, pos, cap,
            "\x1b[2J\x1b[H\x1b[?25l"
            "\x1b[1;37;44m MSP430 GRAVITY RUNNER                          S1:FLIP  S2:PHASE  Ctrl+Q:MENU \x1b[0m");
        frame_flush(frame, &pos, cap);

        /* Ceiling & floor rails (cols 16..65 -> 50 chars with side borders) */
        pos = buf_put_cup(frame, pos, cap, 6u, 16u);
        pos = buf_puts(frame, pos, cap, "\x1b[1;34m+================================================+\x1b[0m");
        frame_flush(frame, &pos, cap);

        pos = buf_put_cup(frame, pos, cap, 14u, 16u);
        pos = buf_puts(frame, pos, cap, "\x1b[1;34m+================================================+\x1b[0m");
        frame_flush(frame, &pos, cap);

        pos = buf_put_cup(frame, pos, cap, 17u, 16u);
        pos = buf_puts(frame, pos, cap, "\x1b[90mS1 / W / Space : Flip Gravity   (grounded only)\x1b[0m");
        frame_flush(frame, &pos, cap);

        pos = buf_put_cup(frame, pos, cap, 18u, 16u);
        pos = buf_puts(frame, pos, cap, "\x1b[90mS2 / D / F     : Phase-Shift    (pass through hazards)\x1b[0m");
        frame_flush(frame, &pos, cap);

        pos = buf_put_cup(frame, pos, cap, 19u, 16u);
        pos = buf_puts(frame, pos, cap, "\x1b[90mHold S1+S2 / Ctrl+Q / Q : Return to Game Select\x1b[0m");
        frame_flush(frame, &pos, cap);
    }

    /* Row 3: Live HUD */
    pos = buf_put_cup(frame, pos, cap, 3u, 16u);
    pos = buf_puts(frame, pos, cap, "\x1b[1;33mSCORE:\x1b[0m");
    pos = buf_put_u16_pad4(frame, pos, cap, s_score);
    pos = buf_puts(frame, pos, cap, "  \x1b[1;37mBEST:\x1b[0m");
    pos = buf_put_u16_pad4(frame, pos, cap, s_runner_stats.best_score);
    pos = buf_puts(frame, pos, cap, "  \x1b[1;36mPHASE:\x1b[0m[");
    for (c = 0u; c < 3u; c++)
    {
        if (pos < cap) { frame[pos++] = (c < s_phase_charges) ? '*' : '-'; }
    }
    pos = buf_puts(frame, pos, cap, s_phase_ticks > 0u ? "] \x1b[1;32mON \x1b[0m" : "]    ");
    frame_flush(frame, &pos, cap);

    /* Rows 7..13: 48x7 scrolling corridor */
    for (r = 0u; r < RUN_H; r++)
    {
        uint8_t cur_col = 0u;
        pos = buf_put_cup(frame, pos, cap, (uint8_t)(7u + r), 16u);
        pos = buf_puts(frame, pos, cap, "\x1b[1;34m|\x1b[0m");

        for (c = 0u; c < RUN_W; c++)
        {
            uint8_t cid = 0u;
            const char *ansi = "";
            char ch = ' ';

            if (c == RUN_PX && r == s_py)
            {
                if (s_phase_ticks > 0u) { cid = 5u; ansi = "\x1b[1;36m"; ch = '='; }
                else                    { cid = 1u; ansi = "\x1b[1;32m"; ch = (s_grav > 0) ? '@' : 'A'; }
            }
            else
            {
                switch (s_grid[r][c])
                {
                    case RT_SPIKE_DN: cid = 2u; ansi = "\x1b[1;31m"; ch = '^'; break;
                    case RT_SPIKE_UP: cid = 2u; ansi = "\x1b[1;31m"; ch = 'v'; break;
                    case RT_WALL:     cid = 3u; ansi = "\x1b[1;33m"; ch = '#'; break;
                    case RT_LASER:    cid = 4u; ansi = "\x1b[1;35m"; ch = '|'; break;
                    case RT_CELL:     cid = 5u; ansi = "\x1b[1;36m"; ch = '*'; break;
                    default:          cid = 0u; ansi = "";           ch = ' '; break;
                }
            }

            if (cid != cur_col)
            {
                pos = buf_puts(frame, pos, cap, (cid == 0u) ? "\x1b[0m" : ansi);
                cur_col = cid;
            }
            if (pos < cap) { frame[pos++] = ch; }
            frame_flush(frame, &pos, cap);
        }

        if (cur_col != 0u) { pos = buf_puts(frame, pos, cap, "\x1b[0m"); }
        pos = buf_puts(frame, pos, cap, "\x1b[1;34m|\x1b[0m");
        frame_flush(frame, &pos, cap);
    }

    if (s_game_over)
    {
        pos = buf_put_cup(frame, pos, cap, 10u, 22u);
        pos = buf_puts(frame, pos, cap, "\x1b[1;37;41m GAME OVER! S1/SPACE:RETRY  Q:MENU \x1b[0m");
        frame_flush(frame, &pos, cap);
    }

    if (pos > 0u)
    {
        uart_write_all(UART_A0, frame, pos);
    }
}

static bool pop_cmd(uint8_t *out)
{
    uint16_t sr = hal_enter_critical();
    if (s_cmd_head == s_cmd_tail)
    {
        hal_exit_critical(sr);
        return false;
    }
    *out = s_cmd_q[s_cmd_tail];
    s_cmd_tail = (uint8_t)((s_cmd_tail + 1u) & RUN_CMD_Q_MASK);
    hal_exit_critical(sr);
    return true;
}

static void handle_cmd(uint8_t cmd)
{
    if (cmd == CMD_BTN_EXIT || cmd == 'q' || cmd == 'Q')
    {
        s_exit_req = true;
        return;
    }
    if (cmd == 0x0Cu || cmd == 'l' || cmd == 'L')
    {
        s_full_redraw = true;
        s_dirty = true;
        return;
    }
    if (s_game_over)
    {
        if (cmd == CMD_BTN_FLIP || cmd == CMD_BTN_PHASE ||
            cmd == ' ' || cmd == '\r' || cmd == '\n' || cmd == 'w' || cmd == 'W')
        {
            start_run();
        }
        return;
    }

    if (cmd == CMD_BTN_FLIP || cmd == 'w' || cmd == 'W' || cmd == 's' || cmd == 'S' || cmd == ' ')
    {
        trigger_flip();
    }
    else if (cmd == CMD_BTN_PHASE || cmd == 'd' || cmd == 'D' || cmd == 'f' || cmd == 'F')
    {
        trigger_phase();
    }
}

void runner_game_process(void)
{
    uint8_t b;
    uint16_t sr = hal_enter_critical();
    bool do_tick = s_tick_due;
    s_tick_due = false;
    hal_exit_critical(sr);

    while (pop_cmd(&b))
    {
        if (s_esc_state == 1u)
        {
            s_esc_state = (b == '[') ? 2u : 0u;
            continue;
        }
        else if (s_esc_state == 2u)
        {
            s_esc_state = 0u;
            if (b == 'A' || b == 'B')      { handle_cmd(CMD_BTN_FLIP); }
            else if (b == 'C')             { handle_cmd(CMD_BTN_PHASE); }
            continue;
        }
        if (b == 0x1Bu) { s_esc_state = 1u; continue; }
        handle_cmd(b);
    }

    if (do_tick && !s_game_over && !s_exit_req)
    {
        step_simulation();
    }

    render_frame();
}
