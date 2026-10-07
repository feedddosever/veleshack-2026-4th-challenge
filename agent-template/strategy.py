"""
Our bidding strategy for the CoGNETs Swarm Arena.

Each round we decide one number - the share of the energy pool to buy - and the
rest follows:

1. Read the field. The rest of the swarm's total bid on each pool last round is
   recovered exactly from the published price (`lambda_k * C_k - our bid`), using
   the capacity of the round the price came from, and rescaled to this round's
   budget, which every node shares.
2. Value each candidate energy share. For every share x we price the energy bid
   (inverse Kelly rule), buy the compute and security floors with a margin, and
   split what is left between compute and security with the exact Kelly best
   response. That gives this round's utility U(x).
3. Plan the battery. Winning energy share x drains c*x + idle charge; a flat
   battery costs whole rounds and resting is the only way to recharge (+0.22 per
   round). A finite-horizon dynamic programme over (rounds left, charge), with
   future rounds drawn from the market states seen recently, gives the expected
   value of every end-of-round charge - including the option of draining to the
   cutoff on purpose and resting. We pick the x that maximises
   U(x) + V(charge after this round).

Everything is recomputed from the arguments each round; the module keeps no state
between calls, so a restarted agent or a new run needs no special handling.

Copyright 2026 The CoGNETs Consortium (template); strategy by Ekaterina Fedoseeva.
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Tuple

import numpy as np

RESOURCES = ("compute", "energy", "security")
LOG = logging.getLogger("agent.strategy")

# Arena physics not exposed by the API. Both shipped scenarios use these values;
# the drain coefficient is re-estimated from our own results as soon as possible.
DEFAULT_DRAIN = 0.30      # charge per unit of energy share won, before mobility
DEFAULT_IDLE_DRAIN = 0.004
RECHARGE = 0.22           # charge recovered per round spent resting
DEFAULT_CUTOFF = 0.05     # at or below this the node sits the next round out
DEFAULT_ROUNDS = 60       # graded scenario; agent.py passes the real value
SPEND = 0.999             # fraction of the budget we bid, clear of float rounding

OPTIONS: Dict[str, Any] = {
    "field": "exact",       # "exact" | "primer" estimate of the rest of the swarm's bid
    "split": "kelly",       # "kelly" | "weights" split of compute vs security
    "floors": True,         # buy q_min / s_min before anything else
    "floor_margin": 0.15,   # aim this far above each floor; the field is an estimate
    "battery": "dp",        # "dp" | "taper" | "none"
    "field_drop": 0.75,     # battery safety: the energy field may shrink this much (bots resting)
    "future": "sampled",    # "sampled" recent market states | "mean" one average state
    "use_history": True,    # False = what the unmodified template loop gives you (nothing)
}

BATTERY_GRID = np.linspace(0.0, 1.0, 201)
N_SHARES = 48             # candidate energy shares per round


def decide_bid(
    budget: float,
    prices: Dict[str, float],
    capacities: Dict[str, float],
    profile: Dict[str, Any],
    history: List[Dict[str, Any]],
) -> Dict[str, float]:
    """Return this round's bid (see the module docstring for the method)."""
    return plan_bid(budget, prices, capacities, profile, history, OPTIONS)


def variant(**overrides: Any):
    """A decide_bid with some OPTIONS changed. Used by tools/sim.py for ablations."""
    options = {**OPTIONS, **overrides}

    def fn(budget, prices, capacities, profile, history):
        return plan_bid(budget, prices, capacities, profile, history, options)

    return fn


