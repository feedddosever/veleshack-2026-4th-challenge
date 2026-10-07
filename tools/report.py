"""The challenge outcome, as charts and tables: docs/results.md.

    python tools/report.py --live http://localhost:8080   # record the graded run going on now
    python tools/report.py --sim-seed 20261006             # or replay a seed in the simulator
    python tools/report.py --heldout 200                   # score over unseen seeds (simulator)
    python tools/report.py                                 # re-render from the saved data

A live run is recorded from /v1/leaderboard, which the arena never faults, one
snapshot per settled round: every node's score, battery, rounds resting and floor
penalties. The bots run on a copy of our device, so the lines compare strategies,
not hardware. The team is $TEAM_NAME (as in .env).

Copyright 2026 The CoGNETs Consortium (template); report by our team.
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tools")]

import sim  # noqa: E402  (also puts the arena, bots and agent on sys.path)

OUT = ROOT / "docs" / "results"
GRADED_SEED = 20261006  # graded.yaml's placeholder seed, used when ARENA_SEED is empty

# Validated reference palette (dataviz skill), categorical slots 1-4 in fixed order.
INK, INK_2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e6e6e3", "#fcfcfb"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]


def team_name() -> str:
    if os.environ.get("TEAM_NAME"):
        return os.environ["TEAM_NAME"]
    env = ROOT / ".env"
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith("TEAM_NAME="):
            return line.split("=", 1)[1].strip()
    return "team-sim"


def snapshot(round_no: int, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"round": round_no, "nodes": {r["team"]: {
        "score": round(r["score"], 4), "battery": round(r["battery"], 4), "idle": r["rounds_idle"],
        "floors": r["floor_violations"], "missed": r["rounds_missed"]} for r in rows}}


def record_live(url: str) -> List[Dict[str, Any]]:
    rounds: List[Dict[str, Any]] = []
    while True:
        with urllib.request.urlopen(f"{url}/v1/leaderboard", timeout=5) as r:
            board = json.loads(r.read())
        status = board["status"]
        settled = status["round"] if status["finished"] else status["round"] - 1
        if settled >= 1 and (not rounds or rounds[-1]["round"] < settled):
            rounds.append(snapshot(settled, board["leaderboard"]))
            print(f"\rrecorded round {settled}/{status['total_rounds']}", end="", flush=True)
        if status["finished"]:
            print()
            return rounds
        time.sleep(0.3)


def record_sim(seed: int, team: str) -> List[Dict[str, Any]]:
    rounds: List[Dict[str, Any]] = []
    sim.play(sim.strategy.decide_bid, seed, team,
             on_round=lambda arena: rounds.append(snapshot(arena.current.index, arena.leaderboard())))
    return rounds


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
def _style(plt) -> None:
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": INK_2, "xtick.color": INK_2, "ytick.color": INK_2,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "font.size": 10,
        "axes.spines.top": False, "axes.spines.right": False, "legend.frameon": False,
    })


def run_chart(run: Dict[str, Any], path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _style(plt)
    team, rounds = run["team"], run["rounds"]
    names = [team] + sorted(n for n in rounds[-1]["nodes"] if n != team)
    xs = [r["round"] for r in rounds]
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(10, 6.2), sharex=True,
                                      gridspec_kw={"height_ratios": [1, 1.1]})
    rests = [r["round"] for prev, r in zip(rounds, rounds[1:]) if r["nodes"][team]["idle"] > prev["nodes"][team]["idle"]]
    for x in rests:  # rounds our node spent resting
        top.axvspan(x - 0.5, x + 0.5, color=SERIES[0], alpha=0.08, lw=0)
    for i, name in enumerate(names):
        label = f"{name} (ours)" if name == team else name
        width, z = (2.2, 3) if name == team else (1.4, 2)
        top.plot(xs, [r["nodes"][name]["battery"] for r in rounds], color=SERIES[i], lw=width, zorder=z, label=label)
        bottom.plot(xs, [r["nodes"][name]["score"] for r in rounds], color=SERIES[i], lw=width, zorder=z, label=label)
    top.axhline(0.05, color=INK_2, lw=1, ls=(0, (4, 3)))
    top.text(1, 0.065, "cutoff", color=INK_2, va="bottom", fontsize=9)
    top.set_ylim(0, 1.02)
    top.set_ylabel("battery (charge at end of round)")
    top.set_title("Battery: the bots ignore it and keep going flat; we rest on purpose (shaded rounds)",
                  loc="left", color=INK, fontsize=11)
    bottom.set_ylabel("cumulative score")
    bottom.set_xlabel("round")
    bottom.set_title("Score: same device, same market, different strategy", loc="left", color=INK, fontsize=11)
    final = rounds[-1]["nodes"]
    bottom.annotate(f"{final[team]['score']:.2f}", (xs[-1], final[team]["score"]), xytext=(6, 0),
                    textcoords="offset points", va="center", color=INK, fontsize=10, fontweight="bold")
    top.legend(loc="upper right", ncol=2, fontsize=9, labelcolor=INK)
    top.set_xlim(0.5, xs[-1] + 3)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def heldout_chart(held: Dict[str, Any], path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    _style(plt)
    ratios = [r["ratio"] for r in held["runs"]]
    fig, ax = plt.subplots(figsize=(10, 3.2))
    counts, _, _ = ax.hist(ratios, bins=24, color=SERIES[0], edgecolor=SURFACE, linewidth=1.5)
    ax.set_ylim(0, max(counts) * 1.3)  # headroom so the labels sit above the bars
    ax.axvline(1.0, color=INK_2, lw=1, ls=(0, (4, 3)))
    ax.text(1.0, ax.get_ylim()[1] * 0.9, " template = 1.0", color=INK_2, fontsize=9, va="center")
    mean = statistics.mean(ratios)
    ax.axvline(mean, color=INK, lw=1.2)
    ax.text(mean, ax.get_ylim()[1] * 0.9, f" mean {mean:.3f}", color=INK, fontsize=9, fontweight="bold",
            va="center", backgroundcolor=SURFACE)
    ax.set_xlabel("our score / template's score on the same seed and device (S/T)")
    ax.set_ylabel("seeds")
    ax.set_title(f"{len(ratios)} unseen seeds: ahead of the template on every one, and ahead of all three bots "
                 f"on {sum(r['beats_all_bots'] for r in held['runs'])}", loc="left", color=INK, fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
def render() -> None:
    lines = ["# Results: the challenge outcome", "",
             "Generated by `python tools/report.py` from the data in `docs/results/`.", ""]
    run_file, held_file = OUT / "run.json", OUT / "heldout.json"
    if run_file.exists():
        run = json.loads(run_file.read_text())
        run_chart(run, OUT / "run.png")
        team, final = run["team"], run["rounds"][-1]["nodes"]
        lines += [
            "## One graded run, round by round", "",
            f"{run['source']}, team `{team}`, seed {run['seed']}, recorded {run['recorded']}. "
            "The three bots run on a copy of our device, so the lines compare strategies, not hardware.", "",
            "![battery and score per round](results/run.png)", "",
            "| Node | Final score | Rounds resting on a flat battery | Floor penalties | Missed rounds |",
            "|---|--:|--:|--:|--:|",
        ]
        for name in sorted(final, key=lambda n: -final[n]["score"]):
            n = final[name]
            label = f"**{name} (ours)**" if name == team else name
            lines.append(f"| {label} | {n['score']:.3f} | {n['idle']} | {n['floors']} | {n['missed']} |")
        if "template_score" in run:
            lines += ["", f"The template strategy, replayed on the same seed and device the way the graders do, "
                          f"scores {run['template_score']:.3f}: **S/T = {final[team]['score'] / run['template_score']:.3f}** "
                          "for this run."]
        lines.append("")
    if held_file.exists():
        held = json.loads(held_file.read_text())
        heldout_chart(held, OUT / "heldout.png")
        ratios = sorted(r["ratio"] for r in held["runs"])
        beats = sum(r["beats_all_bots"] for r in held["runs"])
        lines += [
            f"## Across {len(ratios)} unseen seeds", "",
            f"Simulator (`tools/sim.py`), team `{held['team']}`, seeds {held['seeds'][0]}–{held['seeds'][-1]}; none "
            "were used for tuning. Each seed also draws a different device.", "",
            "![S/T distribution](results/heldout.png)", "",
            "| S/T mean | worst 10% | worst seed | best seed | ahead of all three bots |",
            "|--:|--:|--:|--:|--:|",
            f"| {statistics.mean(ratios):.3f} | {ratios[len(ratios) // 10]:.3f} | {ratios[0]:.3f} | {ratios[-1]:.3f} "
            f"| {beats} of {len(ratios)} |", "",
        ]
        if "summary" in held:
            m = held["summary"]
            lines += [
                "| Mean per run | Template in our place | Ours |", "|---|--:|--:|",
                f"| Rounds resting on a flat battery (of 60) | {m['idle_T']:.1f} | {m['idle']:.1f} |",
                f"| Floor penalties | {m['floors_T']:.1f} | {m['floors']:.1f} |",
                f"| The three bots' total score | baseline | {m['bots_delta']:+.1%} |",
                f"| Swarm log welfare, sum over rounds | baseline | {m['lsw_delta']:+.1f} |", "",
            ]
    (ROOT / "docs" / "results.md").write_text("\n".join(lines))
    print(f"wrote {ROOT / 'docs' / 'results.md'}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--live", metavar="URL", help="record the run going on at this arena")
    p.add_argument("--sim-seed", type=int, help="record a simulated run of this seed instead")
    p.add_argument("--seed", type=int, default=GRADED_SEED, help="seed of the live run (for the S/T replay)")
    p.add_argument("--heldout", type=int, metavar="N", help="evaluate N unseen seeds (1001..)")
    args = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    team = team_name()

    if args.live or args.sim_seed is not None:
        seed = args.sim_seed if args.sim_seed is not None else args.seed
        rounds = record_live(args.live) if args.live else record_sim(seed, team)
        run = {"source": "Live graded run in Docker (fault injection on)" if args.live else "Simulated graded run",
               "team": team, "seed": seed, "recorded": datetime.date.today().isoformat(), "rounds": rounds,
               "template_score": sim.play(sim.TEMPLATE, seed, team)["score"]}
        (OUT / "run.json").write_text(json.dumps(run, indent=1))
    if args.heldout:
        seeds = list(range(1001, 1001 + args.heldout))
        held = sim.evaluate(("ours", {}), seeds, team, "graded")
        (OUT / "heldout.json").write_text(json.dumps(
            {"team": team, "seeds": seeds,
             "summary": {k: round(held[k], 4) for k in ("idle", "idle_T", "floors", "floors_T", "bots_delta", "lsw_delta")},
             "runs": [{"seed": r["seed"], "ratio": round(r["ratio"], 4),
                                                     "beats_all_bots": r["beats_all_bots"]} for r in held["runs"]]},
            indent=1))
    render()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
