# Embedding Evolved GA Policies on the TI MSP430FR5994

This document details **Stage 4** of the `feat/genetic-algorithm` subsystem: deploying both evolved policy families—**Approach A (22-parameter Tactical Heuristic)** and **Approach B (587-parameter Tiny-MLP `24 → 16 → 11`)**—directly onto the **TI MSP430FR5994** microcontroller using zero-float Q7 `int8_t` fixed-point inference stored in non-volatile FRAM.

---

## 1. Target Hardware Constraints

The TI MSP430FR5994 LaunchPad (`MSP-EXP430FR5994`) presents a classic ultra-low-power embedded AI target:

- **CPU:** 16-bit MSP430X RISC core (no hardware Floating-Point Unit).
- **Hardware Multiplier:** 32-bit `MPY32` peripheral (fast integer MAC without soft-float library overhead).
- **Non-Volatile Memory:** 256 KB FRAM (`FRAM` `0x004000–0x00FF7F` + `FRAM2` `0x010000–0x043FF7`, totaling `262,008` bytes usable).
- **Volatile Memory:** 4 KB standard `RAM` (`0x001C00–0x002BFF`, `4,096` bytes) + 4 KB Low-Energy Accelerator SRAM (`LEARAM` + `LEASTACK`, `4,096` bytes).
- **Display / I/O:** 80×24 ANSI/VT100 terminal over backchannel UART (`UART_A0` at `115200` baud).

Because software floating-point emulation (`float`/`double`) bloats code size and execution latency on MSP430, the on-chip AI engine is implemented with **zero floating-point operations** (`int8_t` weights/features and `int32_t` accumulators).

---

## 2. FRAM Weight Header (`app/ga_weights.h`)

`ga/genome.py` (`export_c_header`) exports the Deep Fair Knight champions (`ga/results/heur_fair_knight_deep/best_genome.json` and `ga/results/mlp_fair_knight_deep/best_genome.json`) into `app/ga_weights.h` as `static const int8_t` arrays. The TI MSP430 compiler (`cl430` / Linker `v21.6.1`) places these arrays in FRAM `.const` sections, consuming **0 bytes of SRAM**.

| Symbol | Shape | Type | FRAM Address (`mspgame.map`) | Size (Bytes) | Description |
| :--- | :---: | :---: | :---: | ---: | :--- |
| `GA_HEUR_WEIGHTS` | `[22]` | `const int8_t` | `0x00007c2c` | `22` | Approach A action-feature weights |
| `GA_MLP_W1` | `[16][24]` | `const int8_t` | `0x00007730` | `384` | Approach B Layer 1 weights |
| `GA_MLP_B1` | `[16]` | `const int8_t` | `0x00007c52` | `16` | Approach B Layer 1 biases |
| `GA_MLP_W2` | `[11][16]` | `const int8_t` | `0x00007a18` | `176` | Approach B Layer 2 weights |
| `GA_MLP_B2` | `[11]` | `const int8_t` | `0x00007c8a` | `11` | Approach B Layer 2 biases |
| **Total AI Weights** | — | — | **FRAM `.const`** | **609 B** | **0.23% of 262,008 B FRAM** |

Key compile-time constants in `app/ga_weights.h`:

```c
#define GA_NUM_ACTIONS        11
#define GA_HEUR_NUM_FEATURES  22
#define GA_MLP_IN             24
#define GA_MLP_HIDDEN         16
#define GA_MLP_OUT            11
#define GA_MLP_H_SHIFT        9
```

---

## 3. Zero-Float Q7 Fixed-Point Inference (`app/dungeon_game.c`)

On-chip inference is implemented in `ga_select_action_q7()` (`app/dungeon_game.c`, lines 3130–3272), occupying `0x00000b30` (**2,864 bytes**) of `.text` in `FRAM2` (`0x00010000`).

### 3.1 Single-Pass Fair Perception & Action Masking

Before evaluating either model, `ga_select_action_q7()` performs a single pass over the `48×22` FRAM dungeon grid (`s_map`) respecting **Fair Fog-of-War** (only `TILE_F_EXPLORED` tiles, `TILE_F_VISIBLE` enemies, revealed traps, and awake mimics) to find the nearest stairs (`st_x, st_y`), nearest loot (`lt_x, lt_y`), nearest unexplored frontier (`fr_x, fr_y`), nearest enemy (`en_x, en_y`), and 3×3 adjacent enemy count (`adj3`). It constructs an 11-element boolean mask `valid[11]` so illegal moves (walls, out-of-MP spells, full-HP potions) are never scored.

### 3.2 Approach A: Q7 Linear Action Scorer (22 Weights)

For each valid action `a ∈ [0, 10]`, `ga_select_action_q7()` fills a stack-allocated feature vector `int8_t f[22]` in Q7 (`[-127, 127]`) and computes a 32-bit integer inner product:

```c
int32_t sc = 0;
for (i = 0u; i < GA_HEUR_NUM_FEATURES; i++)
{
    sc += (int32_t)f[i] * (int32_t)GA_HEUR_WEIGHTS[i];
}
if (sc > best_score) { best_score = sc; best_a = a; }
```

- **Overflow Safety:** Worst-case magnitude is $22 \times 127 \times 127 = 354,838 \ll 2^{31}-1$.
- **Compute Cost:** At most $11 \times 22 = 242$ integer MACs per turn (typically 5–7 valid actions, ~130 MACs).

### 3.3 Approach B: Q7 Tiny-MLP (`24 → 16 → 11` ReLU)

