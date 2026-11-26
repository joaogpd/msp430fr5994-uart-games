/*
 * gpio.h - Digital I/O driver for MSP430FR5994
 *
 * Ports P1..P8 support interrupts; PJ does not.
 * Reference: SLAU367P chapter 12 (Digital I/O).
 *
 * Boot order required by the port module (SLAU367P 12.3.1):
 *   1. Configure PxDIR, PxREN, PxOUT, PxIES  (gpio_init_all_low_power, gpio_set_*)
 *   2. Clear LOCKLPM5                         (pmm_unlock_io)
 *   3. If NOT waking from LPMx.5: clear IFGs  (gpio_clear_all_interrupt_flags)
 *   4. Enable port interrupts                 (gpio_interrupt_enable)
 * board_init() performs this sequence for the LaunchPad.
 */
#ifndef DRIVERS_GPIO_H_
#define DRIVERS_GPIO_H_

#include <stdint.h>
#include <stdbool.h>

typedef enum
{
    GPIO_PORT_1 = 0,
    GPIO_PORT_2,
    GPIO_PORT_3,
    GPIO_PORT_4,
    GPIO_PORT_5,
    GPIO_PORT_6,
    GPIO_PORT_7,
    GPIO_PORT_8,
    GPIO_PORT_J,
    GPIO_PORT_COUNT
} gpio_port_t;

#define GPIO_PIN0   (0x01u)
#define GPIO_PIN1   (0x02u)
#define GPIO_PIN2   (0x04u)
#define GPIO_PIN3   (0x08u)
#define GPIO_PIN4   (0x10u)
#define GPIO_PIN5   (0x20u)
#define GPIO_PIN6   (0x40u)
#define GPIO_PIN7   (0x80u)
#define GPIO_PIN_ALL (0xFFu)

/* PxSEL1:PxSEL0 function selection (see datasheet pin-mux tables) */
typedef enum
{
    GPIO_FUNC_IO        = 0, /* SEL1=0 SEL0=0 */
    GPIO_FUNC_PRIMARY   = 1, /* SEL1=0 SEL0=1 */
    GPIO_FUNC_SECONDARY = 2, /* SEL1=1 SEL0=0 */
    GPIO_FUNC_TERTIARY  = 3  /* SEL1=1 SEL0=1 */
} gpio_func_t;

typedef enum
{
    GPIO_PULL_NONE = 0,
    GPIO_PULL_UP,
    GPIO_PULL_DOWN
} gpio_pull_t;

typedef enum
{
    GPIO_EDGE_RISING = 0,   /* PxIES = 0 */
    GPIO_EDGE_FALLING       /* PxIES = 1 */
} gpio_edge_t;

typedef enum
{
    GPIO_OK = 0,
    GPIO_ERR_PARAM,         /* bad port/pin or port has no interrupts (PJ) */
} gpio_status_t;

/*
 * Interrupt callback, executed in ISR context.
 * pin_number: 0..7.  Return true to exit low-power mode on ISR return
 * (wakes main loop), false to stay asleep.
 */
typedef bool (*gpio_callback_t)(gpio_port_t port, uint8_t pin_number);

/*
 * Put every pin (P1..P8, PJ) into GPIO, output-low, no pull: the
 * lowest-leakage state for unconnected pins (SLAU367P 12.3.2).
 * Does not touch PxIES/PxIE/PxIFG so an LPMx.5 wake flag survives.
 * Call before pmm_unlock_io(). Drivers then reclaim the pins they need.
 */
void gpio_init_all_low_power(void);

void    gpio_set_output(gpio_port_t port, uint8_t pins, bool initial_high);
void    gpio_set_input(gpio_port_t port, uint8_t pins, gpio_pull_t pull);
void    gpio_set_function(gpio_port_t port, uint8_t pins, gpio_func_t func);

void    gpio_set(gpio_port_t port, uint8_t pins);
void    gpio_clear(gpio_port_t port, uint8_t pins);
void    gpio_toggle(gpio_port_t port, uint8_t pins);
void    gpio_write(gpio_port_t port, uint8_t pins, bool high);
uint8_t gpio_read(gpio_port_t port, uint8_t pins);   /* returns masked PxIN */

/*
 * Interrupts (P1..P8 only).
 * NOTE: changing PxIES can set PxIFG (SLAU367P 12.2.7.2). Clear the flag
 * afterwards with gpio_interrupt_clear() unless you are on the LPMx.5
 * wake path and need the wake flag.
 */
gpio_status_t gpio_set_interrupt_edge(gpio_port_t port, uint8_t pins, gpio_edge_t edge);
gpio_status_t gpio_set_callback(gpio_port_t port, uint8_t pins, gpio_callback_t cb);
gpio_status_t gpio_interrupt_enable(gpio_port_t port, uint8_t pins);
gpio_status_t gpio_interrupt_disable(gpio_port_t port, uint8_t pins);
gpio_status_t gpio_interrupt_clear(gpio_port_t port, uint8_t pins);
uint8_t       gpio_interrupt_pending(gpio_port_t port, uint8_t pins);
void          gpio_clear_all_interrupt_flags(void);

#endif /* DRIVERS_GPIO_H_ */
