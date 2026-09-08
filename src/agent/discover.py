"""The goal-driven discovery agent loop (assignment 3.1): observe -> decide -> act,
LLM-in-the-loop, against the live Meridian Core mock app, until the goal is met, a
stopping condition is hit, or the agent escalates to a human.

On success, the run is converted into a CapabilityArtifact (assignment 3.2) by
src/agent/artifact_builder.py. Locator targeting during discovery uses the SAME
resolver (src/perception/locate.py) that replay later uses -- so what worked here is
exactly what gets replayed, not a separate "recording format" translated after the
fact.
"""
from __future__ import annotations

import base64
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from anthropic import Anthropic

from src.agent.prompts import SYSTEM_PROMPT, build_user_turn
from src.agent.tools import TOOLS
from src.artifacts.schema import CapabilityArtifact
from src.escalation.control import ControlChannel
from src.observability.logger import RunLogger
from src.perception import locate
from src.perception.snapshot import PageSnapshot, capture
from src.policy.guardrails import PolicyEngine, PolicyViolation, redact
from src.runtime.auth import login
from src.runtime.browser_session import BrowserSession

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
MAX_STEPS = 15


@dataclass
class RecordedStep:
    step_id: str
    action: str  # click | type | select
    element_index: int
    primary_candidate: dict
    fallback_candidates: list[dict]
    typed_value: Optional[str] = None
    reasoning: str = ""


@dataclass
class DiscoveryResult:
    success: bool
    goal: str
    base_url: str
    steps: list[RecordedStep] = field(default_factory=list)
    extracted_outputs: dict[str, str] = field(default_factory=dict)
    checkpoint_text: Optional[str] = None
    checkpoint_step_locator: Optional[dict] = None
    summary: Optional[str] = None
    escalation_used: bool = False
    run_id: str = ""
    logger_dir: str = ""
    model: str = MODEL


def _image_block(png_path) -> dict:
    with open(png_path, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}


def run_discovery(
    goal: str,
    base_url: str,
    capability_name: str,
    headless: bool = True,
    max_steps: int = MAX_STEPS,
    allow_risky_finalize: bool = False,
) -> DiscoveryResult:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. The discovery run must call a real model.")

    client = Anthropic(api_key=api_key)
    policy = PolicyEngine.load()
    logger = RunLogger.create("discovery", label=goal)
    control = ControlChannel(logger.dir / "control.json")

    session = BrowserSession.launch(headless=headless)
    result = DiscoveryResult(success=False, goal=goal, base_url=base_url, run_id=logger.run_id, logger_dir=str(logger.dir))

    try:
        policy.check_navigation(f"{base_url}/login")
        login(session.page, base_url)
        logger.event("auto_login", {"base_url": base_url})

        policy.check_navigation(f"{base_url}/search")
        session.page.goto(f"{base_url}/search", wait_until="networkidle")

        snap_path = logger.screenshot_path("step00_initial")
        snapshot = capture(session.page, screenshot_path=str(snap_path))
        logger.event("observe", {"step": 0, "url": snapshot.url, "screenshot": str(snap_path)})

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_user_turn(goal, snapshot.to_numbered_text())},
                    _image_block(snap_path),
                ],
            }
        ]

        for step_num in range(1, max_steps + 1):
            response = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                tool_choice={"type": "any"},
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            tool_use = next((b for b in response.content if b.type == "tool_use"), None)
            if tool_use is None:
                logger.event("no_tool_call", {"step": step_num, "text": str(response.content)})
                break

            logger.event(
                "llm_decision",
                {"step": step_num, "tool": tool_use.name, "input": tool_use.input},
            )

            if tool_use.name == "finish_success":
                result.success = True
                result.summary = tool_use.input.get("summary")
                result.checkpoint_text = tool_use.input.get("checkpoint_text")
                # capture the checkpoint as a locator against the CURRENT snapshot for the artifact
                result.checkpoint_step_locator = {
                    "strategy": "text", "text": result.checkpoint_text, "confidence": 0.8,
                }
                logger.event("finish_success", {"summary": result.summary, "checkpoint_text": result.checkpoint_text})
                # If this capability's happy path ends at a risky, irreversible control
                # (e.g. "Confirm and Open Account"), record that control's locator too --
                # WITHOUT clicking it -- so replay can offer it as an explicitly-gated
                # final step. See agent/artifact_builder.py.
                result.extracted_outputs["_final_snapshot"] = snapshot.to_dict()
                break

            if tool_use.name == "escalate":
                reason = tool_use.input.get("reason", "unspecified")
                result.escalation_used = True
                shot = logger.screenshot_path(f"step{step_num:02d}_escalate")
                session.page.screenshot(path=str(shot), full_page=True)
                logger.event("escalation_requested", {"step": step_num, "reason": reason, "screenshot": str(shot)})
                control.request_operator(
                    cdp_port=session.cdp_port,
                    reason=reason,
                    context={
                        "phase": "discovery",
                        "goal": goal,
                        "capability_name": capability_name,
                        "step": step_num,
                        "page_url": snapshot.url,
                        "screenshot": str(shot),
                        "operator_hint": "Run: python manage.py operator --run-dir " + str(logger.dir),
                    },
                )
                print(f"\n[ESCALATION] Discovery paused. A human operator is needed.\n"
                      f"  Reason: {reason}\n"
                      f"  Run:   python manage.py operator --run-dir {logger.dir}\n")
                final_state = control.wait_for_handback()
                logger.event("escalation_resolved", {
                    "resolution": final_state.resolution,
                    "human_actions": final_state.human_actions,
                })
                # Re-observe: the human may have changed the page.
                shot2 = logger.screenshot_path(f"step{step_num:02d}_post_escalation")
                snapshot = capture(session.page, screenshot_path=str(shot2))
                tool_result_text = (
                    f"A human operator intervened. Resolution: {final_state.resolution}. "
                    f"Actions taken: {final_state.human_actions}. "
                    f"Current page state after handoff:\n{snapshot.to_numbered_text()}"
                )
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result", "tool_use_id": tool_use.id,
                        "content": [{"type": "text", "text": redact(tool_result_text)}, _image_block(shot2)],
                    }],
                })
                continue

            # click_element / type_text / select_option / extract_output
            tool_result_text, snapshot, snap_path = _execute_tool(
                session, snapshot, tool_use, policy, logger, step_num, result, allow_risky_finalize
            )
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result", "tool_use_id": tool_use.id,
                    "content": [{"type": "text", "text": redact(tool_result_text)}, _image_block(snap_path)],
                }],
            })
        else:
            logger.event("max_steps_reached", {"max_steps": max_steps})

        logger.write_result({
            "success": result.success,
            "summary": result.summary,
            "steps_recorded": len(result.steps),
            "escalation_used": result.escalation_used,
        })
        return result
    finally:
        session.close()


