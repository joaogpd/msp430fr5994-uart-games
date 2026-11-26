"""
Evolutionary genomes, Q7 int8 quantization, and MSP430FR5994 C header export.

Implements:
- Approach A: HeuristicGenome (22 weights scoring each of 11 actions via dot product)
- Approach B: TinyMLPGenome (24 -> 16 -> 11 ReLU MLP, 587 floats)
- Exact int8_t (Q7) integer-only inference mirrors for MSP430 verification
- export_c_header() generating zero-float FRAM-resident const int8_t arrays
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Union

import numpy as np

from ga.features import (
    HEURISTIC_FEATURE_NAMES,
    NUM_HEURISTIC_FEATURES,
    NUM_STATE_FEATURES,
    extract_action_features,
    extract_state_vector,
    precompute_context,
)
from ga.sim import ACTIONS, NUM_ACTIONS, DungeonSim

MLP_IN = NUM_STATE_FEATURES  # 24
MLP_HIDDEN = 16
MLP_OUT = NUM_ACTIONS  # 11
MLP_TOTAL_PARAMS = MLP_IN * MLP_HIDDEN + MLP_HIDDEN + MLP_HIDDEN * MLP_OUT + MLP_OUT  # 587


def _to_q7(x: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(x * 127.0), -127, 127).astype(np.int8)


@dataclass
class QuantizedHeuristic:
    """Zero-float int8_t Q7 representation of HeuristicGenome."""

    q_weights: np.ndarray  # shape (22,), dtype int8
    scale: float

    def select_action(self, sim: DungeonSim, omniscient: bool = False) -> str:
        ctx = precompute_context(sim, omniscient=omniscient)
        mask = ctx.valid_mask
        qw = self.q_weights.astype(np.int32)

        best_idx = 10  # ' ' fallback
        best_score = -2147483648
        for a in range(NUM_ACTIONS):
            if not mask[a]:
                continue
            qf = _to_q7(extract_action_features(sim, a, omniscient=omniscient, ctx=ctx)).astype(np.int32)
            score = int(np.dot(qw, qf))
            if score > best_score:
                best_score = score
                best_idx = a
        return ACTIONS[best_idx]


class HeuristicGenome:
    """Approach A: Linear tactical heuristic over 22 integer-friendly action features."""

    model_type: str = "heuristic"

    def __init__(self, weights: np.ndarray) -> None:
        w = np.asarray(weights, dtype=np.float32).reshape(-1)
        if w.shape[0] != NUM_HEURISTIC_FEATURES:
            raise ValueError(f"Expected {NUM_HEURISTIC_FEATURES} weights, got {w.shape[0]}")
        self.weights = w.copy()

    @classmethod
    def random(cls, rng: np.random.Generator) -> HeuristicGenome:
        w = rng.normal(0.0, 1.0, size=NUM_HEURISTIC_FEATURES).astype(np.float32)
        return cls(w)

    def select_action(self, sim: DungeonSim, omniscient: bool = False) -> str:
        ctx = precompute_context(sim, omniscient=omniscient)
        mask = ctx.valid_mask
        w = self.weights

        best_idx = 10  # ' ' fallback
        best_score = -1e30
        for a in range(NUM_ACTIONS):
            if not mask[a]:
                continue
            feats = extract_action_features(sim, a, omniscient=omniscient, ctx=ctx)
            score = float(np.dot(w, feats))
            if score > best_score:
                best_score = score
                best_idx = a
        return ACTIONS[best_idx]

    def crossover(self, other: HeuristicGenome, rng: np.random.Generator) -> HeuristicGenome:
        alpha = rng.uniform(0.0, 1.0, size=NUM_HEURISTIC_FEATURES).astype(np.float32)
        mask = rng.random(NUM_HEURISTIC_FEATURES) < 0.5
        child = np.where(mask, alpha * self.weights + (1.0 - alpha) * other.weights, self.weights)
        swap = rng.random(NUM_HEURISTIC_FEATURES) < 0.5
        child = np.where(swap, child, other.weights)
        return HeuristicGenome(child)

    def mutate(self, rng: np.random.Generator, rate: float = 0.25, sigma: float = 0.35) -> HeuristicGenome:
        child = self.weights.copy()
        mut_mask = rng.random(NUM_HEURISTIC_FEATURES) < rate
        if np.any(mut_mask):
            noise = rng.normal(0.0, sigma, size=NUM_HEURISTIC_FEATURES).astype(np.float32)
            child[mut_mask] += noise[mut_mask]
        np.clip(child, -8.0, 8.0, out=child)
        return HeuristicGenome(child)

    def quantize_int8(self) -> QuantizedHeuristic:
        max_abs = float(np.max(np.abs(self.weights)))
        scale = (max_abs / 127.0) if max_abs > 1e-8 else 1.0
        q_w = np.clip(np.rint(self.weights / scale), -127, 127).astype(np.int8)
        return QuantizedHeuristic(q_weights=q_w, scale=scale)

    def to_dict(self) -> Dict[str, Any]:
        q = self.quantize_int8()
        return {
            "model_type": self.model_type,
            "num_features": NUM_HEURISTIC_FEATURES,
            "feature_names": list(HEURISTIC_FEATURE_NAMES),
            "weights": [round(float(x), 6) for x in self.weights],
            "q_weights": [int(x) for x in q.q_weights],
            "q_scale": float(q.scale),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HeuristicGenome:
        return cls(np.asarray(data["weights"], dtype=np.float32))


@dataclass
class QuantizedTinyMLP:
    """Zero-float int8_t Q7 representation of TinyMLPGenome (24 -> 16 -> 11, ReLU)."""

    q_w1: np.ndarray  # (16, 24) int8
    q_b1: np.ndarray  # (16,) int8
    q_w2: np.ndarray  # (11, 16) int8
    q_b2: np.ndarray  # (11,) int8
    h_shift: int
    scale1: float
    scale2: float

    def select_action(self, sim: DungeonSim, omniscient: bool = False) -> str:
        ctx = precompute_context(sim, omniscient=omniscient)
        mask = ctx.valid_mask
        qx = _to_q7(extract_state_vector(sim, omniscient=omniscient, ctx=ctx)).astype(np.int32)

        acc1 = (self.q_b1.astype(np.int32) << 7) + (self.q_w1.astype(np.int32) @ qx)
        qh = np.clip(np.maximum(0, acc1) >> self.h_shift, 0, 127).astype(np.int32)
        acc2 = (self.q_b2.astype(np.int32) << 7) + (self.q_w2.astype(np.int32) @ qh)

        best_idx = 10
        best_score = -2147483648
        for a in range(NUM_ACTIONS):
            if mask[a]:
                sc = int(acc2[a])
                if sc > best_score:
                    best_score = sc
                    best_idx = a
        return ACTIONS[best_idx]


class TinyMLPGenome:
    """Approach B: Fixed-topology 24 -> 16 -> 11 ReLU MLP (587 floats)."""

    model_type: str = "mlp"

    def __init__(
        self,
        w1: np.ndarray,
        b1: np.ndarray,
        w2: np.ndarray,
        b2: np.ndarray,
    ) -> None:
        self.w1 = np.asarray(w1, dtype=np.float32).reshape(MLP_HIDDEN, MLP_IN).copy()
        self.b1 = np.asarray(b1, dtype=np.float32).reshape(MLP_HIDDEN).copy()
        self.w2 = np.asarray(w2, dtype=np.float32).reshape(MLP_OUT, MLP_HIDDEN).copy()
        self.b2 = np.asarray(b2, dtype=np.float32).reshape(MLP_OUT).copy()

    @classmethod
    def from_flat(cls, flat: np.ndarray) -> TinyMLPGenome:
        f = np.asarray(flat, dtype=np.float32).reshape(-1)
        if f.shape[0] != MLP_TOTAL_PARAMS:
            raise ValueError(f"Expected {MLP_TOTAL_PARAMS} params, got {f.shape[0]}")
        n1 = MLP_HIDDEN * MLP_IN
        n2 = n1 + MLP_HIDDEN
        n3 = n2 + MLP_OUT * MLP_HIDDEN
        return cls(
            w1=f[:n1].reshape(MLP_HIDDEN, MLP_IN),
            b1=f[n1:n2],
            w2=f[n2:n3].reshape(MLP_OUT, MLP_HIDDEN),
            b2=f[n3:],
        )

    def to_flat(self) -> np.ndarray:
        return np.concatenate([self.w1.ravel(), self.b1, self.w2.ravel(), self.b2])

    @classmethod
    def random(cls, rng: np.random.Generator) -> TinyMLPGenome:
        # He initialization for ReLU hidden layer, Xavier for output layer
        w1 = rng.normal(0.0, np.sqrt(2.0 / MLP_IN), size=(MLP_HIDDEN, MLP_IN)).astype(np.float32)
        b1 = rng.normal(0.0, 0.1, size=(MLP_HIDDEN,)).astype(np.float32)
        w2 = rng.normal(0.0, np.sqrt(1.0 / MLP_HIDDEN), size=(MLP_OUT, MLP_HIDDEN)).astype(np.float32)
        b2 = rng.normal(0.0, 0.1, size=(MLP_OUT,)).astype(np.float32)
        return cls(w1, b1, w2, b2)

    def select_action(self, sim: DungeonSim, omniscient: bool = False) -> str:
        ctx = precompute_context(sim, omniscient=omniscient)
        mask = ctx.valid_mask
        x = extract_state_vector(sim, omniscient=omniscient, ctx=ctx)

        h = np.maximum(0.0, self.w1 @ x + self.b1)
        logits = self.w2 @ h + self.b2

        best_idx = 10
        best_score = -1e30
        for a in range(NUM_ACTIONS):
            if mask[a]:
                sc = float(logits[a])
                if sc > best_score:
                    best_score = sc
                    best_idx = a
        return ACTIONS[best_idx]

    def crossover(self, other: TinyMLPGenome, rng: np.random.Generator) -> TinyMLPGenome:
        f1 = self.to_flat()
        f2 = other.to_flat()
        pick = rng.random(MLP_TOTAL_PARAMS) < 0.5
        blend = rng.random(MLP_TOTAL_PARAMS) < 0.25
        alpha = rng.uniform(0.2, 0.8, size=MLP_TOTAL_PARAMS).astype(np.float32)
        child = np.where(pick, f1, f2)
        child = np.where(blend, alpha * f1 + (1.0 - alpha) * f2, child)
        return TinyMLPGenome.from_flat(child)

    def mutate(self, rng: np.random.Generator, rate: float = 0.08, sigma: float = 0.25) -> TinyMLPGenome:
        child = self.to_flat()
        mut_mask = rng.random(MLP_TOTAL_PARAMS) < rate
        if np.any(mut_mask):
            noise = rng.normal(0.0, sigma, size=MLP_TOTAL_PARAMS).astype(np.float32)
            child[mut_mask] += noise[mut_mask]
        np.clip(child, -6.0, 6.0, out=child)
        return TinyMLPGenome.from_flat(child)

    def quantize_int8(self) -> QuantizedTinyMLP:
        max1 = max(float(np.max(np.abs(self.w1))), float(np.max(np.abs(self.b1))))
        scale1 = (max1 / 127.0) if max1 > 1e-8 else 1.0
        q_w1 = np.clip(np.rint(self.w1 / scale1), -127, 127).astype(np.int8)
        q_b1 = np.clip(np.rint(self.b1 / scale1), -127, 127).astype(np.int8)

        # Choose power-of-2 right-shift h_shift so hidden activations use [0, 127] well
        row_l1 = np.sum(np.abs(q_w1.astype(np.int32)), axis=1) * 127 + np.abs(q_b1.astype(np.int32)) * 128
        typical_peak = max(127.0, float(np.max(row_l1)) * 0.35)
        h_shift = 7
        while h_shift < 11 and (typical_peak / (1 << h_shift)) > 127.0:
            h_shift += 1

        # Real scale of q_h is s_h = (2^h_shift) * scale1 / 127.0
        s_h = (float(1 << h_shift) * scale1) / 127.0
        max_w2 = float(np.max(np.abs(self.w2)))
        max_b2_equiv = float(np.max(np.abs(self.b2))) / max(1e-8, 128.0 * s_h)
        max2 = max(max_w2, max_b2_equiv)
        scale2 = (max2 / 127.0) if max2 > 1e-8 else 1.0

        q_w2 = np.clip(np.rint(self.w2 / scale2), -127, 127).astype(np.int8)
        q_b2 = np.clip(np.rint(self.b2 / (scale2 * s_h * 128.0)), -127, 127).astype(np.int8)

        return QuantizedTinyMLP(
            q_w1=q_w1,
            q_b1=q_b1,
            q_w2=q_w2,
            q_b2=q_b2,
            h_shift=h_shift,
            scale1=scale1,
            scale2=scale2,
        )

    def to_dict(self) -> Dict[str, Any]:
        q = self.quantize_int8()
        return {
            "model_type": self.model_type,
            "topology": [MLP_IN, MLP_HIDDEN, MLP_OUT],
            "total_params": MLP_TOTAL_PARAMS,
            "w1": [[round(float(v), 6) for v in row] for row in self.w1],
            "b1": [round(float(v), 6) for v in self.b1],
            "w2": [[round(float(v), 6) for v in row] for row in self.w2],
            "b2": [round(float(v), 6) for v in self.b2],
            "h_shift": int(q.h_shift),
            "scale1": float(q.scale1),
            "scale2": float(q.scale2),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TinyMLPGenome:
        return cls(
            w1=np.asarray(data["w1"], dtype=np.float32),
            b1=np.asarray(data["b1"], dtype=np.float32),
            w2=np.asarray(data["w2"], dtype=np.float32),
            b2=np.asarray(data["b2"], dtype=np.float32),
        )


Genome = Union[HeuristicGenome, TinyMLPGenome]


def genome_from_dict(data: Dict[str, Any]) -> Genome:
    mtype = data.get("model_type")
    if mtype == "heuristic":
        return HeuristicGenome.from_dict(data)
    if mtype == "mlp":
        return TinyMLPGenome.from_dict(data)
    raise ValueError(f"Unknown model_type: {mtype!r}")


def _format_c_array_1d(arr: np.ndarray, per_line: int = 12) -> str:
    vals = [f"{int(v):4d}" for v in arr.ravel()]
    lines = []
    for i in range(0, len(vals), per_line):
        lines.append("    " + ", ".join(vals[i : i + per_line]))
    return ",\n".join(lines)


def _format_c_array_2d(arr: np.ndarray) -> str:
    rows = []
    for r in range(arr.shape[0]):
        vals = ", ".join(f"{int(v):4d}" for v in arr[r])
        rows.append(f"    {{ {vals} }}")
    return ",\n".join(rows)


def export_c_header(
    heuristic_genome: Optional[HeuristicGenome],
    mlp_genome: Optional[TinyMLPGenome],
    out_path: Union[str, Path],
) -> Path:
    """
    Export quantized int8_t (Q7) weights to a zero-float MSP430FR5994 C header.
    All `const int8_t` arrays reside automatically in FRAM (.rodata).
    """
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "/*",
        " * Auto-generated by ga/genome.py (export_c_header).",
        " * Zero-float Q7 int8_t weights for MSP430FR5994 FRAM inference.",
        " */",
        "#ifndef GA_WEIGHTS_H",
        "#define GA_WEIGHTS_H",
        "",
        "#include <stdint.h>",
        "",
        f"#define GA_NUM_ACTIONS        {NUM_ACTIONS}",
        f"#define GA_HEUR_NUM_FEATURES  {NUM_HEURISTIC_FEATURES}",
        f"#define GA_MLP_IN             {MLP_IN}",
        f"#define GA_MLP_HIDDEN         {MLP_HIDDEN}",
        f"#define GA_MLP_OUT            {MLP_OUT}",
        "",
    ]

    if heuristic_genome is not None:
        qh = heuristic_genome.quantize_int8()
        lines.extend(
            [
                "#define GA_HAS_HEURISTIC      1",
                "",
                "/* Approach A: Evolved Tactical Heuristic Q7 weights (22 bytes in FRAM) */",
                "static const int8_t GA_HEUR_WEIGHTS[GA_HEUR_NUM_FEATURES] = {",
                _format_c_array_1d(qh.q_weights, per_line=11),
                "};",
                "",
            ]
        )
    else:
        lines.extend(["#define GA_HAS_HEURISTIC      0", ""])

    if mlp_genome is not None:
        qm = mlp_genome.quantize_int8()
        lines.extend(
            [
                "#define GA_HAS_MLP            1",
                f"#define GA_MLP_H_SHIFT        {qm.h_shift}",
                "",
                "/* Approach B: Tiny-MLP 24 -> 16 -> 11 ReLU Q7 weights (587 bytes in FRAM) */",
                "static const int8_t GA_MLP_W1[GA_MLP_HIDDEN][GA_MLP_IN] = {",
                _format_c_array_2d(qm.q_w1),
                "};",
                "",
                "static const int8_t GA_MLP_B1[GA_MLP_HIDDEN] = {",
                _format_c_array_1d(qm.q_b1, per_line=8),
                "};",
                "",
                "static const int8_t GA_MLP_W2[GA_MLP_OUT][GA_MLP_HIDDEN] = {",
                _format_c_array_2d(qm.q_w2),
                "};",
                "",
                "static const int8_t GA_MLP_B2[GA_MLP_OUT] = {",
                _format_c_array_1d(qm.q_b2, per_line=11),
                "};",
                "",
            ]
        )
    else:
        lines.extend(["#define GA_HAS_MLP            0", ""])

    lines.append("#endif /* GA_WEIGHTS_H */")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path