#: The ablation ladder reported in the README (tools/sim.py --ablation).
ABLATIONS: Dict[str, Dict[str, Any]] = {
    "kelly split only": {"floors": False, "battery": "none"},
    "+ floors": {"battery": "none"},
    "+ battery taper (primer)": {"battery": "taper"},
    "+ battery DP (ours)": {},
    "ours, DP on mean market": {"future": "mean"},
    "ours, no history": {"use_history": False},
    "ours, primer field est.": {"field": "primer"},
    "ours, weights split": {"split": "weights"},
}


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------
def plan_bid(budget, prices, capacities, profile, history, options) -> Dict[str, float]:
    budget = max(float(budget), 0.0)
    history = history if options["use_history"] else []
    if budget <= 0.0:
        return {k: 0.0 for k in RESOURCES}
    spend = budget * SPEND
    weights = {k: float(profile.get("weights", {}).get(k, 1 / 3)) for k in RESOURCES}
    caps = {k: max(float(capacities.get(k, 1.0)), 1e-6) for k in RESOURCES}
    floors = (float(profile.get("q_min", 0.0)), float(profile.get("s_min", 0.0)))
    if not options["floors"]:
        floors = (0.0, 0.0)
    arena = profile.get("arena") or {}
    current_round = int(profile.get("round") or (history[-1]["round"] + 1 if history else 1))

    field = estimate_field(prices, history, current_round, budget, options["field"])
    curve = utility_curve(spend, caps, field, weights, floors, options)

    if options["battery"] == "none":
        best = max(curve, key=lambda c: c.utility)
    elif options["battery"] == "taper":
        best = taper_choice(curve, battery_now(profile, history), weights, spend, caps, field)
    else:
        phys = physics(profile, history, arena)
        rounds_left = int(arena.get("total_rounds", DEFAULT_ROUNDS)) - current_round + 1
        models = [utility_curve(m_spend, m_caps, m_field, weights, floors, options)
                  for m_spend, m_caps, m_field in market_states(history, options["future"])]
        values = battery_values(models, phys, max(rounds_left - 1, 0))
        charge = battery_now(profile, history)
        best = max(curve, key=lambda c: c.utility + continuation(charge, c, values, phys))
        after = charge - phys.drain(best.energy_share)
        LOG.info("plan r%d: charge %.3f -> %.3f%s, energy share %.3f of %.2f (field %.2f), "
                 "expected utility %.3f", current_round, charge, after,
                 " (rest next)" if after <= phys.cutoff else "", best.energy_share,
                 caps["energy"], field["energy"], best.utility)

    return {"compute": best.bids[0], "energy": best.bids[1], "security": best.bids[2]}


# ---------------------------------------------------------------------------
# 1. The field: everyone else's total bid on each pool
# ---------------------------------------------------------------------------
def estimate_field(prices, history, current_round, budget, mode) -> Dict[str, float]:
    """Predicted total bid of the rest of the swarm on each pool, this round.

    The price published at the start of this round is last round's total bid
    divided by LAST round's capacity, so it has to be multiplied by that
    capacity - not this round's. ("primer" reproduces the docs' estimator, which
    mixes the two, for comparison.) Every node gets the same budget each round
    and the bots spend all of it, so the field is rescaled by budget_now/budget_then.
    """
    if not history:
        return {k: budget for k in RESOURCES}  # three bots, about a third each
    last = history[-1]
    if mode == "primer":
        return {k: max(1e-4, float(prices.get(k, 1.0)) * float(last["capacities"][k])
                       - float(last["bid"][k])) for k in RESOURCES}
    consecutive = int(last.get("round", 0)) == current_round - 1
    field = {}
    for k in RESOURCES:
        if consecutive:
            others = float(prices.get(k, 1.0)) * float(last["capacities"][k]) - float(last["bid"][k])
            then = float(last.get("spend", budget)) / SPEND or budget
        else:  # we sat last round out: its capacity is unknown, use the mean
            others, then = float(prices.get(k, 1.0)), budget
        field[k] = max(others, 1e-4) * budget / then
    return field


def market_states(history: List[Dict[str, Any]], mode: str, window: int = 10):
    """What future rounds may look like: (spend, capacities, field) of recent rounds.

    Recovered exactly from consecutive results. "mean" collapses them into one
    average state with unit budget and pools (for comparison).
    """
    states = []
    for prev, nxt in zip(history[-window - 1:-1], history[-window:]):
        if int(nxt["round"]) != int(prev["round"]) + 1:
            continue
        caps = {k: float(prev["capacities"][k]) for k in RESOURCES}
        field = {k: max(float(nxt["prices"][k]) * caps[k] - float(prev["bid"][k]), 1e-4) for k in RESOURCES}
        states.append((float(prev.get("spend", SPEND)), caps, field))
    unit = {k: 1.0 for k in RESOURCES}
    if not states:
        return [(SPEND, unit, unit)]  # three bots spending a unit budget, a third each
    if mode == "mean":
        field = {k: sum(f[k] / sp for sp, _, f in states) / len(states) * SPEND for k in RESOURCES}
        return [(SPEND, unit, field)]
    return states


