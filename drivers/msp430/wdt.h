/*
 * wdt.h - Watchdog timer (WDT_A) driver
 *
 * Two mutually exclusive uses:
 *   - Watchdog mode: PUC reset if not kicked within the interval.
 *   - Interval mode: periodic interrupt (system tick / LPM wake-up).
 *
 * Clock source advice for a supercap system:
 *   Use ACLK or VLO. Sourcing the WDT from SMCLK keeps SMCLK requested in
 *   LPM3, which costs current and is the asynchronous SMCLK request pattern
 *   that errata PMM31 warns about.
 *   The WDT does not run in LPMx.5.
 *
 * Reference: SLAU367P chapter 23 (WDT_A).
 */
#ifndef DRIVERS_WDT_H_
#define DRIVERS_WDT_H_

#include <stdint.h>
#include <stdbool.h>

typedef enum
{
    WDT_CLK_SMCLK = 0,
    WDT_CLK_ACLK,
    WDT_CLK_VLO,
} wdt_clk_t;

/* Divider = clock cycles per interval (WDTIS). Times shown for 32.768 kHz. */
typedef enum
{
    WDT_DIV_2G   = 0,   /* 2^31  ~18 h   */
    WDT_DIV_128M = 1,   /* 2^27  ~68 min */
    WDT_DIV_8192K = 2,  /* 2^23  256 s   */
    WDT_DIV_512K = 3,   /* 2^19  16 s    */
    WDT_DIV_32K  = 4,   /* 2^15  1 s     */
    WDT_DIV_8192 = 5,   /* 2^13  250 ms  */
    WDT_DIV_512  = 6,   /* 2^9   15.6 ms */
    WDT_DIV_64   = 7    /* 2^6   1.95 ms */
} wdt_div_t;

/* Interval-mode callback, ISR context. Return true to wake main from LPM. */
typedef bool (*wdt_callback_t)(void);

void wdt_hold(void);
void wdt_start_watchdog(wdt_clk_t clk, wdt_div_t div);
void wdt_start_interval(wdt_clk_t clk, wdt_div_t div, wdt_callback_t cb);
void wdt_kick(void);        /* clear counter; harmless in interval mode */
void wdt_pause(void);       /* hold, keeping configuration */
void wdt_resume(void);      /* release hold with saved configuration */

#endif /* DRIVERS_WDT_H_ */
