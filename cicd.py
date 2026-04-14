"""
Fleet CI/CD Pipeline Engine
============================
Continuous Integration and Continuous Deployment for the Pelagic fleet.

Watches fleet repos for changes, runs tests, validates builds,
generates reports, and triggers deployment pipelines.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from git_poller import GitPoller, GitCommit
from test_runner import TestRunner, TestResult
from reporter import CIReporter
from webhook_server import WebhookServer

logger = logging.getLogger("fleet.cicd")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

class PipelineStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    RETRYING = "retrying"


class StageName(Enum):
    DETECT = "detect"
    TEST = "test"
    LINT = "lint"
    VALIDATE = "validate"
    REPORT = "report"
    NOTIFY = "notify"
    DEPLOY = "deploy"


@dataclass
class PipelineConfig:
    """Per-repo pipeline configuration."""
    repo_name: str
    repo_path: str = ""
    test_command: str = "python3 -m pytest tests/ -q"
    lint_command: str = "python3 -m py_compile"
    auto_deploy: bool = False
    deploy_target: str = "local"  # local, docker, remote
    notify_on: list = field(default_factory=lambda: ["failure", "recovery"])
    max_retries: int = 2
    timeout: int = 120  # seconds
    skip_patterns: list = field(default_factory=lambda: ["[skip-ci]"])
    deploy_branch: str = "main"


@dataclass
class StageResult:
    """Result of a single pipeline stage."""
    stage: StageName
    status: PipelineStatus
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration: float = 0.0
    message: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "stage": self.stage.value,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": round(self.duration, 3),
            "message": self.message,
            "data": self.data,
        }


@dataclass
class PipelineRun:
    """A complete pipeline execution record."""
    run_id: str
    repo_name: str
    status: PipelineStatus = PipelineStatus.PENDING
    config: dict = field(default_factory=dict)
    trigger: str = "poll"  # poll, webhook, manual
    commit_sha: str = ""
    commit_message: str = ""
    branch: str = "main"
    stages: list = field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    total_duration: float = 0.0

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "repo_name": self.repo_name,
            "status": self.status.value,
            "config": self.config,
            "trigger": self.trigger,
            "commit_sha": self.commit_sha,
            "commit_message": self.commit_message,
            "branch": self.branch,
            "stages": [s.to_dict() if hasattr(s, "to_dict") else s for s in self.stages],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_duration": round(self.total_duration, 3),
        }


# ---------------------------------------------------------------------------
# Artifact Manager
# ---------------------------------------------------------------------------

class ArtifactManager:
    """Store and retrieve test results, coverage reports, and artifacts."""

    def __init__(self, base_dir: str = ".cicd-artifacts"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir = self.base_dir / "reports"
        self.reports_dir.mkdir(exist_ok=True)
        self.coverage_dir = self.base_dir / "coverage"
        self.coverage_dir.mkdir(exist_ok=True)
        self.logs_dir = self.base_dir / "logs"
        self.logs_dir.mkdir(exist_ok=True)
        logger.info("Artifact manager initialized at %s", self.base_dir)

    def save_report(self, run_id: str, data: dict, fmt: str = "json") -> Path:
        filename = f"{run_id}.{fmt}"
        path = self.reports_dir / filename
        if fmt == "json":
            with open(path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        else:
            path.write_text(str(data))
        logger.info("Report saved: %s", path)
        return path

    def save_log(self, run_id: str, content: str) -> Path:
        path = self.logs_dir / f"{run_id}.log"
        path.write_text(content)
        return path

    def list_reports(self) -> list[dict]:
        results = []
        for p in sorted(self.reports_dir.iterdir(), reverse=True):
            if p.suffix == ".json":
                try:
                    with open(p) as f:
                        results.append(json.load(f))
                except (json.JSONDecodeError, OSError):
                    continue
        return results

    def cleanup(self, keep_last: int = 50):
        for directory in [self.reports_dir, self.logs_dir]:
            files = sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            for f in files[keep_last:]:
                f.unlink()


# ---------------------------------------------------------------------------
# Deploy Manager
# ---------------------------------------------------------------------------

class DeployManager:
    """Handle deployment pipelines for local, docker, and remote targets."""

    def __init__(self):
        self.targets = {
            "local": self._deploy_local,
            "docker": self._deploy_docker,
            "remote": self._deploy_remote,
        }
        logger.info("Deploy manager initialized")

    def deploy(self, target: str, repo_path: str, config: dict = None) -> dict:
        """Execute deployment to the specified target."""
        handler = self.targets.get(target)
        if handler is None:
            return {
                "success": False,
                "message": f"Unknown deploy target: {target}",
            }
        return handler(repo_path, config or {})

    def _deploy_local(self, repo_path: str, config: dict) -> dict:
        """Deploy locally by running install/refresh scripts."""
        logger.info("Deploying locally: %s", repo_path)
        install_script = Path(repo_path) / "scripts" / "install.sh"
        if install_script.exists():
            try:
                result = subprocess.run(
                    ["bash", str(install_script)],
                    capture_output=True, text=True, timeout=120,
                )
                return {
                    "success": result.returncode == 0,
                    "message": "Local deploy complete",
                    "stdout": result.stdout[:500],
                    "stderr": result.stderr[:500],
                }
            except subprocess.TimeoutExpired:
                return {"success": False, "message": "Local deploy timed out"}
            except Exception as e:
                return {"success": False, "message": str(e)}
        return {"success": True, "message": "No install script found, deploy skipped"}

    def _deploy_docker(self, repo_path: str, config: dict) -> dict:
        """Deploy via Docker (docker build + docker run)."""
        logger.info("Deploying via Docker: %s", repo_path)
        dockerfile = Path(repo_path) / "Dockerfile"
        if not dockerfile.exists():
            return {"success": False, "message": "No Dockerfile found"}
        tag = config.get("docker_tag", f"fleet-{Path(repo_path).name}:latest")
        try:
            build = subprocess.run(
                ["docker", "build", "-t", tag, repo_path],
                capture_output=True, text=True, timeout=300,
            )
            if build.returncode != 0:
                return {"success": False, "message": "Docker build failed", "stderr": build.stderr[:500]}
            run = subprocess.run(
                ["docker", "run", "-d", "--restart", "unless-stopped", tag],
                capture_output=True, text=True, timeout=60,
            )
            return {
                "success": run.returncode == 0,
                "message": f"Deployed container: {tag}",
                "container_id": run.stdout.strip()[:12],
            }
        except FileNotFoundError:
            return {"success": False, "message": "Docker not found"}
        except subprocess.TimeoutExpired:
            return {"success": False, "message": "Docker deploy timed out"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def _deploy_remote(self, repo_path: str, config: dict) -> dict:
        """Deploy to a remote host via rsync + ssh."""
        logger.info("Deploying to remote: %s", repo_path)
        remote_host = config.get("remote_host", "deploy@fleet-node")
        remote_path = config.get("remote_path", "/opt/fleet")
        try:
            rsync = subprocess.run(
                ["rsync", "-avz", "--delete", f"{repo_path}/", f"{remote_host}:{remote_path}/"],
                capture_output=True, text=True, timeout=120,
            )
            restart = subprocess.run(
                ["ssh", remote_host, f"cd {remote_path} && systemctl restart fleet-service"],
                capture_output=True, text=True, timeout=60,
            )
            return {
                "success": restart.returncode == 0,
                "message": f"Remote deploy to {remote_host}",
                "rsync_output": rsync.stdout[:200],
                "restart_output": restart.stdout[:200],
            }
        except FileNotFoundError:
            return {"success": False, "message": "rsync/ssh not found"}
        except subprocess.TimeoutExpired:
            return {"success": False, "message": "Remote deploy timed out"}
        except Exception as e:
            return {"success": False, "message": str(e)}


# ---------------------------------------------------------------------------
# Notification Manager
# ---------------------------------------------------------------------------

class NotificationManager:
    """Send CI/CD results to fleet (MUD events, keeper log, lighthouse alert)."""

    def __init__(self, notify_channels: list = None):
        self.channels = notify_channels or ["log"]
        self.history: list[dict] = []

    def notify(self, run: PipelineRun, config: PipelineConfig):
        """Send notification based on config and run status."""
        should_notify = any(
            trigger in config.notify_on
            for trigger in self._get_triggers(run.status)
        )
        if not should_notify:
            return

        message = self._format_message(run)
        for channel in self.channels:
            self._send(channel, message, run)

        self.history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run.run_id,
            "repo": run.repo_name,
            "status": run.status.value,
            "channels": self.channels,
        })

    def _get_triggers(self, status: PipelineStatus) -> list[str]:
        if status == PipelineStatus.FAILED:
            return ["failure"]
        if status == PipelineStatus.PASSED:
            return ["recovery", "success"]
        return []

    def _format_message(self, run: PipelineRun) -> str:
        emoji = "✅" if run.status == PipelineStatus.PASSED else "❌"
        return (
            f"{emoji} CI/CD {run.status.value.upper()} | "
            f"repo={run.repo_name} run={run.run_id} "
            f"commit={run.commit_sha[:8]} duration={run.total_duration:.1f}s"
        )

    def _send(self, channel: str, message: str, run: PipelineRun):
        if channel == "log":
            level = logging.INFO if run.status == PipelineStatus.PASSED else logging.ERROR
            logger.log(level, "[NOTIFY] %s", message)
        elif channel == "mud":
            self._send_mud_event(message, run)
        elif channel == "lighthouse":
            self._send_lighthouse_alert(message, run)
        else:
            logger.warning("Unknown notification channel: %s", channel)

    def _send_mud_event(self, message: str, run: PipelineRun):
        """Send event to MUD system (fleet event bus)."""
        event = {
            "type": "cicd.result",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "cicd-agent",
            "payload": {
                "run_id": run.run_id,
                "repo": run.repo_name,
                "status": run.status.value,
                "commit": run.commit_sha,
                "duration": run.total_duration,
            },
        }
        # In production, this would publish to a MUD event bus
        logger.info("[MUD EVENT] %s", json.dumps(event, default=str))

    def _send_lighthouse_alert(self, message: str, run: PipelineRun):
        """Send alert to lighthouse monitoring."""
        if run.status == PipelineStatus.FAILED:
            logger.error("[LIGHTHOUSE ALERT] Pipeline failure: %s", message)


# ---------------------------------------------------------------------------
# Pipeline Runner
# ---------------------------------------------------------------------------

class PipelineRunner:
    """Chain pipeline stages with pass/fail handling and retries."""

    def __init__(
        self,
        repo_path: str,
        config: PipelineConfig,
        artifacts: ArtifactManager,
        deploy_mgr: DeployManager,
        notifier: NotificationManager,
    ):
        self.repo_path = repo_path
        self.config = config
        self.artifacts = artifacts
        self.deploy_mgr = deploy_mgr
        self.notifier = notifier
        self.test_runner = TestRunner(timeout=config.timeout)
        self.reporter = CIReporter()
        self.runs: list[PipelineRun] = []

    def run_pipeline(
        self,
        trigger: str = "manual",
        commit_sha: str = "",
        commit_message: str = "",
        branch: str = "main",
    ) -> PipelineRun:
        """Execute the full pipeline for a repo."""
        run = PipelineRun(
            run_id=self._generate_run_id(),
            repo_name=self.config.repo_name,
            config=asdict(self.config),
            trigger=trigger,
            commit_sha=commit_sha,
            commit_message=commit_message,
            branch=branch,
        )
        run.started_at = datetime.now(timezone.utc).isoformat()
        run.status = PipelineStatus.RUNNING
        logger.info("Pipeline started: %s for %s", run.run_id, run.repo_name)

        # Check skip patterns
        if self._should_skip(commit_message):
            run.stages.append(StageResult(
                stage=StageName.DETECT,
                status=PipelineStatus.SKIPPED,
                message=f"Commit matched skip pattern: {commit_message}",
            ))
            run.status = PipelineStatus.SKIPPED
            run.finished_at = datetime.now(timezone.utc).isoformat()
            run.total_duration = self._elapsed(run.started_at)
            self.runs.append(run)
            return run

        # Execute stages
        attempts = 0
        max_attempts = 1 + self.config.max_retries

        while attempts < max_attempts:
            attempts += 1
            if attempts > 1:
                run.status = PipelineStatus.RETRYING
                logger.info("Retry attempt %d/%d", attempts, max_attempts)

            run.stages = []
            stages_ok = True

            # Stage 1: Test
            test_result = self._run_test_stage()
            run.stages.append(test_result)
            if test_result.status != PipelineStatus.PASSED:
                stages_ok = False
                if attempts < max_attempts:
                    time.sleep(2)
                    continue

            # Stage 2: Lint
            if stages_ok:
                lint_result = self._run_lint_stage()
                run.stages.append(lint_result)
                if lint_result.status != PipelineStatus.PASSED:
                    stages_ok = False

            # Stage 3: Validate
            validate_result = self._run_validate_stage(run.stages)
            run.stages.append(validate_result)
            if validate_result.status != PipelineStatus.PASSED:
                stages_ok = False

            # Stage 4: Report
            report_result = self._run_report_stage(run)
            run.stages.append(report_result)

            # Stage 5: Notify
            notify_result = self._run_notify_stage(run)
            run.stages.append(notify_result)

            # Stage 6: Deploy (only if all passed and auto_deploy is on)
            if stages_ok and self.config.auto_deploy:
                deploy_result = self._run_deploy_stage()
                run.stages.append(deploy_result)
                if deploy_result.status != PipelineStatus.PASSED:
                    stages_ok = False

            run.status = PipelineStatus.PASSED if stages_ok else PipelineStatus.FAILED
            break  # break retry loop

        run.finished_at = datetime.now(timezone.utc).isoformat()
        run.total_duration = self._elapsed(run.started_at)
        self.runs.append(run)

        # Final notification
        self.notifier.notify(run, self.config)

        logger.info(
            "Pipeline finished: %s status=%s duration=%.1fs",
            run.run_id, run.status.value, run.total_duration,
        )
        return run

    def _should_skip(self, commit_message: str) -> bool:
        if not commit_message:
            return False
        return any(
            pat in commit_message
            for pat in self.config.skip_patterns
        )

    def _run_test_stage(self) -> StageResult:
        start = time.time()
        logger.info("[TEST] Running tests for %s", self.config.repo_name)
        result = self.test_runner.run(
            command=self.config.test_command,
            cwd=self.repo_path,
        )
        duration = time.time() - start
        status = PipelineStatus.PASSED if result.exit_code == 0 else PipelineStatus.FAILED
        return StageResult(
            stage=StageName.TEST,
            status=status,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration=duration,
            message=f"Tests: {result.passed} passed, {result.failed} failed, {result.skipped} skipped",
            data=result.to_dict(),
        )

    def _run_lint_stage(self) -> StageResult:
        start = time.time()
        logger.info("[LINT] Running lint for %s", self.config.repo_name)
        py_files = list(Path(self.repo_path).rglob("*.py"))
        errors = []
        for pf in py_files:
            try:
                subprocess.run(
                    ["python3", "-m", "py_compile", str(pf)],
                    capture_output=True, text=True, timeout=10,
                    check=True,
                )
            except subprocess.CalledProcessError as e:
                errors.append(f"{pf.name}: {e.stderr.strip()}")
            except subprocess.TimeoutExpired:
                errors.append(f"{pf.name}: lint timeout")

        duration = time.time() - start
        status = PipelineStatus.PASSED if not errors else PipelineStatus.FAILED
        return StageResult(
            stage=StageName.LINT,
            status=status,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration=duration,
            message=f"Linted {len(py_files)} files, {len(errors)} errors",
            data={"files_checked": len(py_files), "errors": errors[:10]},
        )

    def _run_validate_stage(self, previous_stages: list[StageResult]) -> StageResult:
        start = time.time()
        all_passed = all(s.status == PipelineStatus.PASSED for s in previous_stages)
        test_stage = next((s for s in previous_stages if s.stage == StageName.TEST), None)
        lint_stage = next((s for s in previous_stages if s.stage == StageName.LINT), None)

        test_data = test_stage.data if test_stage else {}
        lint_data = lint_stage.data if lint_stage else {}

        validation = {
            "all_passed": all_passed,
            "test_summary": {
                "passed": test_data.get("passed", 0),
                "failed": test_data.get("failed", 0),
                "skipped": test_data.get("skipped", 0),
            },
            "lint_summary": {
                "files_checked": lint_data.get("files_checked", 0),
                "errors": len(lint_data.get("errors", [])),
            },
        }

        duration = time.time() - start
        return StageResult(
            stage=StageName.VALIDATE,
            status=PipelineStatus.PASSED if all_passed else PipelineStatus.FAILED,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration=duration,
            message="All checks passed" if all_passed else "Validation failed",
            data=validation,
        )

    def _run_report_stage(self, run: PipelineRun) -> StageResult:
        start = time.time()
        try:
            report = self.reporter.generate_json(run)
            path = self.artifacts.save_report(run.run_id, report, "json")
            duration = time.time() - start
            return StageResult(
                stage=StageName.REPORT,
                status=PipelineStatus.PASSED,
                started_at=datetime.now(timezone.utc).isoformat(),
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration=duration,
                message=f"Report saved to {path}",
            )
        except Exception as e:
            duration = time.time() - start
            return StageResult(
                stage=StageName.REPORT,
                status=PipelineStatus.FAILED,
                started_at=datetime.now(timezone.utc).isoformat(),
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration=duration,
                message=f"Report generation failed: {e}",
            )

    def _run_notify_stage(self, run: PipelineRun) -> StageResult:
        start = time.time()
        try:
            self.notifier.notify(run, self.config)
            duration = time.time() - start
            return StageResult(
                stage=StageName.NOTIFY,
                status=PipelineStatus.PASSED,
                started_at=datetime.now(timezone.utc).isoformat(),
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration=duration,
                message="Notifications sent",
            )
        except Exception as e:
            duration = time.time() - start
            return StageResult(
                stage=StageName.NOTIFY,
                status=PipelineStatus.FAILED,
                started_at=datetime.now(timezone.utc).isoformat(),
                finished_at=datetime.now(timezone.utc).isoformat(),
                duration=duration,
                message=f"Notification failed: {e}",
            )

    def _run_deploy_stage(self) -> StageResult:
        start = time.time()
        result = self.deploy_mgr.deploy(
            self.config.deploy_target,
            self.repo_path,
        )
        duration = time.time() - start
        return StageResult(
            stage=StageName.DEPLOY,
            status=PipelineStatus.PASSED if result["success"] else PipelineStatus.FAILED,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration=duration,
            message=result["message"],
            data=result,
        )

    def _generate_run_id(self) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"{self.config.repo_name}-{ts}"

    @staticmethod
    def _elapsed(started_at: str) -> float:
        try:
            start = datetime.fromisoformat(started_at)
            return (datetime.now(timezone.utc) - start).total_seconds()
        except (ValueError, TypeError):
            return 0.0


# ---------------------------------------------------------------------------
# Fleet CI/CD Orchestrator
# ---------------------------------------------------------------------------

class FleetCICD:
    """Continuous Integration and Continuous Deployment for the Pelagic fleet.

    Watches fleet repos for changes, runs tests, validates builds,
    generates reports, and triggers deployment pipelines.
    """

    def __init__(self, config_dir: str = ".cicd"):
        self.config_dir = Path(config_dir)
        self.config_dir.mkdir(exist_ok=True)
        self.artifacts = ArtifactManager(str(self.config_dir / "artifacts"))
        self.deploy_mgr = DeployManager()
        self.notifier = NotificationManager()
        self.git_poller = GitPoller()
        self.runners: dict[str, PipelineRunner] = {}
        self.repos: dict[str, PipelineConfig] = {}
        self.all_runs: list[PipelineRun] = []
        self._poll_thread: Optional[threading.Thread] = None
        self._webhook_server: Optional[WebhookServer] = None
        self._running = False
        self._lock = threading.Lock()

    # -- Repo Management --

    def add_repo(self, config: PipelineConfig) -> None:
        """Register a repo for CI/CD monitoring."""
        self.repos[config.repo_name] = config
        repo_path = config.repo_path or str(self.config_dir / "repos" / config.repo_name)
        Path(repo_path).mkdir(parents=True, exist_ok=True)
        runner = PipelineRunner(
            repo_path=repo_path,
            config=config,
            artifacts=self.artifacts,
            deploy_mgr=self.deploy_mgr,
            notifier=self.notifier,
        )
        self.runners[config.repo_name] = runner
        self.git_poller.add_repo(config.repo_name, repo_path, config.deploy_branch)
        logger.info("Added repo: %s (%s)", config.repo_name, repo_path)

    def remove_repo(self, repo_name: str) -> bool:
        """Unregister a repo from CI/CD monitoring."""
        if repo_name not in self.repos:
            return False
        del self.repos[repo_name]
        self.runners.pop(repo_name, None)
        self.git_poller.remove_repo(repo_name)
        logger.info("Removed repo: %s", repo_name)
        return True

    def list_repos(self) -> list[dict]:
        """List all registered repos and their status."""
        return [
            {
                "name": cfg.repo_name,
                "path": cfg.repo_path,
                "auto_deploy": cfg.auto_deploy,
                "deploy_target": cfg.deploy_target,
                "test_command": cfg.test_command,
            }
            for cfg in self.repos.values()
        ]

    # -- Pipeline Execution --

    def run_repo(self, repo_name: str, **kwargs) -> Optional[PipelineRun]:
        """Run the CI/CD pipeline for a specific repo."""
        runner = self.runners.get(repo_name)
        if runner is None:
            logger.error("Unknown repo: %s", repo_name)
            return None
        run = runner.run_pipeline(**kwargs)
        with self._lock:
            self.all_runs.append(run)
        return run

    def run_all(self, **kwargs) -> list[PipelineRun]:
        """Run the CI/CD pipeline for all registered repos."""
        results = []
        for name in self.repos:
            run = self.run_repo(name, **kwargs)
            if run:
                results.append(run)
        return results

    # -- Polling --

    def start_polling(self, interval: int = 60) -> None:
        """Start background git polling."""
        self._running = True
        self.git_poller.set_interval(interval)

        def poll_loop():
            logger.info("Polling started (interval=%ds)", interval)
            while self._running:
                try:
                    changes = self.git_poller.poll_all()
                    for repo_name, commits in changes.items():
                        if commits:
                            latest = commits[-1]
                            logger.info(
                                "New commits in %s: %d (latest: %s)",
                                repo_name, len(commits), latest.sha[:8],
                            )
                            self.run_repo(
                                repo_name,
                                trigger="poll",
                                commit_sha=latest.sha,
                                commit_message=latest.message,
                                branch=latest.branch,
                            )
                except Exception as e:
                    logger.error("Polling error: %s", e)
                time.sleep(interval)

        self._poll_thread = threading.Thread(target=poll_loop, daemon=True)
        self._poll_thread.start()

    def stop_polling(self) -> None:
        """Stop background git polling."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=10)
            self._poll_thread = None
        logger.info("Polling stopped")

    # -- Webhook --

    def start_webhook(self, port: int = 9000, secret: str = "") -> None:
        """Start the webhook receiver server."""
        self._webhook_server = WebhookServer(
            port=port,
            secret=secret,
            on_push=self._handle_webhook_push,
        )
        thread = threading.Thread(target=self._webhook_server.serve_forever, daemon=True)
        thread.start()
        logger.info("Webhook server started on port %d", port)

    def _handle_webhook_push(self, repo_name: str, commit_sha: str,
                             commit_message: str, branch: str):
        """Handle an incoming webhook push event."""
        logger.info("Webhook push: %s %s (%s)", repo_name, commit_sha[:8], branch)
        if repo_name in self.runners:
            self.run_repo(
                repo_name,
                trigger="webhook",
                commit_sha=commit_sha,
                commit_message=commit_message,
                branch=branch,
            )

    # -- Status & History --

    def get_status(self) -> dict:
        """Get overall CI/CD system status."""
        with self._lock:
            total = len(self.all_runs)
            passed = sum(1 for r in self.all_runs if r.status == PipelineStatus.PASSED)
            failed = sum(1 for r in self.all_runs if r.status == PipelineStatus.FAILED)
        return {
            "repos_monitored": len(self.repos),
            "polling": self._running,
            "webhook": self._webhook_server is not None,
            "total_runs": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": f"{(passed / total * 100):.1f}%" if total > 0 else "N/A",
        }

    def get_history(self, repo_name: str = None, limit: int = 20) -> list[dict]:
        """Get recent pipeline runs."""
        with self._lock:
            runs = self.all_runs.copy()
        if repo_name:
            runs = [r for r in runs if r.repo_name == repo_name]
        return [r.to_dict() for r in runs[-limit:]]

    # -- Lifecycle --

    def shutdown(self):
        """Gracefully shut down the CI/CD agent."""
        logger.info("Shutting down FleetCICD...")
        self.stop_polling()
        if self._webhook_server:
            self._webhook_server.shutdown()
        logger.info("FleetCICD shutdown complete")
