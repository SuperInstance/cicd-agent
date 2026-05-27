"""
cicd_agent — CI/CD Pipeline Engine
===================================
Automated pipeline management for building, testing, and deploying.

Provides pipeline orchestration with stages, parallel execution, gates,
artifact tracking, event-based triggers, and multiple deployment strategies.
"""

from __future__ import annotations

from cicd_agent.stage import Stage, StageName, StageResult, StageStatus
from cicd_agent.pipeline import Pipeline, PipelineConfig, PipelineRun, PipelineStatus
from cicd_agent.artifact import Artifact, ArtifactManager
from cicd_agent.trigger import TriggerEvent, TriggerManager, TriggerType
from cicd_agent.deploy import Deployer, DeployStrategy, DeployResult

__all__ = [
    "Stage",
    "StageName",
    "StageResult",
    "StageStatus",
    "Pipeline",
    "PipelineConfig",
    "PipelineRun",
    "PipelineStatus",
    "Artifact",
    "ArtifactManager",
    "TriggerEvent",
    "TriggerManager",
    "TriggerType",
    "Deployer",
    "DeployStrategy",
    "DeployResult",
]

__version__ = "0.2.0"
