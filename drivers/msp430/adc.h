/*
 * adc.h - ADC12_B driver: supply voltage and die temperature
 *
 * Measures without any external parts:
 *   - VCC through the internal 1/2 AVCC divider (A31, ADC12BATMAP).
 *     On the LaunchPad with J8 = "Use" this is the supercap voltage.
 *   - Die temperature through the internal sensor (A30, ADC12TCMAP).
 *
 * VCC auto-ranging: the 2.0 V reference needs AVCC >= 2.2 V, so a reading
 * below 2.4 V is repeated on the 1.2 V reference (valid down to 1.8 V).
 *
 * Every call is a blocking single conversion (~0.2 ms incl. reference
 * start-up) in active mode, and powers ADC and REF off afterwards.
 * Staying awake during the conversion is the workaround for errata PMM31
 * (ADC MODCLK request during an LPM transition can lock up the device).
 * Also handled: ADC42 (BUSY checked before starting), ADC71 (ENC and SC
 * set in separate writes).
 *
 * Errata ADC67: on some lots the TLV temperature calibration is wrong.
 * adc_set_temp_calibration() lets you supply your own two-point values.
 *
 * Reference: SLAU367P chapter 34 (ADC12_B), datasheet 8.12.x and 9.14.
 */
#ifndef DRIVERS_ADC_H_
#define DRIVERS_ADC_H_

#include <stdint.h>
#include <stdbool.h>

typedef enum
{
    ADC_OK = 0,
    ADC_ERR_REF,        /* reference never became ready */
    ADC_ERR_TIMEOUT,    /* conversion did not finish */
    ADC_ERR_CAL         /* no usable temperature calibration */
} adc_status_t;

/* Supply voltage in millivolts (factory gain/offset/REF corrections applied). */
adc_status_t adc_read_vcc_mv(uint16_t *mv);

/* Conversions averaged per temperature reading (1..8) */
#ifndef ADC_TEMP_AVG
#define ADC_TEMP_AVG    (8u)
#endif

/* Die temperature in tenths of a degree C (e.g. 253 = 25.3 C),
   averaged over ADC_TEMP_AVG conversions. */
adc_status_t adc_read_temp_dc(int16_t *deci_c);

/*
 * Override the TLV temperature calibration (raw 12-bit counts measured on
 * the 1.2 V reference at 30 C and 85 C). Pass 0,0 to go back to TLV.
 */
void adc_set_temp_calibration(uint16_t raw_30c, uint16_t raw_85c);

#endif /* DRIVERS_ADC_H_ */
