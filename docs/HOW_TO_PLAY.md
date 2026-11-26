# MSP430FR5994 UART Terminal Game — How to Play

## Overview

The active firmware in [`main.c`](../main.c) runs **Dungeon Crawler** (`app/dungeon_game`), a turn-based ANSI/VT100 roguelike on a procedural **48 × 22** tile dungeon (1,056 tiles) with two alternating biomes, a **28 × 17** scrolling viewport + right HUD sidebar (**80 × 24** layout), integer Bresenham line-of-sight, multi-HP tactical combat, 5 enemy archetypes, interactive terrain, and power-loss-safe FRAM checkpoints.

| Setting | Value |
| :--- | :--- |
| **Target Hardware** | TI **MSP-EXP430FR5994** LaunchPad |
| **Active Game** | `dungeon_game` ([`app/dungeon_game.c`](../app/dungeon_game.c)) |
| **UART Interface** | eUSCI_A0 Backchannel UART (`P2.0` TXD / `P2.1` RXD) |
| **Serial Parameters** | **115200 baud**, **8N1**, no flow control |
| **Terminal Size** | **80 × 24** (or larger) |
| **Clocks & Sleep** | 8 MHz DCO (`MCLK`/`SMCLK`), 32.768 kHz LFXT (`ACLK`), sleeps in `LPM0` between events |

