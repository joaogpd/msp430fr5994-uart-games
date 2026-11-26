/*
 * uart.h - eUSCI_A UART driver (A0..A3), interrupt driven
 *
 * - TX goes through a ring buffer the caller provides; the TX interrupt
 *   feeds the hardware, so the CPU sleeps between bytes.
 * - RX hands every byte to a callback in interrupt context.
 * - Baud-rate registers are computed from the clock with the SLAU367P
 *   30.3.10 procedure (UCOS16 when N >= 16, UCBRSx from Table 30-4).
 *
 * Clock choice and power:
 *   UART_CLK_ACLK  : the 32.768 kHz crystal. Works in LPM3 (ACLK keeps
 *                    running), so the CPU can sleep while bytes come and
 *                    go. Fine up to 9600 baud (N = 3.41, the setting TI
 *                    recommends in Table 30-5).
 *   UART_CLK_SMCLK : the DCO. Any baud rate, but the eUSCI then requests
 *                    SMCLK in LPM3, which keeps the DCO on (~100 uA).
 *                    Use only when ACLK cannot reach the baud rate, or the
 *                    crystal failed (ACLK on VLO cannot do 9600).
 *
 * Pins are not touched here: route them first (board_uart_pins() for the
 * LaunchPad backchannel on eUSCI_A0).
 *
 * Errata (SLAZ681O): USCI42 - UCTXCPTIFG is set after every byte, not only
 * the last. The driver does not use UCTXCPTIFG; uart_flush() waits for
 * the software buffer to empty and UCBUSY to clear instead. USCI45/47/50
 * concern SPI mode only.
 *
 * Reference: SLAU367P chapter 30 (eUSCI UART mode).
 */
#ifndef DRIVERS_UART_H_
#define DRIVERS_UART_H_

#include <stdint.h>
#include <stdbool.h>

typedef enum
{
    UART_A0 = 0,        /* LaunchPad backchannel (eZ-FET COM port) */
    UART_A1,
    UART_A2,
    UART_A3,            /* LaunchPad BoosterPack header */
    UART_COUNT
} uart_id_t;

typedef enum
{
    UART_CLK_ACLK = 0,
    UART_CLK_SMCLK
} uart_clk_t;

/*
 * ISR context, one call per received byte. error = framing, parity or
 * overrun error on this byte. Return true to wake main from LPM.
 */
typedef bool (*uart_rx_callback_t)(uart_id_t id, uint8_t byte, bool error);

typedef struct
{
    uint32_t           baud;
    uart_clk_t         clk;
    uint8_t           *tx_buf;      /* ring buffer, owned by the caller */
    uint16_t           tx_size;     /* >= 2; one slot stays free */
    uart_rx_callback_t rx_cb;       /* NULL: receiver interrupt off */
} uart_config_t;

/*
 * 8N1 at cfg->baud. Returns false (and leaves the module in reset) if an
 * argument is bad or the clock is too slow for the baud rate (N < 3).
 * Call after cs_init(): it reads the clock frequencies from cs.
 */
bool uart_init(uart_id_t id, const uart_config_t *cfg);

/* Hold the module in reset; pending TX bytes are dropped. */
void uart_deinit(uart_id_t id);

/* Queue up to len bytes without waiting; returns how many were queued.
   Safe from interrupt context. */
uint16_t uart_write(uart_id_t id, const void *data, uint16_t len);

/* Queue all len bytes, sleeping in LPM3 while the buffer is full.
   Main context only (sets GIE). */
void uart_write_all(uart_id_t id, const void *data, uint16_t len);

/* true when nothing is queued and the last byte has left the pin */
bool uart_tx_idle(uart_id_t id);

/* Wait (LPM3) until everything queued has been sent. Main context only.
   Returns false if the module is not running. */
bool uart_flush(uart_id_t id);

#endif /* DRIVERS_UART_H_ */
