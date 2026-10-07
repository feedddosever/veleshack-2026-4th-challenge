"""Offline simulator: the real arena rules, in-process, without HTTP or sleeps.

It drives `arena.state.Arena` round by round with the three baseline bots and one
strategy under test, the same way the agents do over HTTP. A 60-round graded run
takes milliseconds, so a strategy can be compared over hundreds of seeds.

Scoring mirrors the grading harness: for every seed we play the strategy (S), then
replay the same seed, bots and device with the template strategy in its place (T).

    python tools/sim.py                          # our strategy vs template, 200 seeds
    python tools/sim.py --strategy even_split    # any baseline as the strategy under test
    python tools/sim.py --ablation               # contribution of each part of our strategy

What it does not model: network faults, latency and missed rounds. Those cost
resilience points, not strategy points, and `make check` covers them.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import statistics
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "agent-template"), str(ROOT / "baselines")]

import bot  # noqa: E402  the three baselines
import strategy  # noqa: E402  our agent's strategy
from arena.config import ScenarioConfig  # noqa: E402
from arena.state import Arena  # noqa: E402
from runner import clamp  # noqa: E402  the same clamp the agent applies before sending

Strategy = Callable[..., Dict[str, float]]
TEMPLATE: Strategy = bot.naive_max  # what agent-template/strategy.py ships with


def play(fn: Strategy, seed: int, team: str, scenario: str = "graded",
         on_round: Callable[[Arena], None] = None) -> Dict[str, Any]:
    """One full run: the three bots plus `fn` registered as `team`.

    `on_round(arena)` is called after every settled round (tools/report.py records with it).
    """
    cfg = ScenarioConfig.load(scenario)
    cfg.seed = seed
    arena = Arena(cfg)

    players: Dict[str, Strategy] = {}
    for name, bot_fn in bot.STRATEGIES.items():  # bots first, as with `make up`
        players[arena.register(f"bot-{name}").node_id] = bot_fn
    me = arena.register(team)  # the bots get a copy of this device
    players[me.node_id] = fn
    histories: Dict[str, List[dict]] = {nid: [] for nid in players}

    for _ in range(cfg.total_rounds):
        state = arena.open_round()
        for nid, player in players.items():
            node = arena.nodes[nid]
            if not node.admissible(cfg.battery_cutoff, cfg.kappa_bar):
                continue
            profile = {**node.profile(), "arena": cfg.public(), "round": state.index}  # as agent.py
            bid = player(budget=node.budget, prices=dict(state.prices),
                         capacities=dict(state.capacities), profile=profile,
                         history=histories[nid])
            arena.submit(node, state.index, clamp(bid, node.budget))
        arena.settle()
        # Only our agent records results. The bots' loop (agent-template/runner.py)
        # asks for the result of the round it has just bid in, which is never settled
        # yet, so in a live arena their history stays empty. Simulate that faithfully.
        if me.node_id in state.results:
            histories[me.node_id].append({**state.results[me.node_id], "participated": True})
        if on_round:
            on_round(arena)

    bots = {n.team: round(n.score, 4) for n in arena.nodes.values() if n.is_baseline}
    return {
        "score": me.score,
        "idle": me.rounds_idle,
        "floors": me.floor_violations,
        "kappa": me.compromise,
        "bots": bots,
        "bots_total": sum(bots.values()),
        "lsw": sum(r.lsw for r in arena.rounds),
    }


def resolve(spec) -> Strategy:
    """("ours", {option overrides}) or ("bot", name) -> a strategy function."""
    kind, arg = spec
    if kind == "ours":
        return strategy.variant(**arg) if arg else strategy.decide_bid
    return bot.STRATEGIES[arg]


def run_seed(job) -> Dict[str, Any]:
    spec, seed, team, scenario = job
    s, t = play(resolve(spec), seed, team, scenario), play(TEMPLATE, seed, team, scenario)
    return {"seed": seed, "S": s, "T": t, "ratio": s["score"] / t["score"],
            "beats_all_bots": s["score"] > max(s["bots"].values())}


def evaluate(spec, seeds: List[int], team: str, scenario: str) -> Dict[str, Any]:
    with multiprocessing.get_context("fork").Pool() as pool:
        runs = pool.map(run_seed, [(spec, seed, team, scenario) for seed in seeds])
    ratios = [r["ratio"] for r in runs]
    return {
        "runs": runs,
        "mean_ratio": statistics.mean(ratios),
        "min_ratio": min(ratios),
        "p10_ratio": sorted(ratios)[len(ratios) // 10],
        "beats_all_bots": sum(r["beats_all_bots"] for r in runs) / len(runs),
        "idle": statistics.mean(r["S"]["idle"] for r in runs),
        "idle_T": statistics.mean(r["T"]["idle"] for r in runs),
        "floors": statistics.mean(r["S"]["floors"] for r in runs),
        "floors_T": statistics.mean(r["T"]["floors"] for r in runs),
        # our effect on the rest of the swarm, vs the template in our place
        "bots_delta": statistics.mean(r["S"]["bots_total"] / r["T"]["bots_total"] - 1 for r in runs),
        "lsw_delta": statistics.mean(r["S"]["lsw"] - r["T"]["lsw"] for r in runs),
        "kappa_max": max(r["S"]["kappa"] for r in runs),
    }


def row(name: str, e: Dict[str, Any]) -> str:
    return (f"{name:<28} {e['mean_ratio']:6.3f} {e['p10_ratio']:6.3f} {e['min_ratio']:6.3f} "
            f"{e['beats_all_bots']:6.0%} {e['idle']:5.1f} {e['idle_T']:5.1f} "
            f"{e['floors']:5.1f} {e['floors_T']:5.1f} {e['bots_delta']:+7.1%} {e['lsw_delta']:+8.1f}")


HEADER = (f"{'strategy':<28} {'S/T':>6} {'p10':>6} {'min':>6} {'>bots':>6} {'idle':>5} {'idleT':>5} "
          f"{'flr':>5} {'flrT':>5} {'bots':>7} {'dLSW':>8}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--strategy", default="ours", help="ours, or a baseline name")
    p.add_argument("--seeds", type=int, default=200)
    p.add_argument("--first-seed", type=int, default=1)
    p.add_argument("--team", default="team-sim")
    p.add_argument("--scenario", default="graded")
    p.add_argument("--ablation", action="store_true", help="score each part of our strategy")
    p.add_argument("--json", help="write per-seed results here")
    args = p.parse_args()
    seeds = list(range(args.first_seed, args.first_seed + args.seeds))

    print(f"{len(seeds)} seeds, scenario={args.scenario}. S/T = score / template score on the same "
          f"seed and device; bots/dLSW = effect on the 3 bots' total score and on log social welfare.\n")
    print(HEADER)
    results = {}
    if args.ablation:
        for name, overrides in strategy.ABLATIONS.items():
            results[name] = evaluate(("ours", overrides), seeds, args.team, args.scenario)
            print(row(name, results[name]), flush=True)
    else:
        spec = ("ours", {}) if args.strategy == "ours" else ("bot", args.strategy)
        results[args.strategy] = evaluate(spec, seeds, args.team, args.scenario)
        print(row(args.strategy, results[args.strategy]))
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
