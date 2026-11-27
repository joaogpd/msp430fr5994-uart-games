#include <msp430.h>
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#include "drivers/board/board.h"
#include "drivers/msp430/cs.h"
#include "drivers/msp430/gpio.h"
#include "drivers/msp430/pmm.h"
#include "drivers/msp430/timer.h"
#include "drivers/msp430/uart.h"
#include "app/dungeon_game.h"

/* 1024-byte TX ring buffer holds a full ANSI dungeon frame in one call */
static uint8_t s_uart_tx_buf[1024];

static bool on_uart_rx(uart_id_t id, uint8_t byte, bool error)
{
    (void)id;
    if (error)
    {
        return false;
    }
    return dungeon_game_push_input(byte);
}

static bool on_button_edge(gpio_port_t port, uint8_t pin)
{
    (void)port;
    (void)pin;
    return dungeon_game_on_button_irq();
}

int main(void)
{
    /* 1. Safe pin & watchdog bring-up (configures S1/S2 falling-edge interrupts) */
    board_init(NULL);

    /* 2. Configure 8 MHz DCO (MCLK/SMCLK) and 32.768 kHz LFXT (ACLK) */
    cs_config_t cs_cfg = CS_CONFIG_DEFAULT;
    (void)cs_init(&cs_cfg);

    /* 3. Route P2.0/P2.1 to eUSCI_A0 (SMCLK @ 115200 baud for snappy ANSI frames) */
    board_uart_pins(true);
    uart_config_t uart_cfg = {
        .baud    = 115200u,
        .clk     = UART_CLK_SMCLK,
        .tx_buf  = s_uart_tx_buf,
        .tx_size = (uint16_t)sizeof(s_uart_tx_buf),
        .rx_cb   = on_uart_rx
    };
    (void)uart_init(UART_A0, &uart_cfg);

    /* 4. Enable falling-edge GPIO interrupts on S1 (P5.6) and S2 (P5.5) */
    gpio_set_callback(BOARD_S1_PORT, BOARD_S1_PIN, on_button_edge);
    gpio_set_callback(BOARD_S2_PORT, BOARD_S2_PIN, on_button_edge);
    gpio_interrupt_enable(BOARD_S1_PORT, BOARD_S1_PIN);
    gpio_interrupt_enable(BOARD_S2_PORT, BOARD_S2_PIN);

    /* 5. Initialize dungeon state & start 500 ms terminal-connect probe timer */
    dungeon_game_init();
    (void)timer_start_periodic_ms(TIMER_A1, 500u, dungeon_game_on_probe_timer);
    __bis_SR_register(GIE);

    while (1)
    {
        dungeon_game_process();

        /* Flush UART TX in LPM0 (avoids PMM31), then sleep in LPM3 when idle */
        if (!dungeon_game_has_pending_work() && !uart_tx_idle(UART_A0))
        {
            (void)uart_flush(UART_A0);
        }
        __disable_interrupt();
        if (!dungeon_game_has_pending_work())
        {
            pmm_enter_lpm(uart_tx_idle(UART_A0) ? PMM_LPM3 : PMM_LPM0);
        }
        else
        {
            __enable_interrupt();
        }
    }
}
