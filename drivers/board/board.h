/*
 * board.h - MSP-EXP430FR5994 LaunchPad pin map and bring-up
 *
 * Pin assignments (SLAU678C schematic):
 *   LED1 (red)    P1.0  active high
 *   LED2 (green)  P1.1  active high
 *   S1 (left)     P5.6  active low, needs internal pull-up
 *   S2 (right)    P5.5  active low, needs internal pull-up
 *   Backchannel UART (eUSCI_A0): P2.0 TXD, P2.1 RXD
 *   LFXT 32.768 kHz crystal: PJ.4 LFXIN, PJ.5 LFXOUT
 */
#ifndef DRIVERS_BOARD_H_
#define DRIVERS_BOARD_H_

#include "drivers/msp430/gpio.h"

#define BOARD_LED1_PORT     GPIO_PORT_1
#define BOARD_LED1_PIN      GPIO_PIN0
#define BOARD_LED2_PORT     GPIO_PORT_1
#define BOARD_LED2_PIN      GPIO_PIN1

#define BOARD_S1_PORT       GPIO_PORT_5
#define BOARD_S1_PIN        GPIO_PIN6
#define BOARD_S2_PORT       GPIO_PORT_5
#define BOARD_S2_PIN        GPIO_PIN5

#define BOARD_UART_PORT     GPIO_PORT_2
#define BOARD_UART_TX_PIN   GPIO_PIN0
#define BOARD_UART_RX_PIN   GPIO_PIN1

/*
 * Full safe bring-up, in the order the hardware requires:
 *   - stop the watchdog
 *   - latch the reset cause (pmm_capture_reset_cause)
 *   - all pins to low-power state, then LEDs/buttons/UART pins
 *   - clear LOCKLPM5, clear port IFGs unless this is an LPMx.5 wake
 * Button interrupts are configured for a falling edge (press) but left
 * disabled; enable them with gpio_set_callback() + gpio_interrupt_enable().
 * Clocks are NOT configured here: call cs_init() afterwards.
 *
 * PJ.4/PJ.5 are handed to the 32 kHz crystal before the unlock, so a
 * crystal that kept running through LPM3.5 (for the RTC) never stops.
 *
 * before_unlock (may be NULL) runs just before LOCKLPM5 is cleared: the
 * place to restore RTC configuration after an LPMx.5 wake (rtc_init and
 * rtc_set_*), as SLAU367P 29.2.9 requires.
 */
void board_init(void (*before_unlock)(void));

/*
 * Backchannel UART pins: true hands P2.0/P2.1 to eUSCI_A0 (secondary
 * function), false returns them to the idle state board_init sets
 * (TXD driven high, RXD pulled-up input). The RXD pull-up stays on while
 * the eUSCI owns the pin, so an unplugged line reads idle.
 */
void board_uart_pins(bool on);

static inline void board_led1(bool on)   { gpio_write(BOARD_LED1_PORT, BOARD_LED1_PIN, on); }
static inline void board_led2(bool on)   { gpio_write(BOARD_LED2_PORT, BOARD_LED2_PIN, on); }
static inline void board_led1_toggle(void) { gpio_toggle(BOARD_LED1_PORT, BOARD_LED1_PIN); }
static inline void board_led2_toggle(void) { gpio_toggle(BOARD_LED2_PORT, BOARD_LED2_PIN); }

/* true while the button is held down */
static inline bool board_s1_pressed(void) { return gpio_read(BOARD_S1_PORT, BOARD_S1_PIN) == 0u; }
static inline bool board_s2_pressed(void) { return gpio_read(BOARD_S2_PORT, BOARD_S2_PIN) == 0u; }

#endif /* DRIVERS_BOARD_H_ */
