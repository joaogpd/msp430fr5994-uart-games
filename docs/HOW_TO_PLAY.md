# MSP430FR5994 UART Terminal Game — How to Play

## Overview

The active firmware in [`main.c`](../main.c) runs **Dungeon Crawler** (`app/dungeon_game`), a turn-based ANSI/VT100 roguelike on a procedural **48 × 22** tile dungeon (1,056 tiles) with a **28 × 17** scrolling viewport + right HUD sidebar (**80 × 24** layout). It features **3 playable classes + an autonomous on-chip FRAM AI Demo (`[4] AI DEMO`)**, **3 spells + 3-slot consumable belt**, **in-world dungeon shops**, **integer Bresenham line-of-sight**, **5 regular enemy archetypes + the Floor-5 Boss ("The Bone Warden") & Relics**, **Low-HP visual/LED/audio alerts**, **4-hex seeded runs**, and **power-loss-safe FRAM checkpoints & Top-5 Hall of Fame**.

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
| `W` or `Up Arrow` | Face North and move / melee-attack / open door / bump shop *(Class Select: start Knight; Game Over: open Class Select, or restart AI Demo if in AI Demo)* |
| `S` or `Down Arrow` | Face South and move / melee-attack / open door / bump shop |
| `A` or `Left Arrow` | Face West and move / melee-attack / open door / bump shop |
| `D` or `Right Arrow` | Face East and move / melee-attack / open door / bump shop |
| `F` | Cast **Arcane Bolt** (`-1 MP`, range 8, pierces up to 2 foes for Arcanist, shatters `%%` Cracked Walls, advances turn) |
| `Q` | Cast **Frost Nova** (`-2 MP`, 3 × 3 AoE around player, freezes hit foes for 2 turns `FR` & cancels aiming, advances turn) |
| `E` | Cast **Blink** (`-2 MP`, teleports up to 3 tiles in your facing direction past foes to the farthest open tile, advances turn) |
| `Space` | **Meditate** 1 turn (`+1 MP` up to Max MP **and** empowers your next attack `[EMP]`; advances turn) *(Class Select: start Knight; Game Over: open Class Select, or restart AI Demo; closes Map Overlay without spending a turn)* |
| `1` | Drink **Health Potion** from Belt (`+6 HP`, advances turn) *(Class Select: choose **Knight**)* |
| `2` | Detonate **Dungeon Bomb** from Belt (3 × 3 blast for `ATK + 4` dmg & shatters inner stone/cracked walls, advances turn) *(Class Select: choose **Arcanist**)* |
| `3` | Read **Scroll of Mapping** from Belt (reveals entire 48 × 22 floor & all traps; does **not** advance turn) *(Class Select: choose **Rogue**)* |
| `4` | *(Class Select / AI Demo Game Over)* Start **`[4] AI DEMO (FRAM)`** autonomous on-chip Q7 GA mode |
| `T` | *(AI Demo only)* Toggle live between **Heuristic A (`22 B` Q7)** and **Tiny-MLP B (`587 B` Q7)** on-chip FRAM models |
| `N` | *(AI Demo only)* Exit **AI Demo** and return to **Class Select** |
| `M` | Toggle **1:1 Full-Floor Map Overlay** (48 × 22 view of explored tiles; press `M`, `Space`, or any command to close) |
| `C` | **Re-center camera** on the player without advancing a turn |
| `H` | Toggle **FRAM Hall of Fame** (Top 5 runs; works during play, Class Select, and Game Over; press `H` or any key to close) |
| `?` | Toggle **In-Game Help & Legend** overlay (press `?` or any key to close) |
| `P` | **Save** current active run checkpoint to FRAM *(blocked while dead or in AI Demo)* |
| `R` | **Load** last saved checkpoint from FRAM *(works anytime, including Class Select and Game Over; exits AI Demo if active)* |
| `X` | **Erase** FRAM run checkpoints *(resets `BEST` to current floor; Hall of Fame is preserved; blocked in AI Demo)* |
| `Ctrl+L`, `Enter`, or `L` | Force a full-screen ANSI redraw without advancing a turn |
| `Ctrl+]` | Exit the built-in PowerShell `inline` terminal cleanly |

