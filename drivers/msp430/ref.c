/*
 * ref.c - REF_A internal voltage reference
 */
#include "ref.h"
#include "hal_common.h"

/* TLV REF calibration block (datasheet Table 9-40) */
#define TLV_REF_TAG_ADDR    (0x1A26u)
#define TLV_REF_TAG         (0x12u)
#define TLV_REF_1V2_FACTOR  (0x1A28u)
#define TLV_REF_2V0_FACTOR  (0x1A2Au)
#define TLV_REF_2V5_FACTOR  (0x1A2Cu)

#define TLV16(addr) (*(const volatile uint16_t *)(uintptr_t)(addr))
#define TLV8(addr)  (*(const volatile uint8_t *)(uintptr_t)(addr))

static const uint16_t k_refvsel[] = { REFVSEL_0, REFVSEL_1, REFVSEL_2 };
static const uint16_t k_mv[]      = { 1200u, 2000u, 2500u };

bool ref_enable(ref_level_t level, bool temp_sensor)
{
    uint16_t guard;

    /* REFVSEL must not change while the generator is busy */
    for (guard = 1000u; (REFCTL0 & REFGENBUSY) && guard; guard--) { }

    REFCTL0 = k_refvsel[level] | REFON | (temp_sensor ? 0u : REFTCOFF);

    /* tSETTLE is 40 us typ / 80 us max; poll the ready flag with margin */
    for (guard = 2000u; !(REFCTL0 & REFGENRDY) && guard; guard--) { }
    return (REFCTL0 & REFGENRDY) != 0u;
}

void ref_disable(void)
{
    REFCTL0 = REFTCOFF;     /* reference and temperature sensor off */
}

uint16_t ref_nominal_mv(ref_level_t level)
{
    return k_mv[level];
}

uint16_t ref_cal_factor(ref_level_t level)
{
    static const uint16_t addr[] = { TLV_REF_1V2_FACTOR, TLV_REF_2V0_FACTOR, TLV_REF_2V5_FACTOR };
    if (TLV8(TLV_REF_TAG_ADDR) != TLV_REF_TAG) { return 0x8000u; }
    uint16_t f = TLV16(addr[level]);
    /* sanity: a real factor is within a few percent of 1.0 */
    return (f > 0x7000u && f < 0x9000u) ? f : 0x8000u;
}