# ---------------------------------------------------------------------------
# 2. One round: utility as a function of the energy share we buy
# ---------------------------------------------------------------------------
class Option:
    """One candidate bid: the energy share it targets, the share it could win if the
    field shrinks (part of the swarm goes to rest), its utility and the bids."""
    __slots__ = ("energy_share", "worst_share", "utility", "bids")

    def __init__(self, energy_share, worst_share, utility, bids: Tuple[float, float, float]):
        self.energy_share, self.worst_share = energy_share, worst_share
        self.utility, self.bids = utility, bids


def share(bid: float, others: float, cap: float) -> float:
    return cap * bid / (bid + others) if bid > 0.0 else 0.0


def bid_for(target: float, others: float, cap: float) -> float:
    """The bid that buys `target` of a pool (the Kelly rule, inverted)."""
    return others * target / (cap - target) if target < cap else math.inf


def utility_curve(spend, caps, field, weights, floors, options) -> List[Option]:
    """U(x) for a grid of energy shares x, from 0 up to all-in on energy."""
    top = share(spend, field["energy"], caps["energy"])
    out = []
    for i in range(N_SHARES):
        x = top * (i / (N_SHARES - 1)) ** 2          # denser near 0, where sqrt is steep
        b_e = min(bid_for(x, field["energy"], caps["energy"]), spend)
        b_c, b_s, u = best_compute_security(spend - b_e, x, caps, field, weights, floors, options)
        worst = share(b_e, field["energy"] * (1.0 - options["field_drop"]), caps["energy"])
        out.append(Option(x, worst, u, (b_c, b_e, b_s)))
    return out


def best_compute_security(rest, x_e, caps, field, weights, floors, options):
    """Split `rest` between compute and security, buying the floors if affordable.

    Tries meeting both floors, either one, or neither, and keeps the best -
    one missed floor halves the round, both quarter it.
    """
    q_min, s_min = floors
    margin = 1.0 + options["floor_margin"]
    need = {
        "compute": bid_for(min(q_min * margin, 0.95 * caps["compute"]), field["compute"], caps["compute"]),
        "security": bid_for(min(s_min * margin, 0.95 * caps["security"]), field["security"], caps["security"]),
    }
    energy_term = weights["energy"] * math.sqrt(x_e)
    best = (0.0, max(rest, 0.0), -1.0)
    for meet_c, meet_s in ((True, True), (True, False), (False, True), (False, False)):
        lo = need["compute"] if meet_c and q_min > 0 else 0.0
        lo_s = need["security"] if meet_s and s_min > 0 else 0.0
        if lo + lo_s > rest:
            continue
        b_c = split(rest, lo, rest - lo_s, caps, field, weights, options["split"])
        x_c = share(b_c, field["compute"], caps["compute"])
        x_s = share(rest - b_c, field["security"], caps["security"])
        u = (weights["compute"] * math.sqrt(x_c) + energy_term + weights["security"] * math.sqrt(x_s)) ** 2
        u *= (0.5 if x_c < q_min else 1.0) * (0.5 if x_s < s_min else 1.0)
        if u > best[2]:
            best = (b_c, rest - b_c, u)
    return best


def split(rest, lo, hi, caps, field, weights, mode) -> float:
    """Compute's part of `rest`, within [lo, hi]; security gets the remainder."""
    if mode == "weights":
        w = weights["compute"] / max(weights["compute"] + weights["security"], 1e-9)
        return min(max(rest * w, lo), hi)

    def slope(k, b):  # d/db of w_k * sqrt(share_k(b))
        s, c = field[k], caps[k]
        return weights[k] * math.sqrt(c) * s / (2.0 * math.sqrt(max(b, 1e-12)) * (b + s) ** 1.5)

    a, b = lo, hi  # the objective is concave in compute's bid: bisect on its slope
    for _ in range(25):
        mid = 0.5 * (a + b)
        if slope("compute", mid) > slope("security", rest - mid):
            a = mid
        else:
            b = mid
    return 0.5 * (a + b)


