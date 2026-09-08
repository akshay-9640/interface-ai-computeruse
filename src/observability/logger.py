"""Structured, redacted run logging (assignment 3.5). Every discovery and replay run
gets its own evidence directory with:
  - run.jsonl      one JSON object per event (what happened, and -- for the agent --
                    why, i.e. the LLM's own stated reasoning)
  - screenshots/    a PNG at key steps and always on failure/business-outcome/escalation
  - result.json     the final structured result (written by the caller)

All text passed through here is redacted (src/policy/guardrails.redact) before it
touches disk, so a run log can be handed to a reviewer without also handing them
regulated financial data.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.policy.guardrails import redact

EVIDENCE_ROOT = Path(__file__).resolve().parents[2] / "evidence" / "sessions"


@dataclass
class RunLogger:
    run_id: str
    kind: str  # "discovery" | "replay" | "operator"
    dir: Path

    @classmethod
    def create(cls, kind: str, label: str = "") -> "RunLogger":
        run_id = f"{kind}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
        d = EVIDENCE_ROOT / run_id
        (d / "screenshots").mkdir(parents=True, exist_ok=True)
        logger = cls(run_id=run_id, kind=kind, dir=d)
        logger.event("run_started", {"kind": kind, "label": label})
        return logger

    def screenshot_path(self, tag: str) -> Path:
        return self.dir / "screenshots" / f"{int(time.time() * 1000)}_{tag}.png"

    def event(self, event_type: str, data: dict[str, Any]) -> None:
        safe_data = _redact_deep(data)
        record = {
            "ts": time.time(),
            "event": event_type,
            **safe_data,
        }
        with open(self.dir / "run.jsonl", "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def write_result(self, result: dict[str, Any]) -> Path:
        p = self.dir / "result.json"
        p.write_text(json.dumps(_redact_deep(result), indent=2, default=str))
        return p


def _redact_deep(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: _redact_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_deep(v) for v in obj]
    return obj
