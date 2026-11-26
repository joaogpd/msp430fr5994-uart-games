---
name: game-manual-maintainer
description: >-
  Generates and maintains the player manual (docs/HOW_TO_PLAY.md) directly from
  the MSP430 game source code. Use whenever game mechanics, controls, tiles,
  enemies, items, baud rate, or terminal launch behavior change.
---

# Game Manual Maintainer

Keep [docs/HOW_TO_PLAY.md](../../../docs/HOW_TO_PLAY.md) 100% accurate and in sync with the actual firmware and launcher code. Never guess controls, stats, damage numbers, or baud rates—always derive them directly from the source files.

## Source Files to Inspect

1. `main.c` — active game entry point, UART baud rate, clock source, sleep mode, and timers.
2. `app/dungeon_game.h` & `app/dungeon_game.c` — controls (keyboard + S1/S2 buttons), ANSI glyphs/legend, combat/enemy stats, items, mana/meditation, stairs/floor progression, FRAM checkpoint save/load/erase, and auto-connect/redraw shortcuts.
3. `app/space_game.h` & `app/space_game.c` — alternate game overview (note if `main.c.space_game` is required to switch to it).
4. `play.bat` & `tools/launch_terminal.ps1` — how to launch the serial terminal (PuTTY vs `inline`, default baud rate, exit keys).

## Required Manual Sections (`docs/HOW_TO_PLAY.md`)

1. **Title & Active Game Overview** — what game is currently active in `main.c`, hardware target (`MSP-EXP430FR5994`), and UART settings.
2. **Getting Started (Connecting & Launching)** — flashing in CCS, running `play.bat` / Desktop shortcut / `inline` mode, auto-connect behavior (no RESET needed), and manual redraw shortcuts.
3. **Controls** — clear Markdown tables for:
   - Keyboard controls (WASD, Arrow keys, Arcane Bolt, Meditate, Save/Load/Erase, Redraw, Game Over restart)
   - On-board LaunchPad buttons (`S1` / `S2` short press, hold, both held, hold `S1` on boot)
4. **HUD & Map Legend** — exact HUD fields (`FL`, `HP`, `MP`, `G`, `BEST`) and a table of every on-screen glyph (`^^`/`>>`/`vv`/`<<`, `##`, `>`, `sl`, `SK`, `+`, `*`, `$`, `.`) with its color and effect.
5. **Gameplay Mechanics** — vision/fog-of-war radius, melee vs Arcane Bolt, enemy aggro range & damage/gold rewards, potions/mana crystals/chests, descending stairs, and game-over flow.
6. **FRAM Save System** — automatic floor saves, manual save/load/erase, and power-loss persistence.
7. **Troubleshooting** — quick fixes (COM port not found, blank screen redraw, wrong baud rate, switching to `space_game`).

## Update Procedure

1. Read the source files listed above (or inspect the diff if updating after a small change).
2. Write or update `docs/HOW_TO_PLAY.md` so every number, keybinding, glyph, and baud rate matches the code.
3. Ensure `docs/HOW_TO_PLAY.md` has a newer modification timestamp than the edited game files so the `.agents/hooks.json` sync check passes.
