"""
Artifact — Artifact tracking with checksums and versioning.
===========================================================
Tracks build artifacts, test reports, and deployment packages
with content hashing, metadata, and lifecycle management.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class Artifact:
    """A tracked artifact with checksum and metadata.

    Attributes:
        name: Human-readable artifact name.
        path: Filesystem path to the artifact.
        checksum: SHA-256 hex digest of the artifact content.
        size_bytes: Size of the artifact in bytes.
        version: Semantic version or build number.
        artifact_type: Category (build, test-report, coverage, deployment, etc.).
        tags: Free-form tags for filtering.
        created_at: ISO timestamp when the artifact was registered.
        metadata: Additional key-value metadata.
    """

    name: str
    path: str
    checksum: str = ""
    size_bytes: int = 0
    version: str = ""
    artifact_type: str = "build"
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
        if self.checksum:
            return
        # Auto-compute checksum if file exists
        p = Path(self.path)
        if p.is_file():
            self.checksum = Artifact.sha256(p)
            self.size_bytes = p.stat().st_size

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
            "version": self.version,
            "artifact_type": self.artifact_type,
            "tags": self.tags,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    def verify(self) -> bool:
        """Verify the artifact's checksum matches its current file content."""
        p = Path(self.path)
        if not p.is_file():
            return False
        return Artifact.sha256(p) == self.checksum

    def copy_to(self, dest_dir: str) -> "Artifact":
        """Copy the artifact file to a new directory and return a new Artifact."""
        p = Path(self.path)
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / p.name
        shutil.copy2(p, target)
        return Artifact(
            name=self.name,
            path=str(target),
            checksum=Artifact.sha256(target),
            size_bytes=target.stat().st_size,
            version=self.version,
            artifact_type=self.artifact_type,
            tags=list(self.tags),
            metadata=dict(self.metadata),
        )

    @staticmethod
    def sha256(path: Path) -> str:
        """Compute SHA-256 hex digest of a file."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()


class ArtifactManager:
    """Register, store, query, and clean up pipeline artifacts.

    Usage::

        mgr = ArtifactManager(base_dir="/tmp/artifacts")
        art = mgr.register("build.zip", "/path/to/build.zip", version="1.0.0")
        assert art.verify()
        found = mgr.find(name="build.zip")
        mgr.cleanup(keep=10)
    """

    def __init__(self, base_dir: str = ".artifacts"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._artifacts: dict[str, Artifact] = {}
        self._index_file = self.base_dir / "index.json"
        self._load_index()

    def register(
        self,
        name: str,
        path: str,
        version: str = "",
        artifact_type: str = "build",
        tags: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
    ) -> Artifact:
        """Register a new artifact, computing checksum automatically."""
        art = Artifact(
            name=name,
            path=path,
            version=version,
            artifact_type=artifact_type,
            tags=tags or [],
            metadata=metadata or {},
        )
        self._artifacts[f"{name}:{version}" if version else name] = art
        self._save_index()
        return art

    def get(self, name: str, version: str = "") -> Optional[Artifact]:
        """Retrieve a registered artifact by name (and optionally version)."""
        key = f"{name}:{version}" if version else name
        return self._artifacts.get(key)

    def find(
        self,
        name: Optional[str] = None,
        artifact_type: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[Artifact]:
        """Query artifacts by name pattern, type, or tag."""
        results = list(self._artifacts.values())
        if name:
            results = [a for a in results if name in a.name]
        if artifact_type:
            results = [a for a in results if a.artifact_type == artifact_type]
        if tag:
            results = [a for a in results if tag in a.tags]
        return results

    def verify_all(self) -> dict[str, bool]:
        """Verify checksums for all registered artifacts."""
        return {
            key: art.verify()
            for key, art in self._artifacts.items()
        }

    def cleanup(self, keep: int = 50) -> int:
        """Remove oldest artifacts beyond ``keep`` count. Returns count removed."""
        items = sorted(
            self._artifacts.items(),
            key=lambda kv: kv[1].created_at,
            reverse=True,
        )
        to_keep = dict(items[:keep])
        removed = len(self._artifacts) - len(to_keep)
        self._artifacts = to_keep
        self._save_index()
        return removed

    def list_all(self) -> list[dict]:
        """Return all registered artifacts as dicts."""
        return [a.to_dict() for a in self._artifacts.values()]

    def remove(self, name: str, version: str = "") -> bool:
        """Unregister an artifact."""
        key = f"{name}:{version}" if version else name
        return self._artifacts.pop(key, None) is not None

    # -- Persistence --

    def _save_index(self):
        data = {k: v.to_dict() for k, v in self._artifacts.items()}
        with open(self._index_file, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def _load_index(self):
        if not self._index_file.exists():
            return
        try:
            with open(self._index_file) as f:
                data = json.load(f)
            for key, val in data.items():
                self._artifacts[key] = Artifact(**val)
        except (json.JSONDecodeError, TypeError):
            pass
