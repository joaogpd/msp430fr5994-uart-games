/*
 * board.c - MSP-EXP430FR5994 LaunchPad bring-up
 */
#include "board.h"
#include "drivers/msp430/pmm.h"
#include "drivers/msp430/wdt.h"

void board_init(void (*before_unlock)(void))
{
    wdt_hold();
    pmm_capture_reset_cause();

    /* 1. Port configuration (takes effect when LOCKLPM5 is cleared) */
    gpio_init_all_low_power();

    gpio_set_output(BOARD_LED1_PORT, BOARD_LED1_PIN, false);
    gpio_set_output(BOARD_LED2_PORT, BOARD_LED2_PIN, false);

    gpio_set_input(BOARD_S1_PORT, BOARD_S1_PIN | BOARD_S2_PIN, GPIO_PULL_UP);
    gpio_set_interrupt_edge(BOARD_S1_PORT, BOARD_S1_PIN | BOARD_S2_PIN, GPIO_EDGE_FALLING);

    /*
     * The eZ-FET drives P2.1 (target RXD) through J101. Driving it low would
     * fight the debugger's idle-high TX, so hold it as a pulled-up input.
     * TXD idles high. With J101 removed (supercap use) the pull-up keeps the
     * pin from floating.
     */
    gpio_set_output(BOARD_UART_PORT, BOARD_UART_TX_PIN, true);
    gpio_set_input(BOARD_UART_PORT, BOARD_UART_RX_PIN, GPIO_PULL_UP);

    /* LaunchPad has a 32.768 kHz crystal on PJ.4/PJ.5 (PJSEL0.4 = 1) */
    gpio_set_function(GPIO_PORT_J, GPIO_PIN4 | GPIO_PIN5, GPIO_FUNC_PRIMARY);

    if (before_unlock) { before_unlock(); }

    /* 2. Release the I/O lock */
    pmm_unlock_io();

    /* 3. Clear spurious flags, but keep the wake-up flag after LPMx.5 */
    if (!pmm_woke_from_lpmx5())
    {
        gpio_clear_all_interrupt_flags();
    }
}

void board_uart_pins(bool on)
{
    if (on)
    {
        /* UCA0TXD / UCA0RXD are the secondary function of P2.0 / P2.1 */
        gpio_set_function(BOARD_UART_PORT, BOARD_UART_TX_PIN | BOARD_UART_RX_PIN, GPIO_FUNC_SECONDARY);
    }
    else
    {
        gpio_set_function(BOARD_UART_PORT, BOARD_UART_TX_PIN | BOARD_UART_RX_PIN, GPIO_FUNC_IO);
        gpio_set_output(BOARD_UART_PORT, BOARD_UART_TX_PIN, true);
        gpio_set_input(BOARD_UART_PORT, BOARD_UART_RX_PIN, GPIO_PULL_UP);
    }
}
