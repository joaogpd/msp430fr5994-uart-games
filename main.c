#include <msp430.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>

#include "drivers/board/board.h"
#include "drivers/msp430/cs.h"
#include "drivers/msp430/gpio.h"
#include "drivers/msp430/hal_common.h"
#include "drivers/msp430/pmm.h"
#include "drivers/msp430/timer.h"
#include "drivers/msp430/uart.h"
#include "app/dungeon_game.h"
#include "app/runner_game.h"

#define MENU_CMD_Q_SIZE     16u
#define MENU_CMD_Q_MASK     (MENU_CMD_Q_SIZE - 1u)
#define MENU_DEBOUNCE_MS    20u
#define MENU_BTN_NEXT       0x81u
#define MENU_BTN_LAUNCH     0x82u

typedef enum
{
    APP_MENU    = 0,
    APP_DUNGEON = 1,
    APP_RUNNER  = 2
} active_app_t;

/* 1024-byte TX ring buffer holds a full ANSI frame in one call */
static uint8_t s_uart_tx_buf[1024];

static volatile active_app_t s_app = APP_MENU;
static volatile bool         s_return_to_menu = false;

/* Menu state */
static uint8_t          s_menu_sel = 0u;
static bool             s_menu_dirty = true;
static bool             s_menu_connected = false;
static uint8_t          s_menu_esc = 0u;
static volatile bool    s_menu_probe_due = false;
static volatile uint8_t s_menu_q[MENU_CMD_Q_SIZE];
static volatile uint8_t s_menu_head = 0u;
static volatile uint8_t s_menu_tail = 0u;

/* Menu button debouncer */
static uint8_t s_m_s1_hist = 0u, s_m_s2_hist = 0u;
static bool    s_m_s1_st = false, s_m_s2_st = false;

static bool menu_push_cmd(uint8_t cmd)
{
    uint8_t next = (uint8_t)((s_menu_head + 1u) & MENU_CMD_Q_MASK);
    if (next != s_menu_tail)
    {
        s_menu_q[s_menu_head] = cmd;
        s_menu_head = next;
    }
    return true;
}

static bool on_menu_probe_timer(timer_id_t id)
{
    (void)id;
    s_menu_probe_due = true;
    return true;
}

static bool on_menu_debounce_timer(timer_id_t id)
{
    (void)id;
    bool prev_s1 = s_m_s1_st;
    bool prev_s2 = s_m_s2_st;
    bool raw_s1  = board_s1_pressed();
    bool raw_s2  = board_s2_pressed();
    uint8_t cmd  = 0u;

    s_m_s1_hist = (uint8_t)(((s_m_s1_hist << 1) | (raw_s1 ? 1u : 0u)) & 0x03u);
    s_m_s2_hist = (uint8_t)(((s_m_s2_hist << 1) | (raw_s2 ? 1u : 0u)) & 0x03u);

    if (s_m_s1_hist == 0x03u)      s_m_s1_st = true;
    else if (s_m_s1_hist == 0x00u) s_m_s1_st = false;

    if (s_m_s2_hist == 0x03u)      s_m_s2_st = true;
    else if (s_m_s2_hist == 0x00u) s_m_s2_st = false;

    if (s_m_s1_st && !prev_s1)      { cmd = MENU_BTN_NEXT; }
    else if (s_m_s2_st && !prev_s2) { cmd = MENU_BTN_LAUNCH; }

    if (raw_s1 || raw_s2 || s_m_s1_st || s_m_s2_st || s_m_s1_hist != 0u || s_m_s2_hist != 0u)
    {
        (void)timer_start_oneshot_ms(TIMER_A0, MENU_DEBOUNCE_MS, on_menu_debounce_timer);
    }

    return (cmd != 0u) ? menu_push_cmd(cmd) : false;
}

/* --------------------------------------------------------------------------
 * Unified ISR Dispatchers
 * -------------------------------------------------------------------------- */

static bool on_uart_rx(uart_id_t id, uint8_t byte, bool error)
{
    (void)id;
    if (error) { return false; }

    /* Global Ctrl+Q returns to Game Select from any running game */
    if (byte == 0x11u && s_app != APP_MENU)
    {
        s_return_to_menu = true;
        return true;
    }

    switch (s_app)
    {
        case APP_DUNGEON: return dungeon_game_push_input(byte);
        case APP_RUNNER:  return runner_game_push_input(byte);
        default:          return menu_push_cmd(byte);
    }
}

