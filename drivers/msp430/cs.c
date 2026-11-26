/*
 * cs.c - Clock System driver for MSP430FR5994
 */
#include "cs.h"
#include "gpio.h"
#include "hal_common.h"

#define VLO_NOMINAL_HZ      (9400ul)    /* datasheet fVLO typ */
#define LFXT_HZ             (32768ul)

static uint8_t  s_mclk_mhz  = 1u;       /* after PUC: DCO 8 MHz / 8 */
static uint8_t  s_smclk_div = 3u;       /* log2 divider, PUC default /8 */
static bool     s_lfxt_ok   = false;

static const uint8_t  k_mhz[]      = { 1u, 4u, 8u, 16u };
static const uint16_t k_dco_bits[] =
{
    DCOFSEL_0,              /* 1 MHz  (DCORSEL=0) */
    DCOFSEL_3,              /* 4 MHz  (DCORSEL=0) */
    DCOFSEL_6,              /* 8 MHz  (DCORSEL=0) */
    DCORSEL | DCOFSEL_4     /* 16 MHz (DCORSEL=1) */
};

static inline void cs_unlock(void) { CSCTL0_H = CSKEY_H; }
static inline void cs_lock(void)   { CSCTL0_H = 0u; }

static void fram_set_wait_states(uint16_t nwaits)
{
    FRCTL0 = FRCTLPW | (FRCTL0_L & (uint8_t)~NWAITS) | nwaits;
    FRCTL0_H = 0u;      /* re-lock */
}

/*
 * Core DCO change with the CS12 workaround. CS must be unlocked.
 * new_divs: DIVS__x field value to apply when restoring dividers.
 */
static void dco_change(cs_dco_t dco, uint16_t new_divs)
{
    const bool going_fast = (k_mhz[dco] > 8u);

    if (going_fast)
    {
        fram_set_wait_states(NWAITS_1);     /* before the speed-up */
    }

    uint16_t saved = CSCTL3;

    /* CS12: make sure MCLK and SMCLK are divided by at least 4 */
    uint16_t divm = saved & DIVM;
    uint16_t divs = saved & DIVS;
    if (divm < DIVM__4) { divm = DIVM__4; }
    if (divs < DIVS__4) { divs = DIVS__4; }
    CSCTL3 = (saved & (uint16_t)~(DIVM | DIVS)) | divs | divm;

    CSCTL1 = k_dco_bits[dco];

    /*
     * Wait >= 10 us. Worst case is 16 MHz / 4 = 4 MHz -> 60 cycles = 15 us.
     */
    __delay_cycles(60);

    /* MCLK /1, SMCLK as requested, ACLK divider untouched */
    CSCTL3 = (saved & (uint16_t)~(DIVM | DIVS)) | DIVM__1 | new_divs;

    if (!going_fast)
    {
        fram_set_wait_states(NWAITS_0);     /* after the slow-down */
    }

    s_mclk_mhz = k_mhz[dco];
}

static bool lfxt_start(uint16_t timeout_ms, bool low_drive)
{
    /* PJSEL1.4=0, PJSEL0.4=1 -> LFXT crystal mode (datasheet port PJ table) */
    gpio_set_function(GPIO_PORT_J, GPIO_PIN4 | GPIO_PIN5, GPIO_FUNC_PRIMARY);

    /* Max drive for a reliable start-up, crystal mode, oscillator on */
    CSCTL4 = (CSCTL4 & (uint16_t)~(LFXTOFF | LFXTBYPASS | LFXTDRIVE)) | LFXTDRIVE_3;

    if (timeout_ms == 0u) { timeout_ms = 1000u; }

    do
    {
        CSCTL5 &= (uint16_t)~LFXTOFFG;
        SFRIFG1 &= (uint16_t)~OFIFG;
        cs_delay_ms(1u);
        if (!(SFRIFG1 & OFIFG) && !(CSCTL5 & LFXTOFFG))
        {
            if (low_drive)
            {
                CSCTL4 = (CSCTL4 & (uint16_t)~LFXTDRIVE) | LFXTDRIVE_0;
            }
            return true;
        }
    } while (--timeout_ms);

    /* Failed: switch crystal off and give the pins back as output-low GPIO */
    CSCTL4 |= LFXTOFF;
    gpio_set_function(GPIO_PORT_J, GPIO_PIN4 | GPIO_PIN5, GPIO_FUNC_IO);
    gpio_set_output(GPIO_PORT_J, GPIO_PIN4 | GPIO_PIN5, false);
    CSCTL5 &= (uint16_t)~LFXTOFFG;
    SFRIFG1 &= (uint16_t)~OFIFG;
    return false;
}

cs_status_t cs_init(const cs_config_t *cfg)
{
    static const cs_config_t k_default = CS_CONFIG_DEFAULT;
    if (cfg == 0) { cfg = &k_default; }

    cs_unlock();

    /* ACLK from VLO until the crystal is proven; MCLK/SMCLK from DCO */
    CSCTL2 = SELA__VLOCLK | SELS__DCOCLK | SELM__DCOCLK;
    CSCTL3 = (CSCTL3 & (uint16_t)~DIVA) | DIVA__1;

    s_smclk_div = (uint8_t)cfg->smclk_div;
    dco_change(cfg->dco, (uint16_t)((uint16_t)cfg->smclk_div << 4));   /* DIVS field at bits 4-6 */

    s_lfxt_ok = false;
    if (cfg->use_lfxt)
    {
        s_lfxt_ok = lfxt_start(cfg->lfxt_timeout_ms, cfg->lfxt_low_drive);
        if (s_lfxt_ok)
        {
            CSCTL2 = (CSCTL2 & (uint16_t)~SELA) | SELA__LFXTCLK;
        }
    }
    else
    {
        CSCTL4 |= LFXTOFF;
    }

    cs_lock();
    return (cfg->use_lfxt && !s_lfxt_ok) ? CS_LFXT_FAULT : CS_OK;
}

void cs_set_dco(cs_dco_t dco)
{
    if ((unsigned)dco > (unsigned)CS_DCO_16MHZ) { return; }
    uint16_t s = hal_enter_critical();
    cs_unlock();
    dco_change(dco, (uint16_t)((uint16_t)s_smclk_div << 4));
    cs_lock();
    hal_exit_critical(s);
}

uint32_t cs_get_mclk_hz(void)  { return (uint32_t)s_mclk_mhz * 1000000ul; }
uint32_t cs_get_smclk_hz(void) { return cs_get_mclk_hz() >> s_smclk_div; }
uint32_t cs_get_aclk_hz(void)  { return s_lfxt_ok ? LFXT_HZ : VLO_NOMINAL_HZ; }
bool     cs_lfxt_running(void) { return s_lfxt_ok && !(CSCTL5 & LFXTOFFG); }

void cs_delay_ms(uint16_t ms)
{
    while (ms--)
    {
        uint8_t n = s_mclk_mhz;
        while (n--)
        {
            __delay_cycles(994);    /* + ~6 cycles loop overhead = 1000 */
        }
    }
}

void cs_delay_us(uint16_t us)
{
    /*
     * Approximate: a 16-bit decrement-and-branch loop costs ~4 cycles,
     * so pad each iteration to 1 us at the current MCLK.
     */
    switch (s_mclk_mhz)
    {
    case 16u: while (us--) { __delay_cycles(12); } break;
    case 8u:  while (us--) { __delay_cycles(4);  } break;
    case 4u:  while (us--) { __no_operation();   } break;
    default:  us >>= 2; while (us--) { __no_operation(); } break;  /* 1 MHz */
    }
}
