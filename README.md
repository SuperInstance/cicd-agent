# cicd-agent — Fleet CI/CD Pipeline Engine

**Watch fleet repos, run tests, validate builds, generate reports, trigger deployments. Fully automated.**

## What This Gives You

- **Webhook server** — receive GitHub push/PR events and trigger pipelines
- **Test runner** — discover and execute test suites across fleet repos
- **Build validation** — compile, lint, and validate before merge
- **Deployment engine** — staged deployments with rollback capability
- **Pipeline stages** — lint → test → build → deploy with configurable gates
- **Reporting** — test results, coverage summaries, and deployment status

## Quick Start

```bash
pip install cicd-agent
```

```python
from cicd_agent import Pipeline, Stage, DeployTarget

# Define a pipeline
pipeline = Pipeline(name="fleet-deploy")
pipeline.add_stage(Stage(name="lint", command="ruff check ."))
pipeline.add_stage(Stage(name="test", command="pytest tests/"))
pipeline.add_stage(Stage(name="build", command="python -m build"))
pipeline.add_stage(Stage(
    name="deploy",
    action=DeployTarget(target="staging"),
    gate="manual"  # requires approval
))

# Run the pipeline
result = pipeline.run(repo_path="/path/to/repo")
print(result.status)  # PASSED / FAILED
print(result.stages)  # [{name: "lint", status: "passed"}, ...]
```

### CLI

```bash
# Start the webhook server
cicd-agent serve --port 8080

# Run a pipeline manually
cicd-agent run --repo ./my-repo --pipeline deploy

# Check pipeline status
cicd-agent status
```

## API Reference

### `Pipeline(name)` — `add_stage(stage)`, `run(repo_path) → PipelineResult`
### `Stage(name, command=None, action=None, gate=None)`
### `DeployTarget(target, rollback=True)`
### `TestRunner` — Discover and execute tests
### `Reporter` — Generate test/build reports

## How It Fits

The CI/CD backbone of the [SuperInstance fleet](https://github.com/SuperInstance). Every commit to a fleet repo triggers this pipeline.

- **[branch-sandbox](https://github.com/SuperInstance/branch-sandbox)** — Isolated test execution
- **[clawcommit-lucid](https://github.com/SuperInstance/clawcommit-lucid)** — Validates commit message format
- **[fleet-health-monitor](https://github.com/SuperInstance/fleet-health-monitor)** — Post-deploy health checks
- **[co-captain-git-agent](https://github.com/SuperInstance/co-captain-git-agent)** — Human gates for deployments

## Testing

```bash
pytest tests/
```

## Installation

```bash
pip install cicd-agent
```

Python 3.10+. MIT license.