# ---------------------------------------------------------------------------
# 3. The battery
# ---------------------------------------------------------------------------
class Physics:
    def __init__(self, coef: float, idle: float, cutoff: float):
        self.coef, self.idle, self.cutoff = coef, idle, cutoff

    def drain(self, energy_share):
        return self.coef * energy_share + self.idle


def physics(profile, history, arena) -> Physics:
    """Drain per unit of energy share: fitted on our own results when they vary enough,
    otherwise the scenario default with our mobility surcharge."""
    mobility = float((profile.get("features") or {}).get("mobility", 0.0))
    coef, idle = DEFAULT_DRAIN * (1.0 + mobility), DEFAULT_IDLE_DRAIN
    points = [(float(h["allocation"]["energy"]), float(h["battery_drawn"]))
              for h in history[-20:] if "battery_drawn" in h]
    if len(points) >= 3:
        xs, ys = zip(*points)
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        var = sum((x - mx) ** 2 for x in xs)
        if var > 1e-4:
            coef = sum((x - mx) * (y - my) for x, y in points) / var
            idle = max(my - coef * mx, 0.0)
    return Physics(coef, idle, float(arena.get("battery_cutoff", DEFAULT_CUTOFF)))


def battery_now(profile, history) -> float:
    features = profile.get("features") or {}
    if "battery" in features:
        return float(features["battery"])
    return float(history[-1]["battery"]) if history else 1.0


def battery_values(models: List[List[Option]], phys: Physics, rounds: int) -> List[np.ndarray]:
    """V[r][i]: expected best total utility over r remaining rounds starting with
    charge BATTERY_GRID[i]. Each future round is one of the market states in
    `models` (equally likely), and the energy share is chosen after seeing it.

    Ending a round at or below the cutoff means the next round is spent resting
    (no utility, +RECHARGE). The DP is free to plan that on purpose.
    """
    utils = np.array([[o.utility for o in m] for m in models])[:, None, :]
    shares = np.array([[o.energy_share for o in m] for m in models])[:, None, :]
    worst = np.array([[o.worst_share for o in m] for m in models])[:, None, :]
    charge = BATTERY_GRID[None, :, None]
    values = [np.zeros_like(BATTERY_GRID)]
    for r in range(1, rounds + 1):
        cont = _continuation(charge, shares, worst, values, r, phys)
        values.append((utils + cont).max(axis=2).mean(axis=0))
    return values


def continuation(charge: float, option: Option, values: List[np.ndarray], phys: Physics) -> float:
    """Value of the rounds after this one if we start it with `charge` and take `option`."""
    return float(_continuation(np.array([charge]), np.array([option.energy_share]),
                               np.array([option.worst_share]), values, len(values), phys)[0])


def _continuation(charge, shares, worst_shares, values, r, phys):
    """Shared by the DP and the decision: r-1 rounds follow this one.

    The share we win overshoots the target when part of the field goes to rest,
    so the charge after the round is uncertain. Going on is only counted on if
    even the worst-case share leaves us above the cutoff; resting only if the
    expected share already takes us to it. In between we assume the worse of the two.
    """
    expected = charge - phys.drain(shares)
    worst = charge - phys.drain(worst_shares)
    go = np.interp(expected, BATTERY_GRID, values[r - 1])
    rest = (np.interp(np.minimum(np.maximum(expected, 0.0) + RECHARGE, 1.0), BATTERY_GRID, values[r - 2])
            if r >= 2 else np.zeros_like(expected))
    return np.where(worst > phys.cutoff, go,
                    np.where(expected <= phys.cutoff, rest, np.minimum(go, rest)))


def taper_choice(curve, battery, weights, spend, caps, field) -> Option:
    """The primer's suggestion, for comparison: scale the weight-proportional energy
    bid by the remaining charge, then buy the closest option on the curve."""
    keep = min(max((battery - DEFAULT_CUTOFF) / (1.0 - DEFAULT_CUTOFF), 0.0), 1.0)
    target = share(spend * weights["energy"] * keep, field["energy"], caps["energy"])
    return min(curve, key=lambda c: abs(c.energy_share - target))
