"""
Deploy — Deployment strategies with blue-green, canary, and rollback.
=====================================================================
Provides multiple deployment strategies and a Deployer orchestrator
that manages deployment lifecycle including health checks and rollback.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional


class DeployStrategy(Enum):
    """Supported deployment strategies."""
    DIRECT = "direct"           # Immediate cutover
    BLUE_GREEN = "blue_green"   # Two-environment switch
    CANARY = "canary"           # Gradual traffic shift
    ROLLING = "rolling"         # Incremental instance updates
    ROLLBACK = "rollback"       # Revert to previous version


@dataclass
class DeployResult:
    """Result of a deployment operation.

    Attributes:
        strategy: The strategy used.
        target: Deployment target identifier.
        version: Version being deployed.
        previous_version: Version being replaced.
        success: Whether the deployment succeeded.
        message: Human-readable status message.
        started_at: ISO timestamp.
        finished_at: ISO timestamp.
        duration: Seconds elapsed.
        health_status: Post-deploy health check result.
        rollback_performed: Whether an automatic rollback occurred.
        metadata: Extra data (traffic percentages, instance counts, etc.).
    """

    strategy: Optional[DeployStrategy] = None
    target: str = ""
    version: str = ""
    previous_version: str = ""
    success: bool = False
    message: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration: float = 0.0
    health_status: str = "unknown"
    rollback_performed: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy.value if isinstance(self.strategy, DeployStrategy) else str(self.strategy),
            "target": self.target,
            "version": self.version,
            "previous_version": self.previous_version,
            "success": self.success,
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": round(self.duration, 3),
            "health_status": self.health_status,
            "rollback_performed": self.rollback_performed,
            "metadata": self.metadata,
        }


# Type aliases for injectable behaviors
DeployAction = Callable[[str, str, dict], bool]   # (target, version, config) -> success
HealthCheck = Callable[[str], bool]                # target -> healthy


class Deployer:
    """Orchestrate deployments with multiple strategies.

    Each strategy is implemented as a method, with injectable deploy
    actions and health checks for testing without real infrastructure.

    Usage::

        deployer = Deployer()
        result = deployer.deploy(
            strategy=DeployStrategy.BLUE_GREEN,
            target="production",
            version="2.0.0",
            previous_version="1.9.0",
        )
        if not result.success:
            deployer.rollback("production", "1.9.0")
    """

    def __init__(
        self,
        deploy_action: Optional[DeployAction] = None,
        health_check: Optional[HealthCheck] = None,
    ):
        self._deploy_action = deploy_action or self._default_deploy
        self._health_check = health_check or self._default_health_check
        self._deployments: list[DeployResult] = []
        self._active_versions: dict[str, str] = {}  # target -> version

    def deploy(
        self,
        strategy: DeployStrategy,
        target: str,
        version: str,
        previous_version: str = "",
        canary_steps: Optional[list[float]] = None,
        deploy_config: Optional[dict] = None,
    ) -> DeployResult:
        """Execute a deployment using the specified strategy."""
        config = deploy_config or {}
        previous = previous_version or self._active_versions.get(target, "unknown")

        handlers = {
            DeployStrategy.DIRECT: self._deploy_direct,
            DeployStrategy.BLUE_GREEN: self._deploy_blue_green,
            DeployStrategy.CANARY: self._deploy_canary,
            DeployStrategy.ROLLING: self._deploy_rolling,
        }

        handler = handlers.get(strategy)
        if handler is None:
            return DeployResult(
                strategy=strategy,
                target=target,
                version=version,
                success=False,
                message=f"Unknown strategy: {strategy.value}",
            )

        started = datetime.now(timezone.utc)
        result = handler(target, version, previous, config, canary_steps)
        result.strategy = strategy
        result.target = target
        result.version = version
        result.previous_version = previous
        result.started_at = started.isoformat()
        result.finished_at = datetime.now(timezone.utc).isoformat()
        result.duration = (datetime.now(timezone.utc) - started).total_seconds()

        if result.success:
            self._active_versions[target] = version
        self._deployments.append(result)
        return result

    def rollback(self, target: str, version: str, config: Optional[dict] = None) -> DeployResult:
        """Roll back to a previous version."""
        started = datetime.now(timezone.utc)
        success = self._deploy_action(target, version, config or {})
        result = DeployResult(
            strategy=DeployStrategy.ROLLBACK,
            target=target,
            version=version,
            previous_version=self._active_versions.get(target, "unknown"),
            success=success,
            message=f"Rolled back to {version}" if success else "Rollback failed",
            started_at=started.isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            rollback_performed=True,
        )
        result.duration = (datetime.now(timezone.utc) - started).total_seconds()
        if success:
            self._active_versions[target] = version
        self._deployments.append(result)
        return result

    def get_active_version(self, target: str) -> str:
        """Return the currently deployed version for a target."""
        return self._active_versions.get(target, "unknown")

    def get_history(self, limit: int = 20) -> list[dict]:
        """Return recent deployment results."""
        return [d.to_dict() for d in self._deployments[-limit:]]

    # ------------------------------------------------------------------
    # Strategy implementations
    # ------------------------------------------------------------------

    def _deploy_direct(
        self, target: str, version: str, previous: str, config: dict, _
    ) -> DeployResult:
        """Direct deployment — immediate cutover."""
        success = self._deploy_action(target, version, config)
        healthy = self._health_check(target) if success else False

        return DeployResult(
            success=success and healthy,
            message="Direct deploy complete" if success else "Deploy action failed",
            health_status="healthy" if healthy else ("unhealthy" if success else "unchecked"),
            rollback_performed=False,
        )

    def _deploy_blue_green(
        self, target: str, version: str, previous: str, config: dict, _
    ) -> DeployResult:
        """Blue-green deployment — deploy to idle environment, then switch."""
        # Deploy to "green" (new) environment
        green_target = f"{target}-green"
        green_ok = self._deploy_action(green_target, version, config)
        if not green_ok:
            return DeployResult(
                success=False,
                message="Green environment deploy failed",
                health_status="unchecked",
            )

        # Health check on green
        green_healthy = self._health_check(green_target)
        if not green_healthy:
            return DeployResult(
                success=False,
                message="Green environment unhealthy, aborting switch",
                health_status="unhealthy",
            )

        # Switch traffic (simplified — in reality this updates load balancer)
        switch_ok = self._deploy_action(target, version, config)
        if not switch_ok:
            # Rollback switch
            self._deploy_action(target, previous, config)
            return DeployResult(
                success=False,
                message="Traffic switch failed, rolled back",
                health_status="unknown",
                rollback_performed=True,
            )

        final_healthy = self._health_check(target)
        return DeployResult(
            success=final_healthy,
            message="Blue-green deploy complete" if final_healthy else "Post-switch health check failed",
            health_status="healthy" if final_healthy else "unhealthy",
        )

    def _deploy_canary(
        self, target: str, version: str, previous: str, config: dict,
        canary_steps: Optional[list[float]],
    ) -> DeployResult:
        """Canary deployment — gradually shift traffic percentage."""
        steps = canary_steps or [10.0, 50.0, 100.0]

        for pct in steps:
            step_ok = self._deploy_action(target, version, {
                **config,
                "canary_percent": pct,
            })
            if not step_ok:
                # Rollback to previous
                self._deploy_action(target, previous, config)
                return DeployResult(
                    success=False,
                    message=f"Canary failed at {pct}%, rolled back",
                    health_status="unhealthy",
                    rollback_performed=True,
                    metadata={"failed_step": pct, "steps": steps},
                )

            if not self._health_check(target):
                self._deploy_action(target, previous, config)
                return DeployResult(
                    success=False,
                    message=f"Health check failed at {pct}%, rolled back",
                    health_status="unhealthy",
                    rollback_performed=True,
                    metadata={"failed_step": pct, "steps": steps},
                )

        return DeployResult(
            success=True,
            message=f"Canary deploy complete ({len(steps)} steps)",
            health_status="healthy",
            metadata={"steps_completed": steps},
        )

    def _deploy_rolling(
        self, target: str, version: str, previous: str, config: dict, _
    ) -> DeployResult:
        """Rolling deployment — update instances one batch at a time."""
        total_instances = config.get("instances", 3)
        batch_size = config.get("batch_size", 1)
        batches = (total_instances + batch_size - 1) // batch_size

        for batch_num in range(1, batches + 1):
            batch_ok = self._deploy_action(target, version, {
                **config,
                "batch": batch_num,
                "batch_size": batch_size,
            })
            if not batch_ok:
                # Stop rolling, previous batches are on new version
                return DeployResult(
                    success=False,
                    message=f"Rolling deploy failed at batch {batch_num}/{batches}",
                    health_status="degraded",
                    metadata={"completed_batches": batch_num - 1, "total_batches": batches},
                )

            if not self._health_check(target):
                return DeployResult(
                    success=False,
                    message=f"Health check failed after batch {batch_num}/{batches}",
                    health_status="unhealthy",
                    metadata={"completed_batches": batch_num, "total_batches": batches},
                )

        return DeployResult(
            success=True,
            message=f"Rolling deploy complete ({batches} batches)",
            health_status="healthy",
            metadata={"total_batches": batches, "instances": total_instances},
        )

    # ------------------------------------------------------------------
    # Defaults (for testing — always succeed)
    # ------------------------------------------------------------------

    @staticmethod
    def _default_deploy(target: str, version: str, config: dict) -> bool:
        return True

    @staticmethod
    def _default_health_check(target: str) -> bool:
        return True
