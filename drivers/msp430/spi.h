/*
 * spi.h - eUSCI SPI master driver (A0..A3, B0..B3), polled
 *
 * 3-wire master mode clocked from SMCLK. Chip select is NOT handled here:
 * the device driver drives its own CS GPIO around each transaction, so
 * several devices can share one bus. Pins are not configured here either
 * (same split as uart.c / board.c): the device driver or board code hands
 * SIMO/SOMI/CLK to the eUSCI with gpio_set_function() before using it.
 *
 * Why polled and not interrupt driven: at 4 MHz one byte takes 2 us,
 * which is 16 MCLK cycles at 8 MHz. Entering and leaving an ISR per byte
 * costs more than that, so polling finishes sooner and the CPU gets back
 * to LPM3 sooner. A transfer long enough to matter (a whole frame) is a
 * job for DMA, which can be added later without changing this API.
 *
 * Reference: SLAU367P chapter 31 (eUSCI SPI mode).
 */
#ifndef DRIVERS_SPI_H_
#define DRIVERS_SPI_H_

#include <stdint.h>
#include <stdbool.h>

typedef enum
{
    SPI_A0 = 0,
    SPI_A1,
    SPI_A2,
    SPI_A3,
    SPI_B0,
    SPI_B1,
    SPI_B2,
    SPI_B3,
    SPI_COUNT
} spi_id_t;

/*
 * Standard SPI modes (CPOL, CPHA). The driver translates them to the
 * eUSCI bits: UCCKPL = CPOL, UCCKPH = !CPHA (eUSCI's "phase" bit means
 * "capture on the first edge", the inverse of CPHA).
 */
typedef enum
{
    SPI_MODE_0 = 0,     /* CPOL=0 CPHA=0: idle low, sample on rising edge */
    SPI_MODE_1,         /* CPOL=0 CPHA=1 */
    SPI_MODE_2,         /* CPOL=1 CPHA=0 */
    SPI_MODE_3          /* CPOL=1 CPHA=1 */
} spi_mode_t;

typedef struct
{
    uint32_t   clk_hz;      /* requested SCLK; the actual one is <= this */
    spi_mode_t mode;
    bool       lsb_first;   /* false = MSB first (almost every device) */
} spi_config_t;

/*
 * Configure and enable instance id. Call after cs_init(), since the
 * divider is computed from cs_get_smclk_hz(). Returns false for a bad id,
 * clk_hz of 0, or a clk_hz too low for the 16-bit divider.
 */
bool     spi_init(spi_id_t id, const spi_config_t *cfg);
void     spi_deinit(spi_id_t id);              /* holds the eUSCI in reset */
uint32_t spi_get_clk_hz(spi_id_t id);         /* actual SCLK, 0 if not running */

/*
 * Blocking transfers. All of them return only after the last bit has
 * left the shift register (UCBUSY clear), so the caller may release CS
 * right away. They do nothing if the instance is not running.
 */
uint8_t spi_transfer_byte(spi_id_t id, uint8_t tx);
void    spi_write(spi_id_t id, const void *tx, uint16_t len);        /* RX discarded */
void    spi_read(spi_id_t id, void *rx, uint16_t len, uint8_t fill); /* sends fill */
void    spi_transfer(spi_id_t id, const void *tx, void *rx, uint16_t len);

#endif /* DRIVERS_SPI_H_ */
