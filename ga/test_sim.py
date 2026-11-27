"""Parity & invariant unit tests for ga/sim.py against app/dungeon_game.c."""
import time
import unittest
from ga.sim import (
    CLASS_ARCANIST,
    CLASS_KNIGHT,
    CLASS_ROGUE,
    DNG_H,
    DNG_STAIRS,
    DNG_W,
    ENEMY_BOSS,
    ENEMY_NONE,
    DungeonSim,
)


class TestDungeonSimParity(unittest.TestCase):
    def test_lfsr_sequence_matches_c(self):
        # Exact 16-bit Galois LFSR 0xB400 from initial seed 0x5D17
        sim = DungeonSim(seed=0x5D17, cls=CLASS_KNIGHT)
        sim.rng = 0x5D17
        vals = [sim.next_rand() for _ in range(5)]
        # Verify hand-computed first step: 0x5D17 >> 1 = 0x2E8B ^ 0xB400 = 0x9A8B
        self.assertEqual(vals[0], 0x9A8B)
        self.assertEqual(len(set(vals)), 5)

    def test_determinism(self):
        s1 = DungeonSim(seed=0xA5CD, cls=CLASS_KNIGHT)
        s2 = DungeonSim(seed=0xA5CD, cls=CLASS_KNIGHT)
        self.assertEqual(s1.grid, s2.grid)
        self.assertEqual((s1.px, s1.py), (s2.px, s2.py))
        for a in ("w", "d", "d", "s", " ", "f", "q", "a"):
            s1.step(a)
            s2.step(a)
        self.assertEqual((s1.px, s1.py, s1.hp, s1.mp, s1.gold), (s2.px, s2.py, s2.hp, s2.mp, s2.gold))

    def test_all_three_classes_starting_stats(self):
        k = DungeonSim(seed=0x1234, cls=CLASS_KNIGHT)
        self.assertEqual((k.max_hp, k.max_mp, k.atk, k.def_, k.potions, k.bombs, k.gold), (16, 4, 3, 1, 1, 0, 0))
        a = DungeonSim(seed=0x1234, cls=CLASS_ARCANIST)
        self.assertEqual((a.max_hp, a.max_mp, a.atk, a.def_, a.potions, a.bombs, a.gold), (11, 8, 2, 0, 1, 1, 0))
        r = DungeonSim(seed=0x1234, cls=CLASS_ROGUE)
        self.assertEqual((r.max_hp, r.max_mp, r.atk, r.def_, r.potions, r.bombs, r.scrolls, r.gold), (12, 5, 2, 0, 1, 1, 1, 25))

    def test_stairs_present_on_non_boss_floors(self):
        for seed in (0x1111, 0x2222, 0x5D17, 0xA5CD):
            sim = DungeonSim(seed=seed, cls=CLASS_KNIGHT)
            for fl in (1, 2, 3, 4):
                sim.floor = fl
                sim._generate_floor()
                found = any(sim.tile_type(c, r) == DNG_STAIRS for r in range(DNG_H) for c in range(DNG_W))
                self.assertTrue(found, f"Missing stairs on floor {fl}, seed {seed:#x}")

    def test_boss_sanctum_floor_5(self):
        sim = DungeonSim(seed=0x5D17, cls=CLASS_KNIGHT)
        sim.floor = 5
        sim._generate_floor()
        self.assertEqual(sim.enemies[0].etype, ENEMY_BOSS)
        self.assertEqual(sim.enemies[0].hp, 24)
        self.assertEqual((sim.px, sim.py), (7, 11))

    def test_simulation_throughput(self):
        sim = DungeonSim(seed=0x5D17, cls=CLASS_KNIGHT)
        t0 = time.perf_counter()
        n = 5000
        for i in range(n):
            if sim.game_over:
                sim.reset(seed=(0x5D17 + i) & 0xFFFF, cls=CLASS_KNIGHT)
            sim.step(("w", "d", "s", "a")[i & 3])
        dt = time.perf_counter() - t0
        tps = n / max(dt, 1e-6)
        print(f"\n[BENCH] DungeonSim throughput: {tps:,.0f} turns/sec ({dt*1000:.1f} ms for {n} turns)")
        self.assertGreater(tps, 2000.0)

    def test_quantized_int8_action_parity(self):
        import numpy as np
        from ga.genome import HeuristicGenome, TinyMLPGenome
        from ga.sim import ACTIONS

        rng = np.random.default_rng(7)
        for cls_ctor in (HeuristicGenome, TinyMLPGenome):
            g = cls_ctor.random(rng)
            q = g.quantize_int8()
            sim = DungeonSim(seed=0xA5CD, cls=CLASS_KNIGHT)
            matches = 0
            steps = 40
            for _ in range(steps):
                if sim.game_over:
                    break
                a_f = g.select_action(sim, omniscient=False)
                a_q = q.select_action(sim, omniscient=False)
                self.assertIn(a_f, ACTIONS)
                self.assertIn(a_q, ACTIONS)
                if a_f == a_q:
                    matches += 1
                sim.step(a_q)
            self.assertGreaterEqual(matches / max(1, steps), 0.75)


if __name__ == "__main__":
    unittest.main()
