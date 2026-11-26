/*
 * ckpt.h - Power-loss-safe checkpoint store in FRAM
 *
 * Two slots in persistent FRAM (#pragma PERSISTENT, .TI.persistent: kept
 * across resets and power loss, initialised only when the program is
 * downloaded). Each save goes to the slot that does NOT hold the newest
 * valid copy, in this order:
 *     1. mark the slot invalid
 *     2. write payload, length and sequence number
 *     3. write the CRC16 over length + sequence + payload
 *     4. mark the slot valid
 * A power cut at any point leaves the previous checkpoint intact, and a
 * torn write fails its CRC on restore.
 *
 * MPU: if you enable the MPU in the CCS project (Properties > General >
 * MPU, automatic segments), .TI.persistent lands in the read/write segment
 * and code becomes read/execute only, which protects code against stray
 * writes during brown-out. The info memory segment is read-only in that
 * setup, which is why the store does not use info memory.
 *
 * Reference: SLAU367P chapters 7 (FRAM) and 9 (MPU).
 */
#ifndef DRIVERS_CKPT_H_
#define DRIVERS_CKPT_H_

#include <stdint.h>
#include <stdbool.h>

#define CKPT_MAX_PAYLOAD    (256u)

/*
 * Fault-injection test build (test 4): set to 1 to compile the cut points
 * into ckpt_save() and replace the logger in main.c with fault_test().
 * Leave at 0 for normal use: the save path is then unchanged.
 */
#ifndef CKPT_FAULT_TEST
#define CKPT_FAULT_TEST     0
#endif

/* Save len bytes (<= CKPT_MAX_PAYLOAD). ~20 us per 100 bytes at 8 MHz. */
bool ckpt_save(const void *data, uint16_t len);

/*
 * Copy the newest valid checkpoint into data. Fails if none is valid or
 * its length differs from len (layout changed).
 * seq (optional) receives its sequence number, which increases by one
 * per save.
 */
bool ckpt_restore(void *data, uint16_t len, uint32_t *seq);

/* Invalidate both slots. */
void ckpt_erase(void);

#if CKPT_FAULT_TEST
/*
 * Cut points inside ckpt_save(), in save order. When the save reaches the
 * armed point it calls fire(), which is expected not to return (a software
 * BOR, i.e. a full reset exactly at that point).
 */
typedef enum
{
    CKPT_CUT_NONE = 0,
    CKPT_CUT_AFTER_INVALIDATE,      /* magic = EMPTY written            */
    CKPT_CUT_MID_PAYLOAD,           /* half of the payload written      */
    CKPT_CUT_AFTER_PAYLOAD,         /* payload written, len/seq not yet */
    CKPT_CUT_AFTER_HEADER,          /* len and seq written, CRC not yet */
    CKPT_CUT_BEFORE_COMMIT,         /* CRC written, magic not VALID yet */
    CKPT_CUT_AFTER_COMMIT           /* magic = VALID written            */
} ckpt_cut_t;

void ckpt_fault_arm(ckpt_cut_t point, void (*fire)(void));

/* Corrupt the newest valid slot in place, as a stray FRAM bit error would. */
typedef enum { CKPT_CORRUPT_PAYLOAD, CKPT_CORRUPT_SEQ } ckpt_corrupt_t;
void ckpt_test_corrupt(ckpt_corrupt_t what);
#endif

#endif /* DRIVERS_CKPT_H_ */
