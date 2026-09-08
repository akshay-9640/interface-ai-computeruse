"""Tests for turning a raw discovery step trace into a parameterized, typed
CapabilityArtifact (assignment 3.2) -- in particular the substitution logic that
turns a concrete recorded value ("10001") into a reusable template ("{member_id}")."""
from src.agent.artifact_builder import build_artifact, _substitute
from src.agent.discover import DiscoveryResult, RecordedStep
from src.artifacts.schema import ParamSpec, OutputSpec


def test_substitute_replaces_known_literal():
    known = {"10001": "member_id", "Savings": "account_type"}
    assert _substitute("10001", known) == "{member_id}"
    assert _substitute("Savings", known) == "{account_type}"


def test_substitute_leaves_unknown_literal_untouched():
    known = {"10001": "member_id"}
    assert _substitute("some other text", known) == "some other text"


def test_substitute_passes_through_none():
    assert _substitute(None, {"x": "y"}) is None


def _fake_success_result():
    steps = [
        RecordedStep("step_01_type", "type", 1,
                     {"strategy": "label_adjacent", "text": "Member ID:", "role": "textbox", "confidence": 0.85},
                     [{"strategy": "nth_of_role", "role": "textbox", "nth": 0, "confidence": 0.4}],
                     typed_value="10001", reasoning="type member id"),
        RecordedStep("step_02_click", "click", 2,
                     {"strategy": "role_name", "role": "button", "name": "Search", "confidence": 0.95},
                     [], reasoning="click search"),
    ]
    result = DiscoveryResult(
        success=True, goal="test", base_url="http://127.0.0.1:5001",
        steps=steps, checkpoint_text="Member Search", summary="reached search results",
        run_id="test-run-1", model="claude-test",
    )
    return result


def test_build_artifact_parameterizes_recorded_literal():
    result = _fake_success_result()
    params = [ParamSpec(name="member_id", type="string", required=True, description="id", example="10001")]
    artifact = build_artifact(
        result=result, capability_id="test_cap", name="Test Cap", description="desc",
        app_name="meridian_core", known_params=params, outputs=[],
    )
    type_step = next(s for s in artifact.steps if s.step_id == "step_01_type")
    assert type_step.value_template == "{member_id}"
    click_step = next(s for s in artifact.steps if s.step_id == "step_02_click")
    assert click_step.locator.strategy == "role_name"
    assert click_step.locator.name == "Search"


def test_build_artifact_requires_a_successful_result():
    result = _fake_success_result()
    result.success = False
    try:
        build_artifact(result=result, capability_id="x", name="x", description="x",
                        app_name="meridian_core", known_params=[], outputs=[])
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_build_artifact_sets_checkpoint_from_discovery():
    result = _fake_success_result()
    artifact = build_artifact(result=result, capability_id="test_cap", name="Test", description="d",
                               app_name="meridian_core", known_params=[], outputs=[])
    assert artifact.checkpoint.assertion.text == "Member Search"
    assert artifact.provenance.discovery_run_id == "test-run-1"
    assert artifact.provenance.discovered_by == "llm"
