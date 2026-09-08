"""Unit tests for the safety/policy guardrail engine (assignment 3.4). These don't
need a browser or the mock app -- they exercise the allowlist/redaction logic in
isolation, which is exactly the kind of load-bearing, no-I/O logic worth covering
with fast unit tests."""
import pytest

from src.policy.guardrails import PolicyEngine, PolicyViolation, redact, redact_field


@pytest.fixture
def policy():
    return PolicyEngine.load()


def test_allowed_navigation_passes(policy):
    policy.check_navigation("http://127.0.0.1:5001/search")  # should not raise


def test_navigation_outside_domain_is_blocked(policy):
    with pytest.raises(PolicyViolation):
        policy.check_navigation("http://evil.example.com/search")


def test_navigation_to_blocked_route_is_blocked_even_within_allowed_domain(policy):
    with pytest.raises(PolicyViolation):
        policy.check_navigation("http://127.0.0.1:5001/members/10002/new-subaccount/override")


def test_navigation_to_unlisted_route_prefix_is_blocked(policy):
    with pytest.raises(PolicyViolation):
        policy.check_navigation("http://127.0.0.1:5001/admin/wipe-database")


def test_allowed_action_type_passes(policy):
    policy.check_action_type("click")  # should not raise


def test_blocked_action_type_is_rejected(policy):
    with pytest.raises(PolicyViolation):
        policy.check_action_type("execute_script")


def test_unlisted_action_type_is_rejected(policy):
    with pytest.raises(PolicyViolation):
        policy.check_action_type("drag_and_drop")


def test_risky_click_is_flagged(policy):
    assert policy.is_risky("click", "Confirm and Open Account") is True


def test_safe_click_is_not_flagged(policy):
    assert policy.is_risky("click", "Open New Sub-Account") is False


def test_risky_check_requires_matching_action_type(policy):
    # "type" is never in risky_action_types even with a risky-sounding name
    assert policy.is_risky("type", "Confirm") is False


def test_redact_masks_ssn_like_patterns():
    assert redact("SSN on file: 123-45-6789") == "SSN on file: [REDACTED-SSN]"


def test_redact_masks_password_field():
    assert "[REDACTED]" in redact("password: hunter2")


def test_redact_field_masks_sensitive_field_names():
    assert redact_field("supervisor_pin", "4242") == "[REDACTED]"


def test_redact_field_passes_through_non_sensitive_fields():
    assert redact_field("member_id", "10001") == "10001"
