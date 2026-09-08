"""Control-transfer protocol for human-in-the-loop escalation (assignment 3.6).

The unit of "who is in control" is a small JSON file, `control.json`, written into
the SAME evidence directory as the run's log (src/observability/logger.py) so
context and evidence stay together across the handoff, as required. It carries:

    controller: "agent" | "operator"
    cdp_port:   how to attach to the live session (BrowserSession.attach)
    reason:     why the agent paused (only set when controller == "operator")
    context:    capability/goal, current step, screenshot path
    human_actions: appended to by the operator CLI as it acts, so the automation
                   process (and any later reviewer) can see exactly what a human did
    updated_at

Two processes touch this file: the automation process (agent/discover.py or
replay/engine.py), which writes it once to request control and then POLLS it
waiting for controller to flip back to "agent"; and the operator CLI
(src/escalation/operator.py), which reads it to find the live session, attaches,
lets a human act, appends to human_actions, then flips controller back and exits.
This file IS the seam described in 3.6: "there must be a way to know who is (or
should be) in control."
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


@dataclass
class ControlState:
    controller: str  # "agent" | "operator"
    cdp_port: int
    reason: Optional[str] = None
    context: dict[str, Any] = field(default_factory=dict)
    human_actions: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)
    resolution: Optional[str] = None  # set by operator on handback, e.g. "completed" | "abandoned"


class ControlChannel:
    def __init__(self, path: Path):
        self.path = path

    def write(self, state: ControlState) -> None:
        state.updated_at = time.time()
        self.path.write_text(json.dumps(asdict(state), indent=2))

    def read(self) -> ControlState:
        data = json.loads(self.path.read_text())
        return ControlState(**data)

    def exists(self) -> bool:
        return self.path.exists()

    def request_operator(self, cdp_port: int, reason: str, context: dict[str, Any]) -> None:
        self.write(ControlState(controller="operator", cdp_port=cdp_port, reason=reason, context=context))

    def wait_for_handback(self, poll_seconds: float = 1.0, timeout_seconds: float = 300) -> ControlState:
        """Blocks the automation process until the operator flips control back."""
        waited = 0.0
        while waited < timeout_seconds:
            state = self.read()
            if state.controller == "agent":
                return state
            time.sleep(poll_seconds)
            waited += poll_seconds
        raise TimeoutError(f"Timed out after {timeout_seconds}s waiting for human operator to hand control back.")

    def hand_back(self, resolution: str) -> None:
        state = self.read()
        state.controller = "agent"
        state.resolution = resolution
        self.write(state)

    def append_human_action(self, action: dict[str, Any]) -> None:
        state = self.read()
        state.human_actions.append({**action, "ts": time.time()})
        self.write(state)