### On-Board LaunchPad Buttons & LEDs

Buttons are debounced in 20 ms steps (`TIMER_A0`, 0 Hz when idle); a **hold** registers after **~0.8 s** (40 ticks).

| Hardware Input | Action (During Play) | Action (Class Select / Game Over / Boot) |
| :--- | :--- | :--- |
| **`S1` short press** (`P5.6`, left) | Rotate facing direction 90° clockwise (`N → E → S → W`) | — |
| **`S2` short press** (`P5.5`, right) | Step / melee-attack / open door / bump shop in facing direction | Class Select: start Knight immediately; Game Over: open Class Select |
| **Hold `S1`** (~0.8 s) | Meditate (`+1 MP` & Empower, same as `Space`) | Game Over: load last FRAM checkpoint (`R`) |
| **Hold `S2`** (~0.8 s) | Cast **Arcane Bolt** (`F`) | — |
| **Hold `S1 + S2`** (~0.8 s) | Save checkpoint to FRAM (`P`) | Blocked while dead |
| **Hold `S1` on boot/reset** | — | Skip FRAM auto-resume and open Class Select for a fresh Floor 1 run |

- **Red `LED1` (`P1.0`):** Blinks every **500 ms** on **Low HP**; stays solid **ON** on **Game Over**; **OFF** during normal active play.
- **Green `LED2` (`P1.1`):** Kept **OFF** after FRAM saves to conserve low-power sleep current.

---

## HUD & Map Legend

### Screen Layout (80 × 24)

- **Row 1 (Top Banner):** Displays `MSP430 DUNGEON` (or magenta **`AI:HEURISTIC`** / **`AI:TINY-MLP`** during **`[4] AI DEMO`**), `FL` (current floor), `BEST` (deepest floor reached), 4-hex `SEED`, and active `BIOME` (`STONE CRYPT`, `FUNGAL CAVERNS`, or `BOSS SANCTUM`). Switches to a **pulsing red `[! LOW HP !]` warning** when HP is critical.
- **Rows 2–18 (Left, 56 cols):** **28 × 17** scrolling dungeon viewport (2-character wide glyphs).
- **Rows 2–18 (Right, 22 cols):** Live **HUD Sidebar**:

```text
HP:16/16 MP:04/04
ATK:03  DEF:01
GOLD:0000 [EMP]
KNT DIR:E 12,08
---------------------
VISIBLE FOES:
SK Skeleton 5/5
A! Archer 4/4
(or FR when frozen)
---------------------
[1]Pot:1 [2]Bmb:0
[3]Map:0
---------------------
F:Bolt(1) Q:Nova(2)
E:Blink(2) Spc:Med
REL:[VAMP][ARC]
BOSS[########]24
```

| HUD Field | Description |
| :--- | :--- |
| `FL` / `BEST` / `SEED` | Current floor (`01`+), deepest floor reached (FRAM), and 4-hex **Run Seed** (`0001`–`FFFF`) |
| `BIOME` | Active floor generator: `STONE CRYPT`, `FUNGAL CAVERNS`, or `BOSS SANCTUM` |
| `HP` / `!HP:` | Current / Max HP; flashes red as **`!HP:`** during a Low-HP alert |
| `MP` | Current / Max Mana Points |
| `ATK` / `DEF` | Attack power and Armor Defense |
| `GOLD` / `[EMP]` | Total Gold (`0000`–`9999`) and bright-green `[EMP]` badge when **Empowered** by Meditation |
| `KNT`/`ARC`/`ROG` | Active class code, facing direction (`N`/`E`/`S`/`W`), and `(x, y)` coordinates |
| `VISIBLE FOES` | Lists up to **3** awake enemies in line of sight with live `HP/MaxHP` (`FR` when frozen, `A!`/`B!` when telegraphing) |
| `[1]Pot` `[2]Bmb` `[3]Map` | 3-slot **Consumable Belt** counts (max **9** per slot) |
| `REL:` | Claimed permanent Boss Relics (`[VAMP]`, `[ARC]`, or `(none)`) |
| `BOSS[########]24` | Live **Boss HP bar** on Row 18 while The Bone Warden is alive (otherwise shows `M:Map H:HoF ?:Help`) |

