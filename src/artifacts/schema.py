"""
The capability artifact: a typed, versioned, agent-invocable description of a
recorded flow. This is the contract between "the model discovered a flow" and
"an AI agent can call this reliably in production" (assignment Section 3.2).

Design goals, in priority order (see REPORT.md, "Artifact schema" for the full
rationale):

1. It is a CALLABLE CAPABILITY, not a script. `input_params` and `outputs` are typed
   like a function signature so a calling agent (or a human reviewer) knows what to
   pass in and what it gets back without reading the step list.
2. Locator targeting is DECOUPLED FROM THE HAPPY-PATH STEPS. Each step carries a
   primary + fallback LocatorCandidate, generated from the accessibility-tree-style
   perception layer (src/perception), not CSS selectors -- legacy apps rarely have
   stable selectors (see glossary: "Test ID").
3. ERROR HANDLING IS A SEPARATE, DECLARATIVE LAYER (`error_handlers`), not baked into
   individual steps. Runtime exceptions (not-found, validation error, permission
   denial, session timeout, transient failure) are properties of the TARGET APP, not
   of any one recorded flow -- they can occur after any step, whether or not the
   discovery run happened to hit them. This mirrors the brief's framing: "the
   interesting failures aren't layout drift, they're runtime conditions." An artifact
   ships with a starter set of error handlers (authored by whoever reviews/approves
   it) and the replay engine's error-handler resolution falls back to app-level
   defaults so newly authored capabilities inherit known error signatures for free.
4. The schema is REVIEWABLE: `provenance` records how it was produced (LLM discovery
   run id, model, timestamp) and whether a human has reviewed/approved it, because
   3.2 explicitly asks that "both a human reviewer and a calling agent" be able to
   understand the capability.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

ActionType = Literal["navigate", "click", "type", "select", "wait_for", "assert_text"]
ParamType = Literal["string", "number", "boolean"]
RiskLevel = Literal["safe", "risky"]
ErrorClassification = Literal["business_outcome", "recoverable", "hard_failure"]
RecoveryAction = Literal["retry_once", "dismiss_and_continue", "wait_and_retry"]


class LocatorCandidate(BaseModel):
    """One way to find a target element. `name`/`text` may contain {param}
    placeholders bound at replay time (see replay/engine.py: _substitute)."""
    strategy: Literal["role_name", "label", "text", "label_adjacent", "nth_of_role"]
    role: Optional[str] = None
    name: Optional[str] = None
    text: Optional[str] = None
    nth: Optional[int] = None
    confidence: float = 0.5


class StepAction(BaseModel):
    step_id: str
    action: ActionType
    description: str = Field(..., description="Human-readable intent, from the agent's own reasoning at discovery time.")
    locator: Optional[LocatorCandidate] = None
    locator_fallbacks: list[LocatorCandidate] = Field(default_factory=list)
    value_template: Optional[str] = Field(
        default=None, description="Literal text or a {param_name} template, for 'type'/'select' actions."
    )
    url_template: Optional[str] = Field(default=None, description="For 'navigate' actions.")
    timeout_ms: int = 8000


class ErrorHandler(BaseModel):
    """A declarative rule the replay engine checks after every step. See
    replay/errors.py for matching + recovery logic."""
    handler_id: str
    match_text: str = Field(..., description="Substring/regex checked against the page heading + body text.")
    classification: ErrorClassification
    label: str
    recovery: Optional[RecoveryAction] = None
    max_retries: int = 0
    outputs_on_match: dict[str, str] = Field(
        default_factory=dict,
        description="For business_outcome matches: literal output values to return, e.g. {'found': 'false'}.",
    )


class ParamSpec(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str
    example: Optional[str] = None
    sensitive: bool = Field(
        default=False,
        description="If true, replay never writes this value into logs/artifacts/evidence (redacted).",
    )


class OutputSpec(BaseModel):
    name: str
    type: ParamType
    description: str
    source_step_id: Optional[str] = Field(
        default=None, description="Which step's resulting page this is extracted from."
    )
    source_locator: Optional[LocatorCandidate] = None
    extraction_pattern: Optional[str] = Field(
        default=None,
        description="Regex (one capture group) applied to the resulting page's text. Preferred over a "
        "locator for legacy table layouts where the value has no addressable element of its own.",
    )


class Checkpoint(BaseModel):
    description: str
    assertion: LocatorCandidate = Field(
        ..., description="Element/text that MUST be present for the run to count as successful."
    )


class Provenance(BaseModel):
    discovered_by: Literal["llm", "human"] = "llm"
    model: Optional[str] = None
    discovery_run_id: Optional[str] = None
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reviewed: bool = False
    reviewer: Optional[str] = None


class TargetSpec(BaseModel):
    app_name: str
    base_url: str
    vendor_product: Optional[str] = Field(
        default=None,
        description="Underlying vendor product identifier, for cross-tenant reuse matching (see REPORT.md, "
        "'Heterogeneity & multi-tenant').",
    )
    tenant_id: Optional[str] = Field(default=None, description="Null/omitted on a base/template artifact.")


class CapabilityArtifact(BaseModel):
    schema_version: str = SCHEMA_VERSION
    capability_id: str
    name: str
    description: str
    version: int = 1
    target: TargetSpec

    input_params: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[StepAction]
    error_handlers: list[ErrorHandler] = Field(default_factory=list)
    checkpoint: Checkpoint

    risk_level: RiskLevel = "safe"
    requires_confirmation: bool = False
    confirmation_step_id: Optional[str] = Field(
        default=None,
        description="If set, the replay engine will not execute this step unless the caller passes confirm=True "
        "(assignment 3.4: risky/irreversible actions are handled conservatively).",
    )
    escalate_on_hard_failure: bool = True

    provenance: Provenance = Field(default_factory=Provenance)

    def model_dump_json_pretty(self) -> str:
        return self.model_dump_json(indent=2)
