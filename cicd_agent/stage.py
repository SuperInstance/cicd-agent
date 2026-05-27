"""
Stage — Individual pipeline stage with success/failure handling.
================================================================
Each stage represents a discrete step in a CI/CD pipeline
(build, test, lint, deploy, etc.) with timing, status tracking,
and gate conditions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional


class StageStatus(Enum):
    """Possible states for a pipeline stage."""
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    WARNING = "warning"


class StageName(Enum):
    """Built-in stage names for standard pipelines."""
    BUILD = "build"
    TEST = "test"
    LINT = "lint"
    VALIDATE = "validate"
    SECURITY = "security"
    PACKAGE = "package"
    DEPLOY = "deploy"
    NOTIFY = "notify"
    REPORT = "report"


@dataclass
class StageResult:
    """Result of a single pipeline stage execution."""
    name: str
    status: StageStatus
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration: float = 0.0
    message: str = ""
    data: dict = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": round(self.duration, 3),
            "message": self.message,
            "data": self.data,
            "artifacts": self.artifacts,
            "warnings": self.warnings,
        }

    @property
    def ok(self) -> bool:
        """True if stage passed or has warnings (not a hard failure)."""
        return self.status in (StageStatus.PASSED, StageStatus.WARNING, StageStatus.SKIPPED)


@dataclass
class Stage:
    """A single pipeline stage with an executable action.

    Attributes:
        name: Identifier for this stage.
        action: Callable that performs the stage work. Receives a dict of
                context from previous stages and must return a StageResult.
        depends_on: List of stage names that must complete before this one runs.
        optional: If True, failure won't stop the pipeline.
        gate: If True, pipeline stops if this stage fails (even if optional).
        timeout: Max seconds this stage is allowed to run.
        retry_count: Number of automatic retries on failure.
    """

    name: str
    action: Callable[[dict], StageResult]
    depends_on: list[str] = field(default_factory=list)
    optional: bool = False
    gate: bool = False
    timeout: float = 300.0
    retry_count: int = 0
    retry_delay: float = 2.0

    def execute(self, context: dict) -> StageResult:
        """Run this stage with the given context, handling retries and timing."""
        last_result: Optional[StageResult] = None

        for attempt in range(1 + self.retry_count):
            started = datetime.now(timezone.utc)
            try:
                result = self.action(context)
                if not isinstance(result, StageResult):
                    result = StageResult(
                        name=self.name,
                        status=StageStatus.PASSED if result else StageStatus.FAILED,
                        message=str(result),
                    )
            except Exception as exc:
                result = StageResult(
                    name=self.name,
                    status=StageStatus.FAILED,
                    message=f"Exception: {exc}",
                )

            finished = datetime.now(timezone.utc)
            result.name = self.name
            result.started_at = started.isoformat()
            result.finished_at = finished.isoformat()
            result.duration = (finished - started).total_seconds()
            last_result = result

            if result.status in (StageStatus.PASSED, StageStatus.WARNING, StageStatus.SKIPPED):
                return result

            if attempt < self.retry_count:
                time.sleep(self.retry_delay)

        return last_result or StageResult(
            name=self.name,
            status=StageStatus.FAILED,
            message="No result produced",
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "depends_on": self.depends_on,
            "optional": self.optional,
            "gate": self.gate,
            "timeout": self.timeout,
            "retry_count": self.retry_count,
        }
