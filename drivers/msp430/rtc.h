/*
 * rtc.h - RTC_C real-time clock (calendar mode, binary format)
 *
 * Runs from the 32.768 kHz LFXT crystal (start it with cs_init first) and
 * keeps counting in LPM3 and LPM3.5. It can wake the device from both.
 *
 * LPM3.5 rule (SLAU367P 29.2.9): after an LPMx.5 wake the time counters
 * are kept, but the RTC control registers must be written again BEFORE
 * LOCKLPM5 is cleared, without touching the time. So:
 *   - call rtc_init() and the rtc_set_*() configuration functions from the
 *     board_init() "before_unlock" hook, on every boot;
 *   - rtc_init() keeps the running time after an LPMx.5 wake and only
 *     loads a default time after a real reset.
 *
 * Errata RTC12 (temperature compensation lost after LPM3.5) does not
 * apply: this driver does not use RTCTCMP.
 *
 * Reference: SLAU367P chapter 29 (RTC_C).
 */
#ifndef DRIVERS_RTC_H_
#define DRIVERS_RTC_H_

#include <stdint.h>
#include <stdbool.h>

typedef struct
{
    uint16_t year;      /* 0..4095 */
    uint8_t  month;     /* 1..12 */
    uint8_t  day;       /* 1..31 */
    uint8_t  dow;       /* 0..6, user defined */
    uint8_t  hour;      /* 0..23 */
    uint8_t  min;       /* 0..59 */
    uint8_t  sec;       /* 0..59 */
} rtc_time_t;

/* Periodic interrupt from prescaler RT1PS (128 Hz / 2^(n+1)) */
typedef enum
{
    RTC_PERIOD_OFF = 0,
    RTC_PERIOD_125MS,
    RTC_PERIOD_250MS,
    RTC_PERIOD_500MS,
    RTC_PERIOD_1S,
    RTC_PERIOD_2S
} rtc_period_t;

/* Calendar time event (RTCTEV) */
typedef enum
{
    RTC_TEV_OFF = 0,
    RTC_TEV_MINUTE,
    RTC_TEV_HOUR,
    RTC_TEV_MIDNIGHT,
    RTC_TEV_NOON
} rtc_tev_t;

typedef enum
{
    RTC_EV_PERIODIC = 0,    /* RT1PS interval */
    RTC_EV_TIME_EVENT,      /* minute / hour / ... */
    RTC_EV_ALARM,
    RTC_EV_OSC_FAULT        /* 32 kHz crystal fault */
} rtc_event_t;

/* ISR context. Return true to wake main from LPM. */
typedef bool (*rtc_callback_t)(rtc_event_t ev);

/*
 * Calendar mode, binary format. The time is kept after an LPMx.5 wake,
 * and after any reset that left the RTC running with a valid time;
 * otherwise (brown-out / power loss) the clock starts at *default_time
 * (or 2026-01-01 00:00:00 if NULL). Returns true if the time was kept.
 */
bool rtc_init(const rtc_time_t *default_time, rtc_callback_t cb);

void rtc_set_time(const rtc_time_t *t);
bool rtc_get_time(rtc_time_t *t);           /* false if no stable read */

void rtc_set_periodic(rtc_period_t p);
void rtc_set_time_event(rtc_tev_t tev);
void rtc_set_alarm(uint8_t hour, uint8_t min);  /* daily alarm at hh:mm */
void rtc_clear_alarm(void);

/* Seconds since 00:00:00 of the current day, for quick timestamps */
uint32_t rtc_seconds_of_day(const rtc_time_t *t);

/*
 * Calendar <-> seconds since 2000-01-01 00:00:00 (proleptic Gregorian,
 * valid 2000..2135). rtc_from_epoch also fills dow (0 = Sunday).
 */
uint32_t rtc_to_epoch(const rtc_time_t *t);
void     rtc_from_epoch(uint32_t s, rtc_time_t *t);

/* true if the fields form a real date and time from year 2000 on */
bool rtc_time_valid(const rtc_time_t *t);

#endif /* DRIVERS_RTC_H_ */
