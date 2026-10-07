"""
The agent main loop.

THIS FILE IS GIVEN TO YOU AND IT WORKS. Out of the box it registers, keeps its
lease alive, submits a bid every round, reads back the result, and survives the
faults the graded arena throws at it.

You are not expected to change it. The challenge lives in strategy.py.

Our changes (marked "ours" below):
  * Results are fetched for the PREVIOUS round. The shipped loop only ever asked
    for the round it had just bid in, which is never settled yet - the arena
    opens the next round in the same instant it settles one - so every
    /v1/result call returned 404 and `history` stayed empty for the whole run.
  * The strategy is also told the round number and the arena's parameters
    (total_rounds, battery_cutoff), through `profile["round"]` / `profile["arena"]`.

Run it:
    ARENA_URL=http://localhost:8080 TEAM_NAME=team-kappa python agent.py

Copyright 2026 The CoGNETs Consortium
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from client import (
    AlreadyBid,
    ArenaClient,
    ArenaClientError,
    Ejected,
    NoRound,
    NotAdmissible,
    RoundClosed,
    WrongRound,
)
from strategy import decide_bid

LOG = logging.getLogger("agent")

ARENA_URL = os.environ.get("ARENA_URL", "http://localhost:8080")
TEAM_NAME = os.environ.get("TEAM_NAME", "unnamed-team")
# How long the bid may wait for last round's result (ours). The fetch runs on its
# own thread, so a faulted fetch (the client backs off ~1 s on a 503) never holds
# the bid back for longer than this.
PREFETCH_WAIT_SECONDS = float(os.environ.get("PREFETCH_WAIT_SECONDS", "0.3"))

_stop = threading.Event()


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------
def heartbeat_loop(client: ArenaClient, interval: float) -> None:
    """Renew the lease on its own schedule.

    Deliberately a separate thread. If you heartbeat only inside the bidding
    loop, then one slow round - or one round you sit out because your battery
    is flat - lets your lease expire, and you silently stop being scored. This
    is the single most common way teams lose points.
    """
    LOG.info("heartbeat every %.1fs", interval)
    while not _stop.is_set():
        try:
            client.heartbeat()
        except Ejected:
            LOG.error("ejected from the run; heartbeat thread stopping")
            return
        except ArenaClientError as exc:
            LOG.warning("heartbeat failed: %s", exc)
        _stop.wait(interval)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run() -> int:
    client = ArenaClient(ARENA_URL, TEAM_NAME)

    LOG.info("connecting to %s as '%s'", ARENA_URL, TEAM_NAME)
    if not client.wait_for_arena(timeout=90.0):
        LOG.error("arena at %s never became healthy", ARENA_URL)
        return 1

    client.register()

    lease = float(client.arena_info.get("lease_seconds", 12.0))
    total_rounds = int(client.arena_info.get("total_rounds", 0))
    hb = threading.Thread(
        target=heartbeat_loop, args=(client, max(1.0, lease / 3.0)), daemon=True
    )
    hb.start()

    history: List[Dict[str, Any]] = []
    last_bid_round = 0
    to_collect: List[int] = []  # ours: rounds we bid in whose result we have not read
    idle_polls = 0

    while not _stop.is_set():
        # ---------------------------------------------------------- read round
        try:
            rnd = client.get_round()
        except NoRound:
            idle_polls += 1
            if idle_polls % 20 == 1:
                LOG.info("no round open yet, waiting")
            _stop.wait(0.5)
            continue
        except Ejected as exc:
            LOG.error("ejected: %s", exc)
            return 2
        except ArenaClientError as exc:
            LOG.warning("could not read the round: %s", exc)
            _stop.wait(1.0)
            continue

        idle_polls = 0
        round_index = int(rnd["round"])

        # ------------------------------------------------------ fresh arena?
        # A restarted arena counts from round 1 again. Everything below is keyed
        # off the round number, and `history` describes a run that no longer
        # exists, so both are dropped when the counter goes backwards. If you
        # write your own loop, keep this - without it an agent sits out every
        # round of the new run up to the number the old one reached.
        if round_index < last_bid_round:
            LOG.info(
                "round counter went backwards (%d -> %d): a new run has "
                "started on this arena, resetting per-run state",
                last_bid_round,
                round_index,
            )
            last_bid_round = 0
            to_collect.clear()
            history.clear()

        # ----------------------------------------------------- already done?
        # Bidding comes BEFORE fetching the previous result. Reading results is
        # bookkeeping; missing the bidding window is a lost round. Never put
        # anything that can block in front of the bid.
        if round_index <= last_bid_round or rnd.get("settled"):
            _collect_results(client, history, to_collect, round_index, rnd.get("settled"))
            _stop.wait(min(0.3, max(0.05, float(rnd.get("seconds_remaining", 0.3)))))
            continue

        you = rnd.get("you") or {}
        if you.get("already_submitted"):
            last_bid_round = round_index
            continue
        if you.get("admissible") is False:
            LOG.info("round %d: not admissible (resting), sitting it out",
                     round_index)
            last_bid_round = round_index
            _stop.wait(0.4)
            continue

        # ----------------------------------------- last round's result (ours)
        # It settled the moment this round opened. Reading it first gives the
        # strategy an up-to-date history - if it arrives in time. If not, the
        # strategy bids on the history it has and the result is read afterwards.
        _prefetch_result(client, history, to_collect, round_index)

        # ------------------------------------------------------------- decide
        budget = float(rnd.get("budget", 0.0))
        try:
            bid = decide_bid(
                budget=budget,
                prices=rnd.get("prices", {}),
                capacities=rnd.get("capacities", {}),
                profile={**client.profile, "arena": client.arena_info,
                         "round": round_index},  # ours
                history=history,
            )
        except Exception:
            # A crash in your strategy must never kill the agent. Falling back
            # to a weight-proportional bid costs you a little score; crashing
            # costs you the whole run.
            LOG.exception("strategy raised; falling back to a proportional bid")
            weights = client.profile.get("weights", {})
            bid = {k: budget * float(weights.get(k, 1 / 3)) for k in
                   ("compute", "energy", "security")}

        bid = _sanitise(bid, budget)

        # ---------------------------------------------------------------- bid
        try:
            ack = client.post_bid(round_index, bid)
            last_bid_round = round_index
            to_collect[:] = to_collect[-4:] + [round_index]  # ours
            if ack.get("warnings"):
                LOG.warning("round %d accepted with warnings: %s",
                            round_index, ack["warnings"])
            LOG.debug("round %d bid %s", round_index, _fmt_bundle(bid))
        except AlreadyBid:
            last_bid_round = round_index
        except (RoundClosed, WrongRound) as exc:
            LOG.info("round %d: %s", round_index, exc)
            last_bid_round = round_index
        except NotAdmissible as exc:
            LOG.info("round %d: %s", round_index, exc)
            last_bid_round = round_index
        except Ejected as exc:
            LOG.error("ejected: %s", exc)
            return 2
        except ArenaClientError as exc:
            LOG.warning("round %d: bid failed: %s", round_index, exc)

        # ------------------------------------------------------------- finish
        if total_rounds and round_index >= total_rounds:
            LOG.info("final round submitted; waiting for it to settle")
            _stop.wait(3.0)
            try:
                final = client.me()
                LOG.info(
                    "FINAL  score=%.3f  rounds=%d  missed=%d  floors=%d  kappa=%.2f",
                    final.get("score", 0.0),
                    final.get("rounds_participated", 0),
                    final.get("rounds_missed", 0),
                    final.get("floor_violations", 0),
                    final.get("compromise", 0.0),
                )
            except ArenaClientError:
                pass
            break

        _stop.wait(0.25)

    client.close()
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _prefetch_result(client: ArenaClient, history: List[Dict[str, Any]],
                     to_collect: List[int], open_round: int) -> None:
    """Fetch the latest settled result, waiting at most PREFETCH_WAIT_SECONDS (ours)."""
    settled = [r for r in to_collect if r < open_round]
    if not settled:
        return
    box: List[Dict[str, Any]] = []

    def fetch() -> None:
        try:
            box.append(client.get_result(settled[-1], attempts=1))
        except ArenaClientError:
            pass

    worker = threading.Thread(target=fetch, daemon=True)
    worker.start()
    worker.join(PREFETCH_WAIT_SECONDS)
    if box:  # read in time; otherwise _collect_results picks it up after the bid
        _record(history, to_collect, box[0])


def _collect_results(client: ArenaClient, history: List[Dict[str, Any]],
                     to_collect: List[int], open_round: int, run_over: Any) -> None:
    """Pull our settled results into `history` (ours). Best-effort, one attempt each.

    A round is settled once a later round is open, or once the run is over.
    """
    for round_no in sorted(r for r in to_collect if r < open_round or (run_over and r <= open_round)):
        try:
            result = client.get_result(round_no, attempts=1)
        except NoRound:
            continue
        except ArenaClientError:
            return  # faulted; try again on the next poll
        _record(history, to_collect, result)


def _record(history: List[Dict[str, Any]], to_collect: List[int], result: Dict[str, Any]) -> None:
    """Move one settled result from `to_collect` into `history` (ours)."""
    round_no = int(result.get("round", 0))
    if round_no in to_collect:
        to_collect.remove(round_no)
    if not result.get("participated") or any(int(h["round"]) == round_no for h in history):
        return
    history.append(result)
    history.sort(key=lambda h: int(h.get("round", 0)))
    LOG.info(
        "round %d  spend=%.3f  alloc=%s  utility=%.4f%s  battery=%.2f  total=%.3f",
        result["round"],
        result.get("spend", 0.0),
        _fmt_bundle(result.get("allocation", {})),
        result.get("utility", 0.0),
        _fmt_violations(result.get("floor_violations")),
        result.get("battery", 0.0),
        result.get("cumulative_score", 0.0),
    )


def _sanitise(bid: Any, budget: float) -> Dict[str, float]:
    """Last line of defence before a bid leaves the process.

    The arena raises your compromise score for negative, non-numeric or
    over-budget bids, and ejects you if it happens often enough. Clamping here
    means a strategy bug costs you score rather than the run.
    """
    resources = ("compute", "energy", "security")
    clean: Dict[str, float] = {}
    for k in resources:
        try:
            v = float((bid or {}).get(k, 0.0))
        except (TypeError, ValueError, AttributeError):
            v = 0.0
        if v != v or v in (float("inf"), float("-inf")) or v < 0.0:
            v = 0.0
        clean[k] = v
    total = sum(clean.values())
    if total > budget and total > 0:
        scale = budget / total
        clean = {k: v * scale for k, v in clean.items()}
    return clean


def _fmt_bundle(bundle: Dict[str, float]) -> str:
    return " ".join(f"{k[:3]}={float(v):.3f}" for k, v in sorted(bundle.items()))


def _fmt_violations(violations: Optional[List[str]]) -> str:
    return f"  MISSED:{','.join(violations)}" if violations else ""


def _handle_signal(signum: int, _frame: object) -> None:
    LOG.info("signal %d received, shutting down", signum)
    _stop.set()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        return run()
    except KeyboardInterrupt:
        return 0
    finally:
        _stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
