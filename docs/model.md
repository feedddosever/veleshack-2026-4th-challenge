# The data source and the model

## Data source

There is no dataset in this challenge. The data source is the organisers' **arena**, which
we consume only through its REST API (`/v1/register`, `/v1/heartbeat`, `/v1/round`,
`/v1/bid`, `/v1/result/{round}`). Everything below was read from the arena's source in
`arena/` and then checked against live runs. Nothing is hardcoded against seeds.

## The rules, exactly

| What | Rule | Source |
|---|---|---|
| Allocation | `x_k = C_k · b_k / Σ_j b_j^k` | `game.kelly_allocation` |
| Utility | `u = (Σ_k w_k √x_k)²`, halved per missed floor (`x_C < q_min`, `x_S < s_min`) | `game.ces_utility`, `apply_floors` |
| Published price | `λ_k = Σ_j b_j^k / C_k` of the **previous** round, floored at 0.01 | `state.open_round` |
| Budget | **one** draw per round shared by every node, `W ~ U(0.75, 1.25)`, never carried over | `state._tick_devices` |
| Capacities | `C_k ~ U(0.7, 1.3)` per pool per round | `state.open_round` |
| Battery, taking part | `battery -= 0.30 · x_E · (1 + mobility) + 0.004` | `state._discharge` |
| Battery, sitting out | `battery = min(1, battery + 0.22)`; this is the **only** way to recharge | `state.settle` |
| Admissible | `battery > 0.05`, `κ < 0.60`, not ejected | `NodeState.admissible` |
| Compromise κ | `+0.05` per malformed, negative or over-budget bid, ejected at 0.60 | `state.penalise` |
| Device | drawn from `Random(f"{seed}:{team}")`. The bots are given a **copy of the first participant's device**, battery included | `state._mint_node`, `_share_device_with_baselines` |
| Graded run | 60 rounds × 4 s. 8% 503 and 4% 429 on the agent's critical path; up to 250 ms latency on `/v1/bid` | `scenarios/graded.yaml`, `chaos.py` |
| Bots | never faulted; exempt from lease expiry | `chaos.py`, `state.expire_leases` |

## Two findings that change the strategy

1. **With the shipped loop, `history` is always empty in a live arena.** `agent.py` and
   `runner.py` ask for the result of the round they have just bid in. That round is never
   settled yet, because the arena opens the next round in the same instant it settles one.
   In a live run we measured 21 of 21 `/v1/result` calls returning 404 and 0 rounds recorded.
   Two consequences:
   - The bots never see their own history, so `proportional` always assumes a field of 1.0
     when it buys its floors.
   - Our `agent.py` is fixed: it reads the *previous* round's result, before bidding when the
     window leaves more than 2.5 s, and otherwise straight after.

2. **The primer's estimator for the rest of the field mixes two rounds.** It multiplies last
   round's price by the capacity of the round after it. The exact estimate is
   `S_k = λ_k(now) · C_k(last round) − our last bid`. Because the budget is common, it is then
   rescaled by `W_now / W_then`.

## The simulator (`tools/sim.py`) and its assumptions

`tools/sim.py` drives `arena.state.Arena` in-process with the three real bot strategies. A
60-round run takes milliseconds of game logic, so strategies are compared over hundreds of
seeds. It scores the way the grading harness does: our run (S), then a replay with the
template strategy in our place on the same seed and device (T).

| Assumption | Why it is faithful (or not) |
|---|---|
| Bots get an empty history | Matches the live arena (finding 1) |
| Our agent sees every result before its next bid | Live, it does when the window has more than 2.5 s left (graded rounds have about 3.7 s on arrival). Otherwise it is one round late, which the strategy handles |
| No faults, latency or missed rounds | Those cost resilience points, not strategy points. `make check` covers them |
| Seeds 1..N with team `team-sim` | The graded seed is unpublished. Each seed also draws a different device, so N seeds means N devices |
| R (the reference agent) is not available | We report S/T, and the margin over the best bot |

**Validation.**
- naive-max against the template scores exactly **1.000** (it *is* the template).
- naive-max spends **18.9 of 60 rounds** idle, matching the README's "roughly a third of the run".
- The strongest check is a live graded run in Docker (seed 20261006, team `team-dev-check`,
  98 injected faults). The simulator reproduces every node's final score to three decimals:

| node | live arena | simulator |
|---|--:|--:|
| bot-proportional | 19.174 | 19.1745 |
| bot-even-split | 12.604 | 12.6038 |
| bot-naive-max | 12.414 | 12.4144 |
| our agent (template strategy) | 12.414 | 12.414 |
