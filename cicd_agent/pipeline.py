"""
Pipeline — Orchestrate stages with parallel execution and gates.
================================================================
Manages the lifecycle of a CI/CD pipeline: builds a DAG of stages,
executes them respecting dependencies, collects results, and handles
gate conditions.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from cicd_agent.stage import Stage, StageResult, StageStatus


class PipelineStatus(Enum):
    """Overall pipeline status."""
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"      # some optional stages failed
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


@dataclass
class PipelineConfig:
    """Configuration for a pipeline run."""
    name: str = "default"
    max_workers: int = 4
    timeout: float = 600.0
    fail_fast: bool = False          # stop on first gate failure
    skip_patterns: list[str] = field(default_factory=lambda: ["[skip-ci]"])
    retry_count: int = 0
    tags: list[str] = field(default_factory=list)

    def should_skip(self, message: str) -> bool:
        if not message:
            return False
        return any(p in message for p in self.skip_patterns)


@dataclass
class PipelineRun:
    """Record of a single pipeline execution."""
    run_id: str = ""
    pipeline_name: str = ""
    status: PipelineStatus = PipelineStatus.PENDING
    trigger: str = "manual"
    commit_sha: str = ""
    commit_message: str = ""
    branch: str = "main"
    stage_results: list[StageResult] = field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    total_duration: float = 0.0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.run_id:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            self.run_id = f"{self.pipeline_name}-{ts}-{uuid.uuid4().hex[:6]}"

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "pipeline_name": self.pipeline_name,
            "status": self.status.value,
            "trigger": self.trigger,
            "commit_sha": self.commit_sha,
            "commit_message": self.commit_message,
            "branch": self.branch,
            "stage_results": [r.to_dict() for r in self.stage_results],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_duration": round(self.total_duration, 3),
            "metadata": self.metadata,
        }

    @property
    def passed(self) -> bool:
        return self.status in (PipelineStatus.PASSED, PipelineStatus.PARTIAL)


class Pipeline:
    """Orchestrate a series of stages into a CI/CD pipeline.

    Stages declare dependencies (``depends_on``). The pipeline builds a DAG
    and executes stages in topological order, running independent stages in
    parallel up to ``max_workers``.

    Gate stages will halt the pipeline on failure (unless ``fail_fast`` is
    False, in which case remaining non-dependent stages still run).

    Usage::

        def build(ctx):
            ...
            return StageResult(name="build", status=StageStatus.PASSED)

        def test(ctx):
            ...
            return StageResult(name="test", status=StageStatus.PASSED)

        pipeline = Pipeline(name="my-project")
        pipeline.add_stage(Stage(name="build", action=build, gate=True))
        pipeline.add_stage(Stage(name="test", action=test, depends_on=["build"]))
        run = pipeline.execute(commit_sha="abc123")
    """

    def __init__(self, config: Optional[PipelineConfig] = None):
        self.config = config or PipelineConfig()
        self._stages: dict[str, Stage] = {}
        self._runs: list[PipelineRun] = []

    @property
    def name(self) -> str:
        return self.config.name

    def add_stage(self, stage: Stage) -> "Pipeline":
        """Add a stage to the pipeline. Returns self for chaining."""
        self._stages[stage.name] = stage
        return self

    def remove_stage(self, name: str) -> bool:
        """Remove a stage by name."""
        return self._stages.pop(name, None) is not None

    def list_stages(self) -> list[str]:
        """Return stage names in no particular order."""
        return list(self._stages.keys())

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(
        self,
        trigger: str = "manual",
        commit_sha: str = "",
        commit_message: str = "",
        branch: str = "main",
        metadata: Optional[dict] = None,
    ) -> PipelineRun:
        """Run all stages respecting dependencies and gates."""
        run = PipelineRun(
            pipeline_name=self.config.name,
            trigger=trigger,
            commit_sha=commit_sha,
            commit_message=commit_message,
            branch=branch,
            metadata=metadata or {},
        )
        run.started_at = datetime.now(timezone.utc).isoformat()

        # Skip check
        if self.config.should_skip(commit_message):
            run.status = PipelineStatus.SKIPPED
            run.finished_at = datetime.now(timezone.utc).isoformat()
            self._runs.append(run)
            return run

        run.status = PipelineStatus.RUNNING

        # Topological sort
        order = self._topological_sort()
        completed: dict[str, StageResult] = {}
        failed_gates: set[str] = set()
        cancelled: set[str] = set()

        start_time = time.monotonic()

        # Group stages by "wave" (depth in DAG)
        waves = self._compute_waves(order)

        for wave in waves:
            if self.config.fail_fast and failed_gates:
                # Cancel all remaining stages
                for sname in wave:
                    cancelled.add(sname)
                continue

            # Filter out stages whose deps include a failed gate
            runnable = []
            for sname in wave:
                stage = self._stages[sname]
                if any(d in failed_gates for d in stage.depends_on):
                    cancelled.add(sname)
                else:
                    runnable.append(sname)

            if not runnable:
                continue

            # Execute wave in parallel
            if len(runnable) == 1:
                results = {runnable[0]: self._run_stage(runnable[0], completed)}
            else:
                results = self._run_parallel(runnable, completed)

            for sname, result in results.items():
                run.stage_results.append(result)
                completed[sname] = result
                if result.status == StageStatus.FAILED:
                    stage = self._stages[sname]
                    if stage.gate or not stage.optional:
                        failed_gates.add(sname)

            # Timeout check
            elapsed = time.monotonic() - start_time
            if elapsed > self.config.timeout:
                run.status = PipelineStatus.TIMEOUT
                break

        # Determine final status
        if run.status == PipelineStatus.RUNNING:
            if failed_gates:
                run.status = PipelineStatus.FAILED
            else:
                # Check for optional failures
                has_optional_failure = any(
                    r.status == StageStatus.FAILED
                    for r in run.stage_results
                    if r.name in self._stages and self._stages[r.name].optional
                )
                run.status = PipelineStatus.PARTIAL if has_optional_failure else PipelineStatus.PASSED

        # Add cancelled stages
        for sname in cancelled:
            run.stage_results.append(StageResult(
                name=sname,
                status=StageStatus.CANCELLED,
                message="Cancelled due to upstream gate failure",
            ))

        run.finished_at = datetime.now(timezone.utc).isoformat()
        run.total_duration = time.monotonic() - start_time
        self._runs.append(run)
        return run

    def _run_stage(self, name: str, context: dict[str, StageResult]) -> StageResult:
        """Execute a single stage with context from completed stages."""
        stage = self._stages[name]
        ctx = {n: r.to_dict() for n, r in context.items()}
        return stage.execute(ctx)

    def _run_parallel(
        self, names: list[str], context: dict[str, StageResult]
    ) -> dict[str, StageResult]:
        """Execute multiple independent stages in parallel."""
        results: dict[str, StageResult] = {}
        max_w = min(self.config.max_workers, len(names))
        with ThreadPoolExecutor(max_workers=max_w) as pool:
            futures = {pool.submit(self._run_stage, n, context): n for n in names}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as exc:
                    results[name] = StageResult(
                        name=name,
                        status=StageStatus.FAILED,
                        message=f"Parallel execution error: {exc}",
                    )
        return results

    # ------------------------------------------------------------------
    # DAG utilities
    # ------------------------------------------------------------------

    def _topological_sort(self) -> list[str]:
        """Kahn's algorithm for topological sort."""
        in_degree: dict[str, int] = defaultdict(int)
        graph: dict[str, list[str]] = defaultdict(list)

        for name, stage in self._stages.items():
            if name not in in_degree:
                in_degree[name] = 0
            for dep in stage.depends_on:
                graph[dep].append(name)
                in_degree[name] += 1

        queue = [n for n, d in in_degree.items() if d == 0]
        order: list[str] = []

        while queue:
            node = queue.pop(0)
            order.append(node)
            for neighbour in graph[node]:
                in_degree[neighbour] -= 1
                if in_degree[neighbour] == 0:
                    queue.append(neighbour)

        if len(order) != len(self._stages):
            raise ValueError("Cycle detected in pipeline stage dependencies")
        return order

    def _compute_waves(self, order: list[str]) -> list[list[str]]:
        """Group stages into parallel execution waves."""
        wave_map: dict[str, int] = {}
        for name in order:
            stage = self._stages[name]
            if not stage.depends_on:
                wave_map[name] = 0
            else:
                wave_map[name] = max(wave_map.get(d, 0) for d in stage.depends_on) + 1

        waves: dict[int, list[str]] = defaultdict(list)
        for name, wave_num in wave_map.items():
            waves[wave_num].append(name)
        return [waves[i] for i in sorted(waves.keys())]

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def get_runs(self, limit: int = 20) -> list[PipelineRun]:
        """Return recent pipeline runs."""
        return list(self._runs[-limit:])

    def get_last_run(self) -> Optional[PipelineRun]:
        """Return the most recent pipeline run."""
        return self._runs[-1] if self._runs else None

    def summary(self) -> dict:
        """Return a summary of all runs."""
        total = len(self._runs)
        passed = sum(1 for r in self._runs if r.status == PipelineStatus.PASSED)
        return {
            "pipeline": self.config.name,
            "total_runs": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": f"{(passed / total * 100):.1f}%" if total else "N/A",
        }
