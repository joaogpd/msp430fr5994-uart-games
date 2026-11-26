# `ga/` — Genetic Algorithm Training, Benchmarking & HIL Validation

Headless evolutionary training, Q7 `int8_t` fixed-point quantization, and Hardware-in-the-Loop (HIL) UART validation suite for the TI MSP430FR5994 dungeon crawler (`app/dungeon_game.c`).

For full mathematical details, feature definitions, genome topologies, and MSP430FR5994 FRAM quantization formulas, see [`docs/ga/ARCHITECTURE.md`](../docs/ga/ARCHITECTURE.md).

---

## Prerequisites

- **Python 3.10+**
- Required packages:
  - `numpy` (GA operations, feature extraction, Q7 quantization)
  - `matplotlib` (figure generation via `ga.plots`)
  - `pyserial` (live UART HIL evaluation via `ga.hil_eval`)

```powershell
pip install numpy matplotlib pyserial
```

All commands below assume the repository root (`mspgame/`) as the current working directory.

---

## Module Overview

| Module | Purpose |
| :--- | :--- |
| `ga/sim.py` | Deterministic headless Python simulator mirroring `app/dungeon_game.c` (`0xB400` LFSR, `48x22` maps, Bresenham LOS, 6 enemy types, 3 classes) |
| `ga/test_sim.py` | Parity, determinism, class-stat, boss-sanctum, and throughput unit tests |
| `ga/features.py` | Single-pass integer-friendly feature extractors for Approach A (22 action features) and Approach B (24 state features), under `fair` and `omniscient` modes |
| `ga/genome.py` | `HeuristicGenome` (22 params), `TinyMLPGenome` (`24->16->11` = 587 params), Q7 `int8_t` quantizers, and `export_c_header()` for `app/ga_weights.h` |
| `ga/evolve.py` | Multiprocessing-safe GA trainer with 10-seed held-out Float32 & Int8 evaluation |
| `ga/run_experiments.py` | Reproducible 12-experiment benchmark suite runner + automatic `app/ga_weights.h` export |
| `ga/plots.py` | Generates publication figures in `docs/ga/figures/` from `ga/results/benchmark_summary.json` |
| `ga/hil_eval.py` | Hardware-in-the-Loop evaluator driving a live MSP430FR5994 LaunchPad over UART (`COM4` @ `115200` baud) |

---

## Quickstart Commands

### 1. Run Simulator Parity & Throughput Tests

Verifies the `0xB400` Galois LFSR sequence, deterministic replay, starting stats for all 3 classes, stairs reachability across floors 1–4, Floor-5 Bone Warden sanctum layout, and simulation throughput (`> 2,000 turns/sec`):

```powershell
python -m unittest -v ga.test_sim
```

### 2. Train a Single Evolutionary Run

Run `ga.evolve` for either Approach A (`--model heuristic`) or Approach B (`--model mlp`), under Fair Fog-of-War (`--obs fair`) or Omniscient (`--obs omniscient`), on any playable class (`knight`, `arcanist`, `rogue`):

```powershell
# Quick smoke test (Heuristic A, Fair Fog-of-War, Knight)
python -m ga.evolve --model heuristic --obs fair --cls knight --tier smoke

# Medium tier Tiny-MLP B on Arcanist with explicit seed and worker count
python -m ga.evolve --model mlp --obs fair --cls arcanist --tier medium --seed 42 --workers 8

# Custom budget override
python -m ga.evolve --model heuristic --obs fair --cls rogue --tier deep --pop 72 --gens 120 --out ga/results/custom_rogue
```

**Tier Presets (`TIER_CONFIGS` in `ga/evolve.py`):**

| Tier | Population (`--pop`) | Generations (`--gens`) | Training Seeds (`--seeds`) |
| :--- | :---: | :---: | :---: |
| `smoke` | 24 | 25 | 4 |
| `medium` | 48 | 70 | 5 |
| `deep` | 80 | 160 | 6 |

