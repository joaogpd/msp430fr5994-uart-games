#ifndef APP_DUNGEON_GAME_H_
#define APP_DUNGEON_GAME_H_

#include <stdint.h>
#include <stdbool.h>

#define DNG_W               20u
#define DNG_H               15u
#define DNG_TILE_COUNT      (DNG_W * DNG_H)              /* 300 tiles */
#define DNG_PACKED_BYTES    (DNG_TILE_COUNT / 2u)        /* 150 bytes (4-bit nibbles) */
#define DNG_FOG_BYTES       ((DNG_TILE_COUNT + 7u) / 8u) /* 38 bytes (1 bit/tile) */

typedef enum
{
    DNG_WALL     = 0,
    DNG_FLOOR    = 1,
    DNG_STAIRS   = 2,
    DNG_SLIME    = 3,
    DNG_SKELETON = 4,
    DNG_POTION   = 5,
    DNG_MANA     = 6,
    DNG_CHEST    = 7
} dng_tile_t;

void dungeon_game_init(void);

/* Call from UART RX ISR. Returns true to wake main() from LPM3. */
bool dungeon_game_push_input(uint8_t cmd);

/* Call from GPIO P5.5/P5.6 falling-edge ISR to start one-shot debounce. */
bool dungeon_game_on_button_irq(void);

/* Call from TIMER_A0 one-shot ISR every 40 ms while a button is active. */
bool dungeon_game_on_debounce_timer(void);

/* Process pending command and render ANSI screen over UART if dirty (main context). */
void dungeon_game_process(void);

#endif /* APP_DUNGEON_GAME_H_ */
