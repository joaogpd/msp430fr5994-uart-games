/*
 * rtc.c - RTC_C real-time clock (calendar mode, binary format)
 */
#include "rtc.h"
#include "pmm.h"
#include "hal_common.h"

#define RTCTEV_MASK (0x0003u)

static rtc_callback_t s_cb;

static const uint16_t k_rt1ip[] = { 0u, RT1IP_3, RT1IP_4, RT1IP_5, RT1IP_6, RT1IP_7 };
static const uint16_t k_tev[]   = { 0u, RTCTEV_0, RTCTEV_1, RTCTEV_2, RTCTEV_3 };

static inline void rtc_unlock(void) { RTCCTL0_H = RTCKEY_H; }
static inline void rtc_lock(void)   { RTCCTL0_H = 0u; }

static void write_time(const rtc_time_t *t)
{
    RTCTIM0_L = t->sec;
    RTCTIM0_H = t->min;
    RTCTIM1_L = t->hour;
    RTCTIM1_H = t->dow;
    RTCDATE_L = t->day;
    RTCDATE_H = t->month;
    RTCYEAR   = t->year;
}

bool rtc_init(const rtc_time_t *default_time, rtc_callback_t cb)
{
    static const rtc_time_t k_default = { 2026u, 1u, 1u, 4u, 0u, 0u, 0u };
    bool keep = pmm_woke_from_lpmx5();

    /*
     * A reset that did not clear the RTC (it is reset by a brown-out, which
     * sets RTCHOLD) leaves it running with a valid time: keep that too, so
     * e.g. a watchdog reset does not throw the clock away.
     */
    if (!keep && !(RTCCTL13 & RTCHOLD) && (RTCCTL13 & RTCMODE))
    {
        rtc_time_t now;
        now.sec = RTCTIM0_L;  now.min = RTCTIM0_H;
        now.hour = RTCTIM1_L; now.dow = RTCTIM1_H;
        now.day = RTCDATE_L;  now.month = RTCDATE_H;
        now.year = RTCYEAR;
        keep = rtc_time_valid(&now);
    }

    s_cb = cb;

    /*
     * Interrupt flags are never cleared here or in the rtc_set_*() calls:
     * after an LPM3.5 wake they hold the event that woke the device, which
     * the ISR services once LOCKLPM5 is cleared and GIE is set.
     */
    rtc_unlock();
    RTCCTL0_L &= (uint8_t)~(RTCOFIE | RTCTEVIE | RTCAIE | RTCRDYIE);
    if (keep)
    {
        /* Counters kept through LPM3.5: restore control bits only, never HOLD */
        RTCCTL13 = RTCMODE;                 /* calendar, binary, running */
    }
    else
    {
        RTCCTL13 = RTCHOLD | RTCMODE;       /* calendar, binary, stopped */
        write_time(default_time ? default_time : &k_default);
        RTCCTL13 = RTCMODE;                 /* start */
    }
    rtc_lock();

    RTCPS0CTL &= (uint16_t)~RT0PSIE;
    RTCPS1CTL &= (uint16_t)~RT1PSIE;
    return keep;
}

void rtc_set_time(const rtc_time_t *t)
{
    uint16_t s = hal_enter_critical();
    rtc_unlock();
    uint16_t ctl = RTCCTL13;
    RTCCTL13 = ctl | RTCHOLD;
    write_time(t);
    RTCCTL13 = ctl & (uint16_t)~RTCHOLD;
    rtc_lock();
    hal_exit_critical(s);
}

bool rtc_get_time(rtc_time_t *t)
{
    uint8_t tries;
    for (tries = 0; tries < 3u; tries++)
    {
        /* Keep-out window is ~4 ms around each second update */
        uint16_t guard;
        for (guard = 0xFFFFu; !(RTCCTL13 & RTCRDY) && guard; guard--) { }

        uint8_t sec = RTCTIM0_L;
        t->min   = RTCTIM0_H;
        t->hour  = RTCTIM1_L;
        t->dow   = RTCTIM1_H;
        t->day   = RTCDATE_L;
        t->month = RTCDATE_H;
        t->year  = RTCYEAR;
        t->sec   = sec;

        if ((RTCCTL13 & RTCRDY) && RTCTIM0_L == sec) { return true; }
    }
    return false;
}

void rtc_set_periodic(rtc_period_t p)
{
    if ((unsigned)p > (unsigned)RTC_PERIOD_2S) { return; }
    uint16_t flag = RTCPS1CTL & RT1PSIFG;   /* keep a pending wake event */
    if (p == RTC_PERIOD_OFF)
    {
        RTCPS1CTL = flag;
        return;
    }
    RTCPS1CTL = flag | k_rt1ip[p] | RT1PSIE;
}

void rtc_set_time_event(rtc_tev_t tev)
{
    if ((unsigned)tev > (unsigned)RTC_TEV_NOON) { return; }
    uint16_t s = hal_enter_critical();
    rtc_unlock();
    RTCCTL0_L &= (uint8_t)~RTCTEVIE;
    if (tev != RTC_TEV_OFF)
    {
        RTCCTL13 = (RTCCTL13 & (uint16_t)~(RTCTEV_MASK | RTCHOLD)) | k_tev[tev];
        RTCCTL0_L |= RTCTEVIE;
    }
    rtc_lock();
    hal_exit_critical(s);
}

