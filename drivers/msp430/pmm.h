/*
 * pmm.h - Power management, reset-cause and low-power-mode driver
 *
 * Covers:
 *   - Reset cause capture from SYSRSTIV (all pending causes, not just the
 *     highest priority one), so the application can tell a power loss from
 *     a watchdog reset or an LPMx.5 wake-up.
 *   - LOCKLPM5 release (I/O unlock after BOR / LPMx.5).
 *   - SVSH control in low-power modes.
 *   - LPM0..LPM4 entry with the PMM32 errata workaround (FRAM powered down
 *     from RAM before LPM2/3/4).
 *   - LPMx.5 entry (LPM3.5 if RTC_C runs from LFXT, otherwise LPM4.5).
 *
 * Errata to keep in mind (SLAZ681O):
 *   PMM31: lock-up if MODCLK/SMCLK is requested *asynchronously* (ADC trigger,
 *          eUSCI clock-low timeout, UART RX) during AM -> LPM2/3/4. The PMM32
 *          workaround below does NOT cover PMM31: stay in LPM0/1 while such
 *          peripherals can request clocks, or use LPMx.5.
 *   PMM32: covered by pmm_enter_lpm() for LPM2/3/4.
 *
 * Reference: SLAU367P chapters 1 (SYS) and 2 (PMM).
 */
#ifndef DRIVERS_PMM_H_
#define DRIVERS_PMM_H_

#include <stdint.h>
#include <stdbool.h>

/* One bit per SYSRSTIV value: bit n <=> SYSRSTIV == 2*n */
#define PMM_RST_BIT(iv)         (1ul << ((iv) >> 1))

#define PMM_RST_BOR             PMM_RST_BIT(0x02)   /* brownout: power-up or supply collapse */
#define PMM_RST_RSTNMI          PMM_RST_BIT(0x04)   /* RST pin */
#define PMM_RST_SWBOR           PMM_RST_BIT(0x06)
#define PMM_RST_LPM5WU          PMM_RST_BIT(0x08)   /* wake-up from LPMx.5 */
#define PMM_RST_SECYV           PMM_RST_BIT(0x0A)
#define PMM_RST_SVSH            PMM_RST_BIT(0x0E)   /* VCC fell below SVSH- */
#define PMM_RST_SWPOR           PMM_RST_BIT(0x14)
#define PMM_RST_WDT_TIMEOUT     PMM_RST_BIT(0x16)
#define PMM_RST_WDT_PW          PMM_RST_BIT(0x18)
#define PMM_RST_FRCTL_PW        PMM_RST_BIT(0x1A)
#define PMM_RST_FRAM_UBD        PMM_RST_BIT(0x1C)   /* uncorrectable FRAM bit error */
#define PMM_RST_PERF            PMM_RST_BIT(0x1E)   /* fetch from peripheral area */
#define PMM_RST_PMM_PW          PMM_RST_BIT(0x20)
#define PMM_RST_MPU_PW          PMM_RST_BIT(0x22)
#define PMM_RST_CS_PW           PMM_RST_BIT(0x24)
#define PMM_RST_MPU_SEG         (PMM_RST_BIT(0x26) | PMM_RST_BIT(0x28) | PMM_RST_BIT(0x2A) | \
                                 PMM_RST_BIT(0x2C) | PMM_RST_BIT(0x2E))

/* Coarse classification for application logic */
typedef enum
{
    PMM_BOOT_POWER_ON = 0,  /* BOR or SVSH: supply was lost or collapsed */
    PMM_BOOT_LPMX5_WAKE,    /* planned deep sleep, woke by RTC / pin */
    PMM_BOOT_RESET_PIN,     /* RST button / debugger */
    PMM_BOOT_WATCHDOG,      /* WDT timeout or password violation */
    PMM_BOOT_SOFTWARE,      /* pmm_software_bor/por */
    PMM_BOOT_FAULT          /* FRAM error, MPU, password violations, ... */
} pmm_boot_reason_t;

typedef enum
{
    PMM_LPM0 = 0,
    PMM_LPM1,
    PMM_LPM2,
    PMM_LPM3,
    PMM_LPM4
} pmm_lpm_t;

/*
 * Read and clear every pending SYSRSTIV entry. Call once, as early as
 * possible after reset (board_init does it). Later calls return quickly.
 */
void              pmm_capture_reset_cause(void);
uint32_t          pmm_reset_flags(void);        /* PMM_RST_* bitmask */
uint16_t          pmm_reset_iv_first(void);     /* highest-priority SYSRSTIV value */
pmm_boot_reason_t pmm_boot_reason(void);
bool              pmm_woke_from_lpmx5(void);

/* Clear LOCKLPM5 so port configuration takes effect. */
void pmm_unlock_io(void);

/*
 * true : SVSH also active in LPM2/3/4/x.5 (reset at ~1.8-2.0 V even
 *        while asleep; slightly more current).
 * false: SVSH only in AM/LPM0/LPM1 (PUC default). The BOR still protects
 *        the device, at a lower threshold.
 */
void pmm_svsh_in_lpm(bool enable);

/*
 * Enter LPMx with GIE set. Returns after an ISR clears the LPM bits on exit
 * (e.g. __bic_SR_register_on_exit). For LPM2..4 FRAM is powered down from
 * RAM first (errata PMM32 workaround #2).
 */
void pmm_enter_lpm(pmm_lpm_t mode);

/*
 * Enter LPM3.5 / LPM4.5. Does not return: wake-up is a BOR, and execution
 * restarts at reset with pmm_woke_from_lpmx5() == true. Before calling:
 * all pins GPIO, wake pins configured with IES/IE and IFG cleared, RTC set
 * up (for LPM3.5). RAM is lost - persist state in FRAM.
 */
void pmm_enter_lpmx5(void);

void pmm_software_bor(void);
void pmm_software_por(void);

#endif /* DRIVERS_PMM_H_ */
