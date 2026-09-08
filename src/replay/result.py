"""Structured result contract returned by the replay engine (and, informally, by the
discovery agent's own run summary). Deliberately three distinct shapes rather than a
single success/failure boolean -- see assignment 3.3 and glossary: "Business outcome
vs. failure" is "the most common design mistake" to conflate."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

ResultStatus = Literal["success", "business_outcome", "failure"]


@dataclass
class StepTrace:
    step_id: str
    action: str
    status: Literal["ok", "recovered", "matched_handler", "error"]
    detail: str
    screenshot_path: Optional[str] = None
    # populated only when status == "matched_handler"
    handler_id: Optional[str] = None
    handler_classification: Optional[str] = None
    handler_label: Optional[str] = None
    handler_outputs: dict = field(default_factory=dict)


@dataclass
class ReplayResult:
    status: ResultStatus
    capability_id: str
    outputs: dict = field(default_factory=dict)
    label: Optional[str] = None  # set for business_outcome / failure
    failed_step_id: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None
    escalation_used: bool = False
    trace: list[StepTrace] = field(default_factory=list)
    evidence_dir: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "capability_id": self.capability_id,
            "outputs": self.outputs,
            "label": self.label,
            "failed_step_id": self.failed_step_id,
            "expected": self.expected,
            "observed": self.observed,
            "escalation_used": self.escalation_used,
            "evidence_dir": self.evidence_dir,
            "trace": [
                {
                    "step_id": t.step_id, "action": t.action, "status": t.status,
                    "detail": t.detail, "screenshot_path": t.screenshot_path,
                }
                for t in self.trace
            ],
        }