Once per turn, `ga_select_action_q7()` populates `int8_t x[24]` in Q7 (`[-127, 127]`), then executes the two-layer network using bit-shifted biases (`<< 7`), `int32_t` MAC accumulators, ReLU activation, and power-of-2 right-shift rescaling (`>> GA_MLP_H_SHIFT`, where `GA_MLP_H_SHIFT = 9`):

```c
/* Layer 1: 24 -> 16 with ReLU and Q7 rescaling */
for (i = 0u; i < GA_MLP_HIDDEN; i++)
{
    int32_t acc = (int32_t)GA_MLP_B1[i] << 7;
    for (j = 0u; j < GA_MLP_IN; j++)
    {
        acc += (int32_t)GA_MLP_W1[i][j] * (int32_t)x[j];
    }
    if (acc <= 0) { h[i] = 0; }
    else
    {
        int32_t q = acc >> GA_MLP_H_SHIFT;
        h[i] = (q > 127) ? 127 : (int8_t)q;
    }
}

/* Layer 2: 16 -> 11 masked argmax */
for (a = 0u; a < GA_MLP_OUT; a++)
{
    if (!valid[a]) { continue; }
    int32_t acc = (int32_t)GA_MLP_B2[a] << 7;
    for (i = 0u; i < GA_MLP_HIDDEN; i++)
    {
        acc += (int32_t)GA_MLP_W2[a][i] * (int32_t)h[i];
    }
    if (acc > best_score) { best_score = acc; best_a = a; }
}
```

- **Overflow Safety:** Layer 1 worst-case accumulator magnitude is $127 \times 128 + 24 \times 127 \times 127 = 403,352 \ll 2^{31}-1$; Layer 2 is $127 \times 128 + 16 \times 127 \times 127 = 274,320 \ll 2^{31}-1$.
- **Compute Cost:** $16 \times 24 + 11 \times 16 = 560$ integer MACs per turn (fewer when actions are masked out).

---

## 4. Exact Post-Stage-4 Memory Footprint (`Debug/mspgame.map`)

All figures below are extracted directly from the MSP430 Linker (`PC v21.6.1`) output in `Debug/mspgame.map` after compiling Stage 4 with both models active:

| Memory Region | Origin | Length (Bytes) | Used (Bytes) | Unused (Bytes) | Utilization |
| :--- | :---: | ---: | ---: | ---: | ---: |
| **`RAM`** (Main SRAM) | `0x00001c00` | `4,096` (`0x1000`) | **`2,385`** (`0x0951`) | `1,711` (`0x06af`) | **58.23%** |
| **`LEARAM`** (LEA SRAM) | `0x00002c00` | `3,784` (`0x0ec8`) | **`0`** (`0x0000`) | `3,784` (`0x0ec8`) | **0.00%** |
| **`LEASTACK`** (LEA Stack) | `0x00003ac8` | `312` (`0x0138`) | **`0`** (`0x0000`) | `312` (`0x0138`) | **0.00%** |
| **`FRAM`** (Lower FRAM) | `0x00004000` | `49,024` (`0xbf80`) | **`15,684`** (`0x3d44`) | `33,340` (`0x823c`) | **31.99%** |
| **`FRAM2`** (Upper FRAM) | `0x00010000` | `212,984` (`0x33ff8`) | **`28,082`** (`0x6db2`) | `184,902` (`0x2d246`) | **13.19%** |
| **Combined `FRAM + FRAM2`** | — | **`262,008`** (`262.0 KB`) | **`43,766`** (`43.8 KB`) | **`218,242`** (`218.2 KB`) | **16.70%** |

### AI Subsystem Incremental Footprint Breakdown

- **FRAM Weights (`.const`):** `609` bytes (`22 B` Heuristic A + `587 B` Tiny-MLP B).
- **FRAM Inference Code (`.text:ga_select_action_q7`):** `2,864` bytes (`0x0b30` in `FRAM2`).
- **Static SRAM (`.bss`):** `32` bytes for the 16-entry recent-position ring buffer (`s_ai_hx[16]` at `0x22da`, `s_ai_hy[16]` at `0x22ea`) plus scalar mode flags (`s_ai_demo`, `s_ai_model`, `s_ai_hidx`).
- **Stack Scratchpad:** `22 B` (`f[22]`) for Approach A or `40 B` (`x[24]` + `h[16]`) for Approach B, plus `11 B` (`valid[11]`).
- **Dynamic Heap (`.sysmem`):** `0` bytes (`malloc` is never used).
- **Unused LEA Headroom:** All `4,096` bytes of `LEARAM + LEASTACK` remain completely untouched, leaving room for future hardware-accelerated vector MACs via the MSP430 Low-Energy Accelerator (LEA).

---

## 5. On-Chip `[4] AI DEMO` User Experience

Stage 4 exposes the embedded policies directly inside the game's Class Select screen without requiring a host PC:

1. **Launch:** At the Class Select menu, press **`4`** (`[4] AI DEMO (FRAM) Watch on-chip quantized Q7 GA play autonomously!`).
2. **Autonomous Execution Loop:** The main firmware loop invokes `ga_select_action_q7()` every **500 ms** (`do_probe` tick, `app/dungeon_game.c:3586–3591`) and feeds the returned keypress directly into `handle_command()`, rendering every turn live over the 80×24 ANSI terminal.
3. **Live Model Hot-Swap (`T`):** Pressing **`T`** during AI Demo toggles `s_ai_model` on the fly between:
   - `HEURISTIC A (22B Q7)` (default on entry)
   - `TINY-MLP B (587B Q7)`
4. **Exit / Restart (`N` / `4`):** Pressing **`N`** exits AI Demo and returns cleanly to the Class Select screen; if the AI character dies, pressing **`4`** or **`W`** starts a new autonomous demo run.
