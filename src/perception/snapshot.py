"""
Perception layer: turns a live Playwright page into a structured, numbered list of
interactive elements the agent (LLM or replay engine) can reason about and target --
instead of raw HTML/CSS. This is deliberately CSS/test-id independent: it walks the
DOM at runtime and computes an *accessible name* for each interactive control the
same way a screen reader (or the browser's accessibility tree) would, which is why
it keeps working on legacy, non-semantic markup (Section 1: "Heterogeneous, often
legacy surfaces").

Design note (see REPORT.md, "Artifact schema"): every element gets a small set of
LOCATOR CANDIDATES ranked by robustness, not a single selector. Role+accessible-name
is preferred (works via Playwright's get_by_role, which itself is accessibility-tree
based); visible text and a same-role positional index are fallbacks. Nothing here
depends on CSS classes or element IDs, which legacy enterprise apps essentially never
have (see glossary: "Test ID").
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Optional

# Injected into the page and evaluated once per snapshot. Walks visible, interactive
# elements and computes a best-effort accessible name using the same precedence order
# browsers use for the accessibility tree: aria-label -> associated <label> ->
# placeholder -> value/text content -> title.
_SNAPSHOT_JS = r"""
() => {
  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return false;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    return true;
  }

  function labelFor(el) {
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
    }
    const parentLabel = el.closest('label');
    if (parentLabel && parentLabel.innerText.trim()) return parentLabel.innerText.trim();
    return null;
  }

  function nearbyText(el) {
    // legacy table-based forms: the label is often the <td> immediately to the left
    const td = el.closest('td');
    if (td && td.previousElementSibling) {
      const t = td.previousElementSibling.innerText;
      if (t && t.trim()) return t.trim();
    }
    return null;
  }

  function accessibleName(el) {
    // Returns [name, source]. `source` records WHERE the name came from, because
    // that determines which locator strategies can actually resolve it later --
    // see ElementSnapshot.locator_candidates() in snapshot.py. Only aria-label,
    // <label>, placeholder, and a button/link's own text are part of the REAL
    // browser/Playwright accessible-name computation (get_by_role can find them).
    // nearby_text/title are heuristics WE add for display and a dedicated
    // 'label_adjacent' locator strategy -- get_by_role will not match them.
    const aria = el.getAttribute('aria-label');
    if (aria) return [aria.trim(), 'aria'];
    const lbl = labelFor(el);
    if (lbl) return [lbl, 'label'];
    const placeholder = el.getAttribute('placeholder');
    if (placeholder) return [placeholder.trim(), 'placeholder'];
    if (el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'button')) {
      return [el.value || null, 'own_text'];
    }
    // A <select>'s innerText/textContent concatenates ALL of its <option> text --
    // not a meaningful accessible name -- so skip straight to the nearby-label
    // heuristic for selects instead of treating that concatenation as "own text".
    if (el.tagName !== 'SELECT') {
      const text = el.innerText || el.textContent || '';
      if (text && text.trim()) return [text.trim().slice(0, 120), 'own_text'];
    }
    const near = nearbyText(el);
    if (near) return [near, 'nearby_text'];
    const title = el.getAttribute('title');
    if (title) return [title.trim(), 'title'];
    return [null, null];
  }

  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a' && el.hasAttribute('href')) return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'submit' || type === 'button') return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'password') return 'textbox';
      return 'textbox';
    }
    return tag;
  }

  const SELECTOR = 'a[href], button, input, select, textarea, [role]';
  const nodes = Array.from(document.querySelectorAll(SELECTOR));
  const out = [];
  let idx = 0;
  for (const el of nodes) {
    if (!isVisible(el)) continue;
    if (el.disabled) continue;
    const tag = el.tagName.toLowerCase();
    const type = el.getAttribute('type') || null;
    const [name, nameSource] = accessibleName(el);
    const role = roleOf(el);
    const rect = el.getBoundingClientRect();
    out.push({
      index: idx,
      tag: tag,
      type: type,
      role: role,
      name: name,
      name_source: nameSource,
      field_name: el.getAttribute('name') || null,
      value: (tag === 'input' || tag === 'textarea' || tag === 'select') ? el.value : null,
      options: (tag === 'select') ? Array.from(el.options).map(o => o.value).filter(v => v !== '') : null,
      href: el.getAttribute('href') || null,
      bbox: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
    });
    idx += 1;
  }
  return out;
}
"""

_PAGE_SIGNALS_JS = r"""
() => {
  return {
    url: window.location.href,
    title: document.title,
    heading: (document.querySelector('h1,h2,h3') || {}).innerText || null,
    body_text: (document.body.innerText || '').slice(0, 6000),
  };
}
"""


@dataclass
class ElementSnapshot:
    index: int
    tag: str
    type: Optional[str]
    role: str
    name: Optional[str]
    field_name: Optional[str]
    value: Optional[str]
    href: Optional[str]
    bbox: dict
    options: Optional[list[str]] = None
    name_source: Optional[str] = None

    # name sources that correspond to the browser's REAL accessible-name computation
    # -- these are the only ones get_by_role(role, name=...) can actually match.
    _REAL_ARIA_SOURCES = ("aria", "label", "placeholder", "own_text")

    def locator_candidates(self) -> list[dict]:
        """Ranked locator strategies for this element, most robust first. Mirrors
        CapabilityArtifact.LocatorCandidate (src/artifacts/schema.py).

        Deliberately NOT one-size-fits-all: which strategies are even offered
        depends on where the element's name came from (self.name_source), because
        Playwright's get_by_role/get_by_text only resolve names the browser's own
        accessibility tree agrees with. A name borrowed from a neighboring table
        cell (the common case in this app's legacy, label-less table forms) needs
        a different, structural strategy -- 'label_adjacent' -- not role_name.
        """
        candidates = []
        if self.name and self.name_source in self._REAL_ARIA_SOURCES:
            candidates.append({
                "strategy": "role_name", "role": self.role, "name": self.name, "confidence": 0.95,
            })
        if self.name and self.role in ("button", "link") and self.name_source == "own_text":
            candidates.append({
                "strategy": "text", "text": self.name, "confidence": 0.7,
            })
        if self.name and self.name_source in ("nearby_text", "title") and self.role in (
            "textbox", "combobox", "checkbox", "radio"
        ):
            candidates.append({
                "strategy": "label_adjacent", "text": self.name, "role": self.role, "confidence": 0.85,
            })
        candidates.append({
            "strategy": "nth_of_role", "role": self.role, "nth": self._nth_hint(), "confidence": 0.4,
        })
        return candidates

    def _nth_hint(self) -> int:
        # populated by PageSnapshot.elements ordering; see PageSnapshot.to_numbered_text
        return getattr(self, "_nth", 0)


@dataclass
class PageSnapshot:
    url: str
    title: str
    heading: Optional[str]
    body_text: str
    elements: list[ElementSnapshot]
    screenshot_path: Optional[str] = None

    def to_numbered_text(self) -> str:
        """Human/LLM-readable numbered element list -- the 'set of marks' the agent
        references when choosing an action (e.g. 'click element 4')."""
        role_counts: dict[str, int] = {}
        lines = [f"URL: {self.url}", f"Title: {self.title}"]
        if self.heading:
            lines.append(f"Heading: {self.heading}")
        lines.append("Interactive elements:")
        for el in self.elements:
            n = role_counts.get(el.role, 0)
            el._nth = n  # type: ignore[attr-defined]
            role_counts[el.role] = n + 1
            desc = f"[{el.index}] {el.role}"
            if el.name:
                desc += f' "{el.name}"'
            if el.field_name:
                desc += f" (field={el.field_name})"
            if el.value:
                desc += f" value={el.value!r}"
            if el.options:
                desc += f" options={el.options}"
            lines.append("  " + desc)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "heading": self.heading,
            "elements": [asdict(e) for e in self.elements],
        }


def capture(page, screenshot_path: Optional[str] = None) -> PageSnapshot:
    """Capture a PageSnapshot from a live Playwright page."""
    raw_elements = page.evaluate(_SNAPSHOT_JS)
    signals = page.evaluate(_PAGE_SIGNALS_JS)
    elements = [ElementSnapshot(**e) for e in raw_elements]
    if screenshot_path:
        page.screenshot(path=screenshot_path, full_page=True)
    return PageSnapshot(
        url=signals["url"],
        title=signals["title"],
        heading=signals["heading"],
        body_text=signals["body_text"],
        elements=elements,
        screenshot_path=screenshot_path,
    )
