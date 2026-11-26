/*
 * uart.c - eUSCI_A UART driver (A0..A3), interrupt driven
 */
#include "uart.h"
#include "cs.h"
#include "pmm.h"
#include "hal_common.h"

/* Register offsets, identical on every eUSCI_A instance */
#define OFS_CTLW0   (0x00u)
#define OFS_BRW     (0x06u)
#define OFS_MCTLW   (0x08u)
#define OFS_STATW   (0x0Au)
#define OFS_RXBUF   (0x0Cu)
#define OFS_TXBUF   (0x0Eu)
#define OFS_IE      (0x1Au)
#define OFS_IFG     (0x1Cu)
#define OFS_IV      (0x1Eu)

#define UREG(id, ofs) (*(volatile uint16_t *)(uintptr_t)(k_base[id] + (ofs)))

static const uint16_t k_base[UART_COUNT] = { EUSCI_A0_BASE, EUSCI_A1_BASE, EUSCI_A2_BASE, EUSCI_A3_BASE };

typedef struct
{
    uint8_t           *buf;
    uint16_t           size;
    volatile uint16_t  head;        /* written by uart_write */
    volatile uint16_t  tail;        /* advanced by the TX ISR */
    uart_rx_callback_t rx_cb;
    volatile bool      waiting;     /* main sleeps until the TX ISR moves */
    bool               running;
} uart_state_t;

static uart_state_t s_u[UART_COUNT];

/*
 * SLAU367P Table 30-4: UCBRSx for the fractional part of N, in units of
 * 1/10000. Each value holds from its threshold up to the next one.
 */
static const uint16_t k_frac[] = {    0,  529,  715,  835, 1001, 1252, 1430, 1670, 2147, 2224, 2503, 3000,
                                   3335, 3575, 3753, 4003, 4286, 4378, 5002, 5715, 6003, 6254, 6432, 6667,
                                   7001, 7147, 7503, 7861, 8004, 8333, 8464, 8572, 8751, 9004, 9170, 9288 };
static const uint8_t  k_brs[]  = { 0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x11, 0x21, 0x22, 0x44, 0x25,
                                   0x49, 0x4A, 0x52, 0x92, 0x53, 0x55, 0xAA, 0x6B, 0xAD, 0xB5, 0xB6, 0xD6,
                                   0xB7, 0xBB, 0xDD, 0xED, 0xEE, 0xBF, 0xDF, 0xEF, 0xF7, 0xFB, 0xFD, 0xFE };

static uint8_t brs_for(uint16_t frac_e4)
{
    uint8_t i, v = 0u;
    for (i = 0; i < sizeof k_frac / sizeof k_frac[0]; i++)
    {
        if (frac_e4 >= k_frac[i]) { v = k_brs[i]; }
    }
    return v;
}

bool uart_init(uart_id_t id, const uart_config_t *cfg)
{
    if ((unsigned)id >= UART_COUNT || !cfg || cfg->baud == 0u || !cfg->tx_buf || cfg->tx_size < 2u)
    {
        return false;
    }

    UREG(id, OFS_CTLW0) = UCSWRST;
    s_u[id].running = false;

    uint32_t brclk = (cfg->clk == UART_CLK_ACLK) ? cs_get_aclk_hz() : cs_get_smclk_hz();
    uint32_t n = brclk / cfg->baud;
    if (n < 3u) { return false; }       /* too few clocks per bit to sample */
    uint16_t frac = (uint16_t)(((brclk % cfg->baud) * 10000ul) / cfg->baud);

    uint16_t brw, mctlw;
    if (n >= 16u)
    {
        brw   = (uint16_t)(n / 16u);
        mctlw = (uint16_t)((uint16_t)brs_for(frac) << 8) | (uint16_t)((n % 16u) << 4) | UCOS16;
    }
    else
    {
        brw   = (uint16_t)n;
        mctlw = (uint16_t)((uint16_t)brs_for(frac) << 8);
    }

    s_u[id].buf   = cfg->tx_buf;
    s_u[id].size  = cfg->tx_size;
    s_u[id].head  = 0u;
    s_u[id].tail  = 0u;
    s_u[id].rx_cb = cfg->rx_cb;
    s_u[id].waiting = false;

    UREG(id, OFS_CTLW0) = UCSWRST | ((cfg->clk == UART_CLK_ACLK) ? UCSSEL__ACLK : UCSSEL__SMCLK);
    UREG(id, OFS_BRW)   = brw;
    UREG(id, OFS_MCTLW) = mctlw;
    UREG(id, OFS_CTLW0) &= (uint16_t)~UCSWRST;  /* clears IE: set it after */
    UREG(id, OFS_IFG)   &= (uint16_t)~UCRXIFG;
    UREG(id, OFS_IE)    = cfg->rx_cb ? UCRXIE : 0u;
    s_u[id].running = true;
    return true;
}

void uart_deinit(uart_id_t id)
{
    if ((unsigned)id >= UART_COUNT) { return; }
    UREG(id, OFS_CTLW0) |= UCSWRST;     /* also clears UCxIE */
    s_u[id].running = false;
    s_u[id].head = s_u[id].tail = 0u;
}

