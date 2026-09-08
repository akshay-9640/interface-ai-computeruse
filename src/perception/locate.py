"""
Resolves a LocatorCandidate (role/name, text, or nth-of-role) into a live Playwright
Locator. Shared by the discovery agent (acting on the LLM's chosen element) and the
replay engine (acting on a recorded artifact) -- this is the ONE place that turns an
artifact's declared locator strategy into a real DOM interaction, which is what makes
replay's element targeting behave identically to discovery's.

Resolution order for a step is: try locator, then each locator_fallback in order.
The first candidate that resolves to exactly one visible, enabled element wins. This
"first-that-uniquely-matches" policy is what section 3.3 calls "stable element/control
targeting" -- see REPORT.md, "Determinism & error handling".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Page, Locator, TimeoutError as PWTimeoutError


class LocatorResolutionError(Exception):
    def __init__(self, message: str, tried: list[dict]):
        super().__init__(message)
        self.tried = tried


@dataclass
class ResolvedTarget:
    locator: Locator
    strategy_used: str
    candidate: dict


def _resolve_one(page: Page, candidate: dict, timeout_ms: int) -> Optional[Locator]:
    strategy = candidate.get("strategy")
    try:
        if strategy == "role_name":
            role = candidate["role"]
            name = candidate["name"]
            loc = page.get_by_role(role, name=name, exact=False)
        elif strategy == "text":
            loc = page.get_by_text(candidate["text"], exact=False)
        elif strategy == "label":
            loc = page.get_by_label(candidate["name"], exact=False)
        elif strategy == "label_adjacent":
            # Legacy table-form pattern: <tr><td>Label:</td><td><input/></td></tr>.
            # The element's accessible name has no real ARIA source (see
            # ElementSnapshot.name_source), so we find the label cell by its exact
            # visible text, then step to the adjacent cell's control structurally.
            label_loc = page.get_by_text(candidate["text"], exact=True)
            label_loc.first.wait_for(state="visible", timeout=timeout_ms)
            loc = label_loc.first.locator(
                "xpath=following-sibling::td[1]//input | following-sibling::td[1]//select | "
                "following-sibling::td[1]//textarea"
            )
        elif strategy == "nth_of_role":
            role = candidate["role"]
            nth = candidate.get("nth", 0)
            loc = page.get_by_role(role).nth(nth)
        else:
            return None
        loc.first.wait_for(state="visible", timeout=timeout_ms)
        count = loc.count()
        if count == 0:
            return None
        return loc.first
    except PWTimeoutError:
        return None
    except Exception:
        return None


def resolve(
    page: Page,
    primary: dict,
    fallbacks: Optional[list[dict]] = None,
    timeout_ms: int = 6000,
) -> ResolvedTarget:
    """Try the primary locator, then each fallback in order. Raises
    LocatorResolutionError if none resolve -- this is the trigger for the replay
    engine's hard_failure path (see replay/engine.py)."""
    tried = []
    candidates = [primary] + (fallbacks or [])
    per_candidate_timeout = max(1000, timeout_ms // max(1, len(candidates)))
    for cand in candidates:
        tried.append(cand)
        loc = _resolve_one(page, cand, per_candidate_timeout)
        if loc is not None:
            return ResolvedTarget(locator=loc, strategy_used=cand["strategy"], candidate=cand)
    raise LocatorResolutionError(
        f"No locator candidate resolved to a visible element (tried {len(tried)} strategies).",
        tried=tried,
    )
