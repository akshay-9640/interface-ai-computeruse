"""Tests for the error-handler merge and matching logic (assignment 3.3's error
taxonomy). Uses a real CapabilityArtifact but a hand-built PageSnapshot -- no
browser needed."""
from src.artifacts.schema import (
    CapabilityArtifact, TargetSpec, Checkpoint, LocatorCandidate, ErrorHandler,
)
from src.perception.snapshot import PageSnapshot
from src.replay.errors import effective_handlers, match


def _minimal_artifact(app_name="meridian_core", extra_handlers=None):
    return CapabilityArtifact(
        capability_id="test_cap",
        name="Test",
        description="test",
        target=TargetSpec(app_name=app_name, base_url="http://127.0.0.1:5001"),
        steps=[],
        checkpoint=Checkpoint(description="done", assertion=LocatorCandidate(strategy="text", text="Done")),
        error_handlers=extra_handlers or [],
    )


def _snapshot(heading=None, body_text=""):
    return PageSnapshot(url="http://x", title="t", heading=heading, body_text=body_text, elements=[])


def test_app_level_defaults_are_included():
    artifact = _minimal_artifact()
    handlers = effective_handlers(artifact)
    ids = {h.handler_id for h in handlers}
    assert "member_not_found" in ids
    assert "permission_denied" in ids
    assert "system_busy" in ids


def test_artifact_level_handler_overrides_default_by_id():
    override = ErrorHandler(
        handler_id="member_not_found", match_text="Record Not Found",
        classification="hard_failure", label="overridden for this capability",
    )
    artifact = _minimal_artifact(extra_handlers=[override])
    handlers = effective_handlers(artifact)
    matched = next(h for h in handlers if h.handler_id == "member_not_found")
    assert matched.classification == "hard_failure"
    assert matched.label == "overridden for this capability"


def test_unknown_app_name_yields_no_defaults():
    artifact = _minimal_artifact(app_name="some_other_vendor_app")
    handlers = effective_handlers(artifact)
    assert handlers == []


def test_match_finds_handler_by_body_text():
    artifact = _minimal_artifact()
    handlers = effective_handlers(artifact)
    snap = _snapshot(heading="Member Search", body_text='Record Not Found. No member found matching ID "99999".')
    handler = match(snap, handlers)
    assert handler is not None
    assert handler.handler_id == "member_not_found"


def test_match_returns_none_when_nothing_matches():
    artifact = _minimal_artifact()
    handlers = effective_handlers(artifact)
    snap = _snapshot(heading="Member 10001: Alice Rivera", body_text="Sub-Accounts ...")
    assert match(snap, handlers) is None


def test_match_is_case_insensitive():
    artifact = _minimal_artifact()
    handlers = effective_handlers(artifact)
    snap = _snapshot(heading=None, body_text="permission denied for this operation")
    handler = match(snap, handlers)
    assert handler is not None
    assert handler.handler_id == "permission_denied"
