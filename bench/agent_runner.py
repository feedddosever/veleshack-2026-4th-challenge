"""Runs our agent loop (agent-template/agent.py) in one of two configurations, and measures it.

    DECISIONS=edge   strategy.decide_bid runs inside the agent (our design)
    DECISIONS=cloud  the agent ships its raw observations to bench/cloud.py and waits for the
                     bid. If the cloud is unreachable, agent.py's own fallback bids a plain
                     weight split, exactly as it would for any strategy error.

Measured, written to $BENCH_OUT as JSON when the run ends:
  * payload bytes to/from the arena and to/from the cloud (JSON bodies; HTTP headers excluded)
  * per round: decision source, and latency from first seeing the round to the bid accepted

Copyright 2026 The CoGNETs Consortium (template); benchmark by Ekaterina Fedoseeva.
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-template"))

import agent  # noqa: E402
import client  # noqa: E402
import strategy  # noqa: E402

MODE = os.environ["DECISIONS"]
CLOUD_URL = os.environ.get("CLOUD_URL", "http://127.0.0.1:9000")
CLOUD_TIMEOUT = 1.0
stats = {"mode": MODE, "bytes": {"arena_up": 0, "arena_down": 0, "cloud_up": 0, "cloud_down": 0},
         "rounds": {}}
seen_at: dict = {}


def record(round_no: int, **fields) -> None:
    stats["rounds"].setdefault(str(round_no), {}).update(fields)


# --- what goes over the wire to the arena --------------------------------------------
_request = client.ArenaClient._request


def counted_request(self, method, path, *, json=None, auth=True, allow_reregister=True):
    if json is not None:
        stats["bytes"]["arena_up"] += len(_dumps(json))
    response = _request(self, method, path, json=json, auth=auth, allow_reregister=allow_reregister)
    stats["bytes"]["arena_down"] += len(_dumps(response))
    return response


# --- latency: first sight of a round -> bid accepted ------------------------------------
_get_round, _post_bid = client.ArenaClient.get_round, client.ArenaClient.post_bid


def get_round(self):
    payload = _get_round(self)
    seen_at.setdefault(int(payload["round"]), time.time())
    return payload


def post_bid(self, round_index, bid):
    ack = _post_bid(self, round_index, bid)
    record(round_index, latency_ms=(time.time() - seen_at[round_index]) * 1000.0)
    return ack


# --- the two ways of deciding ------------------------------------------------------------
def edge_decide(budget, prices, capacities, profile, history):
    bid = strategy.decide_bid(budget, prices, capacities, profile, history)
    record(profile["round"], source="planner")
    return bid


sent_rounds: set = set()


def cloud_decide(budget, prices, capacities, profile, history):
    new = [h for h in history if h["round"] not in sent_rounds]
    body = _dumps({"node": agent.TEAM_NAME, "budget": budget, "prices": prices,
                   "capacities": capacities, "profile": profile, "results": new})
    request = urllib.request.Request(f"{CLOUD_URL}/decide", data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=CLOUD_TIMEOUT) as response:
            reply = response.read()
    except OSError:
        record(profile["round"], source="fallback")
        raise  # agent.py logs it and bids a weight split instead
    stats["bytes"]["cloud_up"] += len(body)
    stats["bytes"]["cloud_down"] += len(reply)
    sent_rounds.update(h["round"] for h in new)
    record(profile["round"], source="planner")
    return json.loads(reply)["bid"]


def _dumps(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


def main() -> int:
    client.ArenaClient._request = counted_request
    client.ArenaClient.get_round = get_round
    client.ArenaClient.post_bid = post_bid
    agent.decide_bid = edge_decide if MODE == "edge" else cloud_decide
    try:
        return agent.main()
    finally:
        Path(os.environ["BENCH_OUT"]).write_text(json.dumps(stats))


if __name__ == "__main__":
    raise SystemExit(main())
