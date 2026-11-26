/*
 * gpio.c - Digital I/O driver for MSP430FR5994
 */
#include "gpio.h"
#include "hal_common.h"
#include <stddef.h>

/* Register map for one 8-bit port. Interrupt registers are NULL for PJ. */
typedef struct
{
    volatile uint8_t *in;
    volatile uint8_t *out;
    volatile uint8_t *dir;
    volatile uint8_t *ren;
    volatile uint8_t *sel0;
    volatile uint8_t *sel1;
    volatile uint8_t *selc;
    volatile uint8_t *ies;
    volatile uint8_t *ie;
    volatile uint8_t *ifg;
} gpio_regs_t;

#define PORT_REGS(n) \
    { &P##n##IN, &P##n##OUT, &P##n##DIR, &P##n##REN, &P##n##SEL0, &P##n##SEL1, \
      &P##n##SELC, &P##n##IES, &P##n##IE, &P##n##IFG }

static const gpio_regs_t s_regs[GPIO_PORT_COUNT] =
{
    PORT_REGS(1), PORT_REGS(2), PORT_REGS(3), PORT_REGS(4),
    PORT_REGS(5), PORT_REGS(6), PORT_REGS(7), PORT_REGS(8),
    /* PJ: no interrupt capability */
    { &PJIN_L, &PJOUT_L, &PJDIR_L, &PJREN_L, &PJSEL0_L, &PJSEL1_L,
      &PJSELC_L, NULL, NULL, NULL },
};

#define GPIO_IRQ_PORTS  (8u)    /* P1..P8 */

static gpio_callback_t s_callbacks[GPIO_IRQ_PORTS][8];

static inline bool port_valid(gpio_port_t port)
{
    return (unsigned)port < (unsigned)GPIO_PORT_COUNT;
}

static inline bool port_has_irq(gpio_port_t port)
{
    return (unsigned)port < GPIO_IRQ_PORTS;
}

void gpio_init_all_low_power(void)
{
    unsigned p;
    for (p = 0; p < GPIO_PORT_COUNT; p++)
    {
        const gpio_regs_t *r = &s_regs[p];
        *r->out  = 0x00;
        *r->dir  = 0xFF;
        *r->ren  = 0x00;
        *r->sel0 = 0x00;
        *r->sel1 = 0x00;
    }
}

void gpio_set_output(gpio_port_t port, uint8_t pins, bool initial_high)
{
    if (!port_valid(port)) { return; }
    const gpio_regs_t *r = &s_regs[port];
    uint16_t s = hal_enter_critical();
    if (initial_high) { *r->out |= pins; } else { *r->out &= (uint8_t)~pins; }
    *r->ren &= (uint8_t)~pins;
    *r->dir |= pins;
    hal_exit_critical(s);
}

void gpio_set_input(gpio_port_t port, uint8_t pins, gpio_pull_t pull)
{
    if (!port_valid(port)) { return; }
    const gpio_regs_t *r = &s_regs[port];
    uint16_t s = hal_enter_critical();
    *r->dir &= (uint8_t)~pins;
    switch (pull)
    {
    case GPIO_PULL_UP:
        *r->out |= pins;                /* PxOUT selects pull direction */
        *r->ren |= pins;
        break;
    case GPIO_PULL_DOWN:
        *r->out &= (uint8_t)~pins;
        *r->ren |= pins;
        break;
    default:
        *r->ren &= (uint8_t)~pins;
        break;
    }
    hal_exit_critical(s);
}

void gpio_set_function(gpio_port_t port, uint8_t pins, gpio_func_t func)
{
    if (!port_valid(port)) { return; }
    const gpio_regs_t *r = &s_regs[port];

    uint8_t want0 = (func & 1u) ? pins : 0u;
    uint8_t want1 = (func & 2u) ? pins : 0u;

    uint16_t s = hal_enter_critical();
    uint8_t diff0 = (uint8_t)((*r->sel0 ^ want0) & pins);
    uint8_t diff1 = (uint8_t)((*r->sel1 ^ want1) & pins);

    /*
     * Pins where both SEL bits must flip (01 <-> 10) are changed in one
     * write via PxSELC so the pin never passes through an unintended
     * function (SLAU367P 12.2.6).
     */
    uint8_t both = (uint8_t)(diff0 & diff1);
    if (both) { *r->selc = both; }

    uint8_t only0 = (uint8_t)(diff0 & (uint8_t)~both);
    uint8_t only1 = (uint8_t)(diff1 & (uint8_t)~both);
    if (only0) { *r->sel0 ^= only0; }
    if (only1) { *r->sel1 ^= only1; }
    hal_exit_critical(s);
}

void gpio_set(gpio_port_t port, uint8_t pins)
{
    if (port_valid(port)) { *s_regs[port].out |= pins; }
}

void gpio_clear(gpio_port_t port, uint8_t pins)
{
    if (port_valid(port)) { *s_regs[port].out &= (uint8_t)~pins; }
}

void gpio_toggle(gpio_port_t port, uint8_t pins)
{
    if (port_valid(port)) { *s_regs[port].out ^= pins; }
}

void gpio_write(gpio_port_t port, uint8_t pins, bool high)
{
    if (high) { gpio_set(port, pins); } else { gpio_clear(port, pins); }
}

uint8_t gpio_read(gpio_port_t port, uint8_t pins)
{
    return port_valid(port) ? (uint8_t)(*s_regs[port].in & pins) : 0u;
}

gpio_status_t gpio_set_interrupt_edge(gpio_port_t port, uint8_t pins, gpio_edge_t edge)
{
    if (!port_has_irq(port)) { return GPIO_ERR_PARAM; }
    const gpio_regs_t *r = &s_regs[port];
    uint16_t s = hal_enter_critical();
    if (edge == GPIO_EDGE_FALLING) { *r->ies |= pins; } else { *r->ies &= (uint8_t)~pins; }
    hal_exit_critical(s);
    return GPIO_OK;
}

gpio_status_t gpio_set_callback(gpio_port_t port, uint8_t pins, gpio_callback_t cb)
{
    if (!port_has_irq(port)) { return GPIO_ERR_PARAM; }
    uint8_t i;
    uint16_t s = hal_enter_critical();
    for (i = 0; i < 8u; i++)
    {
        if (pins & (uint8_t)(1u << i)) { s_callbacks[port][i] = cb; }
    }
    hal_exit_critical(s);
    return GPIO_OK;
}

gpio_status_t gpio_interrupt_enable(gpio_port_t port, uint8_t pins)
{
    if (!port_has_irq(port)) { return GPIO_ERR_PARAM; }
    *s_regs[port].ie |= pins;
    return GPIO_OK;
}

gpio_status_t gpio_interrupt_disable(gpio_port_t port, uint8_t pins)
{
    if (!port_has_irq(port)) { return GPIO_ERR_PARAM; }
    *s_regs[port].ie &= (uint8_t)~pins;
    return GPIO_OK;
}

gpio_status_t gpio_interrupt_clear(gpio_port_t port, uint8_t pins)
{
    if (!port_has_irq(port)) { return GPIO_ERR_PARAM; }
    *s_regs[port].ifg &= (uint8_t)~pins;
    return GPIO_OK;
}

uint8_t gpio_interrupt_pending(gpio_port_t port, uint8_t pins)
{
    return port_has_irq(port) ? (uint8_t)(*s_regs[port].ifg & pins) : 0u;
}

void gpio_clear_all_interrupt_flags(void)
{
    unsigned p;
    for (p = 0; p < GPIO_IRQ_PORTS; p++) { *s_regs[p].ifg = 0x00; }
}

/* ---------------------------------------------------------------------------
 * ISRs: reading PxIV returns the highest-priority pending pin and clears
 * its flag. Loop until 0 so simultaneous edges are all serviced.
 * ------------------------------------------------------------------------- */
static bool dispatch(gpio_port_t port, volatile uint16_t *iv)
{
    bool wake = false;
    uint16_t v;
    while ((v = *iv) != 0u)
    {
        uint8_t pin = (uint8_t)((v >> 1) - 1u);    /* 0x02 -> pin 0 ... 0x10 -> pin 7 */
        gpio_callback_t cb = s_callbacks[port][pin & 7u];
        if (cb && cb(port, pin)) { wake = true; }
    }
    return wake;
}

#define GPIO_ISR(n)                                                     \
    __interrupt void port##n##_isr(void)                                \
    {                                                                   \
        if (dispatch(GPIO_PORT_##n, &P##n##IV))                         \
        {                                                               \
            __bic_SR_register_on_exit(LPM4_bits);                       \
        }                                                               \
    }

#pragma vector = PORT1_VECTOR
GPIO_ISR(1)
#pragma vector = PORT2_VECTOR
GPIO_ISR(2)
#pragma vector = PORT3_VECTOR
GPIO_ISR(3)
#pragma vector = PORT4_VECTOR
GPIO_ISR(4)
#pragma vector = PORT5_VECTOR
GPIO_ISR(5)
#pragma vector = PORT6_VECTOR
GPIO_ISR(6)
#pragma vector = PORT7_VECTOR
GPIO_ISR(7)
#pragma vector = PORT8_VECTOR
GPIO_ISR(8)
