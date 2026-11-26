# MSP430FR5994 UART Terminal Games (`mspgame`)

Games developed for the **TI MSP430FR5994** microcontroller (MSP-EXP430FR5994 LaunchPad) that run directly inside any standard **ANSI/VT100 serial terminal** over UART—requiring no custom PC-side software.

## Architecture

- **Microcontroller**: TI MSP430FR5994 (16 MHz max FRAM MCU, 256 KB FRAM, 8 KB SRAM)
- **Communication**: LaunchPad backchannel UART via `eUSCI_A0` (`P2.0` TX / `P2.1` RX)
- **Display & Input**: Rendered entirely by the firmware using ANSI/VT100 escape sequences and keyboard/on-board button (`S1`/`S2`) input.
- **Persistence**: Power-loss-safe two-slot FRAM checkpointing (`drivers/msp430/ckpt.*` with CRC-16).
- **Toolchain**: TI Code Composer Studio 12.5 (`TI v21.6.1.LTS`)

## Repository Structure

```text
mspgame/
├── app/
│   ├── dungeon_game.c/.h       # Turn-based ANSI dungeon crawler (9600 baud ACLK / LPM3)
│   └── space_game.c/.h         # Real-time ANSI space shooter (115200 baud SMCLK / LPM0)
├── drivers/
│   ├── board/                  # MSP-EXP430FR5994 LaunchPad pin map & bring-up
│   └── msp430/                 # Register-level MSP430 peripheral & FRAM checkpoint drivers
├── docs/
│   └── uart-terminal-spec.md   # UART config & ANSI/VT100 terminal conventions
├── targetConfigs/              # CCS target configuration (MSP430FR5994.ccxml)
├── lnk_msp430fr5994.cmd        # Linker command file
├── main.c                      # Active entry point (currently dungeon_game)
├── main.c.space_game           # Alternate entry point for space_game
├── .ccsproject / .cproject / .project
└── README.md
```

## Quick Start

1. Open **TI Code Composer Studio** in `C:\Users\joaog\workspace_v12` with the `mspgame` project.
2. Connect the **MSP-EXP430FR5994 LaunchPad** via USB (ensure the eZ-FET `TXD`/`RXD` jumpers are installed).
3. Build and flash `mspgame`.
4. Open any ANSI/VT100-capable serial terminal (PuTTY, Tera Term, `screen`, `picocom`, or CCS Terminal) on the **MSP Application UART1** COM port:
   - `dungeon_game` (`main.c`): **`9600` baud**, `8N1`, flow control `None`, local echo `Off`
   - `space_game` (`main.c.space_game`): **`115200` baud**, `8N1`, flow control `None`, local echo `Off`
