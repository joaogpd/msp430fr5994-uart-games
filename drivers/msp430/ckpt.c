/*
 * ckpt.c - Power-loss-safe checkpoint store in FRAM
 */
#include "ckpt.h"
#include "crc.h"
#include "hal_common.h"
#include <string.h>

#define SLOT_VALID   (0xC4A5u)
#define SLOT_EMPTY   (0x0000u)

typedef struct
{
    volatile uint16_t magic;            /* written last on save */
    uint16_t len;
    uint32_t seq;
    volatile uint16_t crc;              /* volatile: keeps crc -> magic store order */
    uint8_t  data[CKPT_MAX_PAYLOAD];
} ckpt_slot_t;

#pragma PERSISTENT(s_slot)
static ckpt_slot_t s_slot[2] = { { 0 }, { 0 } };

#if CKPT_FAULT_TEST
static ckpt_cut_t s_cut;
static void (*s_fire)(void);

void ckpt_fault_arm(ckpt_cut_t point, void (*fire)(void))
{
    s_cut = point;
    s_fire = fire;
}

#define CUT(p)  do { if (s_cut == (p) && s_fire) { s_fire(); } } while (0)
#else
#define CUT(p)  do { } while (0)
#endif

static uint16_t slot_crc(const ckpt_slot_t *s)
{
    uint16_t c = crc16(&s->len, sizeof s->len, CRC16_SEED);
    c = crc16(&s->seq, sizeof s->seq, c);
    return crc16(s->data, s->len, c);
}

static bool slot_ok(const ckpt_slot_t *s)
{
    return s->magic == SLOT_VALID && s->len <= CKPT_MAX_PAYLOAD && s->crc == slot_crc(s);
}

/* index of the newest valid slot, or -1 */
static int newest(void)
{
    bool a = slot_ok(&s_slot[0]);
    bool b = slot_ok(&s_slot[1]);
    if (a && b) { return (int32_t)(s_slot[1].seq - s_slot[0].seq) > 0 ? 1 : 0; }
    if (a) { return 0; }
    if (b) { return 1; }
    return -1;
}

bool ckpt_save(const void *data, uint16_t len)
{
    if (len > CKPT_MAX_PAYLOAD) { return false; }

    int cur = newest();
    uint32_t seq = (cur >= 0) ? s_slot[cur].seq + 1u : 1u;
    ckpt_slot_t *s = &s_slot[(cur == 0) ? 1 : 0];

    s->magic = SLOT_EMPTY;
    CUT(CKPT_CUT_AFTER_INVALIDATE);
#if CKPT_FAULT_TEST
    memcpy(s->data, data, len / 2u);
    CUT(CKPT_CUT_MID_PAYLOAD);
    memcpy(s->data + len / 2u, (const uint8_t *)data + len / 2u, len - len / 2u);
#else
    memcpy(s->data, data, len);
#endif
    CUT(CKPT_CUT_AFTER_PAYLOAD);
    s->len = len;
    s->seq = seq;
    CUT(CKPT_CUT_AFTER_HEADER);
    s->crc = slot_crc(s);
    CUT(CKPT_CUT_BEFORE_COMMIT);
    s->magic = SLOT_VALID;              /* commit point */
    CUT(CKPT_CUT_AFTER_COMMIT);

    return slot_ok(s);                   /* read back */
}

bool ckpt_restore(void *data, uint16_t len, uint32_t *seq)
{
    int i = newest();
    if (i < 0 || s_slot[i].len != len) { return false; }
    memcpy(data, s_slot[i].data, len);
    if (seq) { *seq = s_slot[i].seq; }
    return true;
}

void ckpt_erase(void)
{
    s_slot[0].magic = SLOT_EMPTY;
    s_slot[1].magic = SLOT_EMPTY;
}

#if CKPT_FAULT_TEST
void ckpt_test_corrupt(ckpt_corrupt_t what)
{
    int i = newest();
    if (i < 0) { return; }
    if (what == CKPT_CORRUPT_PAYLOAD)
    {
        s_slot[i].data[s_slot[i].len / 2u] ^= 0x10u;    /* one bit */
    }
    else
    {
        s_slot[i].seq ^= 0x00010000ul;                  /* one header bit */
    }
}
#endif
