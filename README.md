# cicd-agent

Automated CI/CD pipeline management — stages, parallel execution, artifact tracking, triggers, and deployment strategies.

Zero external dependencies. Python 3.10+. Uses dataclasses and type hints throughout.

## Architecture

```
cicd_agent/
├── __init__.py       # Public API
├── stage.py          # Stage with success/failure handling
├── pipeline.py       # Pipeline DAG orchestration with parallel execution
├── artifact.py       # Artifact tracking with SHA-256 checksums
├── trigger.py        # Event-based pipeline invocation
├── deploy.py         # Blue-green, canary, rolling, and rollback strategies
tests/
└── test_cicd_agent.py  # Comprehensive test suite
```

Legacy modules (`cicd.py`, `git_poller.py`, `test_runner.py`, `reporter.py`, `webhook_server.py`, `cli.py`) remain for backward compatibility.

## Quick Start

```python
from cicd_agent import (
    Pipeline, PipelineConfig,
    Stage, StageResult, StageStatus,
    Deployer, DeployStrategy,
    ArtifactManager,
    TriggerManager, TriggerType,
)

# Define stages
def build(ctx):
    return StageResult(name="build", status=StageStatus.PASSED, message="Built successfully")

def test(ctx):
    return StageResult(name="test", status=StageStatus.PASSED, message="12 passed")

def deploy(ctx):
    deployer = Deployer()
    result = deployer.deploy(DeployStrategy.BLUE_GREEN, "prod", "1.0.0")
    return StageResult(
        name="deploy",
        status=StageStatus.PASSED if result.success else StageStatus.FAILED,
    )

# Build pipeline
pipeline = Pipeline(config=PipelineConfig(name="my-project"))
pipeline.add_stage(Stage(name="build", action=build, gate=True))
pipeline.add_stage(Stage(name="test", action=test, depends_on=["build"]))
pipeline.add_stage(Stage(name="deploy", action=deploy, depends_on=["test"]))

# Execute
run = pipeline.execute(commit_sha="abc123", branch="main")
print(f"Status: {run.status.value}, Duration: {run.total_duration:.2f}s")
```

## Pipeline Stages

Stages declare dependencies and the pipeline builds a DAG. Independent stages execute in parallel.

```python
from cicd_agent import Stage, StageResult, StageStatus

stage = Stage(
    name="build",
    action=lambda ctx: StageResult(name="build", status=StageStatus.PASSED),
    depends_on=["checkout"],     # runs after "checkout"
    gate=True,                   # failure blocks downstream stages
    retry_count=2,               # retry up to 2 times on failure
    timeout=60.0,                # max 60 seconds
)
```

## Artifact Tracking

```python
from cicd_agent import ArtifactManager

mgr = ArtifactManager(base_dir="./artifacts")
art = mgr.register("app.zip", "/path/to/app.zip", version="2.0", artifact_type="build")
assert art.verify()              # SHA-256 checksum validation
found = mgr.find(artifact_type="build")
mgr.cleanup(keep=20)            # keep only latest 20
```

## Triggers

```python
from cicd_agent import TriggerManager, TriggerType

triggers = TriggerManager()
triggers.register("on-push", TriggerType.WEBHOOK, callback=run_pipeline)
triggers.register("deploy-tag", TriggerType.COMMIT_PATTERN, pattern=r"\[deploy\]", callback=deploy)
triggers.fire(TriggerType.WEBHOOK, source="my-repo", commit_sha="abc123", commit_message="fix [deploy]")
```

## Deployment Strategies

```python
from cicd_agent import Deployer, DeployStrategy

deployer = Deployer()

# Direct
deployer.deploy(DeployStrategy.DIRECT, "prod", "2.0")

# Blue-green
deployer.deploy(DeployStrategy.BLUE_GREEN, "prod", "2.0", previous_version="1.0")

# Canary with custom steps
deployer.deploy(DeployStrategy.CANARY, "prod", "2.0", canary_steps=[10, 25, 50, 100])

# Rolling
deployer.deploy(DeployStrategy.ROLLING, "prod", "2.0",
                deploy_config={"instances": 6, "batch_size": 2})

# Rollback
deployer.rollback("prod", "1.0")
```

## Running Tests

```bash
python3 -m pytest tests/ -v
```

## Design Principles

- **Zero dependencies** — stdlib only, runs anywhere Python 3.10+ is available
- **DAG execution** — stages declare dependencies; independent stages run in parallel
- **Gate conditions** — critical stages can block the entire pipeline on failure
- **Checksum integrity** — all artifacts tracked with SHA-256, verifiable at any time
- **Pluggable strategies** — inject custom deploy actions and health checks for testing
- **Dataclass-based** — full type hints, serializable, no magic
