"""Tests for replay's parameter binding/validation and template rendering --
pure functions, no browser needed."""
import pytest

from src.artifacts.schema import ParamSpec, CapabilityArtifact, TargetSpec, Checkpoint, LocatorCandidate
from src.replay.engine import _validate_params, _render, ParamValidationError


def _artifact_with_params(params):
    return CapabilityArtifact(
        capability_id="test_cap", name="t", description="d",
        target=TargetSpec(app_name="meridian_core", base_url="http://127.0.0.1:5001"),
        input_params=params, steps=[],
        checkpoint=Checkpoint(description="d", assertion=LocatorCandidate(strategy="text", text="Done")),
    )


def test_validate_params_coerces_number():
    artifact = _artifact_with_params([ParamSpec(name="initial_deposit", type="number", description="d")])
    out = _validate_params(artifact, {"initial_deposit": "100.50"})
    assert out["initial_deposit"] == 100.50
    assert isinstance(out["initial_deposit"], float)


def test_validate_params_rejects_non_numeric_for_number_type():
    artifact = _artifact_with_params([ParamSpec(name="initial_deposit", type="number", description="d")])
    with pytest.raises(ParamValidationError):
        _validate_params(artifact, {"initial_deposit": "not-a-number"})


def test_validate_params_requires_required_field():
    artifact = _artifact_with_params([ParamSpec(name="member_id", type="string", required=True, description="d")])
    with pytest.raises(ParamValidationError):
        _validate_params(artifact, {})


def test_validate_params_allows_missing_optional_field():
    artifact = _artifact_with_params([ParamSpec(name="notes", type="string", required=False, description="d")])
    out = _validate_params(artifact, {})
    assert "notes" not in out


def test_validate_params_coerces_string():
    artifact = _artifact_with_params([ParamSpec(name="member_id", type="string", description="d")])
    out = _validate_params(artifact, {"member_id": 10001})
    assert out["member_id"] == "10001"


def test_render_substitutes_single_placeholder():
    assert _render("{member_id}", {"member_id": "10001"}) == "10001"


def test_render_substitutes_multiple_placeholders():
    result = _render("member={member_id} type={account_type}", {"member_id": "10001", "account_type": "Savings"})
    assert result == "member=10001 type=Savings"


def test_render_passes_through_literal_text_unchanged():
    assert _render("Savings", {"member_id": "10001"}) == "Savings"


def test_render_none_template_returns_none():
    assert _render(None, {"member_id": "10001"}) is None
