/*
 * spi.c - eUSCI SPI master driver (A0..A3, B0..B3), polled
 */
#include "spi.h"
#include "cs.h"
#include <msp430.h>

/*
 * eUSCI_A and eUSCI_B share CTLW0/BRW/RXBUF/TXBUF offsets, but STATW,
 * IE and IFG sit at different offsets (SLAU367P Tables 31-3 and 31-4).
 * The SPI-mode bits used here (UCBUSY, UCRXIFG, UCTXIFG, CTLW0 fields)
 * have the same positions in both.
 */
#define OFS_CTLW0       (0x00u)
#define OFS_BRW         (0x06u)
#define OFS_RXBUF       (0x0Cu)
#define OFS_TXBUF       (0x0Eu)
#define OFS_A_STATW     (0x0Au)
#define OFS_A_IE        (0x1Au)
#define OFS_A_IFG       (0x1Cu)
#define OFS_B_STATW     (0x08u)
#define OFS_B_IE        (0x2Au)
#define OFS_B_IFG       (0x2Cu)

#define IS_B(id)        ((id) >= SPI_B0)
#define REG(id, ofs)    (*(volatile uint16_t *)(uintptr_t)(k_base[id] + (ofs)))
#define CTLW0(id)       REG(id, OFS_CTLW0)
#define BRW(id)         REG(id, OFS_BRW)
#define RXBUF(id)       REG(id, OFS_RXBUF)
#define TXBUF(id)       REG(id, OFS_TXBUF)
#define STATW(id)       REG(id, IS_B(id) ? OFS_B_STATW : OFS_A_STATW)
#define IE(id)          REG(id, IS_B(id) ? OFS_B_IE    : OFS_A_IE)
#define IFG(id)         REG(id, IS_B(id) ? OFS_B_IFG   : OFS_A_IFG)

static const uint16_t k_base[SPI_COUNT] = {
    EUSCI_A0_BASE, EUSCI_A1_BASE, EUSCI_A2_BASE, EUSCI_A3_BASE,
    EUSCI_B0_BASE, EUSCI_B1_BASE, EUSCI_B2_BASE, EUSCI_B3_BASE
};

static uint32_t s_clk_hz[SPI_COUNT];   /* 0 = not running */

bool spi_init(spi_id_t id, const spi_config_t *cfg)
{
    if ((unsigned)id >= SPI_COUNT || !cfg || cfg->clk_hz == 0u) { return false; }

    CTLW0(id) = UCSWRST;
    s_clk_hz[id] = 0u;

    /* Round the divider up so SCLK never exceeds what was asked for */
    uint32_t smclk = cs_get_smclk_hz();
    uint32_t div = (smclk + cfg->clk_hz - 1u) / cfg->clk_hz;
    if (div == 0u) { div = 1u; }
    if (div > 0xFFFFu) { return false; }

    uint16_t ctl = UCSWRST | UCMST | UCSYNC | UCMODE_0 | UCSSEL__SMCLK;
    if (!cfg->lsb_first)                                        { ctl |= UCMSB; }
    if (cfg->mode == SPI_MODE_2 || cfg->mode == SPI_MODE_3)     { ctl |= UCCKPL; }
    if (cfg->mode == SPI_MODE_0 || cfg->mode == SPI_MODE_2)     { ctl |= UCCKPH; }

    CTLW0(id) = ctl;
    BRW(id)   = (uint16_t)div;
    IE(id)    = 0u;                     /* polled: no eUSCI interrupts */
    CTLW0(id) &= (uint16_t)~UCSWRST;    /* sets UCTXIFG: TXBUF is free */

    s_clk_hz[id] = smclk / div;
    return true;
}

void spi_deinit(spi_id_t id)
{
    if ((unsigned)id >= SPI_COUNT) { return; }
    CTLW0(id) |= UCSWRST;
    s_clk_hz[id] = 0u;
}

uint32_t spi_get_clk_hz(spi_id_t id)
{
    return ((unsigned)id < SPI_COUNT) ? s_clk_hz[id] : 0u;
}

static inline bool running(spi_id_t id)
{
    return (unsigned)id < SPI_COUNT && s_clk_hz[id] != 0u;
}

/* Wait for the shift register to empty, then drop whatever is in RXBUF
 * (reading it also clears UCOE, set when writes outran reads). */
static void finish_write(spi_id_t id)
{
    while (STATW(id) & UCBUSY) { }
    (void)RXBUF(id);
}

uint8_t spi_transfer_byte(spi_id_t id, uint8_t tx)
{
    if (!running(id)) { return 0u; }
    (void)RXBUF(id);                    /* stale byte would satisfy the wait */
    while (!(IFG(id) & UCTXIFG)) { }
    TXBUF(id) = tx;
    while (!(IFG(id) & UCRXIFG)) { }
    uint8_t rx = (uint8_t)RXBUF(id);
    while (STATW(id) & UCBUSY) { }
    return rx;
}

void spi_write(spi_id_t id, const void *tx, uint16_t len)
{
    if (!running(id) || !tx) { return; }
    const uint8_t *p = (const uint8_t *)tx;
    while (len--)
    {
        while (!(IFG(id) & UCTXIFG)) { }
        TXBUF(id) = *p++;
    }
    finish_write(id);
}

void spi_transfer(spi_id_t id, const void *tx, void *rx, uint16_t len)
{
    if (!running(id) || !tx || !rx) { return; }
    const uint8_t *t = (const uint8_t *)tx;
    uint8_t *r = (uint8_t *)rx;
    (void)RXBUF(id);
    while (len--)
    {
        /* One byte in flight at a time keeps TX and RX paired */
        while (!(IFG(id) & UCTXIFG)) { }
        TXBUF(id) = *t++;
        while (!(IFG(id) & UCRXIFG)) { }
        *r++ = (uint8_t)RXBUF(id);
    }
    while (STATW(id) & UCBUSY) { }
}

void spi_read(spi_id_t id, void *rx, uint16_t len, uint8_t fill)
{
    if (!running(id) || !rx) { return; }
    uint8_t *r = (uint8_t *)rx;
    (void)RXBUF(id);
    while (len--)
    {
        while (!(IFG(id) & UCTXIFG)) { }
        TXBUF(id) = fill;
        while (!(IFG(id) & UCRXIFG)) { }
        *r++ = (uint8_t)RXBUF(id);
    }
    while (STATW(id) & UCBUSY) { }
}
