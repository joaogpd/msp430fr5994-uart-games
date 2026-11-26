/*
 * adc.c - ADC12_B driver: supply voltage and die temperature
 */
#include "adc.h"
#include "ref.h"
#include "hal_common.h"

/* TLV ADC12 calibration block (datasheet Table 9-40) */
#define TLV_ADC_TAG_ADDR    (0x1A14u)
#define TLV_ADC_TAG         (0x11u)
#define TLV_ADC_GAIN        (0x1A16u)
#define TLV_ADC_OFFSET      (0x1A18u)
#define TLV_ADC_T30_1V2     (0x1A1Au)
#define TLV_ADC_T85_1V2     (0x1A1Cu)

#define TLV16(addr) (*(const volatile uint16_t *)(uintptr_t)(addr))
#define TLV8(addr)  (*(const volatile uint8_t *)(uintptr_t)(addr))

#define VCC_RANGE_SWITCH_MV (2400u)     /* below: 2.0 V ref not trustworthy */

static uint16_t s_user_t30, s_user_t85;

static bool tlv_adc_valid(void)
{
    return TLV8(TLV_ADC_TAG_ADDR) == TLV_ADC_TAG;
}

/*
 * One single-channel conversion on ADC12MEM0.
 * inch: ADC12INCH_x, ctl3_map: ADC12BATMAP / ADC12TCMAP.
 */
static adc_status_t convert(uint16_t inch, uint16_t ctl3_map, uint16_t *raw)
{
    uint16_t guard;

    /* ADC42: never start while a conversion is still running */
    for (guard = 1000u; (ADC12CTL1 & ADC12BUSY) && guard; guard--) { }

    ADC12CTL0 &= (uint16_t)~ADC12ENC;               /* config only with ENC=0 */

    /*
     * Sample time: temperature sensor needs >= 30 us, battery monitor
     * >= 1.7 us. 192 MODOSC cycles = 40 us at 4.8 MHz (35 us at +12 %).
     */
    ADC12CTL0 = ADC12SHT0_7 | ADC12ON;
    ADC12CTL1 = ADC12PDIV__1 | ADC12SHS_0 | ADC12SHP | ADC12DIV_0 |
                ADC12SSEL_0 | ADC12CONSEQ_0;        /* MODOSC, single, SC trigger */
    ADC12CTL2 = ADC12RES__12BIT;
    ADC12CTL3 = ctl3_map | ADC12CSTARTADD_0;
    ADC12MCTL0 = ADC12VRSEL_1 | inch;                /* VR+ = buffered VREF, VR- = AVSS */
    ADC12IFGR0 = 0u;

    /* ADC71: ENC and SC in separate writes */
    ADC12CTL0 |= ADC12ENC;
    ADC12CTL0 |= ADC12SC;

    for (guard = 20000u; !(ADC12IFGR0 & ADC12IFG0) && guard; guard--) { }

    adc_status_t st = ADC_OK;
    if (ADC12IFGR0 & ADC12IFG0)
    {
        *raw = ADC12MEM0 & 0x0FFFu;                 /* reading clears IFG0 */
    }
    else
    {
        st = ADC_ERR_TIMEOUT;
    }

    ADC12CTL0 &= (uint16_t)~ADC12ENC;
    ADC12CTL0 &= (uint16_t)~ADC12ON;                /* ADC off */
    ADC12CTL3 = 0u;                                  /* disconnect divider / sensor */
    return st;
}

/* Apply REF factor, then ADC gain and offset (all from TLV) */
static uint16_t correct(uint16_t raw, ref_level_t level)
{
    uint32_t v = ((uint32_t)raw * ref_cal_factor(level)) >> 15;
    if (tlv_adc_valid())
    {
        v = (v * TLV16(TLV_ADC_GAIN)) >> 15;
        int32_t w = (int32_t)v + (int16_t)TLV16(TLV_ADC_OFFSET);
        v = (w < 0) ? 0u : (uint32_t)w;
    }
    return (v > 4095u) ? 4095u : (uint16_t)v;
}

static adc_status_t vcc_on(ref_level_t level, uint16_t *mv)
{
    uint16_t raw = 0;
    if (!ref_enable(level, false))
    {
        ref_disable();
        return ADC_ERR_REF;
    }
    adc_status_t st = convert(ADC12INCH_31, ADC12BATMAP, &raw);
    ref_disable();
    if (st != ADC_OK) { return st; }

    /* VCC = 2 * VREF * code / 4096 */
    *mv = (uint16_t)(((uint32_t)correct(raw, level) * 2u * ref_nominal_mv(level)) >> 12);
    return ADC_OK;
}

adc_status_t adc_read_vcc_mv(uint16_t *mv)
{
    uint16_t v = 0;
    adc_status_t st = vcc_on(REF_2V0, &v);
    if (st == ADC_OK && v < VCC_RANGE_SWITCH_MV)
    {
        /* VCC/2 < 1.2 V here, so the 1.2 V reference is in range and valid */
        st = vcc_on(REF_1V2, &v);
    }
    else if (st == ADC_ERR_REF)
    {
        st = vcc_on(REF_1V2, &v);
    }
    if (st == ADC_OK) { *mv = v; }
    return st;
}

void adc_set_temp_calibration(uint16_t raw_30c, uint16_t raw_85c)
{
    s_user_t30 = raw_30c;
    s_user_t85 = raw_85c;
}

adc_status_t adc_read_temp_dc(int16_t *deci_c)
{
    uint16_t c30 = s_user_t30, c85 = s_user_t85;
    if (c30 == 0u && c85 == 0u)
    {
        if (!tlv_adc_valid()) { return ADC_ERR_CAL; }
        c30 = TLV16(TLV_ADC_T30_1V2);
        c85 = TLV16(TLV_ADC_T85_1V2);
    }
    if (c85 <= c30) { return ADC_ERR_CAL; }

    if (!ref_enable(REF_1V2, true))
    {
        ref_disable();
        return ADC_ERR_REF;
    }
    /*
     * Average ADC_TEMP_AVG conversions with the reference left on: single
     * readings show occasional ~1.5 C glitches. Each conversion takes
     * ~45 us, so this adds well under 1 ms of active time.
     */
    int32_t sum = 0;
    uint8_t n;
    adc_status_t st = ADC_OK;
    for (n = 0; n < ADC_TEMP_AVG && st == ADC_OK; n++)
    {
        uint16_t raw = 0;
        st = convert(ADC12INCH_30, ADC12TCMAP, &raw);
        sum += raw;
    }
    ref_disable();
    if (st != ADC_OK) { return st; }

    /* Two-point line through (c30, 30.0 C) and (c85, 85.0 C), in 0.1 C,
       on the sum of the readings, rounded to nearest */
    int32_t span = (int32_t)(c85 - c30) * ADC_TEMP_AVG;
    int32_t num  = (sum - (int32_t)c30 * ADC_TEMP_AVG) * 550;
    int32_t t = (num >= 0 ? num + span / 2 : num - span / 2) / span + 300;
    *deci_c = (int16_t)t;
    return ADC_OK;
}
