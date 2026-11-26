/*
 * timer.c - Timer_A driver (TA0..TA4), periodic / one-shot / sleep
 */
#include "timer.h"
#include "cs.h"
#include "pmm.h"
#include "hal_common.h"

/* Register offsets, identical on every Timer_A instance */
#define OFS_CTL     (0x00u)
#define OFS_CCTL0   (0x02u)
#define OFS_R       (0x10u)
#define OFS_CCR0    (0x12u)
#define OFS_EX0     (0x20u)

#define TREG(id, ofs) (*(volatile uint16_t *)(uintptr_t)(k_base[id] + (ofs)))

static const uint16_t k_base[TIMER_COUNT] = { TA0_BASE, TA1_BASE, TA2_BASE, TA3_BASE, TA4_BASE };

static timer_callback_t s_cb[TIMER_COUNT];
static volatile uint8_t s_oneshot;      /* bit per timer */
static volatile uint8_t s_running;      /* bit per timer */

/*
 * Pick the smallest total divider (ID x TAIDEX, 1..64) so that the tick
 * count fits in 16 bits. Returns false if even /64 is not enough.
 */
static bool compute(uint32_t ms, uint16_t *ccr0, uint16_t *id_bits, uint16_t *idex)
{
    static const uint8_t k_id[]   = { 1u, 2u, 4u, 8u };
    uint32_t aclk = cs_get_aclk_hz();
    uint8_t i, x;

    if (ms == 0u || ms > 130000ul) { return false; }   /* keeps ms * aclk in 32 bits */
    for (i = 0; i < 4u; i++)
    {
        for (x = 1u; x <= 8u; x++)
        {
            uint32_t div = (uint32_t)k_id[i] * x;
            uint32_t ticks = (ms * aclk + (div * 1000u) / 2u) / (div * 1000u);
            if (ticks >= 1u && ticks <= 65536ul)
            {
                *ccr0 = (uint16_t)(ticks - 1u);     /* up mode period = CCR0 + 1 */
                *id_bits = (uint16_t)i << 6;        /* ID field bits 7-6 */
                *idex = (uint16_t)(x - 1u);         /* TAIDEX 0..7 = /1../8 */
                return true;
            }
        }
    }
    return false;
}

static bool start(timer_id_t id, uint32_t ms, timer_callback_t cb, bool oneshot)
{
    uint16_t ccr0, idb, idex;
    if ((unsigned)id >= TIMER_COUNT || !compute(ms, &ccr0, &idb, &idex)) { return false; }

    uint16_t s = hal_enter_critical();
    TREG(id, OFS_CTL)   = TACLR;                    /* stop and clear */
    TREG(id, OFS_CCTL0) = 0u;
    s_cb[id] = cb;
    if (oneshot) { s_oneshot |= (uint8_t)(1u << id); } else { s_oneshot &= (uint8_t)~(1u << id); }
    s_running |= (uint8_t)(1u << id);
    TREG(id, OFS_EX0)   = idex;
    TREG(id, OFS_CCR0)  = ccr0;
    TREG(id, OFS_CCTL0) = CCIE;
    TREG(id, OFS_CTL)   = TASSEL__ACLK | idb | MC__UP | TACLR;
    hal_exit_critical(s);
    return true;
}

bool timer_start_periodic_ms(timer_id_t id, uint32_t ms, timer_callback_t cb)
{
    return start(id, ms, cb, false);
}

bool timer_start_oneshot_ms(timer_id_t id, uint32_t ms, timer_callback_t cb)
{
    return start(id, ms, cb, true);
}

void timer_stop(timer_id_t id)
{
    if ((unsigned)id >= TIMER_COUNT) { return; }
    TREG(id, OFS_CTL)   = MC__STOP;
    TREG(id, OFS_CCTL0) = 0u;
    s_running &= (uint8_t)~(1u << id);
}

bool timer_running(timer_id_t id)
{
    return ((unsigned)id < TIMER_COUNT) && (s_running & (1u << id));
}

static volatile uint8_t s_sleep_done;

static bool sleep_cb(timer_id_t id)
{
    s_sleep_done |= (uint8_t)(1u << id);
    return true;
}

void timer_sleep_ms(timer_id_t id, uint32_t ms)
{
    if ((unsigned)id >= TIMER_COUNT) { return; }
    s_sleep_done &= (uint8_t)~(1u << id);
    if (!start(id, ms, sleep_cb, true)) { return; }
    while (!(s_sleep_done & (1u << id)))
    {
        pmm_enter_lpm(PMM_LPM3);    /* other wake-ups just loop back here */
    }
}

/* CCR0 vector: CCIFG is cleared automatically when this ISR is serviced */
static bool on_ccr0(timer_id_t id)
{
    if (s_oneshot & (1u << id))
    {
        TREG(id, OFS_CTL)   = MC__STOP;
        TREG(id, OFS_CCTL0) = 0u;
        s_running &= (uint8_t)~(1u << id);
    }
    timer_callback_t cb = s_cb[id];
    return cb ? cb(id) : false;
}

#define TIMER_ISR(n)                                                    \
    __interrupt void timer##n##_a0_isr(void)                            \
    {                                                                   \
        if (on_ccr0(TIMER_A##n)) { __bic_SR_register_on_exit(LPM4_bits); } \
    }

#pragma vector = TIMER0_A0_VECTOR
TIMER_ISR(0)
#pragma vector = TIMER1_A0_VECTOR
TIMER_ISR(1)
#pragma vector = TIMER2_A0_VECTOR
TIMER_ISR(2)
#pragma vector = TIMER3_A0_VECTOR
TIMER_ISR(3)
#pragma vector = TIMER4_A0_VECTOR
TIMER_ISR(4)