def _execute_tool(session, snapshot: PageSnapshot, tool_use, policy: PolicyEngine, logger: RunLogger,
                   step_num: int, result: DiscoveryResult, allow_risky_finalize: bool):
    name = tool_use.name
    inp = tool_use.input

    if name == "extract_output":
        result.extracted_outputs[inp["name"]] = inp["value"]
        logger.event("extract_output", {"name": inp["name"], "value": redact(inp["value"])})
        shot = logger.screenshot_path(f"step{step_num:02d}_extract")
        session.page.screenshot(path=str(shot))
        return (f"Recorded output '{inp['name']}'. Current page unchanged.", snapshot, shot)

    index = inp.get("index")
    element = next((e for e in snapshot.elements if e.index == index), None)
    if element is None:
        return (f"ERROR: no element with index {index} in the last snapshot. Re-check the numbered list.",
                snapshot, logger.screenshot_path(f"step{step_num:02d}_error"))

    action_map = {"click_element": "click", "type_text": "type", "select_option": "select"}
    action = action_map[name]

    try:
        policy.check_action_type(action)
    except PolicyViolation as e:
        logger.event("policy_blocked", {"step": step_num, "reason": str(e)})
        return (f"BLOCKED BY POLICY: {e}", snapshot, logger.screenshot_path(f"step{step_num:02d}_blocked"))

    if policy.is_risky(action, element.name) and not allow_risky_finalize:
        logger.event("policy_blocked_risky", {"step": step_num, "element": element.name})
        shot = logger.screenshot_path(f"step{step_num:02d}_blocked_risky")
        session.page.screenshot(path=str(shot))
        return (
            f"BLOCKED BY POLICY: '{element.name}' is classified as a risky/irreversible action. "
            f"This discovery run is not authorized to finalize it. If the goal is satisfied by "
            f"reaching this point, call finish_success now instead.",
            snapshot, shot,
        )

    candidates = element.locator_candidates()
    primary, fallbacks = candidates[0], candidates[1:]
    try:
        resolved = locate.resolve(session.page, primary, fallbacks)
    except Exception as e:
        return (f"ERROR resolving element {index}: {e}", snapshot, logger.screenshot_path(f"step{step_num:02d}_resolve_error"))

    typed_value = None
    if action == "click":
        resolved.locator.click()
    elif action == "type":
        typed_value = inp["text"]
        resolved.locator.fill(typed_value)
    elif action == "select":
        typed_value = inp["value"]
        resolved.locator.select_option(value=typed_value)

    try:
        session.page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:
        pass

    step_id = f"step_{step_num:02d}_{action}"
    result.steps.append(RecordedStep(
        step_id=step_id, action=action, element_index=index,
        primary_candidate=primary, fallback_candidates=fallbacks,
        typed_value=typed_value, reasoning=inp.get("reasoning", ""),
    ))

    shot = logger.screenshot_path(f"step{step_num:02d}_{action}")
    new_snapshot = capture(session.page, screenshot_path=str(shot))
    logger.event("action_executed", {
        "step": step_num, "step_id": step_id, "action": action, "element": element.name,
        "strategy_used": resolved.strategy_used, "new_url": new_snapshot.url,
    })
    return (f"Action '{action}' on element {index} ('{element.name}') executed via {resolved.strategy_used}.\n"
            f"New page state:\n{new_snapshot.to_numbered_text()}", new_snapshot, shot)
