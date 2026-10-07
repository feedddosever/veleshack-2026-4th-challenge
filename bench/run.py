"""Benchmark: decisions at the edge (ours) vs decisions in the cloud (baseline).

Same scripted workload for both: the organisers' arena on the graded scenario (fault
injection on), the three baseline bots, one agent, and a 30-second cloud outage that
starts at round 15. Each run plays both configurations on the same seed; runs use
different seeds.

    baseline (cloud)  the node ships its raw observations - round data, profile, every new
                      result - to bench/cloud.py, which decides the bid and sends it back
    ours (edge)       the node decides; only the bid itself leaves it

    python bench/run.py                     # 3 runs x 2 configurations, ~15 minutes
    python bench/run.py --runs 1 --round-seconds 1.0 --rounds 30   # quick check
    python bench/run.py --report-only       # re-render the table and chart from raw.json

Writes bench/results/results.md, chart.png and raw.json.

Copyright 2026 The CoGNETs Consortium (template); benchmark by Ekaterina Fedoseeva.
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "bench" / "results"
sys.path.insert(0, str(ROOT / "bench"))

from cloud import CloudDecider  # noqa: E402

CONFIGS = {"cloud": "Baseline: decided in the cloud", "edge": "Ours: decided at the edge"}
TEAM = "bench-team"
SEEDS = [101, 202, 303, 404, 505]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get_json(url: str) -> Any:
    for _ in range(20):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                return json.loads(response.read())
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"no answer from {url}")


def play(mode: str, seed: int, args: argparse.Namespace, workdir: Path) -> Dict[str, Any]:
    """One full arena run with one agent in `mode`; returns its measurements."""
    port, cloud_port = free_port(), free_port()
    arena_url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "ARENA_SCENARIO": "graded", "ARENA_SEED": str(seed),
           "ARENA_ROUND_SECONDS": str(args.round_seconds), "ARENA_TOTAL_ROUNDS": str(args.rounds),
           "ARENA_LEASE_SECONDS": str(max(6.0, 3 * args.round_seconds)), "ARENA_START_DELAY": "4",
           "LOG_LEVEL": "WARNING"}
    log = open(workdir / f"{mode}-{seed}.log", "w")
    procs = [subprocess.Popen([sys.executable, "-m", "arena", "--port", str(port), "--log-level", "warning"],
                              cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)]
    get_json(f"{arena_url}/healthz")
    for bot in ("naive-max", "even-split", "proportional"):
        procs.append(subprocess.Popen([sys.executable, "baselines/bot.py"], cwd=ROOT, stdout=log,
                                      stderr=subprocess.STDOUT,
                                      env={**env, "BOT": bot, "TEAM_NAME": f"bot-{bot}", "ARENA_URL": arena_url}))
    cloud = CloudDecider(cloud_port)
    cloud.start()
    out = workdir / f"{mode}-{seed}.json"
    agent = subprocess.Popen([sys.executable, "bench/agent_runner.py"], cwd=ROOT, stdout=log,
                             stderr=subprocess.STDOUT,
                             env={**env, "DECISIONS": mode, "ARENA_URL": arena_url, "TEAM_NAME": TEAM,
                                  "CLOUD_URL": f"http://127.0.0.1:{cloud_port}", "BENCH_OUT": str(out)})
    procs.append(agent)

    score_at: Dict[int, float] = {}  # our cumulative score when round r opened
    outage = {"from": None, "to": None, "cut_at": 0.0}
    try:
        while True:
            board = get_json(f"{arena_url}/v1/leaderboard")
            status = board["status"]
            now_round = int(status["round"])
            me = next((r for r in board["leaderboard"] if r["team"] == TEAM), None)
            if me:
                score_at.setdefault(now_round, me["score"])
            if outage["from"] is None and now_round >= args.outage_round:
                cloud.stop()  # the cloud becomes unreachable (connections refused)
                outage.update({"from": now_round, "cut_at": time.time()})
            if outage["from"] and outage["to"] is None and time.time() - outage["cut_at"] >= args.outage_seconds:
                cloud.start()
                outage["to"] = now_round
            if status["finished"]:
                break
            time.sleep(0.2)
        agent.wait(timeout=30)
    finally:
        cloud.stop()
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            p.wait(timeout=10)
        log.close()

    final = {r["team"]: r for r in board["leaderboard"]}
    me, stats = final[TEAM], json.loads(out.read_text())
    rounds = {int(k): v for k, v in stats["rounds"].items()}
    latencies = sorted(v["latency_ms"] for v in rounds.values() if "latency_ms" in v)
    window = range(outage["from"], outage["to"] or args.rounds + 1)
    in_outage = [rounds.get(r, {}) for r in window]
    participated = max(me["rounds_participated"], 1)
    b = stats["bytes"]
    return {
        "mode": mode, "seed": seed,
        "uplink_bytes_per_round": (b["arena_up"] + b["cloud_up"]) / participated,
        "decision_bytes_per_round": (b["cloud_up"] + b["cloud_down"]) / participated,
        "latency_p50_ms": latencies[len(latencies) // 2],
        "latency_p95_ms": latencies[int(0.95 * (len(latencies) - 1))],
        "outage_rounds": len(window),
        "outage_planner_share": sum(r.get("source") == "planner" for r in in_outage) / max(len(window), 1),
        "outage_fallback_rounds": sum(r.get("source") == "fallback" for r in in_outage),
        "score_in_outage": score_at.get(window.stop, me["score"]) - score_at.get(window.start, 0.0),
        "score_after_outage": me["score"] - score_at.get(window.stop, me["score"]),
        "score_total": me["score"],
        "rounds_idle": me["rounds_idle"],
        "rounds_missed": me["rounds_missed"],
        "kappa": me["compromise"],
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
ROWS = [  # key, label, format
    ("uplink_bytes_per_round", "Bytes sent by the node per round (arena + cloud)", "{:.0f}"),
    ("decision_bytes_per_round", "...of which decision traffic to/from the cloud", "{:.0f}"),
    ("latency_p50_ms", "Decision latency p50, round seen -> bid accepted (ms)", "{:.0f}"),
    ("latency_p95_ms", "Decision latency p95 (ms)", "{:.0f}"),
    ("outage_planner_share", "Outage: rounds decided by the planner", "{:.0%}"),
    ("outage_fallback_rounds", "Outage: rounds on the blind fallback bid", "{:.1f}"),
    ("score_in_outage", "Score earned during the outage", "{:.2f}"),
    ("score_after_outage", "Score earned after the outage", "{:.2f}"),
    ("score_total", "Total score", "{:.2f}"),
    ("rounds_idle", "Rounds lost resting on a flat battery", "{:.1f}"),
    ("rounds_missed", "Rounds missed (awake, no bid)", "{:.1f}"),
]


def summarise(runs: List[Dict[str, Any]], key: str, fmt: str) -> str:
    values = [r[key] for r in runs]
    mean, lo, hi = statistics.mean(values), min(values), max(values)
    return f"{fmt.format(mean)} ({fmt.format(lo)} – {fmt.format(hi)})"


def write_report(results: Dict[str, List[Dict[str, Any]]], args: argparse.Namespace) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "raw.json").write_text(json.dumps({"args": vars(args), "runs": results}, indent=1))
    seeds = ", ".join(str(r["seed"]) for r in results["edge"])
    lines = [
        "# Edge vs cloud decisions: benchmark results",
        "",
        f"{args.runs} runs (seeds {seeds}), each playing both configurations on the same seed. "
        f"Graded scenario with fault injection, {args.rounds} rounds of {args.round_seconds:g} s, "
        f"three baseline bots. The cloud is unreachable for {args.outage_seconds:g} s from round "
        f"{args.outage_round}. Values are **mean (min – max)** over the runs. Generated by "
        "`python bench/run.py`.",
        "",
        f"| Metric | {CONFIGS['cloud']} | {CONFIGS['edge']} |",
        "|---|---|---|",
    ]
    for key, label, fmt in ROWS:
        lines.append(f"| {label} | {summarise(results['cloud'], key, fmt)} | {summarise(results['edge'], key, fmt)} |")
    lines.append("| Events correctly detected | n/a: this challenge has no event ground truth | n/a |")
    lines += ["", "![chart](chart.png)", ""]
    (RESULTS / "results.md").write_text("\n".join(lines))
    chart(results)


def chart(results: Dict[str, List[Dict[str, Any]]]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker

    panels = [("uplink_bytes_per_round", "Bytes sent per round"),
              ("latency_p95_ms", "Decision latency p95 (ms)"),
              ("outage_planner_share", "Outage rounds decided by planner"),
              ("score_total", "Total score")]
    colors = {"cloud": "#9aa0a6", "edge": "#1a73e8"}
    fig, axes = plt.subplots(1, len(panels), figsize=(14, 3.6))
    for ax, (key, title) in zip(axes, panels):
        for i, mode in enumerate(("cloud", "edge")):
            values = [r[key] for r in results[mode]]
            mean = statistics.mean(values)
            ax.bar(i, mean, color=colors[mode], yerr=[[mean - min(values)], [max(values) - mean]], capsize=6)
        ax.set_xticks([0, 1], ["cloud\n(baseline)", "edge\n(ours)"])
        if key == "outage_planner_share":
            ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
        ax.set_title(title, fontsize=11)
        ax.spines[["top", "right"]].set_visible(False)
    fig.suptitle("Same workload, decision at the cloud vs at the edge (bars: mean, whiskers: min-max)",
                 fontsize=12)
    fig.tight_layout()
    fig.savefig(RESULTS / "chart.png", dpi=130)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--rounds", type=int, default=60)
    p.add_argument("--round-seconds", type=float, default=2.0)
    p.add_argument("--outage-round", type=int, default=15)
    p.add_argument("--outage-seconds", type=float, default=30.0)
    p.add_argument("--report-only", action="store_true", help="re-render from results/raw.json")
    args = p.parse_args()
    if args.report_only:
        raw = json.loads((RESULTS / "raw.json").read_text())
        write_report(raw["runs"], argparse.Namespace(**raw["args"]))
        return 0

    results: Dict[str, List[Dict[str, Any]]] = {"cloud": [], "edge": []}
    with tempfile.TemporaryDirectory(prefix="bench-") as tmp:
        for seed in SEEDS[: args.runs]:
            for mode in ("cloud", "edge"):
                r = play(mode, seed, args, Path(tmp))
                results[mode].append(r)
                print(f"seed {seed} {mode:5}: score {r['score_total']:.2f}  "
                      f"bytes/round {r['uplink_bytes_per_round']:.0f}  p50 {r['latency_p50_ms']:.0f} ms  "
                      f"outage planner {r['outage_planner_share']:.0%}", flush=True)
    write_report(results, args)
    print(f"\nwrote {RESULTS / 'results.md'} and chart.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
