# CCS 12.5 EnergyTrace Capture Protocol (`mspgame`)

This runbook defines the mandatory hardware setup, TI Code Composer Studio (CCS) 12.5 EnergyTrace configuration, host workload synchronization (`tools/power_bench.py`), and CSV export steps for reproducible, apples-to-apples **Baseline (`5cf488d`) vs. Optimized** power measurements on the **TI MSP-EXP430FR5994 LaunchPad**.

---

## 1. Pre-Flight Hardware Checklist

Before powering or capturing data on the MSP-EXP430FR5994 LaunchPad, verify every item below:

1. **Direct PC USB Connection**
   - Plug the LaunchPad micro-USB cable **directly** into an active PC USB port.
   - Do **not** use unpowered/bus-powered USB hubs or long extension cables (EnergyTrace DC-DC calibration is sensitive to USB VBUS droop).
2. **Isolation Header `J101` Jumpers**
   - **Installed (`ON`)**: `3V3`, `TXD`, `RXD`, `SBWTDIO`, `SBWTCK` (and `GND` if present).
     - `3V3` **must** remain fitted: EnergyTrace measures current supplied through the eZ-FET `3V3` rail (external supply yields `0 uA` / no data).
     - `TXD` and `RXD` **must** remain fitted for connected-terminal captures (`COM4 @ 115200` 8N1).
   - **Optional / Uninstalled**: `RTS`, `CTS` (unused; UART is `8N1` with no hardware flow control).
3. **Supercapacitor Jumper `J8 = OFF`**
   - Set jumper **`J8`** to **`OFF`** (open/disconnected) to isolate the **`0.22 F` supercapacitor `C1`** from `VCC`.
   - *(Hardware note: **`C1`** is the `0.22 F` supercapacitor controlled by `J8`; **`C48`** on the board is a `0.22 uF` decoupling capacitor. Leaving `J8` in `Charge` or `Use` adds capacitor leakage and charge-pump transients that corrupt DC-DC pulse measurements.)*
4. **Empty microSD Slot & No BoosterPacks**
   - Confirm the microSD card socket is **empty** (an inserted SD card draws ~100+ uA even at idle).
   - Remove all BoosterPacks, breadboard jumpers, and external logic probes.
5. **Serial Port Exclusivity**
   - Close any open serial terminal (`PuTTY`, `play.bat`, CCS Terminal, etc.) so `tools/power_bench.py` has exclusive access to **`COM4`**. (CCS EnergyTrace communicates via `COM5` / eZ-FET Debug Interface without conflicting with `COM4`.)

---

## 2. Mandatory CCS 12.5 Configuration

> **CRITICAL — Baseline Preservation (`5cf488d`)**
> Before capturing **Baseline** data, do **not** rebuild or overwrite `Debug/mspgame.out` (`387,492` B from commit `5cf488d`). Either use the already-flashed Baseline firmware on the LaunchPad or flash the unmodified `5cf488d` image first, and complete **all** Baseline captures before flashing the Optimized build.

1. **Enable EnergyTrace (Energy-Only Mode)**
   - Open `Window -> Preferences -> Code Composer Studio -> Advanced Tools -> EnergyTrace™ Technology`.
   - Check **Enable EnergyTrace™ Technology**.
   - Under collection mode, select **`EnergyTrace`** (energy-only).
   - **FORBIDDEN:** Do **NOT** select `EnergyTrace+[CPU State]+[Peripheral States]` (**EnergyTrace++**). EnergyTrace++ continuously polls JTAG/SBW, injecting **~150 uA** of debug overhead into LPM measurements and suppressing absolute current scaling.
2. **Set Capture Period to `30 sec`**
   - Open the EnergyTrace **`Profile`** view (`View -> EnergyTrace Profile`).
   - Verify the toolbar mode icon indicates **EnergyTrace** (energy-only, no `++` / state trace).
   - Set the Capture Period dropdown on the EnergyTrace control bar to **`30 sec`**.
3. **Run Target in `Free Run` Mode (NEVER `Resume`)**
   - After flashing or connecting to the target in CCS Debug, start execution using **`Run -> Free Run`**.
   - **FORBIDDEN:** Do **NOT** use Debugger **`Resume`** (`F8`), single-step, or leave active hardware breakpoints enabled. Leaving the debugger attached in `Resume` mode keeps JTAG polling active and adds **~150 uA** overhead, invalidating `LPM3`/`LPM0` idle comparisons.

---

## 3. Synchronized Capture Workflow (`tools/power_bench.py` + CCS GUI)

Run each profile individually so CCS captures a clean 30-second window per workload.

### 3.1 Core 5 Profiles

Run the matching command in PowerShell from the repository root (`c:\Users\joaog\workspace_v12\mspgame`):

| Capture | `power_bench.py` Command | Firmware Pre-State Staged Before Prompt |
|---|---|---|
| **1. Idle (Connected)** | `python tools/power_bench.py --port COM4 --profile idle --duration 30 --wait-start --countdown 3` | Class Select (`NEW ADVENTURE`), terminal connected (`0x0C` + DSR responder), 0 gameplay keys |
| **2. Active Play (1 key/s)** | `python tools/power_bench.py --port COM4 --profile play --duration 30 --wait-start --countdown 3` | Knight run started (`'1'`), then deterministic `['w','d','s','a',' ']` at `1.0 Hz` during 30 s window |
| **3a. AI Demo: Heuristic A** | `python tools/power_bench.py --port COM4 --profile ai-heuristic --duration 30 --wait-start --countdown 3` | On-chip `Heuristic A` (22 B Q7) active (`'4'`), auto-restarting if `AI DEMO ENDED` occurs |
| **3b. AI Demo: Tiny-MLP B** | `python tools/power_bench.py --port COM4 --profile ai-mlp --duration 30 --wait-start --countdown 3` | On-chip `Tiny-MLP B` (587 B Q7) active (`'4'` + `'t'`), auto-restarting (`'4'` then `'t'`) if needed |
| **4. Low-HP Alert** | `python tools/power_bench.py --port COM4 --profile low-hp --duration 30 --wait-start --countdown 3` | Deterministic BFS seeks damage until `LOW HP` + `BEL` stability (2.5 s) verified, then 0 gameplay keys (DSR responder active) |

