/*
 * timer.h - Timer_A driver (TA0..TA4), periodic / one-shot / sleep
 *
 * Every timer runs from ACLK (32.768 kHz LFXT, or VLO if the crystal
 * failed - see cs_get_aclk_hz), so it keeps counting in LPM3 and can wake
 * the CPU from it. SMCLK is deliberately not offered: an SMCLK-clocked
 * timer requests SMCLK asynchronously in LPM3, which costs current and is
 * the pattern errata PMM31 warns about.
 *
 * Uses CCR0 in up mode; the driver owns the TIMERx_A0 vectors. CCR1+ and
 * the TAxIV vectors stay free for PWM or capture code of your own.
 *
 * Resolution is one ACLK tick (30.5 us); durations up to ~128 s.
 *
 * Reference: SLAU367P chapter 25 (Timer_A).
 */
#ifndef DRIVERS_TIMER_H_
#define DRIVERS_TIMER_H_

#include <stdint.h>
#include <stdbool.h>

typedef enum
{
    TIMER_A0 = 0,
    TIMER_A1,
    TIMER_A2,
    TIMER_A3,
    TIMER_A4,
    TIMER_COUNT
} timer_id_t;

/* ISR context. Return true to wake main from LPM. */
typedef bool (*timer_callback_t)(timer_id_t id);

/* false if ms is 0 or too long for the divider range */
bool timer_start_periodic_ms(timer_id_t id, uint32_t ms, timer_callback_t cb);
bool timer_start_oneshot_ms(timer_id_t id, uint32_t ms, timer_callback_t cb);
void timer_stop(timer_id_t id);
bool timer_running(timer_id_t id);

/*
 * Sleep in LPM3 for ms using timer id (main context only, GIE gets set).
 * Other interrupts are serviced meanwhile; it returns only when the time
 * is up. Replaces busy-wait delays: ~1 uA instead of ~1 mA while waiting.
 */
void timer_sleep_ms(timer_id_t id, uint32_t ms);

#endif /* DRIVERS_TIMER_H_ */