- **Row 20 (Bright Yellow):** Combat, shop, item, relic, AI, and FRAM status messages. Quiet walking steps automatically clear old Row 20 messages after **2 steps**.
- **Row 21 (Dark Gray):** Quick keybind reminder bar.

### Low-HP Visual, LED & Audio Alert

Whenever your HP drops to **`≤ 25%` of Max HP** or **`≤ 3 HP`** (while alive):
- **Row 1 Banner** switches to a pulsing red `[! LOW HP !] FL:xx HP:xx/xx PRESS 1 FOR POTION OR RETREAT!` alert (alternating bright white-on-red and red-on-black every 500 ms).
- **Sidebar Row 2** flashes `!HP:xx/xx` in red.
- **Hardware LED:** Red **`LED1` (`P1.0`)** blinks every **500 ms** (`TIMER_A1`).
- **Audio Cue:** Emits an immediate ANSI **`BEL` (`0x07`)** on taking critical damage, plus a periodic `BEL` every **2.0 s** while Low HP persists and a terminal is connected.

### Map Glyphs

Each viewport tile is rendered as a 2-character wide ANSI glyph; the `M` Full-Floor Map Overlay uses compact 1-character glyphs.

| Viewport Glyph | Map (`M`) | Color | Tile / Entity | Effect / Notes |
| :---: | :---: | :--- | :--- | :--- |
| `^^` `>>` `vv` `<<` | `@` | Bright Green | **Player** | Shows your facing direction (North, East, South, West) |
| `##` | `#` | White (lit) / Dark Gray (dim) | **Stone Wall** | Impassable; blocks LOS, movement (except Wraiths), and spells; inner walls shattered by Bombs (`2`) |
| `++` | `+` | Yellow | **Closed Door** | Blocks LOS & non-Wraith movement; bump to open (advances turn) |
| `/` | `/` | Dark Gray | **Open Door / Spent Shrine** | Passable; does not block LOS |
| `%%` | `%` | Yellow | **Cracked Wall** | Blocks LOS & movement; shatter with **Arcane Bolt (`F`)** or **Bomb (`2`)** to reach secret alcoves |
| `^` | `^` | Bright Red | **Spike Trap** | Hidden until ≤ 2 tiles away (≤ 4 for Rogue), revealed by Mapping Scroll, or triggered; deals **2 raw dmg** |
| `&` | `&` | Bright Magenta | **Shrine** | Walkable single-use altar (**Blood Altar** or **Aegis Font**); becomes `/` once used |
| `$$` | `$` | Bright Green | **Shop Pedestal** | Found on even non-boss floors; bump once to preview price, bump again to buy |
| `*V` | `*` | Bright Red *(Magenta on `M`)* | **Vampiric Fang** | Boss Relic: **`+1 HP` on every enemy kill** |
| `*A` | `*` | Bright Cyan *(Magenta on `M`)* | **Arcane Ring** | Boss Relic: **`+2 Max MP`**, instant MP refill, and **`+1 MP` per floor** |
| `>` | `>` | Bright Yellow | **Stairs Down** | Descends to `FL + 1`, updates `BEST`, and auto-saves to FRAM |
| `+` | `+` | Bright Cyan | **Health Potion** | Stashes into Belt slot `1` (max 9); if full, drinks immediately for **+6 HP** |
| `*` | `*` | Bright Blue | **Mana Crystal** | Restores **+3 MP** (capped at Max MP) |
| `$` | `$` | Bright Yellow | **Treasure Chest** | **+25 Gold**, **+1 Max HP**, **+1 HP**, plus 25% each for **+1 ATK**, **+1 Bomb**, or **+1 Mapping Scroll** *(watch for Mimics!)* |
| `sl` | `!` | Bright Red | **Slime** | Slow melee foe (moves every 2nd turn) |
| `SK` | `!` | Bright Magenta | **Skeleton** | Sturdy melee foe (Floor 2+ and Boss summon) |
| `AR` / `A!` | `!` | Bright Yellow / White on Red | **Cultist Archer** | Telegraphs (`A!`) on Turn 1, fires a 6-tile arrow on Turn 2 |
| `WR` | `!` | Bright Cyan | **Wraith** | Phases through walls/doors; melee drains `-1 MP` (Floor 3+) |
| `MM` *(or `$`)* | `!` *(or `$`)* | Bright Red *(or Yellow)* | **Mimic** | Disguised as a Chest (`$`) until bumped or hit (Floor 2+) |
| `BW` / `B!` | `!` | Bright Magenta / White on Red | **The Bone Warden** | Floor 5 / 10 / 15… Boss; telegraphs (`B!`) a 4-way Bone Wave |
| `!!` | — | Bright Red | **Bone Wave Danger** | Telegraphed 6-tile N/S/E/W danger cross from `B!` — move off its row/col! |
| `FR` | `!` | White on Cyan | **Frozen Foe** | Frozen by Frost Nova (`Q`); skips turns & cancels aiming while thawing |
| `.` | `.` | Dark Gray | **Visible Floor** | Open floor within current line of sight |
| *(blank)* | `.` / *(blank)* | — | **Dim Floor / Unexplored Fog** | Explored open floor outside LOS, or unexplored tile |

