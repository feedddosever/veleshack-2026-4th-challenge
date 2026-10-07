# Pitch: three slides on the organisers' template

The template has a title slide and three content slides with fixed titles (GitHub repo, Summary,
Highlights). The pitch is mapped onto them like this: problem, user and solution go on
**Summary**; live architecture, proof number and roadmap go on **Highlights**. The filled deck is
submitted on taikai and is not kept in this repository.

## Title: Second Wind

- **Subtitle:** An edge agent that knows when to rest. VelesHack 2026, Challenge 4 (CoGNETs), team
  `feed_ddos_ever`.
- **Speaker note:** "We built the node that wins by knowing when to sleep."

## 1. GitHub repo

- **Link:** github.com/feedddosever/veleshack-2026-4th-challenge
- **Run it:** `cp .env.example .env` (set `TEAM_NAME`), then `make up`, then `make agent`
- **Submission:** `agent-template/Dockerfile`. Strategy in `strategy.py`; simulator in `tools/`;
  edge-vs-cloud benchmark in `bench/`
- **Team / `TEAM_NAME`:** `feed_ddos_ever`
- **Speaker note:** "Everything we show is in this repo and runs with three commands."

## 2. Summary: problem, user, solution

- **Problem:** in a CoGNETs swarm, the energy a node wins drains its own battery. A flat node sits
  out whole rounds, and the baseline bots lose a third of every run that way.
- **User:** the operator of a battery-powered edge device that buys shared resources on its own,
  with no central scheduler and no guaranteed cloud.
- **Solution:** an agent that reads the market exactly, buys its service floors first, and plans its
  battery with a dynamic programme, choosing when to spend energy and when to rest.
- **Speaker note:** "The bots treat energy as free; we treat the battery as a budget over time, and
  rest on purpose."

## 3. Highlights: live architecture, proof number, roadmap

- **Live architecture:** arena (registry, Kelly auction) → round data → our agent: field estimate →
  utility U(x) → battery DP → bid of 3 numbers. All decisions are made on the node.
- **Proof number:** 1.33× the template's score on 200 unseen seeds, ahead of all three bots on
  198/200 (a live graded run: 20.05 vs the best bot's 13.33). With the cloud cut for 30 s, 100% vs
  4% of rounds are still decided (edge vs cloud), with 10× fewer bytes.
- **Roadmap:** a cooperative mode that also optimises swarm welfare, learning recharge online, a
  Raspberry Pi test, and Zenoh discovery in the full CoGNETs stack.
- **Speaker note:** "1.33 times the template, 198 out of 200 seeds, and it keeps deciding when the
  cloud is gone."
