# UART & ANSI/VT100 Terminal Specification

## Hardware UART Configuration (MSP-EXP430FR5994)

The MSP-EXP430FR5994 LaunchPad routes the eZ-FET backchannel USB-to-UART bridge to **eUSCI_A0**:

| Signal | MCU Pin | Peripheral | Notes |
| --- | --- | --- | --- |
| TXD (MCU -> PC) | `P2.0` | `UCA0TXD` | Requires eZ-FET TXD jumper installed |
| RXD (PC -> MCU) | `P2.1` | `UCA0RXD` | Requires eZ-FET RXD jumper installed |

### Serial Parameters

- **Format**: `8` data bits, `No` parity, `1` stop bit (`8N1`)
- **Flow control**: `None`
- **Local echo**: `Off` (firmware handles all screen updates)
- **Baud rate**:
  - `115200` bps (`UART_CLK_SMCLK`, 8 MHz DCO, `LPM0`) for both `dungeon_game` (differential 80×24 ANSI roguelike + 500 ms `TIMER_A1` DSR/AI tick) and `space_game` (real-time 50 Hz arcade shooter)
  - Optional `9600` bps (`UART_CLK_ACLK`, 32.768 kHz LFXT, `LPM3`) remains supported by the `drivers/msp430/uart` driver for ultra-low-power applications

## Terminal Compatibility Guidelines

- Target a minimum terminal viewport of **80x24** characters.
- Use standard **VT100 / ANSI CSI** sequences (`\x1b[...`):
  - Cursor home / positioning: `\x1b[H`, `\x1b[<row>;<col>H`
  - Clear screen: `\x1b[2J\x1b[H`
  - Hide / show cursor: `\x1b[?25l` / `\x1b[?25h`
  - Reset attributes: `\x1b[0m`
- Prefer cursor-home (`\x1b[H`) over-drawing or differential updates inside a single `uart_write_all()` frame buffer rather than full-screen clears (`\x1b[2J`) every frame to eliminate flicker.
