"""Error-handler resolution: merges an artifact's own error_handlers with the
target app's default taxonomy (src/policy/error_taxonomy.yaml), then checks a page
snapshot against every handler's match_text. See src/policy/error_taxonomy.yaml for
why app-level defaults exist."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import yaml

from src.artifacts.schema import CapabilityArtifact, ErrorHandler
from src.perception.snapshot import PageSnapshot

_TAXONOMY_PATH = Path(__file__).resolve().parents[2] / "src" / "policy" / "error_taxonomy.yaml"


def _load_app_defaults(app_name: str) -> list[ErrorHandler]:
    if not _TAXONOMY_PATH.exists():
        return []
    data = yaml.safe_load(_TAXONOMY_PATH.read_text()) or {}
    raw = data.get(app_name, [])
    return [ErrorHandler(**h) for h in raw]


def effective_handlers(artifact: CapabilityArtifact) -> list[ErrorHandler]:
    """Artifact-declared handlers win over app defaults on handler_id collision;
    everything else from the app defaults is included too."""
    defaults = _load_app_defaults(artifact.target.app_name)
    by_id = {h.handler_id: h for h in defaults}
    for h in artifact.error_handlers:
        by_id[h.handler_id] = h
    return list(by_id.values())


def match(snapshot: PageSnapshot, handlers: list[ErrorHandler]) -> Optional[ErrorHandler]:
    haystack = f"{snapshot.heading or ''}\n{snapshot.body_text}"
    for h in handlers:
        if re.search(re.escape(h.match_text), haystack, re.IGNORECASE):
            return h
    return None
