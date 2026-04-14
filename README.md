# Fleet CI/CD Agent

Continuous Integration and Continuous Deployment for the Pelagic fleet.

Watches fleet repos for changes, runs tests, validates builds, generates reports, and triggers deployment pipelines — all with **stdlib only** (zero external dependencies).

## Architecture

```
fleet/cicd-agent/
├── cicd.py              # Pipeline engine (orchestrator, stages, lifecycle)
├── git_poller.py        # Git change detection (polling, commit parsing)
├── test_runner.py       # Test execution (subprocess, parallel, trends)
├── reporter.py          # Report generator (JSON, text, Markdown)
├── webhook_server.py    # GitHub webhook receiver (HTTP server)
├── cli.py               # Command-line interface
├── tests/
│   └── test_cicd_agent.py   # Comprehensive test suite
├── pyproject.toml
└── README.md
```

## Pipeline Stages

| Stage   | Description                                      |
|---------|--------------------------------------------------|
| Detect  | Poll git repos or receive webhooks for changes   |
| Test    | Run pytest for each changed repo                  |
| Lint    | Run `py_compile` on all `.py` files              |
| Validate| Check all tests pass, report coverage            |
| Report  | Generate CI/CD reports (JSON + human-readable)   |
| Notify  | Send results to fleet (MUD events, keeper log)   |
| Deploy  | Optional: deploy to staging/production            |

## Quick Start

```bash
# Onboard — set up directories and example config
python3 cli.py onboard

# Run pipeline for all configured repos
python3 cli.py run --all

# Run pipeline for a specific repo
python3 cli.py run --repo my-agent

# Start the full CI/CD service (polling + webhook)
python3 cli.py serve --interval 60 --webhook-port 9000

# Start webhook receiver only
python3 cli.py webhook-serve --port 9000 --secret your-github-secret
```

## Configuration

Create `.cicd/fleet-cicd.json`:

```json
{
  "repos": [
    {
      "name": "lighthouse-agent",
      "path": "./agents/lighthouse",
      "test_command": "python3 -m pytest tests/ -q",
      "lint_command": "python3 -m py_compile",
      "auto_deploy": false,
      "deploy_target": "local",
      "notify_on": ["failure", "recovery"],
      "max_retries": 2,
      "timeout": 120
    }
  ],
  "webhook": {
    "port": 9000,
    "secret": ""
  },
  "polling": {
    "interval": 60
  }
}
```

## Commit Message Triggers

| Tag          | Effect                        |
|--------------|-------------------------------|
| `[skip-ci]`  | Skip the CI pipeline entirely |
| `[deploy]`   | Flag for deployment (manual)  |
| `[urgent]`   | Mark as high-priority         |
| `[no-test]`  | Skip test stage               |
| `[force-ci]` | Force run even if skipped     |

## Webhook Endpoints

| Method | Path                | Description           |
|--------|---------------------|-----------------------|
| POST   | `/webhook/github`   | Receive push events   |
| GET    | `/status`           | Pipeline status       |
| GET    | `/history`          | Recent pipeline runs  |
| GET    | `/health`           | Health check          |

## Running Tests

```bash
cd fleet/cicd-agent
python3 -m pytest tests/ -v
```

## CLI Reference

```
fleet-cicd serve              Start the CI/CD service
fleet-cicd run --repo <name>  Run pipeline for a specific repo
fleet-cicd run --all          Run pipeline for all fleet repos
fleet-cicd status             Show pipeline status
fleet-cicd history [--repo]   Show pipeline history
fleet-cicd report [--format]  Generate report (json|text|markdown)
fleet-cicd webhook-serve      Start webhook receiver
fleet-cicd onboard            Set up the agent
```

## Design Principles

- **Stdlib only**: No external dependencies — runs anywhere Python 3.10+ is available
- **Pluggable stages**: Each pipeline stage is independent and composable
- **Parallel execution**: Multi-repo test runs via thread pool
- **Trend analysis**: Historical tracking with flaky test detection
- **Multi-format reports**: JSON, plain text, and Markdown output
- **Webhook + polling**: Dual-mode change detection
