/*
 * hal_common.h - small helpers shared by the drivers
 */
#ifndef DRIVERS_HAL_COMMON_H_
#define DRIVERS_HAL_COMMON_H_

#include <msp430.h>
#include <stdint.h>
#include <stdbool.h>

/*
 * Critical section that restores the previous GIE state, so it is safe to
 * nest and safe to call from code that may already run with GIE=0.
 *
 *   uint16_t s = hal_enter_critical();
 *   ...
 *   hal_exit_critical(s);
 */
static inline uint16_t hal_enter_critical(void)
{
    uint16_t sr = __get_SR_register();
    __disable_interrupt();
    return sr;
}

static inline void hal_exit_critical(uint16_t sr)
{
    if (sr & GIE)
    {
        __enable_interrupt();
    }
}

#endif /* DRIVERS_HAL_COMMON_H_ */
