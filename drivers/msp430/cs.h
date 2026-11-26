/*
 * cs.h - Clock System driver for MSP430FR5994
 *
 * Clock tree after cs_init():
 *   MCLK  = DCO / 1
 *   SMCLK = DCO / smclk_div
 *   ACLK  = LFXT 32.768 kHz, or VLO (~9.4 kHz) if the crystal fails/disabled
 *
 * Handles:
 *   - FRAM wait states: NWAITS=1 above 8 MHz (datasheet "Recommended
 *     Operating Conditions"), set BEFORE raising and cleared AFTER lowering.
 *   - Errata CS12 (DCO overshoot on DCOFSEL change): dividers are raised to
 *     at least /4 around the change and restored after ~10 us.
 *
 * Reference: SLAU367P chapter 3 (CS), SLAZ681O CS12.
 */
#ifndef DRIVERS_CS_H_
#define DRIVERS_CS_H_

#include <stdint.h>
#include <stdbool.h>

typedef enum
{
    CS_DCO_1MHZ = 0,
    CS_DCO_4MHZ,
    CS_DCO_8MHZ,
    CS_DCO_16MHZ
} cs_dco_t;

typedef enum
{
    CS_DIV_1 = 0,
    CS_DIV_2,
    CS_DIV_4,
    CS_DIV_8,
    CS_DIV_16,
    CS_DIV_32
} cs_div_t;

typedef struct
{
    cs_dco_t dco;           /* MCLK frequency */
    cs_div_t smclk_div;     /* SMCLK = DCO / div */
    bool     use_lfxt;      /* start the 32 kHz crystal for ACLK */
    bool     lfxt_low_drive;/* drop to LFXTDRIVE_0 once running (saves current,
                               verify stability on your board) */
    uint16_t lfxt_timeout_ms; /* give up and use VLO after this (0 = 1000 ms) */
} cs_config_t;

typedef enum
{
    CS_OK = 0,
    CS_LFXT_FAULT       /* crystal did not start; ACLK runs from VLO */
} cs_status_t;

/* Recommended default: 8 MHz (no FRAM wait states), SMCLK 8 MHz, LFXT on */
#define CS_CONFIG_DEFAULT { CS_DCO_8MHZ, CS_DIV_1, true, false, 1000u }

cs_status_t cs_init(const cs_config_t *cfg);

/* Change MCLK/SMCLK at runtime (keeps current SMCLK divider). */
void cs_set_dco(cs_dco_t dco);

uint32_t cs_get_mclk_hz(void);
uint32_t cs_get_smclk_hz(void);
uint32_t cs_get_aclk_hz(void);      /* nominal: 32768 or 9400 (VLO, +/-40%) */
bool     cs_lfxt_running(void);

/* Busy-wait; accuracy depends on DCO tolerance. */
void cs_delay_ms(uint16_t ms);
void cs_delay_us(uint16_t us);

#endif /* DRIVERS_CS_H_ */
