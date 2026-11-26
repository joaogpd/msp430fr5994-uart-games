"""
Reproducible experiment suite runner for MSP430FR5994 Genetic Algorithm benchmarks.

Runs:
  1. Progressive budget comparison (Smoke -> Medium -> Deep) on Knight (Fair) for Heuristic A & Tiny-MLP B
  2. Observability ablation (Fair Fog-of-War vs Omniscient) on Knight (Medium) for A & B
  3. Class specialists (Arcanist & Rogue, Fair, Medium) for A & B

Exports:
  - ga/results/<exp_id>/{metrics.csv, best_genome.json, summary.json}
  - ga/results/benchmark_summary.json
  - app/ga_weights.h (quantized int8_t weights for best Fair Heuristic A + best Fair Tiny-MLP B)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ga.genome import HeuristicGenome, TinyMLPGenome, export_c_header

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "ga" / "results"

EXPERIMENTS = [
    # 1. Progressive Tiers (Knight, Fair Fog-of-War)
    ("heur_fair_knight_smoke",  "heuristic", "fair",       "knight",   "smoke",  None, None),
    ("mlp_fair_knight_smoke",   "mlp",       "fair",       "knight",   "smoke",  None, None),
    ("heur_fair_knight_medium", "heuristic", "fair",       "knight",   "medium", None, None),
    ("mlp_fair_knight_medium",  "mlp",       "fair",       "knight",   "medium", None, None),
    ("heur_fair_knight_deep",   "heuristic", "fair",       "knight",   "deep",   72,   120),
    ("mlp_fair_knight_deep",    "mlp",       "fair",       "knight",   "deep",   72,   120),
    # 2. Observability Ablation (Knight, Omniscient, Medium)
    ("heur_omni_knight_medium", "heuristic", "omniscient", "knight",   "medium", None, None),
    ("mlp_omni_knight_medium",  "mlp",       "omniscient", "knight",   "medium", None, None),
    # 3. Class Specialists (Arcanist & Rogue, Fair, Medium)
    ("heur_fair_arcanist_med",  "heuristic", "fair",       "arcanist", "medium", None, None),
    ("mlp_fair_arcanist_med",   "mlp",       "fair",       "arcanist", "medium", None, None),
    ("heur_fair_rogue_med",     "heuristic", "fair",       "rogue",    "medium", None, None),
    ("mlp_fair_rogue_med",      "mlp",       "fair",       "rogue",    "medium", None, None),
]


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    workers = min(10, os.cpu_count() or 4)
    summaries = []
    t_all = time.perf_counter()

    for idx, (exp_id, model, obs, cls, tier, pop_ov, gens_ov) in enumerate(EXPERIMENTS, 1):
        out_dir = RESULTS_DIR / exp_id
        cmd = [
            sys.executable, "-m", "ga.evolve",
            "--model", model,
            "--obs", obs,
            "--cls", cls,
            "--tier", tier,
            "--workers", str(workers),
            "--seed", str(1000 + idx * 17),
            "--out", str(out_dir),
        ]
        if pop_ov is not None:
            cmd += ["--pop", str(pop_ov)]
        if gens_ov is not None:
            cmd += ["--gens", str(gens_ov)]

        print(f"\n[{idx}/{len(EXPERIMENTS)}] Running {exp_id} ({model}, {obs}, {cls}, {tier})...", flush=True)
        t0 = time.perf_counter()
        subprocess.run(cmd, cwd=str(ROOT), check=True)
        dt = time.perf_counter() - t0
        summary = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
        summary["exp_id"] = exp_id
        summary["wall_s"] = round(dt, 2)
        summaries.append(summary)
        hf = summary["held_out_float"]
        hq = summary["held_out_int8"]
        print(
            f"  -> DONE in {dt:.1f}s | Held-out Float: fit={hf['mean_fitness']:.1f} fl={hf['mean_floor']:.2f} "
            f"kills={hf['mean_kills']:.1f} | Int8: fit={hq['mean_fitness']:.1f} fl={hq['mean_floor']:.2f}",
            flush=True,
        )

    bench_path = RESULTS_DIR / "benchmark_summary.json"
    bench_path.write_text(json.dumps(summaries, indent=2), encoding="utf-8")

    # Export best Fair Knight Heuristic A + best Fair Knight Tiny-MLP B to app/ga_weights.h
    best_heur_dir = RESULTS_DIR / "heur_fair_knight_deep"
    best_mlp_dir = RESULTS_DIR / "mlp_fair_knight_deep"
    h_genome = HeuristicGenome.from_dict(json.loads((best_heur_dir / "best_genome.json").read_text(encoding="utf-8")))
    m_genome = TinyMLPGenome.from_dict(json.loads((best_mlp_dir / "best_genome.json").read_text(encoding="utf-8")))
    c_hdr = ROOT / "app" / "ga_weights.h"
    export_c_header(h_genome, m_genome, str(c_hdr))
    print(f"\nExported quantized int8 weights to {c_hdr}")
    print(f"Total benchmark suite wall time: {time.perf_counter() - t_all:.1f}s")


if __name__ == "__main__":
    main()