> *(Tip: Because CCS stops automatically after `30 sec`, you may optionally pass `--duration 32` on `power_bench.py` if you want a 2-second tail buffer so the entire 30 s CCS window stays strictly inside steady-state execution.)*

### 3.2 Step-by-Step Synchronization Per Run

1. Verify CCS shows the target running in **`Run -> Free Run`** and any previous EnergyTrace capture is stopped/cleared.
2. Execute the `tools/power_bench.py` command for the desired profile. Wait until it finishes staging the firmware (for `low-hp`, this takes a few seconds while the BFS hazard-seeker drops HP to `<= 4`) and prints:
   ```text
   [SYNC] Profile '<profile>' is staged in state '<state>'.
          Verify CCS is in Free Run & EnergyTrace (energy-only), then press ENTER to begin countdown...
   ```
3. Position your mouse over the EnergyTrace **Start / Enable** button in CCS.
4. Press **`ENTER`** in the PowerShell terminal (`3... 2... 1... GO!`).
5. Immediately click **Start / Enable** in CCS EnergyTrace on **`GO!`** (or at `1...` for stationary profiles if using `--duration 32`).
6. Wait for the `30 sec` CCS EnergyTrace progress bar to complete and `power_bench.py` to print its JSON summary and return the board to Class Select.

### 3.3 Optional Profile: Disconnected Standby (`*_disconnected.csv`)

If capturing optional disconnected standby:
1. Ensure **no** host process (`power_bench.py`, PuTTY, etc.) has `COM4` open.
2. Remove the **`TXD` and `RXD`** jumpers on `J101` (keep **`3V3`**, `SBWTDIO`, `SBWTCK` fitted). *(Note: `P2.1`/`UCA0RXD` has an internal pull-up configured in `board.c` so the floating RX pin does not glitch.)*
3. Power-cycle or reset the board, enter **`Run -> Free Run`**, wait ~3 s for the initial DSR probes to time out (`s_terminal_connected = false`), and capture a `30 sec` EnergyTrace window.
4. Re-install `TXD` and `RXD` on `J101` before running any connected profile.

---

## 4. Exporting Raw Data CSVs (Exact Filenames)

After each `30 sec` capture finishes in CCS:

1. In the CCS **EnergyTrace Profile** window, click **Save / Export Raw Data to CSV** (exporting time-series samples, **NOT** the Profile/Function summary).
   - **REQUIRED 4-column Raw Data header:**
     ```text
     Time (ns),Current (nA),Voltage (mV),Energy (uJ)
     ```
   - **REJECTED Summary header (DO NOT SAVE THIS):**
     ```text
     Item,Runtime (nS),Energy (nJ)
     ```
     *(That 3-column file is the aggregated Profile summary and lacks time-series waveforms needed by `tools/power_plots.py`.)*

2. Save every capture into `docs/power/data/` using these exact filenames:

| Profile | Baseline (`5cf488d`) Filename | Optimized Filename |
|---|---|---|
| Idle (Connected) | `docs/power/data/baseline_idle.csv` | `docs/power/data/optimized_idle.csv` |
| Active Play (`1 key/s`) | `docs/power/data/baseline_play.csv` | `docs/power/data/optimized_play.csv` |
| AI Demo: Heuristic A | `docs/power/data/baseline_ai_heuristic.csv` | `docs/power/data/optimized_ai_heuristic.csv` |
| AI Demo: Tiny-MLP B | `docs/power/data/baseline_ai_mlp.csv` | `docs/power/data/optimized_ai_mlp.csv` |
| Low-HP Alert | `docs/power/data/baseline_low_hp.csv` | `docs/power/data/optimized_low_hp.csv` |
| *(Optional)* Disconnected | `docs/power/data/baseline_disconnected.csv` | `docs/power/data/optimized_disconnected.csv` |

---

## 5. Immediate Post-Export Sanity Checks

Immediately after exporting each CSV (before moving to the next run), verify:

1. **Row Count & Header**:
   - The file has `> 100` rows.
   - Line 1 begins with `Time (` (e.g., `Time (ns),Current (nA),Voltage (mV),Energy (uJ)`).
   - There is **no `DSR` column** (if `DSR` appears, EnergyTrace++ was accidentally enabled — re-enable energy-only `EnergyTrace` and re-capture).
2. **Duration & Non-Zero Current**:
   - Final timestamp minus first timestamp is ~`30 s` (`~30,000,000,000 ns`).
   - Current values are non-zero (if `0 nA` throughout, `J101` `3V3` was disconnected or the board was externally powered).
   - Baseline connected `idle` should show ~`250–350 uA` average (`LPM0` + 8 MHz SMCLK + 2 Hz `TIMER_A1`), **not** `~400–500 uA` (`+150 uA` `Resume`/EnergyTrace++ artifact).
3. **Automated Ingestion Check**:
   Run `tools/power_plots.py` in partial mode at any time to validate exported CSVs and inspect time-weighted statistics:
   ```powershell
   python tools/power_plots.py --allow-partial
   ```
