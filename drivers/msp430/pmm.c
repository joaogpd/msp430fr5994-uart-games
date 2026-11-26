/*
 * pmm.c - Power management, reset-cause and low-power-mode driver
 */
#include "pmm.h"
#include "hal_common.h"

static uint32_t s_flags;
static uint16_t s_first_iv;
static bool     s_captured;

void pmm_capture_reset_cause(void)
{
    if (s_captured) { return; }

    uint16_t iv;
    uint8_t guard = 32u;            /* SYSRSTIV has 23 sources */
    s_flags = 0;
    s_first_iv = SYSRSTIV;          /* reading clears the highest pending */
    iv = s_first_iv;
    while ((iv != 0u) && guard--)
    {
        s_flags |= PMM_RST_BIT(iv);
        iv = SYSRSTIV;
    }
    s_captured = true;
}

uint32_t pmm_reset_flags(void)    { return s_flags; }
uint16_t pmm_reset_iv_first(void) { return s_first_iv; }

bool pmm_woke_from_lpmx5(void)
{
    return (s_flags & PMM_RST_LPM5WU) != 0u;
}

pmm_boot_reason_t pmm_boot_reason(void)
{
    const uint32_t f = s_flags;
    const uint32_t faults = PMM_RST_SECYV | PMM_RST_FRCTL_PW | PMM_RST_FRAM_UBD |
                            PMM_RST_PERF | PMM_RST_PMM_PW | PMM_RST_MPU_PW |
                            PMM_RST_CS_PW | PMM_RST_MPU_SEG;

    /*
     * Order matters: several flags can be pending together (e.g. RST pin
     * plus BOR after a debugger reset). Check the most specific first.
     */
    if (f & PMM_RST_LPM5WU)                          { return PMM_BOOT_LPMX5_WAKE; }
    if (f & (PMM_RST_BOR | PMM_RST_SVSH))            { return PMM_BOOT_POWER_ON; }
    if (f & faults)                                  { return PMM_BOOT_FAULT; }
    if (f & (PMM_RST_WDT_TIMEOUT | PMM_RST_WDT_PW))  { return PMM_BOOT_WATCHDOG; }
    if (f & (PMM_RST_SWBOR | PMM_RST_SWPOR))         { return PMM_BOOT_SOFTWARE; }
    if (f & PMM_RST_RSTNMI)                          { return PMM_BOOT_RESET_PIN; }
    return PMM_BOOT_POWER_ON;
}

void pmm_unlock_io(void)
{
    PM5CTL0 &= (uint16_t)~LOCKLPM5;
}

void pmm_svsh_in_lpm(bool enable)
{
    PMMCTL0_H = PMMPW_H;
    if (enable) { PMMCTL0_L |= (uint8_t)SVSHE; }
    else        { PMMCTL0_L &= (uint8_t)~SVSHE; }
    PMMCTL0_H = 0u;
}

/*
 * PMM32 workaround #2: put FRAM in inactive mode before LPM2/3/4 entry.
 * Must execute from RAM (SLAU367P 7.8: "The CPU must execute from RAM to
 * clear the FRPWR bit"). FRAM powers back up on the first access after the
 * wake-up, i.e. when the ISR (in FRAM) is fetched.
 * FR5994 has no FRLPMPWR bit, so only FRPWR is cleared.
 */
#pragma CODE_SECTION(pmm_lpm_from_ram, ".TI.ramfunc")
static void pmm_lpm_from_ram(uint16_t sr_bits)
{
    FRCTL0 = FRCTLPW;
    GCCTL0 &= (uint16_t)~FRPWR;
    FRCTL0_H = 0u;
    __bis_SR_register(sr_bits);
    __no_operation();
}

void pmm_enter_lpm(pmm_lpm_t mode)
{
    switch (mode)
    {
    case PMM_LPM0:
        __bis_SR_register(LPM0_bits | GIE);
        __no_operation();
        break;
    case PMM_LPM1:
        __bis_SR_register(LPM1_bits | GIE);
        __no_operation();
        break;
    case PMM_LPM2:
        pmm_lpm_from_ram(LPM2_bits | GIE);
        break;
    case PMM_LPM3:
        pmm_lpm_from_ram(LPM3_bits | GIE);
        break;
    case PMM_LPM4:
    default:
        pmm_lpm_from_ram(LPM4_bits | GIE);
        break;
    }
}

void pmm_enter_lpmx5(void)
{
    PMMCTL0_H = PMMPW_H;
    PMMCTL0_L |= (uint8_t)PMMREGOFF;
    PMMCTL0_H = 0u;

    /* GIE set so any pending flag is serviced before the regulator stops */
    __bis_SR_register(LPM4_bits | GIE);
    __no_operation();

    for (;;) { }    /* not reached unless an ISR cleared the LPM bits */
}

void pmm_software_bor(void)
{
    PMMCTL0 = PMMPW | PMMSWBOR;
    for (;;) { }
}

void pmm_software_por(void)
{
    PMMCTL0 = PMMPW | PMMSWPOR;
    for (;;) { }
}
