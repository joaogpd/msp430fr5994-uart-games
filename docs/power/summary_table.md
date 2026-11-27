# EnergyTrace Summary Table

| Profile | Variant | $T$ (s) | $N$ | $E_{\text{tot}}$ (mJ) | $P_{\text{mean}}$ (uW) | $I_{\text{ev}}$ (uA) | $I_{\text{tw}}$ (uA) | $I_{p50}$ (uA) | $I_{p95}$ (uA) | $V$–$I$ Diff (%) | CR2032 (days) | C1 Cap (min) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Idle (Connected) | baseline | 30.00 | 115246 | 19.023 | 634.11 | 192.78 | 192.20 | 189.90 | 193.40 | 0.30 | 43.37 | 22.12 |
| Idle (Connected) | optimized | 30.00 | 115484 | 6.839 | 227.96 | 69.29 | 68.66 | 69.20 | 69.60 | 0.90 | 120.63 | 61.52 |
| Active Play (1 key/s) | baseline | 30.00 | 114782 | 24.195 | 806.50 | 245.19 | 261.84 | 190.00 | 744.20 | 6.79 | 34.10 | 17.39 |
| Active Play (1 key/s) | optimized | 30.00 | 114814 | 13.630 | 454.33 | 138.11 | 197.06 | 66.60 | 641.60 | 42.67 | 60.53 | 30.87 |
| AI Demo: Heuristic A | baseline | 30.00 | 115260 | 19.041 | 634.70 | 192.96 | 192.34 | 190.00 | 193.50 | 0.32 | 43.33 | 22.10 |
| AI Demo: Heuristic A | optimized | 30.00 | 114099 | 24.467 | 815.58 | 247.93 | 368.96 | 343.20 | 642.40 | 48.80 | 33.72 | 17.20 |
| AI Demo: Tiny-MLP B | baseline | 30.00 | 114923 | 22.527 | 750.89 | 228.28 | 232.36 | 190.20 | 525.60 | 1.79 | 36.62 | 18.68 |
| AI Demo: Tiny-MLP B | optimized | 30.00 | 114346 | 21.135 | 704.51 | 214.16 | 327.57 | 192.40 | 650.40 | 52.95 | 39.03 | 19.91 |
| Low-HP Alert | baseline | 30.00 | 113401 | 192.720 | 6423.91 | 1953.00 | 1993.20 | 3345.20 | 4096.10 | 2.05 | 4.28 | 2.18 |
| Low-HP Alert | optimized | 30.00 | 115109 | 24.020 | 800.69 | 243.38 | 913.57 | 100.50 | 3260.00 | 275.35 | 34.35 | 17.52 |

## Baseline vs. Optimized Deltas

| Profile | $\Delta I_{\text{ev}}$ (uA) | $\Delta I_{\text{tw}}$ (uA) | $\Delta P_{\text{mean}}$ (uW) | Power Reduction (%) | Factor |
|---|---:|---:|---:|---:|---:|
| Idle (Connected) | 123.49 | 123.53 | 406.15 | 64.05% | 2.78x |
| Active Play (1 key/s) | 107.08 | 64.78 | 352.16 | 43.67% | 1.78x |
| AI Demo: Heuristic A | -54.98 | -176.62 | -180.88 | -28.50% | 0.78x |
| AI Demo: Tiny-MLP B | 14.12 | -95.21 | 46.38 | 6.18% | 1.07x |
| Low-HP Alert | 1709.62 | 1079.63 | 5623.23 | 87.54% | 8.02x |

> **Note ($I_{\text{ev}}$ vs. $I_{\text{tw}}$):** $I_{\text{ev}} = E_{\text{tot}} / (T \cdot V_{\text{mean}})$ is derived directly from CCS EnergyTrace's hardware-accumulated `Energy (uJ)` counter (exact mean current), whereas $I_{\text{tw}}$ is the time-weighted integral of `Current (nA)`, which reflects CCS's `EnergyTraceRunningAverageFilter` (~100–175 ms sample-hold tail after brief active pulses).