---

## Gameplay Mechanics

### Playable Classes & Seeded Runs

Every new run opens the **Class Select** screen (`1`, `2`, `3`, or `4` for **AI Demo**; `W`/`Space`/`S2` starts Knight immediately) and mixes the free-running `TIMER_A1` counter into the PRNG to generate a **4-hex Run Seed** (`SEED:XXXX`).

| Key | Class | Code | HP | MP | ATK | DEF | Gold | Starting Belt | Passive Ability |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- | :--- |
| `1` | **Knight** | `KNT` | 16 | 4 | 3 | 1 | 0 | `1 Pot` | Highest starting HP, ATK, and armor DEF |
| `2` | **Arcanist** | `ARC` | 11 | 8 | 2 | 0 | 0 | `1 Pot`, `1 Bmb` | **Arcane Bolt (`F`) pierces up to 2 enemies** in a line |
| `3` | **Rogue** | `ROG` | 12 | 5 | 2 | 0 | 25 | `1 Pot`, `1 Bmb`, `1 Map` | **`+1` vision radius** (7), **spots traps at distance 4**, and **`+2` Crit dmg** on first melee strike vs. full-HP foes |
| `4` | **AI Demo (FRAM)** | `KNT` | 16 | 4 | 3 | 1 | 0 | `1 Pot` | Autonomous on-chip Q7 GA agent stepping every **500 ms** (see below) |

### On-Chip FRAM AI Demo (`[4] AI DEMO`)

Selecting **`4`** on the Class Select screen launches an autonomous **Knight** run powered entirely by zero-float quantized **Q7 (`int8_t`)** Genetic Algorithm inference stored in MSP430 FRAM ([`app/ga_weights.h`](../app/ga_weights.h)) — with **zero PC script required**:

- **Autonomous Cadence:** Executes one AI action every **500 ms** (`TIMER_A1`) while active and not paused by an overlay (`M`, `H`, `?`). Uses Fair Fog-of-War sensing (only explored tiles and visible enemies), a valid-action mask (no wall bumps), and a 16-entry anti-oscillation position history.
- **Live Model Toggle (`T`):** Press **`T`** at any time during AI Demo to switch live between the two evolved on-chip FRAM models, shown in the magenta Row 1 banner:
  - **`AI:HEURISTIC`** — **Heuristic A (`22 B` Q7):** 22-feature evolved tactical heuristic (`GA_HEUR_WEIGHTS`).
  - **`AI:TINY-MLP`** — **Tiny-MLP B (`587 B` Q7):** `24 → 16 → 11` ReLU neural network (`GA_MLP_W1`/`B1`/`W2`/`B2`).
