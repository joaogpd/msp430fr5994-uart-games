/*
 * crc.c - CRC16 hardware module (CRC-16/CCITT-FALSE)
 */
#include "crc.h"
#include "hal_common.h"

uint16_t crc16(const void *data, uint16_t len, uint16_t seed)
{
    const uint8_t *p = (const uint8_t *)data;
    CRCINIRES = seed;
    while (len--)
    {
        CRCDIRB_L = *p++;
    }
    return CRCINIRES;
}

static uint16_t crc16_sw(const uint8_t *p, uint16_t len, uint16_t crc)
{
    while (len--)
    {
        uint8_t b;
        crc ^= (uint16_t)*p++ << 8;
        for (b = 0; b < 8u; b++)
        {
            crc = (crc & 0x8000u) ? (uint16_t)((crc << 1) ^ 0x1021u) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

bool crc16_self_test(void)
{
    static const uint8_t k_check[9] = { '1', '2', '3', '4', '5', '6', '7', '8', '9' };
    uint16_t hw = crc16(k_check, sizeof k_check, CRC16_SEED);
    uint16_t sw = crc16_sw(k_check, sizeof k_check, CRC16_SEED);
    return (hw == 0x29B1u) && (sw == 0x29B1u);
}
