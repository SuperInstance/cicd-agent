"""
Fleet CI/CD Agent CLI
======================
Command-line interface for the CI/CD pipeline engine.

Subcommands:
    serve          Start the CI/CD service (polling + webhook)
    run            Run pipeline for one or all repos
    status         Show pipeline status
    history        Show pipeline history
    report         Generate a report
    webhook-serve  Start webhook receiver only
    onboard        Set up the agent
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path

from cicd import (
    FleetCICD,
    PipelineConfig,
    PipelineStatus,
)
from reporter import CIReporter

logger = logging.getLogger("fleet.cicd.cli")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_DIR = ".cicd"
DEFAULT_CONFIG_FILE = "fleet-cicd.json"
DEFAULT_WEBHOOK_PORT = 9000
DEFAULT_POLL_INTERVAL = 60


def find_config_dir() -> Path:
    """Locate the CI/CD configuration directory."""
    # Check current directory
    cwd = Path.cwd()
    for candidate in [cwd / DEFAULT_CONFIG_DIR, cwd]:
        if candidate.is_dir():
            return candidate
    # Fall back to creating one
    path = cwd / DEFAULT_CONFIG_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_repos_from_config(config_dir: Path) -> list[PipelineConfig]:
    """Load repo configurations from a JSON file."""
    config_file = config_dir / DEFAULT_CONFIG_FILE
    if not config_file.exists():
        logger.info("No config file found at %s", config_file)
        return []

    try:
        with open(config_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load config: %s", e)
        return []

    repos = []
    for entry in data.get("repos", []):
        config = PipelineConfig(
            repo_name=entry.get("name", entry.get("repo_name", "unknown")),
            repo_path=entry.get("path", entry.get("repo_path", "")),
            test_command=entry.get("test_command", "python3 -m pytest tests/ -q"),
            lint_command=entry.get("lint_command", "python3 -m py_compile"),
            auto_deploy=entry.get("auto_deploy", False),
            deploy_target=entry.get("deploy_target", "local"),
            notify_on=entry.get("notify_on", ["failure", "recovery"]),
            max_retries=entry.get("max_retries", 2),
            timeout=entry.get("timeout", 120),
        )
        repos.append(config)
    return repos


def save_example_config(config_dir: Path) -> Path:
    """Write an example configuration file."""
    config_file = config_dir / DEFAULT_CONFIG_FILE
    example = {
        "repos": [
            {
                "name": "example-agent",
                "path": "./agents/example-agent",
                "test_command": "python3 -m pytest tests/ -q",
                "lint_command": "python3 -m py_compile",
                "auto_deploy": False,
                "deploy_target": "local",
                "notify_on": ["failure", "recovery"],
                "max_retries": 2,
                "timeout": 120,
            }
        ],
        "webhook": {
            "port": DEFAULT_WEBHOOK_PORT,
            "secret": "",
        },
        "polling": {
            "interval": DEFAULT_POLL_INTERVAL,
        },
    }
    config_file.write_text(json.dumps(example, indent=2))
    return config_file


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_serve(args):
    """Start the full CI/CD service."""
    config_dir = find_config_dir()
    repos = load_repos_from_config(config_dir)

    if args.config:
        extra = load_repos_from_config(Path(args.config))
        repos.extend(extra)

    if not repos:
        logger.warning("No repos configured. Create a %s file or use --config.",
                       DEFAULT_CONFIG_FILE)

    cicd = FleetCICD(config_dir=str(config_dir))
    for repo in repos:
        cicd.add_repo(repo)

    logger.info("Starting CI/CD service with %d repo(s)", len(repos))
    cicd.start_polling(interval=args.interval)
    cicd.start_webhook(
        port=args.webhook_port,
        secret=args.webhook_secret or "",
    )

    print(f"Fleet CI/CD Agent running (poll={args.interval}s, webhook=:{args.webhook_port})")
    print(f"Monitoring {len(repos)} repo(s)")
    print("Press Ctrl+C to stop")

    def signal_handler(sig, frame):
        print("\nShutting down...")
        cicd.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Block forever
    import time
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        cicd.shutdown()


def cmd_run(args):
    """Run pipeline for a specific repo or all repos."""
    config_dir = find_config_dir()
    repos = load_repos_from_config(config_dir)

    cicd = FleetCICD(config_dir=str(config_dir))
    for repo in repos:
        cicd.add_repo(repo)

    if args.all_flag:
        print("Running pipeline for all repos...")
        runs = cicd.run_all()
        for run in runs:
            status_icon = "✅" if run.status == PipelineStatus.PASSED else "❌"
            print(f"  {status_icon} {run.repo_name}: {run.status.value} ({run.total_duration:.1f}s)")
    elif args.repo:
        print(f"Running pipeline for {args.repo}...")
        run = cicd.run_repo(args.repo)
        if run:
            status_icon = "✅" if run.status == PipelineStatus.PASSED else "❌"
            print(f"  {status_icon} {run.repo_name}: {run.status.value} ({run.total_duration:.1f}s)")
            for stage in run.stages:
                icon = "✅" if stage.status == PipelineStatus.PASSED else "❌"
                print(f"    {icon} {stage.stage.value}: {stage.message}")
        else:
            print(f"  Error: repo '{args.repo}' not found")
    else:
        print("Error: specify --repo <name> or --all")
        sys.exit(1)


def cmd_status(args):
    """Show pipeline status."""
    config_dir = find_config_dir()
    cicd = FleetCICD(config_dir=str(config_dir))
    repos = load_repos_from_config(config_dir)
    for repo in repos:
        cicd.add_repo(repo)

    status = cicd.get_status()
    print("Fleet CI/CD Agent Status")
    print(f"  Repos monitored:  {status['repos_monitored']}")
    print(f"  Polling:          {'Yes' if status['polling'] else 'No'}")
    print(f"  Webhook:          {'Yes' if status['webhook'] else 'No'}")
    print(f"  Total runs:       {status['total_runs']}")
    print(f"  Passed:           {status['passed']}")
    print(f"  Failed:           {status['failed']}")
    print(f"  Pass rate:        {status['pass_rate']}")


def cmd_history(args):
    """Show pipeline history."""
    config_dir = find_config_dir()
    cicd = FleetCICD(config_dir=str(config_dir))
    repos = load_repos_from_config(config_dir)
    for repo in repos:
        cicd.add_repo(repo)

    history = cicd.get_history(repo_name=args.repo, limit=args.limit)
    if not history:
        print("No pipeline runs found.")
        return

    reporter = CIReporter()
    print(f"Recent pipeline runs (showing last {len(history)}):")
    print("-" * 70)
    for run in history:
        status = run.get("status", "unknown")
        icon = "✅" if status == "passed" else "❌" if status == "failed" else "⏭️"
        print(f"  {icon} {run.get('run_id', 'N/A'):<30} {status:<10} "
              f"{run.get('total_duration', 0):.1f}s  "
              f"{run.get('commit_sha', '')[:8]}")


def cmd_report(args):
    """Generate a report."""
    config_dir = find_config_dir()
    cicd = FleetCICD(config_dir=str(config_dir))
    repos = load_repos_from_config(config_dir)
    for repo in repos:
        cicd.add_repo(repo)

    history = cicd.get_history(repo_name=args.repo, limit=1)
    if not history:
        print("No pipeline runs found to report.")
        return

    reporter = CIReporter()
    fmt = args.format or "text"

    # Reconstruct a simple run object from the last run
    run_data = history[-1]

    if fmt == "json":
        print(json.dumps(run_data, indent=2))
    elif fmt == "markdown":
        print(reporter.generate_markdown(run_data))
    else:
        # Use the text reporter with the dict directly
        print(reporter.generate_text(run_data))


def cmd_webhook_serve(args):
    """Start the webhook receiver."""
    from webhook_server import WebhookServer

    server = WebhookServer(
        port=args.port,
        secret=args.secret or "",
    )
    print(f"Webhook server starting on port {args.port}")
    print(f"Endpoints:")
    print(f"  POST /webhook/github  — receive push events")
    print(f"  GET  /status          — pipeline status")
    print(f"  GET  /history         — recent events")
    print(f"  GET  /health          — health check")
    server.serve_forever()


def cmd_onboard(args):
    """Set up the CI/CD agent."""
    config_dir = find_config_dir()
    print(f"Setting up Fleet CI/CD Agent in {config_dir}")

    # Create directory structure
    (config_dir / "repos").mkdir(parents=True, exist_ok=True)
    (config_dir / "artifacts" / "reports").mkdir(parents=True, exist_ok=True)
    (config_dir / "artifacts" / "coverage").mkdir(parents=True, exist_ok=True)
    (config_dir / "artifacts" / "logs").mkdir(parents=True, exist_ok=True)

    # Write example config
    config_path = save_example_config(config_dir)
    print(f"  Created config: {config_path}")

    # Create __init__.py for the package
    init_file = Path(__file__).parent / "__init__.py"
    if not init_file.exists():
        init_file.write_text('"""Fleet CI/CD Agent."""\n')
        print(f"  Created: {init_file}")

    print()
    print("Onboard complete! Next steps:")
    print(f"  1. Edit {config_path} to add your repos")
    print(f"  2. Run: python3 cli.py serve")
    print(f"  3. Or run a one-off: python3 cli.py run --all")


# ---------------------------------------------------------------------------
# Argument Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet-cicd",
        description="Fleet CI/CD Agent — git polling, test runner, report generator, webhook receiver",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # serve
    serve_p = sub.add_parser("serve", help="Start the CI/CD service")
    serve_p.add_argument("--config", "-c", help="Path to additional config JSON")
    serve_p.add_argument("--interval", "-i", type=int, default=DEFAULT_POLL_INTERVAL,
                         help=f"Polling interval in seconds (default: {DEFAULT_POLL_INTERVAL})")
    serve_p.add_argument("--webhook-port", "-w", type=int, default=DEFAULT_WEBHOOK_PORT,
                         help=f"Webhook server port (default: {DEFAULT_WEBHOOK_PORT})")
    serve_p.add_argument("--webhook-secret", "-s", default="", help="GitHub webhook secret")
    serve_p.set_defaults(func=cmd_serve)

    # run
    run_p = sub.add_parser("run", help="Run pipeline for one or all repos")
    run_p.add_argument("--repo", "-r", help="Run pipeline for a specific repo")
    run_p.add_argument("--all", dest="all_flag", action="store_true", help="Run for all repos")
    run_p.set_defaults(func=cmd_run)

    # status
    status_p = sub.add_parser("status", help="Show pipeline status")
    status_p.set_defaults(func=cmd_status)

    # history
    hist_p = sub.add_parser("history", help="Show pipeline history")
    hist_p.add_argument("--repo", "-r", help="Filter by repo name")
    hist_p.add_argument("--limit", "-n", type=int, default=20, help="Number of runs to show")
    hist_p.set_defaults(func=cmd_history)

    # report
    report_p = sub.add_parser("report", help="Generate a report")
    report_p.add_argument("--repo", "-r", help="Filter by repo name")
    report_p.add_argument("--format", "-f", choices=["json", "text", "markdown"],
                          default="text", help="Report format (default: text)")
    report_p.set_defaults(func=cmd_report)

    # webhook-serve
    wh_p = sub.add_parser("webhook-serve", help="Start webhook receiver")
    wh_p.add_argument("--port", "-p", type=int, default=DEFAULT_WEBHOOK_PORT,
                      help=f"Port (default: {DEFAULT_WEBHOOK_PORT})")
    wh_p.add_argument("--secret", "-s", default="", help="GitHub webhook secret")
    wh_p.set_defaults(func=cmd_webhook_serve)

    # onboard
    onboard_p = sub.add_parser("onboard", help="Set up the agent")
    onboard_p.set_defaults(func=cmd_onboard)

    return parser


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main():
    parser = build_parser()
    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