- **Exit / Restart:** Press **`N`** at any time to exit AI Demo and return to **Class Select**. When an AI Demo run ends, press **`4`**, **`W`**, **`Up Arrow`**, **`Space`**, or **`S2`** to start another AI Demo, or **`N`** for Class Select.
- **FRAM Hall of Fame & Save Protection:** AI Demo runs do **not** record into the human **FRAM Hall of Fame (`H`)** and do **not** overwrite human active FRAM saves on death.

### Procedural 48 × 22 Biomes & Floor Layout

- **Stone Crypt** *(Floors 1–2, 6, 9, …)*: Up to **8 rooms** connected by corridors plus **2 loop corridors** for flanking/kiting, with up to **6 closed doors (`++`)**. Stairs spawn in the room farthest from start.
- **Fungal Caverns** *(Floors 3–4, 7–8, …)*: Organic cave system carved by a 420-step drunkard's walk from the center of the floor. Stairs spawn at the farthest carved tile.
- **Boss Sanctum** *(Floors 5, 10, 15, …)*: Handcrafted symmetrical arena — West antechamber with a `+` Potion and `*` Mana Crystal, a closed door (`++`), and an East 4-pillar arena housing **The Bone Warden**.
- Normal floors populate **1–2 secret cracked-wall alcoves** (1 on Floor 1; 2 on Floor 2+), **1 Shrine**, **4 Spike Traps**, up to **6 items** (2 Potions, 2 Mana Crystals, 2 Chests), **7–14 enemies** (`min(6 + floor, 14)`), and on **even non-boss floors** (`2, 4, 6, 8, …`) an **In-World Dungeon Shop (`$$`)**.

### Line-of-Sight, Camera & Full-Floor Map

- **Integer Bresenham LOS (Radius 6; Radius 7 for Rogue):** Sight rays stop behind opaque tiles (`##` Stone Walls, `%%` Cracked Walls, `++` Closed Doors).
- **Fog of War:** Explored terrain remains visible in dim memory and on the `M` map overlay; enemies only appear when inside current LOS.
- **Dead-Zone Camera:** The 28 × 17 viewport pans automatically within **3 tiles** of a viewport edge. Press **`C`** to re-center.
- **1:1 Full-Floor Map (`M`):** Displays the entire 48 × 22 floor at once.

### Combat, Spells, Belt & Empowered Meditation

- **Melee Attack:** Step into an adjacent enemy to deal **`ATK`** damage (Rogue adds **`+2` Crit dmg** if the foe is at full HP).
- **Empowered Meditation (`Space` / Hold `S1`):** Restores **`+1 MP`** (up to Max MP) and grants **`[EMP]`** for your next attack (advances turn):
  - **Empowered Melee:** Deals **`+50%` damage** rounded up (`ATK + (ATK + 1) / 2`), plus Rogue's `+2` Crit if applicable.
  - **Empowered Arcane Bolt (`F`):** Deals **`ATK + 3`** instead of `ATK + 2`.
  - **Empowered Frost Nova (`Q`):** Deals **`ATK + 1`** instead of `ATK`.
- **Defense (`DEF`):** Subtracts from incoming enemy and Spike Trap damage (`damage = max(1, raw_dmg - DEF)`).

#### Spellbook

| Key | Spell | Cost | Effect |
| :---: | :--- | :---: | :--- |
| `F` | **Arcane Bolt** | `1 MP` | Fires up to **8 tiles** in your facing direction for **`ATK + 2`** dmg (`ATK + 3` Empowered). **Pierces up to 2 foes** for **Arcanist**. Shatters Cracked Walls (`%%`). Stops on Stone Walls (`##`) and Closed Doors (`++`). |
| `Q` | **Frost Nova** | `2 MP` | Hits all foes in a **3 × 3** area around you for **`ATK`** dmg (`ATK + 1` Empowered), **freezes** them for **2 turns (`FR`)**, and cancels telegraphed attacks. |
| `E` | **Blink** | `2 MP` | Teleports up to **3 tiles** in your facing direction (can jump over enemies to the farthest open tile; blocked by walls, closed doors, and shop pedestals). |

#### 3-Slot Consumable Belt (Max 9 per Slot)

