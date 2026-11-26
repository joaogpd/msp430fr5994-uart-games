/*
 * ref.h - REF_A internal voltage reference
 *
 * Minimum AVCC per level (datasheet "REF, Built-In Reference"):
 *   1.2 V -> 1.8 V,  2.0 V -> 2.2 V,  2.5 V -> 2.7 V
 * Below those the reference is not regulated and readings are wrong.
 * Supply current ~19 uA while on, so switch it off between measurements.
 *
 * Reference: SLAU367P chapter 33 (REF_A).
 */
#ifndef DRIVERS_REF_H_
#define DRIVERS_REF_H_

#include <stdint.h>
#include <stdbool.h>

typedef enum
{
    REF_1V2 = 0,
    REF_2V0,
    REF_2V5
} ref_level_t;

/*
 * Turn the reference on and wait until it reports ready.
 * temp_sensor: also power the temperature sensor (needed for ADC A30).
 * Returns false if it did not become ready within ~1 ms.
 */
bool     ref_enable(ref_level_t level, bool temp_sensor);
void     ref_disable(void);
uint16_t ref_nominal_mv(ref_level_t level);

/*
 * Factory correction factor for the level (TLV, 1.15 fixed point,
 * 0x8000 = 1.0). Returns 0x8000 if the TLV entry is missing.
 */
uint16_t ref_cal_factor(ref_level_t level);

#endif /* DRIVERS_REF_H_ */
