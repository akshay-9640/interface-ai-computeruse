"""Safety & policy guardrails (assignment 3.4). Enforces an explicit, configurable
allowlist and classifies actions as safe/reversible vs. risky/irreversible. Both the
discovery agent and the replay engine call into this module BEFORE executing an
action -- it raises PolicyViolation rather than letting the caller decide whether to
comply, so a misbehaving/misprompted LLM cannot argue its way past it."""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

_ALLOWLIST_PATH = Path(__file__).resolve().parent / "allowlist.yaml"

# --- Redaction -------------------------------------------------------------------
# Applied to every string written into logs, evidence, or artifacts. Never persist
# secrets or raw sensitive data (3.4). This is deliberately conservative (a few false
# positives are fine; a leaked SSN/PIN is not).
_REDACT_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED-SSN]"),
    (re.compile(r"\b\d{4,6}\b(?=.{0,20}\bpin\b)", re.IGNORECASE), "[REDACTED-PIN]"),
    (re.compile(r"\bpin\b\s*[:=]?\s*\d{3,8}", re.IGNORECASE), "PIN: [REDACTED-PIN]"),
    (re.compile(r"\bpassword\b\s*[:=]?\s*\S+", re.IGNORECASE), "password: [REDACTED]"),
]

SENSITIVE_FIELD_NAMES = {"password", "supervisor_pin", "ssn", "tax_id", "credential", "token"}


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for pattern, replacement in _REDACT_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_field(field_name: str, value: str) -> str:
    if field_name and field_name.lower() in SENSITIVE_FIELD_NAMES:
        return "[REDACTED]"
    return redact(value)


def redact_deep(obj):
    """Recursively applies the generic pattern-based `redact` to every string in a
    nested dict/list -- the safety-net pass used before ANYTHING is written to
    disk (evidence logs, control-state files). This catches secrets that are
    *shaped* like a secret (an SSN, a "PIN: 1234" phrase) wherever they appear.
    It deliberately does NOT know about field semantics (e.g. that a "text" value
    is sensitive because a sibling "element" key says "Supervisor PIN:") -- that
    context-aware case is handled at the call site instead (see
    src/escalation/operator.py, which redacts a typed value by the target
    element's field_name BEFORE it ever reaches here)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_deep(v) for v in obj]
    return obj


class PolicyViolation(Exception):
    pass


@dataclass
class PolicyEngine:
    allowed_domains: list[str]
    allowed_route_prefixes: list[str]
    blocked_routes: list[str]
    allowed_action_types: list[str]
    blocked_action_types: list[str]
    risky_name_patterns: list[str]
    risky_action_types: list[str]

    @classmethod
    def load(cls, path: Path = _ALLOWLIST_PATH) -> "PolicyEngine":
        data = yaml.safe_load(path.read_text())
        return cls(
            allowed_domains=data.get("allowed_domains", []),
            allowed_route_prefixes=data.get("allowed_route_prefixes", []),
            blocked_routes=data.get("blocked_routes", []),
            allowed_action_types=data.get("allowed_action_types", []),
            blocked_action_types=data.get("blocked_action_types", []),
            risky_name_patterns=[p.lower() for p in data.get("risky_name_patterns", [])],
            risky_action_types=data.get("risky_action_types", []),
        )

    def check_navigation(self, url: str) -> None:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host not in self.allowed_domains:
            raise PolicyViolation(f"Domain '{host}' is not in the allowlist ({self.allowed_domains}).")
        path = parsed.path or "/"
        for blocked in self.blocked_routes:
            if fnmatch.fnmatch(path, blocked):
                raise PolicyViolation(f"Route '{path}' is explicitly blocked by policy ({blocked}).")
        if not any(path.startswith(prefix) for prefix in self.allowed_route_prefixes):
            raise PolicyViolation(f"Route '{path}' is not under any allowed prefix {self.allowed_route_prefixes}.")

    def check_action_type(self, action_type: str) -> None:
        if action_type in self.blocked_action_types:
            raise PolicyViolation(f"Action type '{action_type}' is explicitly blocked by policy.")
        if action_type not in self.allowed_action_types:
            raise PolicyViolation(f"Action type '{action_type}' is not in the allowed action types.")

    def is_risky(self, action_type: str, element_name: str | None) -> bool:
        if action_type not in self.risky_action_types:
            return False
        if not element_name:
            return False
        name = element_name.lower()
        return any(pat in name for pat in self.risky_name_patterns)