| Key | Item | Advances Turn? | Effect |
| :---: | :--- | :---: | :--- |
| `1` | **Health Potion** | Yes | Restores **`+6 HP`** (capped at Max HP; blocked if already at full HP). |
| `2` | **Dungeon Bomb** | Yes | Blasts a **3 × 3** area around you for **`ATK + 4`** dmg to all adjacent foes and **shatters inner Stone Walls (`##`) & Cracked Walls (`%%`)** into open floor. |
| `3` | **Scroll of Mapping** | **No** | Permanently reveals the **entire 48 × 22 floor** and **all hidden Spike Traps** (`^`). Press `M` to view the full map! |

### In-World Dungeon Shops (`$$`)

On every **even non-boss floor** (`2, 4, 6, 8, …`), **3 green `$$` pedestals** spawn in open rooms (never blocking 1-tile corridors):

| Pedestal | Stock | Price Formula |
| :---: | :--- | :--- |
| **Slot 1** | **`+1 ATK SWORD`** | `25 + min(floor × 5, 50)` Gold *(e.g. `35G` on FL 2, `45G` on FL 4, caps at `75G`)* |
| **Slot 2** | **`+1 DEF ARMOR`** | `30 + min(floor × 5, 50)` Gold *(e.g. `40G` on FL 2, `50G` on FL 4, caps at `80G`)* |
| **Slot 3** | **Belt Supply** (random) | **Health Potion** (`15G`), **Dungeon Bomb** (`20G`), or **Mapping Scroll** (`18G`) |

- **How to Buy:** Bump a `$$` pedestal **once** to preview the item and price in the status bar; bump it a **second time** to buy. Neither previewing nor buying advances the enemy turn.

### Enemies & The Floor-5 Boss

Awake regular enemies within **Manhattan distance ≤ 8** act after each of your turns. Starting on **Floor 4**, regular enemies gain bonus Max HP equal to **`(floor - 2) / 2`**. **The Bone Warden** starts at **24 HP** on Floor 5 and gains **`+6 HP`** on every subsequent Boss floor (`30 HP` on FL 10, `36 HP` on FL 15, …).

| Enemy | Glyph | Appears From | Base HP | Raw DMG | Gold (Melee / Spell) | Special Behavior |
| :--- | :---: | :--- | :---: | :---: | :---: | :--- |
| **Slime** | `sl` | Floor 1+ | 3 | 1 | 6 / 8 | Slow: moves every **2nd turn** (attacks every turn when adjacent) |
| **Skeleton** | `SK` | Floor 2+ | 5 | 2 | 12 / 15 | Relentless melee pursuer; moves every turn |
| **Cultist Archer** | `AR` / `A!` | Floor 1+ | 4 | 2 | 14 / 16 | Aligned in clear LOS within **2–5 tiles**: telegraphs (`A!`) Turn 1, fires 6-tile arrow Turn 2 — sidestep to dodge |
| **Wraith** | `WR` | Floor 3+ | 3 | 1 | 16 / 18 | **Phases through walls & closed doors**, ignores Spike Traps, and melee hits drain **`-1 MP`** |
| **Mimic** | `MM` *(as `$`)* | Floor 2+ (1/floor) | 6 | 3 | 30 / 30 | Dormant & disguised as a Chest (`$`). Bumping wakes it without damaging it; spells/bombs wake & damage it |
| **The Bone Warden** | `BW` / `B!` | Floor 5, 10, 15… | 24+ | 3 | 100 / 100 | **Boss:** Telegraphs (`B!`) a 6-tile 4-way **Bone Wave (`!!`)** dealing 3 raw dmg next turn (leave its row/col or hide behind a pillar!); summons a **Skeleton (`SK`)** once at `≤ 50% HP` |

#### Boss Victory & Permanent Relics

Slaying **The Bone Warden** awards **`+100 Gold`**, unlocks the **Stairs Down (`>`)** to the Endless Deep, and drops **2 Relic pedestals** (step onto one to claim it; the other dissolves):
- **`*V` Vampiric Fang (`[VAMP]`):** Heals **`+1 HP`** on every enemy kill (up to Max HP).
- **`*A` Arcane Ring (`[ARC]`):** Permanently grants **`+2 Max MP`**, refills MP to full immediately, and restores **`+1 MP`** each time you descend stairs.

