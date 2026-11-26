/*
 * wdt.c - Watchdog timer (WDT_A) driver
 */
#include "wdt.h"
#include "hal_common.h"

static const uint16_t k_ssel[] = { WDTSSEL__SMCLK, WDTSSEL__ACLK, WDTSSEL__VLOCLK };

/* Low byte of WDTCTL (no password, no CNTCL). Written as WDTPW | s_ctl. */
static uint16_t       s_ctl = WDTHOLD;
static wdt_callback_t s_cb;

void wdt_hold(void)
{
    WDTCTL = WDTPW | WDTHOLD;
    s_ctl = WDTHOLD;
}

void wdt_start_watchdog(wdt_clk_t clk, wdt_div_t div)
{
    SFRIE1 &= (uint16_t)~WDTIE;
    s_ctl = k_ssel[clk] | (uint16_t)div;
    WDTCTL = WDTPW | WDTCNTCL | s_ctl;
}

void wdt_start_interval(wdt_clk_t clk, wdt_div_t div, wdt_callback_t cb)
{
    WDTCTL = WDTPW | WDTHOLD;           /* hold while reconfiguring */
    s_cb  = cb;
    s_ctl = WDTTMSEL | k_ssel[clk] | (uint16_t)div;
    SFRIFG1 &= (uint16_t)~WDTIFG;
    WDTCTL = WDTPW | WDTCNTCL | s_ctl;
    SFRIE1 |= WDTIE;
}

void wdt_kick(void)
{
    WDTCTL = WDTPW | WDTCNTCL | s_ctl;
}

void wdt_pause(void)
{
    WDTCTL = WDTPW | WDTHOLD | s_ctl;
}

void wdt_resume(void)
{
    if (s_ctl == WDTHOLD) { return; }   /* never configured: stay held */
    s_ctl &= (uint16_t)~WDTHOLD;
    WDTCTL = WDTPW | WDTCNTCL | s_ctl;
}

/* Interval mode: WDTIFG is cleared automatically when this ISR is serviced */
#pragma vector = WDT_VECTOR
__interrupt void wdt_isr(void)
{
    if (s_cb && s_cb())
    {
        __bic_SR_register_on_exit(LPM4_bits);
    }
}