static bool on_button_edge(gpio_port_t port, uint8_t pin)
{
    (void)port;
    (void)pin;
    switch (s_app)
    {
        case APP_DUNGEON: return dungeon_game_on_button_irq();
        case APP_RUNNER:  return runner_game_on_button_irq();
        case APP_MENU:
            if (!timer_running(TIMER_A0))
            {
                (void)timer_start_oneshot_ms(TIMER_A0, MENU_DEBOUNCE_MS, on_menu_debounce_timer);
            }
            return false;
        default:
            return false;
    }
}

/* --------------------------------------------------------------------------
 * App Switching & Zero-RAM-Buffer Game Select Renderer
 * -------------------------------------------------------------------------- */

static void stop_all_timers_and_leds(void)
{
    timer_stop(TIMER_A0);
    timer_stop(TIMER_A1);
    timer_stop(TIMER_A2);
    board_led1(false);
    board_led2(false);
}

static void enter_menu(void)
{
    stop_all_timers_and_leds();
    s_app            = APP_MENU;
    s_return_to_menu = false;
    s_menu_head      = 0u;
    s_menu_tail      = 0u;
    s_menu_esc       = 0u;
    s_menu_dirty     = true;
    s_menu_probe_due = false;

    if (!s_menu_connected)
    {
        (void)timer_start_periodic_ms(TIMER_A1, 500u, on_menu_probe_timer);
    }
}

static void launch_app(uint8_t idx)
{
    stop_all_timers_and_leds();
    if (idx == 0u)
    {
        s_app = APP_DUNGEON;
        dungeon_game_init();
        (void)dungeon_game_push_input(0x0Cu); /* Force immediate redraw */
    }
    else
    {
        s_app = APP_RUNNER;
        runner_game_init();
    }
}

static void menu_write_str(const char *s)
{
    uart_write_all(UART_A0, s, (uint16_t)strlen(s));
}

static void render_menu(void)
{
    uint16_t best = runner_game_best_score();
    char best_str[5];
    if (best > 9999u) { best = 9999u; }
    best_str[0] = (char)('0' + ((best / 1000u) % 10u));
    best_str[1] = (char)('0' + ((best / 100u)  % 10u));
    best_str[2] = (char)('0' + ((best / 10u)   % 10u));
    best_str[3] = (char)('0' + (best % 10u));
    best_str[4] = '\0';

    menu_write_str(
        "\x1b[2J\x1b[H\x1b[?25l"
        "\x1b[1;37;44m MSP430FR5994 ARCADE LAUNCHER                                       115200 8N1 \x1b[0m"
        "\x1b[4;5H\x1b[1;33mSELECT A GAME:\x1b[0m"
        "\x1b[7;5H");
    menu_write_str((s_menu_sel == 0u) ? "\x1b[1;32m> [1] DUNGEON CRAWLER\x1b[0m" : "  [1] DUNGEON CRAWLER");
    menu_write_str(
        "   \x1b[90mTurn-based Roguelike + FRAM Saves & Q7 AI\x1b[0m"
        "\x1b[9;5H");
    menu_write_str((s_menu_sel == 1u) ? "\x1b[1;35m> [2] GRAVITY RUNNER \x1b[0m" : "  [2] GRAVITY RUNNER ");
    menu_write_str("   \x1b[90m2-Button Gravity-Flip Runner  BEST:\x1b[1;33m");
    menu_write_str(best_str);
    menu_write_str(
        "\x1b[0m"
        "\x1b[13;5H\x1b[90m----------------------------------------------------------------------\x1b[0m"
        "\x1b[15;5H\x1b[1;37mS1 / W,S / Arrows\x1b[0m : Select    \x1b[1;37mS2 / Enter / 1-2\x1b[0m : Launch"
        "\x1b[16;5H\x1b[1;37mCtrl+Q\x1b[0m            : Return to this menu from any game");
}

static bool menu_pop_cmd(uint8_t *out)
{
    uint16_t sr = hal_enter_critical();
    if (s_menu_head == s_menu_tail)
    {
        hal_exit_critical(sr);
        return false;
    }
    *out = s_menu_q[s_menu_tail];
    s_menu_tail = (uint8_t)((s_menu_tail + 1u) & MENU_CMD_Q_MASK);
    hal_exit_critical(sr);
    return true;
}

