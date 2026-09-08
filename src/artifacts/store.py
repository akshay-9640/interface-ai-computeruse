"""Load/save capability artifacts as versioned JSON files on disk. One file per
version (capability_id.v{version}.json) so an artifact is never silently overwritten
-- a reviewer or a calling agent can always see the exact version they're invoking."""
from __future__ import annotations

import json
from pathlib import Path

from src.artifacts.schema import CapabilityArtifact

DEFAULT_ARTIFACT_DIR = Path(__file__).resolve().parents[2] / "artifacts_store"


def save(artifact: CapabilityArtifact, directory: Path = DEFAULT_ARTIFACT_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{artifact.capability_id}.v{artifact.version}.json"
    path.write_text(artifact.model_dump_json_pretty())
    latest = directory / f"{artifact.capability_id}.latest.json"
    latest.write_text(artifact.model_dump_json_pretty())
    return path


def load(path: Path) -> CapabilityArtifact:
    data = json.loads(Path(path).read_text())
    return CapabilityArtifact.model_validate(data)


def load_latest(capability_id: str, directory: Path = DEFAULT_ARTIFACT_DIR) -> CapabilityArtifact:
    return load(directory / f"{capability_id}.latest.json")


def list_capabilities(directory: Path = DEFAULT_ARTIFACT_DIR) -> list[CapabilityArtifact]:
    """Small catalog listing -- backs the optional 'agent-facing capability
    interface' stretch goal (CLI `list` / `invoke` commands)."""
    if not directory.exists():
        return []
    out = []
    for p in sorted(directory.glob("*.latest.json")):
        out.append(load(p))
    return out
