"""Unit tests for strategy.py. Run from agent-template/:  python -m unittest discover tests

Copyright 2026 The CoGNETs Consortium (template); tests by Ekaterina Fedoseeva.
SPDX-License-Identifier: Apache-2.0
"""

import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy  # noqa: E402

RES = strategy.RESOURCES
PROFILE = {"weights": {"compute": 0.4, "energy": 0.25, "security": 0.35},
           "q_min": 0.18, "s_min": 0.12,
           "features": {"battery": 0.8, "mobility": 0.2},
           "arena": {"total_rounds": 60, "battery_cutoff": 0.05}}


def result(round_no, bid, caps, prices, energy=0.05, battery=0.7):
    """A settled-round result shaped like GET /v1/result."""
    return {"round": round_no, "bid": bid, "spend": sum(bid.values()), "capacities": caps,
            "prices": prices, "allocation": {"compute": 0.3, "energy": energy, "security": 0.3},
            "battery_drawn": 0.3 * 1.2 * energy + 0.004, "battery": battery, "participated": True}


def assert_valid(test, bid, budget):
    test.assertEqual(set(bid), set(RES))
    for v in bid.values():
        test.assertTrue(math.isfinite(v) and v >= 0.0, bid)
    test.assertLessEqual(sum(bid.values()), budget + 1e-12)


class BidValidity(unittest.TestCase):
    """Whatever comes in, the bid must never cost us a compromise point."""

    def test_random_markets_and_histories(self):
        rng = random.Random(7)
        for _ in range(150):
            budget = rng.choice([0.0, 1e-6, rng.uniform(0.5, 1.5)])
            caps = {k: rng.uniform(0.7, 1.3) for k in RES}
            prices = {k: rng.choice([0.01, rng.uniform(0.01, 5.0)]) for k in RES}
            profile = {**PROFILE, "round": rng.randint(1, 60),
                       "features": {"battery": rng.uniform(0.0, 1.0), "mobility": rng.uniform(0, 0.45)}}
            history = [result(r, {k: rng.uniform(0, 0.6) for k in RES}, caps, prices,
                              energy=rng.uniform(0, 0.4)) for r in range(1, rng.randint(1, 15))]
            assert_valid(self, strategy.decide_bid(budget, prices, caps, profile, history), budget)

    def test_sparse_inputs(self):
        bid = strategy.decide_bid(1.0, {}, {}, {}, [])
        assert_valid(self, bid, 1.0)


class Field(unittest.TestCase):
    def test_recovers_the_field_exactly(self):
        """Last round: others bid 0.9 on compute against capacity 1.2, we bid 0.3."""
        caps_then = {"compute": 1.2, "energy": 0.8, "security": 1.0}
        ours = {"compute": 0.3, "energy": 0.1, "security": 0.4}
        others = {"compute": 0.9, "energy": 0.5, "security": 1.1}
        prices_now = {k: (ours[k] + others[k]) / caps_then[k] for k in RES}  # published this round
        history = [result(4, ours, caps_then, {k: 1.0 for k in RES})]
        field = strategy.estimate_field(prices_now, history, 5, ours and 0.8, "exact")
        for k in RES:  # rescaled by budget now (0.8) / budget then (0.8 / SPEND)
            self.assertAlmostEqual(field[k], others[k] * strategy.SPEND, places=9)


class Floors(unittest.TestCase):
    def test_buys_both_floors_when_affordable(self):
        caps = {k: 1.0 for k in RES}
        field = {k: 1.0 for k in RES}
        b_c, b_s, _ = strategy.best_compute_security(0.9, 0.05, caps, field, PROFILE["weights"],
                                                     (0.18, 0.12), strategy.OPTIONS)
        self.assertGreaterEqual(strategy.share(b_c, 1.0, 1.0), 0.18)
        self.assertGreaterEqual(strategy.share(b_s, 1.0, 1.0), 0.12)


class Battery(unittest.TestCase):
    """Field of 1.0 per pool (no history), unit pools, mobility 0.2."""

    def _after(self, battery, round_no):
        """(energy bid, expected charge after the round) for this decision."""
        profile = {**PROFILE, "round": round_no, "features": {"battery": battery, "mobility": 0.2}}
        bid = strategy.decide_bid(1.0, {k: 1.0 for k in RES}, {k: 1.0 for k in RES}, profile, [])
        won = strategy.share(bid["energy"], 1.0, 1.0)
        return bid["energy"], battery - (strategy.DEFAULT_DRAIN * 1.2 * won + strategy.DEFAULT_IDLE_DRAIN)

    def test_a_nearly_flat_battery_rests_on_purpose(self):
        # At 0.06 the idle drain alone reaches the cutoff within ~3 rounds, so a rest
        # is coming anyway: better to buy energy now and rest next round than to
        # starve first. Whatever it does, it must not dip under by accident.
        energy, after = self._after(0.06, 20)
        self.assertTrue(energy == 0.0 or after <= 0.05, (energy, after))

    def test_a_healthy_battery_is_not_flattened_mid_run(self):
        energy, after = self._after(0.5, 20)
        self.assertGreater(energy, 0.0)
        self.assertGreater(after, 0.3)

    def test_spends_charge_in_the_last_round(self):
        energy, _ = self._after(0.06, 60)  # charge is worthless after the run
        self.assertGreater(energy, 0.05)


if __name__ == "__main__":
    unittest.main()