static void menu_handle_cmd(uint8_t cmd)
{
    if (cmd != MENU_BTN_NEXT && cmd != MENU_BTN_LAUNCH && !s_menu_connected)
    {
        s_menu_connected = true;
        timer_stop(TIMER_A1);
        s_menu_dirty = true;
    }

    if (cmd == '1') { launch_app(0u); return; }
    if (cmd == '2') { launch_app(1u); return; }

    if (cmd == MENU_BTN_NEXT || cmd == 's' || cmd == 'S' || cmd == 'w' || cmd == 'W')
    {
        s_menu_sel ^= 1u;
        s_menu_dirty = true;
    }
    else if (cmd == MENU_BTN_LAUNCH || cmd == '\r' || cmd == '\n' || cmd == ' ')
    {
        launch_app(s_menu_sel);
    }
    else if (cmd == 0x0Cu || cmd == 'l' || cmd == 'L')
    {
        s_menu_dirty = true;
    }
}

static void menu_process(void)
{
    uint8_t b;
    uint16_t sr = hal_enter_critical();
    bool do_probe = s_menu_probe_due;
    s_menu_probe_due = false;
    hal_exit_critical(sr);

    while (menu_pop_cmd(&b))
    {
        if (s_menu_esc == 1u)
        {
            s_menu_esc = (b == '[') ? 2u : 0u;
            continue;
        }
        else if (s_menu_esc == 2u)
        {
            if (b == 'A' || b == 'B') { s_menu_esc = 0u; menu_handle_cmd('s'); continue; }
            else if (b == '0')        { s_menu_esc = 3u; continue; }
            s_menu_esc = 0u;
            continue;
        }
        else if (s_menu_esc == 3u)
        {
            s_menu_esc = 0u;
            if (b == 'n' && !s_menu_connected)
            {
                s_menu_connected = true;
                timer_stop(TIMER_A1);
                s_menu_dirty = true;
            }
            continue;
        }
        if (b == 0x1Bu) { s_menu_esc = 1u; continue; }

        menu_handle_cmd(b);
        if (s_app != APP_MENU) { return; }
    }

    if (do_probe && !s_menu_connected && uart_tx_idle(UART_A0))
    {
        static const char dsr[4] = "\x1b[5n";
        (void)uart_write(UART_A0, dsr, 4u);
    }

    if (s_menu_dirty)
    {
        s_menu_dirty = false;
        render_menu();
    }
}

static bool app_has_pending_work(void)
{
    if (s_return_to_menu) { return true; }
    switch (s_app)
    {
        case APP_DUNGEON: return dungeon_game_has_pending_work();
        case APP_RUNNER:  return runner_game_has_pending_work();
        default:          return (s_menu_head != s_menu_tail) || s_menu_probe_due || s_menu_dirty;
    }
}

int main(void)
{
    board_init(NULL);

    cs_config_t cs_cfg = CS_CONFIG_DEFAULT;
    cs_cfg.dco = CS_DCO_4MHZ;
    (void)cs_init(&cs_cfg);

    board_uart_pins(true);
    uart_config_t uart_cfg = {
        .baud    = 115200u,
        .clk     = UART_CLK_SMCLK,
        .tx_buf  = s_uart_tx_buf,
        .tx_size = (uint16_t)sizeof(s_uart_tx_buf),
        .rx_cb   = on_uart_rx
    };
    (void)uart_init(UART_A0, &uart_cfg);

    gpio_set_callback(BOARD_S1_PORT, BOARD_S1_PIN, on_button_edge);
    gpio_set_callback(BOARD_S2_PORT, BOARD_S2_PIN, on_button_edge);
    gpio_interrupt_enable(BOARD_S1_PORT, BOARD_S1_PIN);
    gpio_interrupt_enable(BOARD_S2_PORT, BOARD_S2_PIN);

    enter_menu();
    __bis_SR_register(GIE);

    while (1)
    {
        if (s_return_to_menu)
        {
            enter_menu();
        }

        switch (s_app)
        {
            case APP_MENU:
                menu_process();
                break;

            case APP_DUNGEON:
                dungeon_game_process();
                break;

            case APP_RUNNER:
                runner_game_process();
                if (runner_game_exit_requested())
                {
                    enter_menu();
                }
                break;

            default:
                enter_menu();
                break;
        }

        if (!app_has_pending_work() && !uart_tx_idle(UART_A0))
        {
            (void)uart_flush(UART_A0);
        }
        __disable_interrupt();
        if (!app_has_pending_work())
        {
            pmm_enter_lpm(uart_tx_idle(UART_A0) ? PMM_LPM1 : PMM_LPM0);
        }
        else
        {
            __enable_interrupt();
        }
    }
}
