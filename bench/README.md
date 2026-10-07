# Edge vs cloud decisions

`python bench/run.py` plays the same scripted workload twice per seed:

* **Baseline:** the node ships its raw observations to a cloud service (`cloud.py`), which
  decides and returns the bid.
* **Ours:** the node decides itself.

Both run the same planner (`agent-template/strategy.py`), so with the cloud up they bid
identically. **Only where the decision runs differs.**

| Setting | Value |
|---|---|
| Workload | the organisers' arena on the graded scenario, fault injection on |
| Field | the three baseline bots |
| Rounds | 60 of 2 s (graded uses 4 s, halved to fit 3 runs) |
| Outage | the cloud refuses connections for 30 s from round 15 |
| Seeds | three: 101, 202, 303 |

In the baseline, `agent.py`'s own fallback bids a weight split whenever the cloud is unreachable.

**Results:** [`results/results.md`](results/results.md) (table), [`results/chart.png`](results/chart.png)
(chart), [`results/raw.json`](results/raw.json) (per run).

```bash
pip install -e . -r bench/requirements.txt -r agent-template/requirements.txt
python bench/run.py               # ~15 min
python bench/run.py --report-only # re-render the table and chart
```

## What the numbers do and don't show

Deciding at the edge cut what the node transmits about 10× (109 vs 1,129 bytes per round).
It also kept every round planner-decided through the 30-second outage, which earned 1.1 more
points during the outage and 1.3 more (+7%) in total, mean over three seeds. The outage
result holds by construction, because our agent has no cloud in its decision path.

They do not show a latency advantage. With the "cloud" on loopback and no WAN round-trip,
both placements take about 0.23–0.24 s at the median, because the ~0.1 s battery planner
dominates. Three runs of one scripted workload are also too few to claim the score gap
generalises beyond outages like this one.
