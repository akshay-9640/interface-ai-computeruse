"""Converts a successful DiscoveryResult (a raw step trace + the LLM's own transcript)
into a CapabilityArtifact: a typed, versioned, agent-invocable capability, decoupled
from the raw model transcript (assignment 3.2).

Two things happen here that are NOT just "serialize the trace":

1. PARAMETERIZATION. The discovery run is driven with concrete example values (a
   real member ID, a real deposit amount). `known_param_values` maps each concrete
   value back to a declared input parameter name, so the recorded step's
   value_template becomes "{member_id}" instead of the literal "10001" -- this is
   what makes the artifact re-invocable with different inputs rather than a fixed
   macro. (This is also the seam a full canonicalization pass -- optional stretch
   goal 8 -- would extend: generalizing concrete route/value patterns, not just
   literal substring matches.)

2. THE FINAL RISKY STEP IS ADDED BY THE HARNESS, NOT THE LLM. If the capability's
   happy path stops one screen short of an irreversible action (by design -- see
   agent/prompts.py: the agent is instructed never to click past a "Confirm"-style
   control), the harness inspects the checkpoint page's own perception snapshot,
   finds that control's locator, and appends ONE more step marked as the artifact's
   `confirmation_step_id`. The replay engine will only execute that step if the
   caller explicitly passes confirm=True. The LLM never decided to perform the
   irreversible action; a human (via the discovery goal + this review step) decided
   the capability may.
"""
from __future__ import annotations

import re
from typing import Optional

from src.agent.discover import DiscoveryResult
from src.artifacts.schema import (
    CapabilityArtifact, StepAction, LocatorCandidate, ParamSpec, OutputSpec,
    Checkpoint, TargetSpec, Provenance,
)
from src.perception.snapshot import ElementSnapshot, PageSnapshot


def _substitute(value: Optional[str], known_param_values: dict[str, str]) -> Optional[str]:
    if value is None:
        return None
    for literal, param_name in known_param_values.items():
        if literal and literal == value:
            return "{" + param_name + "}"
    return value


def build_artifact(
    result: DiscoveryResult,
    capability_id: str,
    name: str,
    description: str,
    app_name: str,
    known_params: list[ParamSpec],
    outputs: list[OutputSpec],
    risk_level: str = "safe",
    requires_confirmation: bool = False,
) -> CapabilityArtifact:
    if not result.success:
        raise ValueError("Cannot build an artifact from a discovery run that did not succeed.")

    known_param_values = {p.example: p.name for p in known_params if p.example}

    steps: list[StepAction] = []
    for rs in result.steps:
        primary = LocatorCandidate(**rs.primary_candidate)
        fallbacks = [LocatorCandidate(**c) for c in rs.fallback_candidates]
        value_template = _substitute(rs.typed_value, known_param_values)
        steps.append(StepAction(
            step_id=rs.step_id,
            action=rs.action,
            description=rs.reasoning or f"{rs.action} on element {rs.element_index}",
            locator=primary,
            locator_fallbacks=fallbacks,
            value_template=value_template,
        ))

    confirmation_step_id = None
    final_snapshot_dict = result.extracted_outputs.pop("_final_snapshot", None)
    if requires_confirmation and final_snapshot_dict:
        confirm_el = _find_confirm_element(final_snapshot_dict)
        if confirm_el is not None:
            step_id = "step_confirm_finalize"
            candidates = confirm_el.locator_candidates()
            steps.append(StepAction(
                step_id=step_id,
                action="click",
                description=f"Finalize the irreversible action by clicking '{confirm_el.name}'. "
                "Only executed if the caller passes confirm=True.",
                locator=LocatorCandidate(**candidates[0]),
                locator_fallbacks=[LocatorCandidate(**c) for c in candidates[1:]],
            ))
            confirmation_step_id = step_id

    checkpoint = Checkpoint(
        description=result.summary or "Discovery-declared success checkpoint.",
        assertion=LocatorCandidate(strategy="text", text=result.checkpoint_text, confidence=0.8),
    )

    artifact = CapabilityArtifact(
        capability_id=capability_id,
        name=name,
        description=description,
        target=TargetSpec(app_name=app_name, base_url=result.base_url, vendor_product="meridian_core_vendor"),
        input_params=known_params,
        outputs=outputs,
        steps=steps,
        checkpoint=checkpoint,
        risk_level=risk_level,
        requires_confirmation=requires_confirmation,
        confirmation_step_id=confirmation_step_id,
        provenance=Provenance(
            discovered_by="llm",
            model=result.model,
            discovery_run_id=result.run_id,
            reviewed=False,
        ),
    )
    return artifact


def _find_confirm_element(snapshot_dict: dict) -> Optional[ElementSnapshot]:
    for e in snapshot_dict.get("elements", []):
        name = (e.get("name") or "").lower()
        if e.get("role") == "button" and "confirm" in name:
            el = ElementSnapshot(**e)
            return el
    return None
