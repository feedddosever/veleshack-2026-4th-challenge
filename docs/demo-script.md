# Demo script: 2 minutes, shot by shot

About 250 spoken words; at a normal pace that is 1:55–2:00.

**Before recording:**
- A graded run takes 4 minutes, so **pre-record** shots 2 and 3: set `SCENARIO=graded` and
  `TEAM_NAME=feed_ddos_ever` in `.env`, then run `make up` and `make agent`. The recorded run in
  `docs/results.md` scored 20.05 against the bots' 11.7–13.3.
- Terminal at 18 pt or larger.
- Open tabs: the leaderboard (`localhost:8080`), the README's Results section, and
  `bench/results/chart.png`.

| # | Time | On screen | You say |
|---|---|---|---|
| 1 | 0:00–0:12 | Title slide: **Second Wind** | "Every node in a CoGNETs swarm buys compute, energy and security each round, with no central scheduler. The catch: the energy you win drains your own battery, and a flat battery takes you out of the swarm." |
| 2 | 0:12–0:27 | Terminal: `make up`, then `make agent`. Zoom on one log line: `plan r12: charge 0.43 -> 0.41, energy share 0.05 ...` | "This is our agent: one container, three commands. Every decision happens here, on the node. Each round it reads the market, prices its options and plans its battery." |
| 3 | 0:27–0:45 | Leaderboard near the end of the graded run. Point at the bots' `idle` rounds, then at ours | "The three bots never think about their battery, so they spend a third of every run asleep. Ours drains on purpose when energy is cheap, rests for one round, and saves the rest for the end of the run." |
| 4 | 0:45–1:05 | README → Results: held-out table, then the ablation table | "We measured every idea against the template on 200 seeds we never tuned on. The battery plan is the whole game: 1.33 times the template's score, ahead of all three bots on 198 of 200. The exact Kelly split? A tie with bidding your weights. That's a negative result, and we kept it." |
| 5 | 1:05–1:22 | `agent-template/agent.py` diff (the "ours" comments), then conformance output: `functional 30.0/30, resilience 20.0/20` | "Measuring also found two bugs in the shipped loop. It never received a single round result, and it slept away whole rounds on injected faults. Fixed: 30 of 30 functional, 20 of 20 resilience." |
| 6 | 1:22–1:45 | `bench/results/chart.png` | "Why decide at the edge? Same workload, same planner, run in the cloud versus on the node, and then we cut the cloud for 30 seconds. On the node we send ten times fewer bytes, and nothing breaks." |
| 7 | 1:45–2:00 | Full-screen number: **100% vs 4%**, captioned "rounds decided during a 30 s cloud outage, edge vs cloud" | "During the outage, our agent still decided 100 percent of its rounds. The cloud-dependent version: 4 percent." |

**Where each number comes from:**
- 1.33× and 198/200: `python3 tools/report.py --heldout 200`, which writes `docs/results.md`
- 30/30 and 20/20: `make check-docker`
- 10× (109 vs 1,129 bytes per round) and 100% vs 4%: `bench/results/results.md`

Don't claim a latency win: the benchmark shows none.
