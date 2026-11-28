#ifndef APP_RUNNER_GAME_H_
#define APP_RUNNER_GAME_H_

#include <stdint.h>
#include <stdbool.h>
#include "drivers/msp430/timer.h"

void     runner_game_init(void);
bool     runner_game_push_input(uint8_t cmd);
bool     runner_game_on_button_irq(void);
bool     runner_game_on_debounce_timer(timer_id_t id);
bool     runner_game_has_pending_work(void);
bool     runner_game_exit_requested(void);
void     runner_game_process(void);
uint16_t runner_game_best_score(void);

#endif /* APP_RUNNER_GAME_H_ */
