/*
 * crc.h - CRC16 hardware module (CRC-16/CCITT-FALSE)
 *
 * Polynomial 0x1021, MSB first, no output XOR. Seed 0xFFFF gives the
 * standard CRC-16/CCITT-FALSE ("123456789" -> 0x29B1).
 * Data is fed through CRCDIRB (bit-reversed input register), which makes
 * the module's LSB-first engine produce the MSB-first standard result.
 *
 * Not reentrant: do not use from an ISR and main at the same time.
 *
 * Reference: SLAU367P chapter 10 (CRC16).
 */
#ifndef DRIVERS_CRC_H_
#define DRIVERS_CRC_H_

#include <stdint.h>
#include <stdbool.h>

#define CRC16_SEED  (0xFFFFu)

uint16_t crc16(const void *data, uint16_t len, uint16_t seed);

/* Compares the hardware against a software implementation and the
   published check value. Run once at start-up. */
bool crc16_self_test(void);

#endif /* DRIVERS_CRC_H_ */