void rtc_set_alarm(uint8_t hour, uint8_t min)
{
    uint16_t s = hal_enter_critical();
    rtc_unlock();
    RTCCTL0_L &= (uint8_t)~RTCAIE;                 /* SLAU367P: AIE off while setting */
    RTCADOWDAY = 0u;                               /* day / weekday not compared */
    RTCAMINHR  = HOURAE | ((uint16_t)hour << 8) | MINAE | min;
    RTCCTL0_L |= RTCAIE;
    rtc_lock();
    hal_exit_critical(s);
}

void rtc_clear_alarm(void)
{
    uint16_t s = hal_enter_critical();
    rtc_unlock();
    RTCCTL0_L &= (uint8_t)~(RTCAIE | RTCAIFG);
    RTCAMINHR = 0u;
    rtc_lock();
    hal_exit_critical(s);
}

uint32_t rtc_seconds_of_day(const rtc_time_t *t)
{
    return (uint32_t)t->hour * 3600ul + (uint16_t)t->min * 60u + t->sec;
}

/*
 * Days between 2000-01-01 and y-m-d, counting years from March so the
 * leap day falls at the end (H. Hinnant's days_from_civil, unsigned).
 * 730425 = days from 0000-03-01 to 2000-01-01.
 */
static uint32_t days_from_2000(uint16_t y, uint8_t m, uint8_t d)
{
    uint32_t yy  = (uint32_t)y - (m <= 2u ? 1u : 0u);
    uint32_t era = yy / 400u;
    uint32_t yoe = yy - era * 400u;
    uint32_t mp  = (m + 9u) % 12u;                          /* March = 0 */
    uint32_t doy = (153u * mp + 2u) / 5u + d - 1u;
    uint32_t doe = yoe * 365u + yoe / 4u - yoe / 100u + doy;
    return era * 146097ul + doe - 730425ul;
}

uint32_t rtc_to_epoch(const rtc_time_t *t)
{
    return days_from_2000(t->year, t->month, t->day) * 86400ul + rtc_seconds_of_day(t);
}

void rtc_from_epoch(uint32_t s, rtc_time_t *t)
{
    uint32_t days = s / 86400ul;
    uint32_t sod  = s % 86400ul;
    t->hour = (uint8_t)(sod / 3600u);
    t->min  = (uint8_t)((sod / 60u) % 60u);
    t->sec  = (uint8_t)(sod % 60u);
    t->dow  = (uint8_t)((days + 6u) % 7u);                  /* 2000-01-01 was a Saturday */

    uint32_t z   = days + 730425ul;
    uint32_t era = z / 146097ul;
    uint32_t doe = z - era * 146097ul;
    uint32_t yoe = (doe - doe / 1460u + doe / 36524ul - doe / 146096ul) / 365u;
    uint32_t doy = doe - (365u * yoe + yoe / 4u - yoe / 100u);
    uint32_t mp  = (5u * doy + 2u) / 153u;
    uint8_t  m   = (uint8_t)(mp < 10u ? mp + 3u : mp - 9u);
    t->day   = (uint8_t)(doy - (153u * mp + 2u) / 5u + 1u);
    t->month = m;
    t->year  = (uint16_t)(yoe + era * 400u + (m <= 2u ? 1u : 0u));
}

bool rtc_time_valid(const rtc_time_t *t)
{
    static const uint8_t k_mdays[12] = { 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 };
    if (t->year < 2000u || t->year > 2135u || t->month < 1u || t->month > 12u) { return false; }
    if (t->hour > 23u || t->min > 59u || t->sec > 59u || t->day < 1u) { return false; }
    if (t->day > k_mdays[t->month - 1u]) { return false; }
    if (t->month == 2u && t->day == 29u)
    {
        bool leap = (t->year % 4u == 0u) && (t->year % 100u != 0u || t->year % 400u == 0u);
        if (!leap) { return false; }
    }
    return true;
}

#pragma vector = RTC_C_VECTOR
__interrupt void rtc_isr(void)
{
    bool wake = false;
    uint16_t iv;
    while ((iv = RTCIV) != RTCIV__NONE)     /* reading clears the highest flag */
    {
        rtc_event_t ev;
        switch (iv)
        {
        case RTCIV__RT1PSIFG: ev = RTC_EV_PERIODIC;   break;
        case RTCIV__RTCTEVIFG: ev = RTC_EV_TIME_EVENT; break;
        case RTCIV__RTCAIFG:  ev = RTC_EV_ALARM;      break;
        case RTCIV__RTCOFIFG: ev = RTC_EV_OSC_FAULT;  break;
        default:              continue;             /* RDY, RT0PS: unused */
        }
        if (s_cb && s_cb(ev)) { wake = true; }
    }
    if (wake) { __bic_SR_register_on_exit(LPM4_bits); }
}
