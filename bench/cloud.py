"""The baseline's cloud: nodes ship it their raw observations and it decides their bids.

    POST /decide  {"node", "budget", "prices", "capacities", "profile", "results": [new ones]}
               -> {"bid": {...}}

It keeps each node's result history, so a node only sends what is new. It runs the
same planner as our edge agent (`strategy.decide_bid`), so with the cloud reachable
both configurations bid identically and only the placement of the decision differs.

`stop()` / `start()` simulate an outage: the port refuses connections while down.

Copyright 2026 The CoGNETs Consortium (template); benchmark by Ekaterina Fedoseeva.
SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-template"))

import strategy  # noqa: E402


class CloudDecider:
    def __init__(self, port: int) -> None:
        self.port = port
        self.histories: Dict[str, List[Dict[str, Any]]] = {}
        self._server: Optional[ThreadingHTTPServer] = None

    def decide(self, request: Dict[str, Any]) -> Dict[str, float]:
        history = self.histories.setdefault(request["node"], [])
        known = {h["round"] for h in history}
        history.extend(r for r in request["results"] if r["round"] not in known)
        history.sort(key=lambda h: h["round"])
        return strategy.decide_bid(request["budget"], request["prices"], request["capacities"],
                                   request["profile"], history)

    def start(self) -> None:
        cloud = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 (http.server naming)
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                reply = json.dumps({"bid": cloud.decide(body)}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args: Any) -> None:  # keep the bench output readable
                pass

        ThreadingHTTPServer.allow_reuse_address = True
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