> **Alternate Game:** This repository also includes an arcade space shooter ([`app/space_game.h`](../app/space_game.h) / [`app/space_game.c`](../app/space_game.c)). See [Switching to `space_game`](#switching-to-space_game) in Troubleshooting to build and flash it.

---

## Getting Started (Connecting & Launching)

1. **Build & Flash** the project to the MSP-EXP430FR5994 LaunchPad in Code Composer Studio (CCS) over USB (ensure the eZ-FET `TXD`/`RXD` jumpers are installed).
2. **Launch the Terminal** using [`play.bat`](../play.bat) (or your Desktop shortcut):
   - **Default (auto):** double-click `play.bat` or run `play.bat` from a terminal. It auto-detects the `MSP Application UART1` COM port and opens **PuTTY** at **115200 8N1** if installed, or falls back to the built-in PowerShell inline terminal.
   - **Built-in inline terminal:** run `play.bat inline` (press **`Ctrl+]`** to exit cleanly).
   - **Force PuTTY:** run `play.bat putty`.
   - **Direct PowerShell launcher:**
     ```powershell
     .\tools\launch_terminal.ps1 -Baud 115200 -Mode auto
     # Optional manual COM port override:
     .\tools\launch_terminal.ps1 -Port COM4 -Mode inline
     ```

### Auto-Connect & Screen Redraw (No RESET Needed)

You do **not** need to press the LaunchPad's `RESET` button after opening the terminal:

- **VT100 Auto-Detect (PuTTY):** Every 500 ms (`TIMER_A1`), the firmware sends a VT100 Device Status Report probe (`ESC [ 5 n`). When your terminal replies (`ESC [ 0 n`), the game automatically triggers a full-screen redraw.
- **Inline Terminal Auto-Redraw:** `tools/launch_terminal.ps1` immediately sends `Ctrl+L` (`0x0C`) upon opening the COM port to trigger an instant full-screen redraw.
- **Manual Redraw:** Press **`Ctrl+L`**, **`Enter`**, or **`L`** at any time to redraw the entire screen without advancing a turn.

---

## Controls

### Keyboard Controls

Letter keys are case-insensitive.

| Key | Action |
| :--- | :--- |
| `W` or `Up Arrow` | Face North and move / melee-attack / open door *(starts a new run on Game Over)* |
| `S` or `Down Arrow` | Face South and move / melee-attack / open door |
| `A` or `Left Arrow` | Face West and move / melee-attack / open door |
| `D` or `Right Arrow` | Face East and move / melee-attack / open door |
| `E` | Rotate facing direction 90° clockwise (`N → E → S → W`) without moving or advancing a turn |
| `F` | Cast **Arcane Bolt** in your facing direction (`-1 MP`, range 8, shatters Cracked Walls `%%`, advances turn) |
| `Space` | **Meditate** 1 turn (`+1 MP` up to Max MP **and** empowers your next attack; advances turn) *(starts a new run on Game Over; closes Map Overlay without spending a turn)* |
| `M` | Toggle **1:1 Full-Floor Map Overlay** (48 × 22 view of explored tiles; press `M`, `Space`, or any command to close) |
| `C` | **Re-center camera** on the player without advancing a turn |
| `P` | **Save** current active run checkpoint to FRAM *(blocked while dead)* |
| `R` | **Load** last saved checkpoint from FRAM *(works anytime, including on Game Over)* |
| `X` | **Erase** all FRAM checkpoints *(resets `BEST` to current floor)* |
| `Ctrl+L`, `Enter`, or `L` | Force a full-screen ANSI redraw without advancing a turn |
| `Ctrl+]` | Exit the built-in PowerShell `inline` terminal cleanly |

### On-Board LaunchPad Buttons & LEDs

Buttons are debounced in 20 ms steps (`TIMER_A0`, 0 Hz when idle); a **hold** registers after **~0.8 s** (40 ticks).

| Hardware Input | Action (During Play) | Action (On Game Over / Boot) |
| :--- | :--- | :--- |
| **`S1` short press** (`P5.6`, left) | Rotate facing direction 90° clockwise (`E`) | — |
| **`S2` short press** (`P5.5`, right) | Step / melee-attack / open door in facing direction | Start a new run (Floor 1) |
| **Hold `S1`** (~0.8 s) | Meditate (`+1 MP` & Empower, same as `Space`) | Load last FRAM checkpoint (`R`) |
| **Hold `S2`** (~0.8 s) | Cast **Arcane Bolt** (`F`) | — |
| **Hold `S1 + S2`** (~0.8 s) | Save checkpoint to FRAM (`P`) | Blocked while dead |
| **Hold `S1` on boot/reset** | — | Skip FRAM auto-resume and start fresh on Floor 1 |

- **Red `LED1` (`P1.0`):** Turns **ON** on Game Over; **OFF** during active play.
- **Green `LED2` (`P1.1`):** Kept **OFF** after FRAM saves to conserve low-power sleep current.

---

## HUD & Map Legend

### Screen Layout (80 × 24)

- **Row 1 (Top Banner):** Displays `FL` (current floor), `BEST` (deepest floor reached), and active `BIOME` (`STONE CRYPT` or `FUNGAL CAVERNS`).
- **Rows 2–18 (Left, 56 cols):** **28 × 17** scrolling dungeon viewport (2-character wide glyphs).
- **Rows 2–18 (Right, 22 cols):** Live **HUD Sidebar** and compact legend:

```text
HP:14/14 MP:05/05
ATK:02  DEF:00
GOLD:0000 [EMP]
DIR:E POS:12,08
---------------------
VISIBLE FOES:
SK Skeleton 5/5
A! Archer 4/4
```

| HUD Field | Description |
| :--- | :--- |
| `FL` / `BEST` | Current dungeon floor (`01`+) and deepest floor reached (persisted in FRAM) |
| `BIOME` | Active floor generator: `STONE CRYPT` or `FUNGAL CAVERNS` |
| `HP` | Current / Maximum Hit Points (starts at `14/14`) |
| `MP` | Current / Maximum Mana Points (starts at `05/05`) |
| `ATK` / `DEF` | Attack power (starts at `02`) and Armor Defense (starts at `00`) |
| `GOLD` | Total Gold collected (`0000`–`9999`) |
| `[EMP]` | Shown in bright green when **Empowered** by Meditation (`Space`) |
| `DIR` / `POS` | Facing direction (`N`/`E`/`S`/`W`) and `(x, y)` map coordinates |
| `VISIBLE FOES` | Lists up to **4** awake enemies currently in line of sight with live `HP/MaxHP` (dormant Mimics stay hidden) |

- **Row 20 (Bright Yellow):** Combat, item, terrain, and FRAM status messages.
- **Row 21 (Dark Gray):** Quick keybind reminder bar.

### Map Glyphs

Each viewport tile is rendered as a 2-character wide ANSI glyph; the `M` Full-Floor Map Overlay uses compact 1-character glyphs.

| Viewport Glyph | Map (`M`) | Color | Tile / Entity | Effect / Notes |
| :---: | :---: | :--- | :--- | :--- |
| `^^` `>>` `vv` `<<` | `@` | Bright Green | **Player** | Shows your facing direction (North, East, South, West) |
| `##` | `#` | White (lit) / Dark Gray (dim) | **Stone Wall** | Impassable; blocks LOS, movement (except Wraiths), and Arcane Bolt |
| `++` | `+` | Yellow | **Closed Door** | Blocks LOS & non-Wraith movement; bump to open (advances turn) |
| `/` | `/` | Dark Gray | **Open Door / Spent Shrine** | Passable; does not block LOS |
| `%%` | `%` | Yellow | **Cracked Wall** | Blocks LOS & movement; shatter with **Arcane Bolt (`F`)** to reach secret alcoves |
| `^` | `^` | Bright Red | **Spike Trap** | Hidden (looks like floor) until ≤ 2 tiles away or triggered; deals **2 dmg** |
| `&` | `&` | Bright Magenta | **Shrine** | Walkable single-use altar (**Blood Altar** or **Aegis Font**); becomes `/` once used |
| `>` | `>` | Bright Yellow | **Stairs Down** | Descends to `FL + 1`, updates `BEST`, and auto-saves to FRAM |
| `+` | `+` | Bright Cyan | **Health Potion** | Restores **+5 HP** (capped at Max HP) |
| `*` | `*` | Bright Blue | **Mana Crystal** | Restores **+3 MP** (capped at Max MP) |
| `$` | `$` | Bright Yellow | **Treasure Chest** | **+25 Gold**, **+1 Max HP**, **+1 HP**, and 25% chance for **+1 ATK** *(watch out for Mimics!)* |
| `sl` | `!` | Bright Red | **Slime** | Slow melee foe (moves every 2nd turn) |
| `SK` | `!` | Bright Magenta | **Skeleton** | Sturdy melee foe (Floor 2+) |
| `AR` / `A!` | `!` | Bright Yellow / White on Red | **Cultist Archer** | Telegraphs (`A!`) on Turn 1, fires a 6-tile arrow on Turn 2 |
| `WR` | `!` | Bright Cyan | **Wraith** | Phases through walls/doors; melee drains `-1 MP` (Floor 3+) |
| `MM` *(or `$`)* | `!` *(or `$`)* | Bright Red *(or Bright Yellow)* | **Mimic** | Disguised as a Chest (`$`) until bumped or hit (Floor 2+) |
| `.` | `.` | Dark Gray | **Visible Floor** | Open floor within current line of sight |
| *(blank)* | `.` / *(blank)* | — | **Dim Floor / Unexplored Fog** | Explored open floor outside LOS, or unexplored tile |

---

## Gameplay Mechanics

### Starting Stats & Procedural 48 × 22 Biomes

- You begin Floor 1 with **14/14 HP**, **5/5 MP**, **ATK: 2**, **DEF: 0**, **0 Gold**, facing **East** (`>>`).
- Floors alternate between two procedural biome generators every 2 floors:
  - **Stone Crypt** *(Floors 1–2, 5–6, 9–10, …)*: Up to **8 rooms** connected by corridors plus **2 loop corridors** for flanking/kiting, with up to **6 closed doors (`++`)**. Stairs spawn in the room farthest from start.
  - **Fungal Caverns** *(Floors 3–4, 7–8, 11–12, …)*: Organic cave system carved by a 420-step drunkard's walk from the center of the floor. Stairs spawn at the farthest carved tile.
- Every floor also populates **1–2 secret cracked-wall alcoves** (1 on Floor 1; 2 on Floor 2+), **1 Shrine**, **4 Spike Traps**, up to **6 items** (2 Health Potions, 2 Mana Crystals, 2 Treasure Chests), and **7–14 enemies** (`min(6 + floor, 14)`).

### Line-of-Sight, Camera & Full-Floor Map

- **Integer Bresenham LOS (Radius 6):** Sight rays extend up to 6 tiles in all directions and stop behind opaque tiles (`##` Stone Walls, `%%` Cracked Walls, and `++` Closed Doors).
- **Fog of War:** Explored terrain remains visible in dim memory and on the `M` map overlay; enemies are only shown when currently inside your line of sight.
- **Dead-Zone Camera:** The 28 × 17 viewport pans automatically when you step within **3 tiles** of a viewport edge. Press **`C`** at any time to re-center on your character.
- **1:1 Full-Floor Map (`M`):** Displays the entire 48 × 22 floor at once so you can plan routes back to unexplored rooms, shrines, or stairs.

### Combat, Defense & Empowered Meditation

- **Melee Attack:** Step directly into an adjacent enemy to deal **`ATK`** damage and advance the enemy turn.
- **Empowered Meditation (`Space` / Hold `S1`):** Restores **+1 MP** (up to Max MP of 5) and grants **`[EMP]`** for your next attack (advances the enemy turn):
  - **Empowered Melee:** Deals **`+50%` damage** (rounded up: `ATK + (ATK + 1) / 2`, e.g., `2 → 3`, `3 → 5`).
  - **Empowered Arcane Bolt:** Deals **`+1` extra damage** (`ATK + 3` instead of `ATK + 2`).
- **Arcane Bolt (`F` / Hold `S2`):** Costs **1 MP**. Fires a straight beam up to **8 tiles** in your facing direction:
  - Deals **`ATK + 2`** damage (or **`ATK + 3`** when Empowered) to the first enemy hit.
  - **Shatters Cracked Walls (`%%`)** into open floor, exposing hidden alcoves.
  - Stops on Stone Walls (`##`) and Closed Doors (`++`).
  - Casting at `0 MP` prints a warning and does **not** advance the enemy turn.
- **Defense (`DEF`):** Subtracts from incoming enemy and Spike Trap damage (`damage = max(1, raw_dmg - DEF)`).

### Enemies

Awake enemies within **Manhattan distance ≤ 8** act after each of your turns. Starting on **Floor 4**, enemies gain bonus Max HP equal to **`(floor - 2) / 2`** (e.g., `+1 HP` on Floors 4–5, `+2 HP` on Floors 6–7).

| Enemy | Glyph | Appears From | Base HP | Raw DMG | Gold (Melee / Bolt) | Special Behavior |
| :--- | :---: | :--- | :---: | :---: | :---: | :--- |
| **Slime** | `sl` | Floor 1+ | 3 | 1 | 6 / 8 | Slow: moves every **2nd turn** (attacks every turn when adjacent) |
| **Skeleton** | `SK` | Floor 2+ | 5 | 2 | 12 / 15 | Relentless melee pursuer; moves every turn |
| **Cultist Archer** | `AR` / `A!` | Floor 1+ | 4 | 2 | 14 / 16 | If aligned in clear LOS within **2–5 tiles**, telegraphs (`A!`) on Turn 1 and fires a 6-tile arrow on Turn 2 — sidestep to dodge! |
| **Wraith** | `WR` | Floor 3+ | 3 | 1 | 16 / 18 | **Phases through walls & closed doors**, ignores Spike Traps, and melee hits drain **`-1 MP`** |
| **Mimic** | `MM` *(as `$`)* | Floor 2+ (1/floor) | 6 | 3 | 30 / 30 | Dormant and disguised as a Chest (`$`). Bumping wakes it (without damaging it on that bump); Arcane Bolt wakes & damages it |

### Interactive Terrain, Items & Stairs

| Feature / Item | Glyph | Effect |
| :--- | :---: | :--- |
| **Closed Door** | `++` | Blocks LOS and non-Wraith foes; bump to open into `/` (advances turn) |
| **Cracked Wall** | `%%` | Blast with **Arcane Bolt (`F`)** to shatter into floor and reveal a secret `$` Chest or `+` Potion |
| **Spike Trap** | `^` | Hidden until ≤ 2 tiles away or triggered; deals **2 raw dmg** to player and **2 HP** to non-Wraith foes (trap kills award melee Gold!) |
| **Blood Altar** *(odd floors)* | `&` | Walkable shrine: sacrifices **3 HP** (requires `HP > 3`) for permanent **`+1 ATK`** |
| **Aegis Font** *(even floors)* | `&` | Walkable shrine: restores **full HP** if injured, or grants permanent **`+1 DEF`** if already at full HP |
| **Health Potion** | `+` | Restores **+5 HP** (up to Max HP) |
| **Mana Crystal** | `*` | Restores **+3 MP** (up to Max MP) |
| **Treasure Chest** | `$` | Grants **+25 Gold**, **+1 Max HP**, **+1 current HP**, and a **25% chance** for **`+1 ATK`** |
| **Stairs Down** | `>` | Advances to `FL + 1`, updates `BEST`, generates a new floor, and auto-saves to FRAM |

### Game Over

When your HP reaches `0`, red **`LED1`** lights up. Your existing active FRAM save is **preserved** (never overwritten with a dead state); if no active save exists, your `BEST` floor is still saved to FRAM. From Game Over you can:
- **Start a new Floor 1 run:** press `W`, `Up Arrow`, `Space`, or `S2` short press.
- **Reload your last active FRAM checkpoint:** press `R` or Hold `S1` (~0.8 s).

---

## FRAM Save System

The game uses the MSP430FR5994's non-volatile **FRAM** (`drivers/msp430/ckpt`, two CRC-16 protected ping-pong slots with a **1,536-byte** max payload) to store a **~1,244-byte** snapshot containing your floor, best floor, gold, HP/Max HP, MP/Max MP, `ATK`, `DEF`, Empowered state, position, facing direction, RNG state, the full 48 × 22 packed map (tiles, fog of war, doors, traps, shrines), and all enemy states.

| Trigger | Command | Behavior |
| :--- | :--- | :--- |
| **Auto-Save** | Descend stairs (`>`) | Automatically commits the new floor to FRAM |
| **Manual Save** | `P` or Hold `S1 + S2` (~0.8 s) | Saves the exact mid-floor state to FRAM *(blocked while dead)* |
| **Manual Load** | `R` or Hold `S1` on Game Over | Restores the last saved active FRAM checkpoint |
| **Death Protection** | Player reaches `0 HP` | Preserves any existing active FRAM checkpoint so you can press `R` to retry |
| **Auto-Resume** | Power cycle / `RESET` | Automatically resumes the saved FRAM run on boot |
| **Fresh Boot** | Hold `S1` while resetting | Skips FRAM auto-resume and starts a new run on Floor 1 |
| **Erase Saves** | `X` | Wipes both FRAM checkpoint slots and resets `BEST` to current floor |

Because FRAM retains data without power and writes atomically across two slots, you can unplug the LaunchPad at any time and resume your exact dungeon run later.

---

## Troubleshooting

- **COM port not found (`Could not find 'MSP Application UART1'`):**
  - Check the USB cable and verify the LaunchPad's eZ-FET `TXD`/`RXD` jumpers are installed.
  - Check Device Manager for the COM port number and pass it explicitly: `.\tools\launch_terminal.ps1 -Port COMx`.
- **Blank terminal screen after connecting:**
  - Press **`Ctrl+L`**, **`Enter`**, or **`L`** to force a full-screen redraw without spending a turn.
  - In PuTTY, wait ~0.5 s for the automatic VT100 DSR probe to redraw the screen.
- **Sidebar or Full-Floor Map (`M`) wraps or looks misaligned:**
  - Resize your terminal window to at least **80 columns × 24 rows** and press **`Ctrl+L`** to redraw.
- **Garbled text or unresponsive keys:**
  - Confirm your terminal is configured for **115200 baud, 8N1, no flow control**.
- **PuTTY is not installed:**
  - Run `play.bat inline` (or plain `play.bat`, which automatically falls back to the built-in PowerShell ANSI terminal).
- <a id="switching-to-space_game"></a>**Switching to `space_game`:**
  - Replace `main.c` with [`main.c.space_game`](../main.c.space_game), rebuild, and flash in CCS. `space_game` also uses **115200 baud, 8N1**.
