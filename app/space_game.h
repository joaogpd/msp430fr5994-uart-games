#ifndef APP_SPACE_GAME_H_
#define APP_SPACE_GAME_H_

#include <stdint.h>
#include <stdbool.h>

#define GAME_GRID_W         20u
#define GAME_GRID_H         15u
#define GAME_TILE_COUNT     (GAME_GRID_W * GAME_GRID_H)       /* 300 tiles */
#define GAME_PACKED_BYTES   (GAME_TILE_COUNT / 2u)            /* 150 bytes (4-bit nibbles) */

typedef enum
{
    TILE_EMPTY      = 0,
    TILE_PLAYER     = 1,
    TILE_ENEMY      = 2,
    TILE_LASER      = 3,
    TILE_SHIELD     = 4,
    TILE_EXPLOSION  = 5,
    TILE_ENEMY_SHOT = 6,
    TILE_POWERUP    = 7
} game_tile_t;

void space_game_init(void);

/* Call from UART RX ISR. Returns true to wake main() from LPM. */
bool space_game_push_input(uint8_t cmd);

/* Advance one 20 ms (50 Hz) input & physics step (main context). */
void space_game_tick(void);

/* Render ANSI terminal frame over UART_A0 every 4th tick / 80 ms (main context). */
void space_game_render(void);

#endif /* APP_SPACE_GAME_H_ */