static uint16_t used(const uart_state_t *u)
{
    uint16_t h = u->head, t = u->tail;
    return (h >= t) ? (uint16_t)(h - t) : (uint16_t)(u->size - t + h);
}

uint16_t uart_write(uart_id_t id, const void *data, uint16_t len)
{
    if ((unsigned)id >= UART_COUNT || !s_u[id].running) { return 0u; }
    uart_state_t *u = &s_u[id];
    const uint8_t *p = (const uint8_t *)data;
    uint16_t n = 0u;

    uint16_t s = hal_enter_critical();
    while (n < len && used(u) < (uint16_t)(u->size - 1u))
    {
        u->buf[u->head] = p[n++];
        u->head = (uint16_t)((u->head + 1u) % u->size);
    }
    if (n && !(UREG(id, OFS_IE) & UCTXIE))
    {
        /*
         * TX was idle. Reading UCxIV in the ISR that went idle cleared
         * UCTXIFG although TXBUF was free, so set it again by hand: TXBUF
         * is empty whenever UCTXIE is off (the ISR only turns it off after
         * its last write has moved on to the shift register).
         */
        UREG(id, OFS_IFG) |= UCTXIFG;
        UREG(id, OFS_IE)  |= UCTXIE;
    }
    hal_exit_critical(s);
    return n;
}

void uart_write_all(uart_id_t id, const void *data, uint16_t len)
{
    if ((unsigned)id >= UART_COUNT || !s_u[id].running) { return; }
    const uint8_t *p = (const uint8_t *)data;
    for (;;)
    {
        uint16_t n = uart_write(id, p, len);
        p += n;
        len = (uint16_t)(len - n);
        if (!len) { return; }

        /* Buffer full: sleep until the TX ISR frees a slot */
        __disable_interrupt();
        s_u[id].waiting = true;
        if (used(&s_u[id]) >= (uint16_t)(s_u[id].size - 1u))
        {
            pmm_enter_lpm(PMM_LPM3);    /* sets GIE atomically */
        }
        else
        {
            __enable_interrupt();
        }
        s_u[id].waiting = false;
    }
}

bool uart_tx_idle(uart_id_t id)
{
    if ((unsigned)id >= UART_COUNT || !s_u[id].running) { return true; }
    return s_u[id].head == s_u[id].tail && !(UREG(id, OFS_STATW) & UCBUSY);
}

bool uart_flush(uart_id_t id)
{
    if ((unsigned)id >= UART_COUNT || !s_u[id].running) { return false; }

    /* 1. Let the ISR move every queued byte into TXBUF */
    for (;;)
    {
        __disable_interrupt();
        if (s_u[id].head == s_u[id].tail) { __enable_interrupt(); break; }
        s_u[id].waiting = true;
        pmm_enter_lpm(PMM_LPM3);
        s_u[id].waiting = false;
    }

    /*
     * 2. The last byte is still shifting out (UCBUSY, at most ~1.1 ms at
     * 9600 baud). Poll: UCTXCPTIFG cannot mark the last byte (USCI42).
     */
    uint32_t guard;
    for (guard = 200000ul; (UREG(id, OFS_STATW) & UCBUSY) && guard; guard--) { }
    return guard != 0u;
}

/* ------------------------------------------------------------------ ISR */

static bool service(uart_id_t id)
{
    uart_state_t *u = &s_u[id];
    bool wake = false;
    uint16_t iv;
    while ((iv = UREG(id, OFS_IV)) != UCIV__NONE)
    {
        if (iv == UCIV__UCRXIFG)
        {
            bool err = (UREG(id, OFS_STATW) & UCRXERR) != 0u;   /* read before RXBUF */
            uint8_t b = (uint8_t)UREG(id, OFS_RXBUF);           /* clears UCRXIFG and errors */
            if (u->rx_cb && u->rx_cb(id, b, err)) { wake = true; }
        }
        else if (iv == UCIV__UCTXIFG)
        {
            if (u->head != u->tail)
            {
                UREG(id, OFS_TXBUF) = u->buf[u->tail];
                u->tail = (uint16_t)((u->tail + 1u) % u->size);
            }
            else
            {
                UREG(id, OFS_IE) &= (uint16_t)~UCTXIE;
            }
            if (u->waiting) { wake = true; }
        }
    }
    return wake;
}

#pragma vector = EUSCI_A0_VECTOR
__interrupt void uart_a0_isr(void) { if (service(UART_A0)) { __bic_SR_register_on_exit(LPM4_bits); } }

#pragma vector = EUSCI_A1_VECTOR
__interrupt void uart_a1_isr(void) { if (service(UART_A1)) { __bic_SR_register_on_exit(LPM4_bits); } }

#pragma vector = EUSCI_A2_VECTOR
__interrupt void uart_a2_isr(void) { if (service(UART_A2)) { __bic_SR_register_on_exit(LPM4_bits); } }

#pragma vector = EUSCI_A3_VECTOR
__interrupt void uart_a3_isr(void) { if (service(UART_A3)) { __bic_SR_register_on_exit(LPM4_bits); } }
