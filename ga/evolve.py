"""
Multiprocessing-safe Genetic Algorithm trainer for mspgame.

Supports:
- Approach A (--model heuristic) and Approach B (--model mlp)
- Fair Fog-of-War (--obs fair) and Omniscient (--obs omniscient)
- All 3 classes (--cls knight|arcanist|rogue)
- Tiers: smoke (pop=24, gens=25, seeds=4), medium (48, 70, 5), deep (80, 160, 6)
- Post-training evaluation of champion (float AND quantized int8_t) on 10 fixed
  held-out seeds, writing metrics.csv, best_genome.json, and summary.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from ga.genome import Genome, HeuristicGenome, TinyMLPGenome
from ga.sim import CLASS_ARCANIST, CLASS_KNIGHT, CLASS_ROGUE, DungeonSim

HELD_OUT_SEEDS: Tuple[int, ...] = (
    0x1357,
    0x2468,
    0x3579,
    0x468A,
    0x579B,
    0x68AC,
    0x79BD,
    0x8ACE,
    0x9BDF,
    0xACE1,
)

TIER_CONFIGS: Dict[str, Dict[str, int]] = {
    "smoke": {"pop": 24, "gens": 25, "seeds": 4},
    "medium": {"pop": 48, "gens": 70, "seeds": 5},
    "deep": {"pop": 80, "gens": 160, "seeds": 6},
}

CLASS_MAP: Dict[str, int] = {
    "knight": CLASS_KNIGHT,
    "arcanist": CLASS_ARCANIST,
    "rogue": CLASS_ROGUE,
}


def generate_train_seeds(ga_seed: int, count: int) -> Tuple[int, ...]:
    """Generate deterministic 16-bit dungeon seeds disjoint from HELD_OUT_SEEDS."""
    rng = np.random.default_rng(ga_seed + 10007)
    held_set = set(HELD_OUT_SEEDS)
    seeds: List[int] = []
    while len(seeds) < count:
        cand = int(rng.integers(1, 0xFFFF))
        if cand not in held_set and cand not in seeds:
            seeds.append(cand)
    return tuple(seeds)


def run_episode(
    policy: Any,
    seed: int,
    cls_id: int,
    omniscient: bool,
    max_turns: int = 350,
    stall_turns: int = 140,
) -> Dict[str, float]:
    """Run one deterministic episode and return telemetry metrics."""
    sim = DungeonSim(seed=seed, cls=cls_id)
    for _ in range(max_turns):
        if sim.game_over or sim.floor_turns >= stall_turns:
            break
        action = policy.select_action(sim, omniscient=omniscient)
        sim.step(action)

    return {
        "fitness": float(sim.fitness()),
        "best_floor": float(sim.best_floor),
        "kills": float(sim.kills),
        "boss_kills": float(sim.boss_kills),
        "gold": float(sim.gold),
        "explored": float(sim.total_explored),
        "turns": float(sim.turns),
        "wall_bumps": float(sim.wall_bumps),
    }


def evaluate_policy(
    policy: Any,
    seeds: Sequence[int],
    cls_id: int,
    omniscient: bool,
    max_turns: int = 350,
    stall_turns: int = 140,
) -> Dict[str, float]:
    """Evaluate a policy across multiple dungeon seeds and aggregate metrics."""
    episodes = [
        run_episode(
            policy,
            seed=s,
            cls_id=cls_id,
            omniscient=omniscient,
            max_turns=max_turns,
            stall_turns=stall_turns,
        )
        for s in seeds
    ]
    fits = [ep["fitness"] for ep in episodes]
    floors = [ep["best_floor"] for ep in episodes]
    kills = [ep["kills"] for ep in episodes]
    boss_kills = [ep["boss_kills"] for ep in episodes]
    golds = [ep["gold"] for ep in episodes]
    explored = [ep["explored"] for ep in episodes]
    turns = [ep["turns"] for ep in episodes]
    bumps = [ep["wall_bumps"] for ep in episodes]

    return {
        "mean_fit": float(np.mean(fits)),
        "max_fit": float(np.max(fits)),
        "min_fit": float(np.min(fits)),
        "std_fit": float(np.std(fits)),
        "mean_floor": float(np.mean(floors)),
        "max_floor": float(np.max(floors)),
        "mean_kills": float(np.mean(kills)),
        "max_kills": float(np.max(kills)),
        "total_boss_kills": float(np.sum(boss_kills)),
        "mean_gold": float(np.mean(golds)),
        "mean_explored": float(np.mean(explored)),
        "mean_turns": float(np.mean(turns)),
        "mean_wall_bumps": float(np.mean(bumps)),
    }


def _eval_genome_task(
    args: Tuple[Genome, Tuple[int, ...], int, bool, int, int],
) -> Dict[str, float]:
    """Top-level picklable worker function for Windows multiprocessing.Pool."""
    genome, seeds, cls_id, omniscient, max_turns, stall_turns = args
    return evaluate_policy(
        genome,
        seeds=seeds,
        cls_id=cls_id,
        omniscient=omniscient,
        max_turns=max_turns,
        stall_turns=stall_turns,
    )


def _tournament_select(
    population: List[Genome],
    fitnesses: List[float],
    rng: np.random.Generator,
    k: int = 3,
) -> Genome:
    n = len(population)
    k = min(k, n)
    idxs = rng.choice(n, size=k, replace=False)
    best_i = int(idxs[0])
    best_f = fitnesses[best_i]
    for i in idxs[1:]:
        ii = int(i)
        if fitnesses[ii] > best_f:
            best_f = fitnesses[ii]
            best_i = ii
    return population[best_i]


def evolve(
    model: str = "heuristic",
    obs: str = "fair",
    cls_name: str = "knight",
    tier: str = "smoke",
    pop_size: int | None = None,
    gens: int | None = None,
    num_seeds: int | None = None,
    workers: int | None = None,
    seed: int = 42,
    out_dir: str | Path | None = None,
    max_turns: int = 350,
    stall_turns: int = 140,
    quiet: bool = False,
) -> Dict[str, Any]:
    """Run GA evolution, benchmark float & int8 champion on held-out seeds, and save artifacts."""
    t_start = time.perf_counter()

    tier_cfg = TIER_CONFIGS[tier]
    pop_n = pop_size if pop_size is not None else tier_cfg["pop"]
    num_gens = gens if gens is not None else tier_cfg["gens"]
    seeds_n = num_seeds if num_seeds is not None else tier_cfg["seeds"]
    num_workers = workers if workers is not None else min(8, os.cpu_count() or 1)
    omniscient = obs == "omniscient"
    cls_id = CLASS_MAP[cls_name]

    run_id = f"{model}_{obs}_{cls_name}_{tier}_s{seed}"
    out_path = Path(out_dir) if out_dir is not None else Path("ga/results") / run_id
    out_path.mkdir(parents=True, exist_ok=True)

    train_seeds = generate_train_seeds(seed, seeds_n)
    rng = np.random.default_rng(seed)

    genome_cls = HeuristicGenome if model == "heuristic" else TinyMLPGenome
    population: List[Genome] = [genome_cls.random(rng) for _ in range(pop_n)]

    elitism = min(pop_n, 2 if pop_n <= 32 else 4)
    if model == "heuristic":
        mut_rate = 0.25
        sigma_start, sigma_end = 0.45, 0.10
    else:
        mut_rate = 0.08
        sigma_start, sigma_end = 0.35, 0.08

    metrics_rows: List[Dict[str, Any]] = []
    best_genome: Genome = population[0]
    best_train_stats: Dict[str, float] = {}
    best_overall_fit = -1e30

    if not quiet:
        print(
            f"[GA] run={run_id} pop={pop_n} gens={num_gens} seeds={seeds_n} "
            f"workers={num_workers} obs={obs} cls={cls_name}"
        )

    pool = mp.Pool(processes=num_workers) if num_workers > 1 else None
    try:
        for gen in range(1, num_gens + 1):
            tasks = [
                (g, train_seeds, cls_id, omniscient, max_turns, stall_turns)
                for g in population
            ]
            if pool is not None:
                results = pool.map(_eval_genome_task, tasks)
            else:
                results = [_eval_genome_task(t) for t in tasks]

            fits = [r["mean_fit"] for r in results]
            order = sorted(range(pop_n), key=lambda i: fits[i], reverse=True)

            gen_best_idx = order[0]
            gen_best_res = results[gen_best_idx]
            gen_best_fit = fits[gen_best_idx]
            gen_mean_fit = float(np.mean(fits))
            gen_min_fit = float(np.min(fits))
            gen_best_floor = float(max(r["max_floor"] for r in results))
            gen_mean_floor = float(np.mean([r["mean_floor"] for r in results]))
            gen_best_kills = float(max(r["max_kills"] for r in results))
            elapsed_s = time.perf_counter() - t_start

            if gen_best_fit > best_overall_fit:
                best_overall_fit = gen_best_fit
                best_genome = population[gen_best_idx]
                best_train_stats = dict(gen_best_res)

            row = {
                "gen": gen,
                "best_fit": round(gen_best_fit, 3),
                "mean_fit": round(gen_mean_fit, 3),
                "min_fit": round(gen_min_fit, 3),
                "best_floor": round(gen_best_floor, 2),
                "mean_floor": round(gen_mean_floor, 3),
                "best_kills": round(gen_best_kills, 2),
                "elapsed_s": round(elapsed_s, 3),
            }
            metrics_rows.append(row)

            if not quiet and (gen == 1 or gen == num_gens or gen % max(1, num_gens // 10) == 0):
                print(
                    f"  gen {gen:3d}/{num_gens} | best={gen_best_fit:8.1f} "
                    f"mean={gen_mean_fit:8.1f} floor={gen_best_res['mean_floor']:.2f} "
                    f"(max {gen_best_floor:.0f}) kills={gen_best_res['mean_kills']:.1f} "
                    f"({elapsed_s:.1f}s)"
                )

            if gen < num_gens:
                progress = (gen - 1) / max(1, num_gens - 1)
                sigma = sigma_start + (sigma_end - sigma_start) * progress

                next_pop: List[Genome] = [population[i] for i in order[:elitism]]
                while len(next_pop) < pop_n:
                    p1 = _tournament_select(population, fits, rng, k=3)
                    if rng.random() < 0.80:
                        p2 = _tournament_select(population, fits, rng, k=3)
                        child = p1.crossover(p2, rng)  # type: ignore[arg-type]
                    else:
                        child = p1
                    child = child.mutate(rng, rate=mut_rate, sigma=sigma)
                    next_pop.append(child)
                population = next_pop
    finally:
        if pool is not None:
            pool.close()
            pool.join()

    # Evaluate champion (both float and quantized int8_t) on 10 fixed held-out seeds
    held_out_float = evaluate_policy(
        best_genome,
        seeds=HELD_OUT_SEEDS,
        cls_id=cls_id,
        omniscient=omniscient,
        max_turns=max_turns,
        stall_turns=stall_turns,
    )
    quantized_champion = best_genome.quantize_int8()
    held_out_int8 = evaluate_policy(
        quantized_champion,
        seeds=HELD_OUT_SEEDS,
        cls_id=cls_id,
        omniscient=omniscient,
        max_turns=max_turns,
        stall_turns=stall_turns,
    )

    wall_clock_s = time.perf_counter() - t_start

    # Save metrics.csv
    csv_path = out_path / "metrics.csv"
    fieldnames = [
        "gen",
        "best_fit",
        "mean_fit",
        "min_fit",
        "best_floor",
        "mean_floor",
        "best_kills",
        "elapsed_s",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(metrics_rows)

    # Save best_genome.json
    genome_payload = best_genome.to_dict()
    genome_payload["meta"] = {
        "run_id": run_id,
        "model": model,
        "obs": obs,
        "cls": cls_name,
        "tier": tier,
        "seed": seed,
    }
    (out_path / "best_genome.json").write_text(
        json.dumps(genome_payload, indent=2), encoding="utf-8"
    )

    # Save summary.json
    retention = (
        (held_out_int8["mean_fit"] / held_out_float["mean_fit"])
        if abs(held_out_float["mean_fit"]) > 1e-6
        else 1.0
    )
    summary: Dict[str, Any] = {
        "config": {
            "run_id": run_id,
            "model": model,
            "obs": obs,
            "cls": cls_name,
            "tier": tier,
            "pop": pop_n,
            "gens": num_gens,
            "seeds": seeds_n,
            "train_seeds": list(train_seeds),
            "held_out_seeds": list(HELD_OUT_SEEDS),
            "workers": num_workers,
            "seed": seed,
            "max_turns": max_turns,
            "stall_turns": stall_turns,
            "out_dir": str(out_path),
        },
        "train_stats": best_train_stats,
        "held_out_float": held_out_float,
        "held_out_int8": held_out_int8,
        "quantization_retention": round(float(retention), 4),
        "wall_clock_s": round(float(wall_clock_s), 3),
    }
    (out_path / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    if not quiet:
        print(
            f"[DONE] {run_id} in {wall_clock_s:.2f}s | "
            f"held-out float={held_out_float['mean_fit']:.1f} "
            f"(floor {held_out_float['mean_floor']:.2f}) | "
            f"int8={held_out_int8['mean_fit']:.1f} "
            f"(floor {held_out_int8['mean_floor']:.2f}, retention={retention:.1%})"
        )

    return summary


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Evolve MSP430 dungeon agents with a Genetic Algorithm.")
    p.add_argument("--model", choices=("heuristic", "mlp"), default="heuristic")
    p.add_argument("--obs", choices=("fair", "omniscient"), default="fair")
    p.add_argument("--cls", dest="cls_name", choices=("knight", "arcanist", "rogue"), default="knight")
    p.add_argument("--tier", choices=("smoke", "medium", "deep"), default="smoke")
    p.add_argument("--pop", type=int, default=None, help="Override population size from tier.")
    p.add_argument("--gens", type=int, default=None, help="Override generation count from tier.")
    p.add_argument("--seeds", type=int, default=None, help="Override training seed count from tier.")
    p.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    p.add_argument("--seed", type=int, default=42, help="GA RNG seed.")
    p.add_argument("--out", type=str, default=None, help="Output directory (default: ga/results/<run_id>).")
    p.add_argument("--max-turns", type=int, default=350)
    p.add_argument("--stall-turns", type=int, default=140)
    p.add_argument("--quiet", action="store_true")
    return p


def main() -> None:
    args = build_arg_parser().parse_args()
    evolve(
        model=args.model,
        obs=args.obs,
        cls_name=args.cls_name,
        tier=args.tier,
        pop_size=args.pop,
        gens=args.gens,
        num_seeds=args.seeds,
        workers=args.workers,
        seed=args.seed,
        out_dir=args.out,
        max_turns=args.max_turns,
        stall_turns=args.stall_turns,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