### Interactive Terrain, Items & Stairs

| Feature / Item | Glyph | Effect |
| :--- | :---: | :--- |
| **Closed Door** | `++` | Blocks LOS and non-Wraith foes; bump to open into `/` (advances turn) |
| **Cracked Wall** | `%%` | Blast with **Arcane Bolt (`F`)** or **Bomb (`2`)** to shatter into floor and reveal a secret `$` Chest or `+` Potion |
| **Spike Trap** | `^` | Hidden until ≤ 2 tiles away (≤ 4 for Rogue) or revealed; deals **2 raw dmg** to player and **2 HP** to non-Wraith foes (trap kills award melee Gold) |
| **Blood Altar** *(odd floors)* | `&` | Walkable shrine: sacrifices **3 HP** (requires `HP > 3`) for permanent **`+1 ATK`** |
| **Aegis Font** *(even floors)* | `&` | Walkable shrine: restores **full HP** if injured, or grants permanent **`+1 DEF`** if already at full HP |
| **Health Potion** | `+` | Stashes into Belt slot `1` (max 9); if Belt is full, drinks immediately for **+6 HP** |
| **Mana Crystal** | `*` | Restores **+3 MP** (up to Max MP) |
| **Treasure Chest** | `$` | Grants **+25 Gold**, **+1 Max HP**, **+1 HP**, plus a 25% chance each for **`+1 ATK`**, **`+1 Bomb`**, or **`+1 Mapping Scroll`** |
| **Stairs Down** | `>` | Advances to `FL + 1`, updates `BEST`, grants `+1 MP` if holding Arcane Ring, generates next floor, and auto-saves to FRAM |

### Game Over

When your HP reaches `0`, red **`LED1`** stays solid **ON** and your run is automatically recorded into the **FRAM Hall of Fame (`H`)** if it ranks in the Top 5. Your existing active FRAM save is **preserved** (never overwritten with a dead state). From Game Over you can:
- **Start a new run (Class Select):** press `W`, `Up Arrow`, `Space`, or `S2` short press.
- **Reload your last active FRAM checkpoint:** press `R` or Hold `S1` (~0.8 s).
- **View Hall of Fame:** press `H`.

---

## FRAM Save System & Hall of Fame

The game uses the MSP430FR5994's non-volatile **FRAM** (`drivers/msp430/ckpt`, two CRC-16 protected ping-pong slots with a **1,536-byte** max payload) to persist your complete run snapshot — including class, 4-hex run seed, floor, best floor, gold, HP/Max HP, MP/Max MP, `ATK`, `DEF`, relics, Empowered state, 3-slot consumable belt, shop pedestals, position, facing direction, RNG state, the full 48 × 22 packed map, and all enemies.

In addition, the **Top-5 Hall of Fame (`H`)** is stored directly in non-volatile `.TI.persistent` FRAM, ranking runs by deepest **Floor** (tie-broken by **Gold**) with Class, Floor, Gold, and Run Seed.

| Trigger | Command | Behavior |
| :--- | :--- | :--- |
| **Auto-Save** | Descend stairs (`>`) | Automatically commits the new floor to FRAM |
| **Manual Save** | `P` or Hold `S1 + S2` (~0.8 s) | Saves the exact mid-floor state to FRAM *(blocked while dead)* |
| **Manual Load** | `R` or Hold `S1` on Game Over | Restores the last saved active FRAM checkpoint |
| **Death Protection** | Player reaches `0 HP` | Preserves any existing active FRAM checkpoint and records Top-5 human runs in Hall of Fame *(AI Demo deaths are excluded)* |
| **Auto-Resume** | Power cycle / `RESET` | Automatically resumes the saved FRAM run on boot |
| **Fresh Boot** | Hold `S1` while resetting | Skips FRAM auto-resume and opens Class Select for Floor 1 |
| **Erase Saves** | `X` | Wipes both FRAM checkpoint slots and resets `BEST` to current floor *(Hall of Fame is kept)* |
| **Hall of Fame** | `H` | Displays the persistent Top-5 FRAM leaderboard |

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