**CLI Flags (`python -m ga.evolve --help`):**

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--model` | `heuristic` | Genome type: `heuristic` (Approach A, 22 params) or `mlp` (Approach B, 587 params) |
| `--obs` | `fair` | Observability regime: `fair` (Fog-of-War) or `omniscient` |
| `--cls` | `knight` | Playable class: `knight`, `arcanist`, or `rogue` |
| `--tier` | `smoke` | Preset budget: `smoke`, `medium`, or `deep` |
| `--pop` | *from tier* | Override population size |
| `--gens` | *from tier* | Override generation count |
| `--seeds` | *from tier* | Override number of training dungeon seeds |
| `--workers` | `min(8, cpu_count)` | Multiprocessing worker count |
| `--seed` | `42` | GA RNG seed (training seeds are derived deterministically via `seed + 10007`, disjoint from the 10 held-out seeds) |
| `--out` | `ga/results/<run_id>` | Output directory for `metrics.csv`, `best_genome.json`, and `summary.json` |
| `--max-turns` | `350` | Maximum turns per evaluation episode |
| `--stall-turns` | `140` | Per-floor turn cap before early episode termination |
| `--quiet` | `False` | Suppress per-generation progress output |

Each run evaluates the best genome (both Float32 and Q7 Int8) on 10 fixed held-out seeds (`0x1357`, `0x2468`, `0x3579`, `0x468A`, `0x579B`, `0x68AC`, `0x79BD`, `0x8ACE`, `0x9BDF`, `0xACE1`) and writes:
- `metrics.csv` — per-generation `best_fit`, `mean_fit`, `min_fit`, `best_floor`, `mean_floor`, `best_kills`, `elapsed_s`
- `best_genome.json` — champion weights, Q7 quantized representation, and run metadata
- `summary.json` — configuration, training stats, held-out Float32/Int8 metrics, `quantization_retention`, and wall-clock time

### 3. Run the Full 12-Experiment Benchmark Suite

Executes all 12 benchmark configurations sequentially (using `min(10, cpu_count)` workers per run) and automatically exports the quantized `int8_t` weights of `heur_fair_knight_deep` and `mlp_fair_knight_deep` to `app/ga_weights.h`:

```powershell
python -m ga.run_experiments
```

**Experiment Matrix (`EXPERIMENTS` in `ga/run_experiments.py`):**

1. **Progressive Budgets (Knight, Fair):** `smoke`, `medium`, and `deep` (overridden to `pop=72, gens=120`) for both `heuristic` and `mlp` (6 runs).
2. **Observability Ablation (Knight, Medium):** `omniscient` for both `heuristic` and `mlp` (2 runs).
3. **Class Specialists (Fair, Medium):** `arcanist` and `rogue` for both `heuristic` and `mlp` (4 runs).

Each experiment `idx` (`1..12`) uses deterministic GA seed `1000 + idx * 17`. Outputs are written to `ga/results/<exp_id>/`, aggregated in `ga/results/benchmark_summary.json`, and exported to `app/ga_weights.h`.

### 4. Generate Benchmark Figures

Reads `ga/results/benchmark_summary.json` and per-run `metrics.csv` files to generate PNG figures in `docs/ga/figures/`:

```powershell
python -m ga.plots
```

Generated artifacts:
- `docs/ga/figures/fitness_curves.png` — Evolutionary convergence (`heur_fair_knight_deep`, `mlp_fair_knight_deep`, and `heur_omni_knight_medium`)
- `docs/ga/figures/heldout_comparison.png` — Float32 vs Q7 Int8 held-out mean fitness across all 12 runs

### 5. Hardware-in-the-Loop (HIL) Validation on MSP430FR5994 (`COM4`)

Connects to a flashed TI MSP430FR5994 LaunchPad over backchannel UART (`COM4` @ `115200` baud), parses the live `80x24` ANSI/VT100 viewport and HUD, responds to terminal DSR probes (`ESC[5n` -> `ESC[0n`), and sends tactical keypresses guided by a saved `best_genome.json`:

```powershell
python -m ga.hil_eval `
  --port COM4 `
  --baud 115200 `
  --genome ga/results/heur_fair_knight_deep/best_genome.json `
  --turns 80 `
  --delay-ms 120 `
  --out ga/results/hil_knight_run.json
```

**CLI Flags (`python -m ga.hil_eval --help`):**

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--port` | `COM4` | Serial port connected to the MSP430FR5994 LaunchPad |
| `--baud` | `115200` | UART baud rate |
| `--genome` | *(required)* | Path to a saved `best_genome.json` |
| `--turns` | `80` | Number of live turns to execute |
| `--delay-ms` | `120` | Inter-turn drain/settle delay in milliseconds |
| `--out` | `None` | Optional path to save JSON telemetry (`max_floor`, `max_gold`, `deaths`, `bel_count`, `dsr_count`, final banner/status) |
