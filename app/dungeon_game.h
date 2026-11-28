#ifndef APP_DUNGEON_GAME_H_
#define APP_DUNGEON_GAME_H_

#include <stdint.h>
#include <stdbool.h>
#include "drivers/msp430/timer.h"

/* 48x22 map (1,056 tiles = 3.5x larger; fits 1:1 on an 80x24 terminal overlay) */
#define DNG_W               48u
#define DNG_H               22u
#define DNG_TILE_COUNT      (DNG_W * DNG_H)              /* 1,056 tiles */

/* 28x17 2-char/tile viewport (56x17 chars) + 22-col right HUD sidebar = 80x24 */
#define DNG_VIEW_W          28u
#define DNG_VIEW_H          17u

#define DNG_MAX_ENEMIES     20u

/* Low 4 bits of each packed map byte store the base tile type (0..15) */
typedef enum
{
    DNG_WALL         = 0,
    DNG_FLOOR        = 1,
    DNG_STAIRS       = 2,
    DNG_DOOR_CLOSED  = 3,
    DNG_DOOR_OPEN    = 4,
    DNG_CRACKED_WALL = 5,
    DNG_TRAP         = 6,
    DNG_SHRINE       = 7,
    DNG_POTION       = 8,
    DNG_MANA         = 9,
    DNG_CHEST        = 10,
    DNG_SHOP         = 11,
    DNG_RELIC        = 12
} dng_tile_t;

#define DNG_MAX_SHOP_ITEMS  3u
#define DNG_HOF_ENTRIES     5u

/* High 4 bits of each packed map byte store per-tile flags */
#define TILE_TYPE_MASK      0x0Fu
#define TILE_F_EXPLORED     0x10u
#define TILE_F_VISIBLE      0x20u
#define TILE_F_REVEALED     0x40u   /* Trap spotted/triggered, or Shrine used */
#define TILE_F_ALT          0x80u   /* Shrine variant: 0=Blood Altar, 1=Aegis Font */

typedef enum
{
    ENEMY_NONE     = 0,
    ENEMY_SLIME    = 1,
    ENEMY_SKELETON = 2,
    ENEMY_ARCHER   = 3,
    ENEMY_WRAITH   = 4,
    ENEMY_MIMIC    = 5,
    ENEMY_BOSS     = 6
} dng_enemy_type_t;

typedef enum
{
    CLASS_KNIGHT   = 0,
    CLASS_ARCANIST = 1,
    CLASS_ROGUE    = 2
} dng_class_t;

typedef enum
{
    DIFF_NORMAL = 0,
    DIFF_EASY   = 1,
    DIFF_HARD   = 2
} dng_diff_t;

/* Permanent relic bitmask */
#define RELIC_VAMP          0x01u   /* Vampiric Fang: +1 HP on every enemy kill */
#define RELIC_ARCANE        0x02u   /* Arcane Ring: +2 Max MP & +1 MP per floor */

void dungeon_game_init(void);

/* Call from UART RX ISR. Returns true to wake main() from LPM. */
bool dungeon_game_push_input(uint8_t cmd);

/* Call from GPIO P5.5/P5.6 falling-edge ISR to start one-shot debounce. */
bool dungeon_game_on_button_irq(void);

/* Call from TIMER_A0 one-shot ISR every 20 ms while a button is active. */
bool dungeon_game_on_debounce_timer(timer_id_t id);

/* Call from TIMER_A1 periodic ISR (500 ms) to auto-detect terminal connect. */
bool dungeon_game_on_probe_timer(timer_id_t id);

/* Returns true if queued input, a probe tick, or a dirty frame is pending. */
bool dungeon_game_has_pending_work(void);

/* Process pending commands and render ANSI screen over UART if dirty (main context). */
void dungeon_game_process(void);

#endif /* APP_DUNGEON_GAME_H_ */
