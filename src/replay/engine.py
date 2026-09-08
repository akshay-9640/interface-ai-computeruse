"""Deterministic replay (assignment 3.3): given a saved CapabilityArtifact and a set
of input parameters, drives the SAME target app via the SAME locator-resolution code
discovery used (src/perception/locate.py) -- but with no LLM in the decision loop.
Every action to take, and in what order, comes from the artifact.

What replay adds beyond "click through the recorded steps":
  - parameter binding + type/required validation against ParamSpec
  - policy re-check at load time (an artifact recorded against a since-narrowed
    allowlist should not silently still work)
  - after every step, a scan against the merged error taxonomy (src/replay/errors.py)
    that can end the run early with a business_outcome, retry a recoverable
    condition, or stop with a hard_failure -- optionally escalating to a human
    instead of just failing (CapabilityArtifact.escalate_on_hard_failure)
  - checkpoint verification and typed output extraction
  - the risky, capability-level `confirmation_step_id` is skipped entirely unless
    the caller explicitly passes confirm=True
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from src.artifacts.schema import CapabilityArtifact, StepAction
from src.escalation.control import ControlChannel
from src.observability.logger import RunLogger
from src.perception import locate
from src.perception.snapshot import capture
from src.policy.guardrails import PolicyEngine, PolicyViolation, redact_field
from src.replay.errors import effective_handlers, match as match_handler
from src.replay.result import ReplayResult, StepTrace
from src.runtime.auth import login
from src.runtime.browser_session import BrowserSession


class ParamValidationError(Exception):
    pass


def _validate_params(artifact: CapabilityArtifact, params: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for spec in artifact.input_params:
        if spec.name not in params:
            if spec.required:
                raise ParamValidationError(f"Missing required param '{spec.name}'.")
            continue
        value = params[spec.name]
        if spec.type == "number":
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ParamValidationError(f"Param '{spec.name}' must be a number, got {value!r}.")
        elif spec.type == "boolean":
            value = bool(value)
        else:
            value = str(value)
        out[spec.name] = value
    return out


def _render(template: Optional[str], params: dict[str, Any]) -> Optional[str]:
    if template is None:
        return None
    out = template
    for k, v in params.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _extract_outputs(artifact: CapabilityArtifact, page) -> dict[str, str]:
    outputs: dict[str, str] = {}
    body_text = page.evaluate("() => document.body.innerText")
    for spec in artifact.outputs:
        if spec.extraction_pattern:
            m = re.search(spec.extraction_pattern, body_text)
            if m:
                outputs[spec.name] = m.group(1) if m.groups() else m.group(0)
        elif spec.source_locator:
            try:
                cand = spec.source_locator.model_dump()
                resolved = locate.resolve(page, cand, [], timeout_ms=3000)
                outputs[spec.name] = resolved.locator.inner_text()
            except Exception:
                pass
    return outputs


def replay(
    artifact: CapabilityArtifact,
    params: dict[str, Any],
    confirm: bool = False,
    headless: bool = True,
    policy: Optional[PolicyEngine] = None,
) -> ReplayResult:
    policy = policy or PolicyEngine.load()
    policy.check_navigation(f"{artifact.target.base_url}/login")

    bound_params = _validate_params(artifact, params)
    logger = RunLogger.create("replay", label=artifact.capability_id)
    control = ControlChannel(logger.dir / "control.json")
    handlers = effective_handlers(artifact)

    result = ReplayResult(status="failure", capability_id=artifact.capability_id, evidence_dir=str(logger.dir))
    session = BrowserSession.launch(headless=headless)

    try:
        login(session.page, artifact.target.base_url)
        policy.check_navigation(f"{artifact.target.base_url}/search")
        session.page.goto(f"{artifact.target.base_url}/search", wait_until="networkidle")
        logger.event("auto_login", {"base_url": artifact.target.base_url})
        logger.event("params_bound", {
            k: (redact_field(k, str(v)) if _is_sensitive(artifact, k) else v) for k, v in bound_params.items()
        })

        finalized_risky_step = False
        for step in artifact.steps:
            if artifact.confirmation_step_id == step.step_id:
                if not confirm:
                    logger.event("skipped_unconfirmed_step", {"step_id": step.step_id})
                    snapshot = capture(session.page)
                    outcome = _handle_pending_confirmation(artifact, snapshot, bound_params, result, logger)
                    return outcome
                finalized_risky_step = True

            step_result = _execute_step(session, policy, step, bound_params, logger, handlers, artifact, control)
            result.trace.append(step_result)

            if step_result.status == "matched_handler":
                classification = step_result.handler_classification
                handler_label = step_result.handler_label
                outputs_on_match = step_result.handler_outputs
                handler_id = step_result.handler_id
                if classification == "business_outcome":
                    result.status = "business_outcome"
                    result.label = handler_label
                    result.outputs.update(outputs_on_match)
                    logger.write_result(result.to_dict())
                    return result
                if classification == "hard_failure":
                    if artifact.escalate_on_hard_failure:
                        escalated = _escalate(session, control, logger, artifact, step, handler_label)
                        result.escalation_used = True
                        if escalated:
                            terminal = _finalize_if_terminal(artifact, session, result, logger)
                            if terminal is not None:
                                return terminal
                            continue  # human resolved it without finishing -- resume the recorded steps
                    result.status = "failure"
                    result.label = handler_label
                    result.failed_step_id = step.step_id
                    result.expected = f"successful '{step.action}' on '{step.description}'"
                    result.observed = f"error page matched handler '{handler_id}': {handler_label}"
                    logger.write_result(result.to_dict())
                    return result
                # recoverable was already retried inside _execute_step; if we're here
                # with status matched_handler + recoverable, retries were exhausted.
                result.status = "failure"
                result.label = handler_label
                result.failed_step_id = step.step_id
                result.expected = "recovery to succeed within max_retries"
                result.observed = f"handler '{handler_id}' still matched after retries"
                logger.write_result(result.to_dict())
                return result

            if step_result.status == "error":
                if artifact.escalate_on_hard_failure:
                    escalated = _escalate(session, control, logger, artifact, step, step_result.detail)
                    result.escalation_used = True
                    if escalated:
                        terminal = _finalize_if_terminal(artifact, session, result, logger)
                        if terminal is not None:
                            return terminal
                        continue
                result.status = "failure"
                result.failed_step_id = step.step_id
                result.expected = f"'{step.action}' on '{step.description}' to succeed"
                result.observed = step_result.detail
                logger.write_result(result.to_dict())
                return result

        # All recorded steps executed. If the capability's gated risky step ran
        # (confirm=True), the page has moved PAST the recorded checkpoint (which
        # was captured at the safe confirmation screen -- the LLM never visited,
        # and by design never authored a checkpoint for, whatever page follows the
        # irreversible action). In that case we verify success structurally, via
        # successful extraction of at least one declared output, rather than
        # against checkpoint text the discovery run had no way to know.
        final_snapshot = capture(session.page, screenshot_path=str(logger.screenshot_path("final_checkpoint")))

        if finalized_risky_step:
            extracted = _extract_outputs(artifact, session.page)
            if artifact.outputs and not extracted:
                result.status = "failure"
                result.label = "post_confirmation_state_unverified"
                result.expected = "at least one declared output to be extractable after finalizing"
                result.observed = f"no declared output pattern matched the page at {final_snapshot.url}"
                logger.write_result(result.to_dict())
                return result
            result.status = "success"
            result.outputs.update(extracted)
            logger.event("post_confirmation_verified", {"outputs": extracted})
            logger.write_result(result.to_dict())
            return result

        checkpoint_ok = bool(artifact.checkpoint.assertion.text) and artifact.checkpoint.assertion.text in (
            (final_snapshot.heading or "") + "\n" + final_snapshot.body_text
        )
        if not checkpoint_ok:
            result.status = "failure"
            result.label = "checkpoint_not_met"
            result.expected = artifact.checkpoint.description
            result.observed = (
                f"page heading/body did not contain expected checkpoint text "
                f"'{artifact.checkpoint.assertion.text}'"
            )
            logger.write_result(result.to_dict())
            return result

        result.status = "success"
        result.outputs.update(_extract_outputs(artifact, session.page))
        logger.event("checkpoint_verified", {"checkpoint": artifact.checkpoint.description})
        logger.write_result(result.to_dict())
        return result
    finally:
        session.close()


def _is_sensitive(artifact: CapabilityArtifact, param_name: str) -> bool:
    return any(p.name == param_name and p.sensitive for p in artifact.input_params)


def _execute_step(session, policy: PolicyEngine, step: StepAction, params: dict[str, Any],
                   logger: RunLogger, handlers, artifact: CapabilityArtifact, control: ControlChannel) -> StepTrace:
    try:
        policy.check_action_type(step.action)
    except PolicyViolation as e:
        return StepTrace(step.step_id, step.action, "error", f"policy violation: {e}")

    if step.action == "navigate":
        url = _render(step.url_template, params) or ""
        try:
            policy.check_navigation(url)
        except PolicyViolation as e:
            return StepTrace(step.step_id, step.action, "error", f"policy violation: {e}")
        session.page.goto(url, wait_until="networkidle")
    else:
        if step.locator is None:
            return StepTrace(step.step_id, step.action, "error", "step has no locator")
        primary = step.locator.model_dump()
        primary["name"] = _render(primary.get("name"), params)
        primary["text"] = _render(primary.get("text"), params)
        fallbacks = []
        for f in step.locator_fallbacks:
            fd = f.model_dump()
            fd["name"] = _render(fd.get("name"), params)
            fd["text"] = _render(fd.get("text"), params)
            fallbacks.append(fd)
        try:
            resolved = locate.resolve(session.page, primary, fallbacks, timeout_ms=step.timeout_ms)
        except Exception as e:
            return StepTrace(step.step_id, step.action, "error", f"locator resolution failed: {e}")

        value = _render(step.value_template, params)
        try:
            if step.action == "click":
                resolved.locator.click()
            elif step.action == "type":
                resolved.locator.fill(value or "")
            elif step.action == "select":
                resolved.locator.select_option(value=value)
            elif step.action == "wait_for":
                resolved.locator.wait_for(state="visible", timeout=step.timeout_ms)
            elif step.action == "assert_text":
                pass
            session.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception as e:
            return StepTrace(step.step_id, step.action, "error", f"action execution failed: {e}")

    snapshot = capture(session.page, screenshot_path=str(logger.screenshot_path(f"{step.step_id}")))
    handler = match_handler(snapshot, handlers)
    logger.event("step_executed", {"step_id": step.step_id, "action": step.action, "url": snapshot.url,
                                    "matched_handler": handler.handler_id if handler else None})

    if handler is None:
        return StepTrace(step.step_id, step.action, "ok", "no error handler matched")

    if handler.classification == "recoverable":
        for attempt in range(handler.max_retries):
            logger.event("recovery_attempt", {"step_id": step.step_id, "handler": handler.handler_id, "attempt": attempt + 1})
            if handler.recovery == "wait_and_retry":
                time.sleep(1.5)
            # retry the SAME step from scratch
            retry_trace = _execute_step(session, policy, step, params, logger, handlers, artifact, control)
            if retry_trace.status == "ok":
                return StepTrace(step.step_id, step.action, "recovered",
                                  f"recovered from '{handler.handler_id}' after {attempt + 1} attempt(s)")
            if retry_trace.status == "error":
                return retry_trace
            if retry_trace.status == "matched_handler" and retry_trace.handler_id != handler.handler_id:
                return retry_trace  # a different condition now -- stop retrying this one, surface the new one
        return StepTrace(
            step.step_id, step.action, "matched_handler", f"handler '{handler.handler_id}' persisted after retries",
            handler_id=handler.handler_id, handler_classification=handler.classification,
            handler_label=handler.label, handler_outputs={},
        )

    return StepTrace(
        step.step_id, step.action, "matched_handler", f"handler '{handler.handler_id}' matched",
        handler_id=handler.handler_id, handler_classification=handler.classification,
        handler_label=handler.label, handler_outputs=handler.outputs_on_match,
    )


def _finalize_if_terminal(artifact: CapabilityArtifact, session, result: ReplayResult,
                           logger: RunLogger) -> Optional[ReplayResult]:
    """After a human hands control back, the live page may already reflect full
    completion via a path the recorded steps never anticipated (e.g. a supervisor
    override that jumps straight to the success page instead of the recorded
    confirm-screen checkpoint). Checked first by trying to extract this capability's
    declared outputs; if that succeeds, the run is done. Otherwise we fall back to
    the artifact's own recorded checkpoint text. If neither matches, the caller
    resumes executing the remaining recorded steps from wherever the human left the
    page -- appropriate for a lighter-touch intervention (e.g. dismissing an
    unexpected interstitial) rather than a full alternate path to completion."""
    snapshot = capture(session.page, screenshot_path=str(logger.screenshot_path("post_escalation_check")))
    outputs = _extract_outputs(artifact, session.page)
    if outputs:
        result.status = "success"
        result.label = "completed_via_human_operator"
        result.outputs.update(outputs)
        logger.event("finalized_via_escalation", {"outputs": outputs, "path": "output_match"})
        logger.write_result(result.to_dict())
        return result

    haystack = (snapshot.heading or "") + "\n" + snapshot.body_text
    if artifact.checkpoint.assertion.text and artifact.checkpoint.assertion.text in haystack:
        result.status = "success"
        result.label = "completed_via_human_operator"
        result.outputs.update(_extract_outputs(artifact, session.page))
        logger.event("finalized_via_escalation", {"path": "checkpoint_match"})
        logger.write_result(result.to_dict())
        return result

    return None


def _handle_pending_confirmation(artifact, snapshot, params, result: ReplayResult, logger: RunLogger) -> ReplayResult:
    result.status = "business_outcome"
    result.label = "pending_confirmation"
    result.outputs = {k: str(v) for k, v in params.items()}
    result.outputs["_note"] = (
        "This capability reached its confirmation checkpoint but was not finalized because "
        "confirm=True was not supplied. Re-invoke with confirm=True to complete the irreversible step."
    )
    logger.event("pending_confirmation", {"checkpoint": artifact.checkpoint.description})
    logger.write_result(result.to_dict())
    return result


def _escalate(session, control: ControlChannel, logger: RunLogger, artifact: CapabilityArtifact,
              step: StepAction, reason: str) -> bool:
    shot = logger.screenshot_path(f"{step.step_id}_escalate")
    session.page.screenshot(path=str(shot), full_page=True)
    logger.event("escalation_requested", {"step_id": step.step_id, "reason": reason, "screenshot": str(shot)})
    control.request_operator(
        cdp_port=session.cdp_port,
        reason=reason,
        context={
            "phase": "replay",
            "capability_id": artifact.capability_id,
            "step": step.step_id,
            "screenshot": str(shot),
            "operator_hint": "Run: python manage.py operator --run-dir " + str(logger.dir),
        },
    )
    print(f"\n[ESCALATION] Replay paused at step '{step.step_id}'.\n"
          f"  Reason: {reason}\n"
          f"  Run:   python manage.py operator --run-dir {logger.dir}\n")
    try:
        final_state = control.wait_for_handback()
    except TimeoutError:
        logger.event("escalation_timed_out", {"step_id": step.step_id})
        return False
    logger.event("escalation_resolved", {"resolution": final_state.resolution, "human_actions": final_state.human_actions})
    return final_state.resolution == "completed"
